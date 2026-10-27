"""I25 按所有者隔离的原生运行时；生命周期策略由 Docker 宿主机负责。

这是独立于 I22 的新应用入口，尚未校准。空闲时保留真实的历史图、模型、
工具桥接器和保存器。每个新业务任务独立绑定客户端、工具、图和 SQLite
保存器；最近一次自然完成的任务对象可持续查询，直到下一个任务或正常关闭。
两种策略使用相同生命周期，由宿主机决定完成后保留还是关闭整个运行时。

按行分隔的 JSON 命令通道仅供可信实验编排器使用，并非产品认证边界、
任务队列或资源池。所有路径、凭证、作用域和提示词都来自只读启动绑定；
命令只能选择已绑定的任务 ID。此处不提供故障恢复。
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from uuid import uuid4


SCHEMA = "harness-lab-resident-runtime/1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SCOPE_FIELDS = frozenset({"experiment_id", "variant", "user_id", "session_id"})
BUILTINS = frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "task", "write_todos"})
SYSTEM_PROMPT = "使用提供的 lab_* 工具完成用户任务。这些是共享实验工具，以其输入和输出为准。"
MODEL_URL = "http://model-proxy:8082/v1"
TOOLS_URL = "http://tools:8081"
COMMON_BINDING_FIELDS = frozenset({"task_id", "scope", "state_dir", "workspace", "output_dir", "tool_token_file", "model_token_file", "tools_schema_file"})


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("可信常驻运行绑定字段无效")


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or value.lower() == "old":
        raise ValueError("常驻运行标识无效")
    return value


def _path(value):
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value or "\0" in value:
        raise ValueError("必须使用当前实验的绝对路径")
    if any(part in {".", ".."} or part.lower() == "old" for part in value.split("/")):
        raise ValueError("常驻运行绑定路径不安全")
    return Path(value)


def _existing_path(value, *, directory=False):
    path = _path(value)
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("常驻运行路径不得跟随符号链接")
    if path.resolve(strict=True) != path or directory and not path.is_dir():
        raise ValueError("常驻运行路径不存在或类型错误")
    return path


def _read_file(value, *, limit=1024 * 1024, readonly=False):
    path = _existing_path(value)
    if readonly and not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise ValueError("常驻运行配置必须以只读方式挂载")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("必须提供大小受限的常驻运行普通文件")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("常驻运行文件增长后超过大小上限")
    return data


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _decode(data):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("常驻运行配置的 JSON 字段重复")
            result[key] = item
        return result
    return json.loads(data, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("常驻运行 JSON 包含非有限数值")))


def _native(value):
    """在 JSON 将命名元组静默转换为数组之前进行显式转换。"""
    if hasattr(value, "_asdict"):
        return _native(value._asdict())
    if hasattr(value, "model_dump"):
        return _native(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _native(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_native(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_native(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (datetime, Path)):
        return str(value)
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__module__ + "." + type(value).__qualname__, "message": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("不支持的原生投影类型：" + type(value).__name__)


def validate_binding(value):
    """仅校验不可变路由；此函数不执行文件系统或 SDK I/O。"""
    _fields(value, {"schema", "candidate", "owner", "model_name", "limits", "history", "tasks"})
    if value["schema"] != SCHEMA or value["candidate"] != "deepagents_langgraph":
        raise ValueError("常驻运行时结构或候选不符合预期")
    owner = _identifier(value["owner"])
    model = value["model_name"]
    if not isinstance(model, str) or not 0 < len(model) <= 256 or any(character in model for character in "\r\n\0"):
        raise ValueError("已冻结的常驻运行模型无效")
    _fields(value["limits"], {"max_output_tokens", "timeout_seconds", "max_retries"})
    for name, low, high in (("max_output_tokens", 1, 16384), ("timeout_seconds", 1, 300), ("max_retries", 0, 2)):
        if type(value["limits"][name]) is not int or not low <= value["limits"][name] <= high:
            raise ValueError("已冻结的常驻运行限制无效")
    if not isinstance(value["tasks"], list) or not 0 <= len(value["tasks"]) <= 16:
        raise ValueError("必须绑定明确且数量受限的业务任务清单")
    all_bindings = [value["history"], *value["tasks"]]
    task_ids, sessions, variants, mutable_roots, configuration = set(), set(), set(), [], []
    experiment = None
    for index, binding in enumerate(all_bindings):
        _fields(binding, COMMON_BINDING_FIELDS | ({"sentinel"} if index == 0 else {"prompt_file"}))
        task_id = _identifier(binding["task_id"])
        _fields(binding["scope"], SCOPE_FIELDS)
        scope = {key: _identifier(item) for key, item in binding["scope"].items()}
        if scope["user_id"] != owner or task_id in task_ids or scope["session_id"] in sessions or scope["variant"] in variants:
            raise ValueError("任务、会话或变体重复，或存在跨所有者绑定")
        if experiment is None:
            experiment = scope["experiment_id"]
        if scope["experiment_id"] != experiment:
            raise ValueError("常驻运行所有者不能切换实验")
        task_ids.add(task_id)
        sessions.add(scope["session_id"])
        variants.add(scope["variant"])
        mutable_roots.extend(_path(binding[name]) for name in ("state_dir", "workspace", "output_dir"))
        configuration.extend(_path(binding[name]) for name in ("tool_token_file", "model_token_file", "tools_schema_file"))
        if index == 0:
            if not isinstance(binding["sentinel"], str) or not 0 < len(binding["sentinel"]) <= 4096:
                raise ValueError("历史状态必须包含长度受限且非空的业务标记")
        else:
            configuration.append(_path(binding["prompt_file"]))
    for index, left in enumerate(mutable_roots):
        if any(left == right or left.is_relative_to(right) or right.is_relative_to(left) for right in mutable_roots[index + 1:]):
            raise ValueError("每份历史状态和每个任务必须使用独立的状态、工作区和证据目录树")
    if any(config == root or config.is_relative_to(root) for config in configuration for root in mutable_roots):
        raise ValueError("可信凭证、结构定义或提示词不能位于候选可修改的目录树内")
    return value


def load_binding(filename):
    data = _read_file(filename, readonly=True)
    value = validate_binding(_decode(data))
    startup = _path(filename)
    for binding in [value["history"], *value["tasks"]]:
        for name in ("state_dir", "workspace", "output_dir"):
            root = _existing_path(binding[name], directory=True)
            if startup == root or startup.is_relative_to(root):
                raise ValueError("启动绑定不能位于候选可修改的目录树内")
    return value, _sha(data)


class _NativeObjects:
    def __init__(self, stack, binding, evidence, model, tools, graph, saver):
        self.stack, self.binding, self.evidence = stack, binding, evidence
        self.model, self.tools, self.graph, self.saver = model, tools, graph, saver

    def identity(self):
        return {"pid": os.getpid(), "task_id": self.binding["task_id"], "scope": self.binding["scope"],
            "objects": {name: {"python_id": id(getattr(self, name)),
                "type": type(getattr(self, name)).__module__ + "." + type(getattr(self, name)).__qualname__}
                for name in ("model", "tools", "graph", "saver")},
            "evidence_kind": "candidate_reported_process_local_object_identity_not_protected_process_proof"}

    async def close(self):
        await self.stack.aclose()


class ResidentRuntime:
    """绑定唯一且不可变的所有者，使用真实原生对象，并为每个任务显式重建。"""
    def __init__(self, binding, provenance, binding_sha256):
        self.binding = validate_binding(binding)
        self.provenance, self.binding_sha256 = provenance, binding_sha256
        self.tasks = {item["task_id"]: item for item in binding["tasks"]}
        self.history = None
        self.active = None
        self.retained_task = None
        self.closed = False
        self.poisoned = False
        self.secrets = []
        self._loop = None
        self._operation = None
        self._material_hashes = {}

    def encode(self, value):
        text = json.dumps(_native(value), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for secret in self.secrets:
            text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[redacted-local-secret]")
            text = text.replace(secret, "[redacted-local-secret]")
        return text

    def _guard(self, *, closing=False):
        if self.closed or self.history is None:
            raise ValueError("常驻运行时尚未打开")
        if self.poisoned and not closing:
            raise ValueError("原生清理失败；必须回收此 Worker")
        if asyncio.get_running_loop() is not self._loop:
            raise ValueError("常驻运行时只能使用其原始事件循环")

    def _token(self, filename):
        value = self._material(filename, limit=8192).decode().strip()
        if not value or re.search(r"\s", value):
            raise ValueError("限定作用域的本地令牌不能为空")
        self.secrets.append(value)
        return value

    def _material(self, filename, *, limit=1024 * 1024):
        data = _read_file(filename, limit=limit, readonly=True)
        if filename in self._material_hashes and self._material_hashes[filename] != _sha(data):
            raise ValueError("只读常驻运行启动材料发生变化")
        self._material_hashes[filename] = _sha(data)
        return data

    def _evidence(self, binding):
        from detailed_support import Evidence
        evidence = Evidence(_existing_path(binding["output_dir"], directory=True), self.encode, binding["scope"])
        # 后续冷启动可能追加原始证据；保留真实行号，
        # 不假定每个新进程的证据都从第一行开始。
        for path in evidence.directory.glob("*.jsonl"):
            data = _read_file(str(path), limit=64 * 1024 * 1024)
            if data and not data.endswith(b"\n"):
                raise ValueError("已有证据流存在不完整记录，已拒绝继续")
            evidence.lines[path.name] = data.count(b"\n")
        return evidence

    @staticmethod
    def _config(binding):
        return {"configurable": {"thread_id": binding["scope"]["session_id"], "checkpoint_ns": ""}, "recursion_limit": 64}

    @staticmethod
    def _schemas(tools):
        return {"tools": sorted(({"name": tool.name.removeprefix("lab_"), "description": tool.description,
            "input_schema": tool.args_schema} for tool in tools), key=lambda item: item["name"])}

    def _frozen_tools(self, bridge, schema):
        """仅重建可信结构；执行仍通过 HttpTools.forward。"""
        from langchain_core.tools import StructuredTool
        _fields(schema, {"tools"})
        if not isinstance(schema["tools"], list) or not schema["tools"]:
            raise ValueError("必须提供真实且已冻结的工具结构清单")
        result = []
        for entry in schema["tools"]:
            _fields(entry, {"name", "description", "input_schema"})
            name, description, arguments = entry["name"], entry["description"], entry["input_schema"]
            if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in bridge.names.values():
                raise ValueError("已冻结的 HTTP 工具名称无效或重复")
            if not isinstance(description, str) or not isinstance(arguments, dict) or arguments.get("type") != "object":
                raise ValueError("已冻结的 HTTP 工具定义无效")
            exposed = "lab_" + name
            bridge.names[exposed] = name
            result.append(StructuredTool(name=exposed, description=description, args_schema=arguments,
                coroutine=bridge.forward(name, exposed), handle_tool_error=True))
        return result

    async def _build(self, binding, *, live_tools):
        build_started = time.monotonic()
        from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
        import httpx
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from http_tools import HttpTools

        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            state = _existing_path(binding["state_dir"], directory=True)
            database = state / "checkpoints.sqlite"
            if database.exists() and (database.is_symlink() or not database.is_file()):
                raise ValueError("必须提供原始原生 SQLite 数据库普通文件")
            lock_fd = os.open(state / ".resident-runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            stack.callback(os.close, lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            tool_token = self._token(binding["tool_token_file"])
            model_token = self._token(binding["model_token_file"])
            frozen = _decode(self._material(binding["tools_schema_file"]))
            evidence = self._evidence(binding)

            def observe(event):
                return evidence.append("http-events.jsonl", {"time": evidence.now(), "scope": binding["scope"], **event})

            bridge = HttpTools(TOOLS_URL, tool_token, self.binding["limits"]["timeout_seconds"],
                on_event=observe, evidence=evidence, scope=binding["scope"])
            stack.push_async_callback(bridge.close)
            if live_tools:
                provided = await bridge.load()
                actual = self._schemas(provided)
                expected = {"tools": sorted(frozen.get("tools", []), key=lambda item: item["name"])}
                if actual != expected or set(frozen) != {"tools"}:
                    raise ValueError("实时 HTTP 结构与冻结的原生运行时材料不一致")
                evidence.save("live-tools-schema.json", {"scope": binding["scope"], "schema": actual,
                    "source": "通过 StructuredTool 投影真实 HttpTools.load /tools 响应", "http_metadata_read_only": True})
            else:
                provided = self._frozen_tools(bridge, frozen)
            register_harness_profile("openai:" + self.binding["model_name"], HarnessProfile(
                excluded_tools=BUILTINS, general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
            # 显式持有的公开 HTTP 客户端可以实际关闭。供应商密钥
            # 不会进入 Worker；model_token 是限定作用域的代理令牌。
            http_sync = stack.enter_context(httpx.Client(trust_env=False, transport=httpx.HTTPTransport(retries=0)))
            http_async = await stack.enter_async_context(httpx.AsyncClient(trust_env=False, transport=httpx.AsyncHTTPTransport(retries=0)))
            limits = self.binding["limits"]
            model = ChatOpenAI(model=self.binding["model_name"], base_url=MODEL_URL, api_key=model_token,
                use_responses_api=False, max_tokens=limits["max_output_tokens"], max_retries=limits["max_retries"],
                timeout=limits["timeout_seconds"], http_client=http_sync, http_async_client=http_async)
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(database)))
            graph = create_deep_agent(model=model, tools=provided, system_prompt=SYSTEM_PROMPT, checkpointer=saver, interrupt_on=None)
            objects = _NativeObjects(stack, binding, evidence, model, bridge, graph, saver)
            evidence.append("resident-lifecycle.jsonl", {"time": evidence.now(), "phase": "native_objects_opened",
                **objects.identity(), "live_tool_schema_verified": live_tools,
                "build_elapsed_seconds": time.monotonic() - build_started,
                "provenance": self.provenance, "binding_sha256": self.binding_sha256,
                "native_capability_claim": "objects_loaded_only_not_task_completion_or_multi_user_isolation"})
            return objects
        except BaseException:
            await stack.aclose()
            raise

    async def open(self):
        if self.history is not None or self.closed:
            raise ValueError("常驻运行时只能打开一次")
        self._loop = asyncio.get_running_loop()
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        credentials = set()
        for binding in [self.binding["history"], *self.binding["tasks"]]:
            tool, model = self._token(binding["tool_token_file"]), self._token(binding["model_token_file"])
            if tool in credentials or model in credentials or tool == model:
                raise ValueError("每份历史状态和每个任务都需要独立且限定作用域的工具与模型凭证")
            credentials.update({tool, model})
            self._material(binding["tools_schema_file"])
            if "prompt_file" in binding:
                self._material(binding["prompt_file"], limit=65536)
        self.history = await self._build(self.binding["history"], live_tools=False)
        return self.history.identity()

    async def _snapshot(self, objects, *, reason):
        snapshot = await objects.graph.aget_state(self._config(objects.binding))
        name = "native-state-" + uuid4().hex + ".json"
        data = self.encode(snapshot)
        path = objects.evidence.directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        checkpoint = snapshot.config.get("configurable", {}).get("checkpoint_id")
        source = {"path": name, "sha256": _sha(data.encode()), "public_api": "CompiledStateGraph.aget_state",
            "encoding": "保留类型的原生投影，并对本地凭证脱敏"}
        summary = {"task_id": objects.binding["task_id"], "scope": objects.binding["scope"], "reason": reason,
            "checkpoint_id": checkpoint, "messages": len(snapshot.values.get("messages", [])),
            "next": list(snapshot.next), "tasks": len(snapshot.tasks), "source": source,
            "objects": objects.identity(), "state_is_nonempty": bool(snapshot.values)}
        objects.evidence.append("native-observations.jsonl", {"time": objects.evidence.now(), **summary})
        return snapshot, summary

    async def prepare_history(self):
        """准备真实的非空状态，不伪造已完成的 Agent 运行。"""
        self._guard()
        if self._operation is not None:
            raise ValueError("业务任务运行期间不能准备历史状态")
        self._operation = "prepare_history"
        try:
            return await self._prepare_history()
        finally:
            self._operation = None

    async def _prepare_history(self):
        started = time.monotonic()
        from langchain_core.messages import HumanMessage
        from langgraph.constants import START, END
        objects, binding = self.history, self.binding["history"]
        before = await objects.graph.aget_state(self._config(binding))
        if before.values or before.next or before.tasks:
            raise ValueError("准备历史状态必须使用新的空原生线程")
        marker = Path(binding["workspace"]) / "prepared-history.json"
        if marker.exists() or marker.is_symlink():
            raise ValueError("不得覆盖已准备的历史状态产物")
        message_id = "prepared-history-" + binding["task_id"]
        message = HumanMessage(content=binding["sentinel"], id=message_id)
        returned = await objects.graph.aupdate_state(self._config(binding), {"messages": [message]}, as_node=START)
        first = objects.evidence.append("history-preparation.jsonl", {"time": objects.evidence.now(),
            "scope": binding["scope"], "api": "CompiledStateGraph.aupdate_state", "as_node": START,
            "message_id": message_id, "returned": returned,
            "kind": "application_prepared_history_not_an_agent_run"})
        # 公开 API 明确支持通过 values=None、as_node=END 清空
        # 已调度任务；此接口仅用于全新的准备线程。
        # 此处不处理任何真实业务任务或待写入记录。
        returned = await objects.graph.aupdate_state(self._config(binding), None, as_node=END)
        second = objects.evidence.append("history-preparation.jsonl", {"time": objects.evidence.now(),
            "scope": binding["scope"], "api": "CompiledStateGraph.aupdate_state", "as_node": END,
            "values": None, "returned": returned, "clears_only_application_preparation_input": True})
        snapshot, summary = await self._snapshot(objects, reason="after_application_history_preparation")
        messages = snapshot.values.get("messages", [])
        if (not summary["checkpoint_id"] or snapshot.next or snapshot.tasks or getattr(snapshot, "interrupts", ())
                or not any(getattr(item, "id", None) == message_id and getattr(item, "content", None) == binding["sentinel"] for item in messages)):
            raise ValueError("公开准备接口未产生可读、非空且空闲的原生历史状态")
        payload = {"schema": "harness-lab-prepared-history/1", "scope": binding["scope"], "sentinel": binding["sentinel"],
            "native_source": summary["source"], "preparation_sources": [first, second],
            "kind": "application_prepared_history_not_agent_business_output", "agent_task_completed": False}
        data = self.encode(payload).encode()
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return {**summary, "preparation_artifact": {"path": str(marker), "sha256": _sha(data)},
            "kind": payload["kind"], "agent_task_completed": False,
            "preparation_elapsed_seconds": time.monotonic() - started,
            "model_invocation_requested_by_this_method": False, "tool_call_requested_by_this_method": False}

    async def state(self, task_id):
        self._guard()
        if task_id == self.binding["history"]["task_id"]:
            _, summary = await self._snapshot(self.history, reason="retained_history_read")
            return summary
        if task_id not in self.tasks:
            raise ValueError("任务不在不可变所有者绑定范围内")
        if self.active is not None and self.active.binding["task_id"] == task_id:
            _, summary = await self._snapshot(self.active, reason="active_task_public_read")
            return summary
        if self.retained_task is not None and self.retained_task.binding["task_id"] == task_id:
            _, summary = await self._snapshot(self.retained_task, reason="naturally_completed_task_retained_read")
            return summary
        binding = self.tasks[task_id]
        database = Path(binding["state_dir"]) / "checkpoints.sqlite"
        if not database.is_file():
            raise ValueError("原始任务检查点不存在")
        objects = await self._build(binding, live_tools=False)
        try:
            _, summary = await self._snapshot(objects, reason="cold_original_task_public_read")
            return summary
        finally:
            await objects.close()

    async def rebuild_tools_graph_for_task(self, task_id):
        """始终使用新任务的真实作用域、凭证和原生存储。"""
        self._guard()
        if self.active is not None or task_id not in self.tasks or self._operation != task_id:
            raise ValueError("任务未知，或另一个任务正在执行")
        binding = self.tasks[task_id]
        if any(Path(binding["output_dir"]).iterdir()):
            raise ValueError("新业务任务必须使用全新的证据目录")
        await self._close_retained_task(reason="before_new_independent_task")
        return await self._build(binding, live_tools=True)

    async def _close_retained_task(self, *, reason):
        objects = self.retained_task
        if objects is None:
            return
        try:
            await objects.close()
        except BaseException:
            self.poisoned = True
            raise
        objects.evidence.append("resident-lifecycle.jsonl", {"time": objects.evidence.now(),
            "phase": "completed_business_native_objects_closed", "task_id": objects.binding["task_id"],
            "scope": objects.binding["scope"], "reason": reason, "saver_context_closed": True,
            "tool_and_owned_model_http_clients_closed": True})
        objects.evidence.finalize()
        self.retained_task = None

    async def run_task(self, task_id):
        """仅用于新的独立任务；不得重新提交失败或未完成的运行。"""
        self._guard()
        if self._operation is not None or task_id not in self.tasks:
            raise ValueError("任务未知，或另一个操作正在执行")
        binding = self.tasks[task_id]
        if any(Path(binding["output_dir"]).iterdir()):
            raise ValueError("新业务任务必须使用全新的证据目录；不得覆盖旧结果")
        self._operation = task_id
        try:
            return await self._run_task(task_id, binding)
        finally:
            self._operation = None

    async def _run_task(self, task_id, binding):
        from langchain_core.messages import AIMessage
        from langgraph.runtime import RunControl
        from detailed_support import completion
        started = time.monotonic()
        objects = None
        summary = {"task_id": task_id, "status": "failed", "native_status": "not_started", "stop_reason": "runtime_setup_error"}
        prompt = self._material(binding["prompt_file"], limit=65536).decode()
        if not prompt:
            raise ValueError("必须提供长度受限的真实业务提示词")
        try:
            async with asyncio.timeout(self.binding["limits"]["timeout_seconds"]):
                objects = await self.rebuild_tools_graph_for_task(task_id)
                self.active = objects
                config = self._config(binding)
                previous = await objects.graph.aget_state(config)
                if previous.values or previous.next or previous.tasks:
                    raise ValueError("新业务任务不接受已有原生线程；不允许隐式重试或恢复")
                objects.evidence.append("resident-lifecycle.jsonl", {"time": objects.evidence.now(), "phase": "business_task_started",
                    "task_id": task_id, "scope": binding["scope"], "history_objects_retained": self.history.identity(),
                    "semantics": "new_task_new_scope_token_budget_and_native_store_not_session_relabeling"})
                async for mode, event in objects.graph.astream({"messages": [{"role": "user", "content": prompt}]},
                        config=config, stream_mode=["updates", "messages", "tasks", "checkpoints"],
                        durability="sync", control=RunControl()):
                    objects.evidence.append("raw-events.jsonl", {"time": objects.evidence.now(), "task_id": task_id,
                        "scope": binding["scope"], "stream_mode": mode, "data": event})
                snapshot, native_state = await self._snapshot(objects, reason="after_business_task_stream")
                messages = snapshot.values.get("messages", [])
                last = messages[-1] if messages else None
                native = completion(last_is_ai=isinstance(last, AIMessage), tool_calls=getattr(last, "tool_calls", []),
                    invalid_tool_calls=getattr(last, "invalid_tool_calls", []), metadata=getattr(last, "response_metadata", {}),
                    pending=snapshot.next, task_errors=[task.error for task in snapshot.tasks if task.error],
                    interrupts=[interrupt for task in snapshot.tasks for interrupt in task.interrupts])
                summary.update(native)
                summary.update({"status": "denied" if objects.tools.denied and native["native_status"] == "completed" else native["native_status"],
                    "scope": binding["scope"], "native_state": native_state,
                    "final_response": last.content if isinstance(last, AIMessage) else None})
        except Exception as error:
            summary.update({"status": "timed_out" if isinstance(error, TimeoutError) else "failed",
                "native_status": "timed_out" if isinstance(error, TimeoutError) else "failed",
                "stop_reason": "active_timeout" if isinstance(error, TimeoutError) else "runtime_error", "error_type": type(error).__name__})
            if objects is not None:
                _, summary["native_state"] = await self._snapshot(objects, reason="after_failed_business_task")
        finally:
            try:
                if objects is not None:
                    try:
                        if summary.get("status") == "completed" and summary.get("native_status") == "completed":
                            self.retained_task = objects
                            objects.evidence.append("resident-lifecycle.jsonl", {"time": objects.evidence.now(),
                                "phase": "naturally_completed_business_objects_retained", "task_id": task_id,
                                "scope": binding["scope"], "objects": objects.identity(),
                                "no_pending_business_work": True})
                            summary["task_objects_retained_after_natural_completion"] = True
                        else:
                            await objects.close()
                    except Exception as error:
                        self.poisoned = True
                        summary.update({"status": "failed", "stop_reason": "native_object_cleanup_error",
                            "cleanup_error_type": type(error).__name__, "task_objects_closed": False})
                    else:
                        summary["task_objects_closed"] = self.retained_task is not objects
                        if self.retained_task is not objects:
                            objects.evidence.append("resident-lifecycle.jsonl", {"time": objects.evidence.now(),
                                "phase": "business_native_objects_closed", "task_id": task_id, "scope": binding["scope"],
                                "history_objects_retained": self.history.identity(),
                                "saver_context_closed": True, "tool_and_owned_model_http_clients_closed": True})
            finally:
                self.active = None
        summary.update({"elapsed_seconds": time.monotonic() - started, "history_objects_retained": self.history.identity(),
            "binding_sha256": self.binding_sha256, "provenance": self.provenance,
            "application_entry": "resident_runtime", "fault_resume_supported_by_this_entry": False})
        evidence = objects.evidence if objects is not None else self._evidence(binding)
        evidence.save("result.json", summary)
        evidence.finalize()
        return summary

    async def close(self):
        self._guard(closing=True)
        if self._operation is not None:
            raise ValueError("不能回收正在执行的原生任务")
        await self._close_retained_task(reason="normal_runtime_recycle")
        objects = self.history
        await objects.close()
        objects.evidence.append("resident-lifecycle.jsonl", {"time": objects.evidence.now(),
            "phase": "history_native_objects_closed", **objects.identity(),
            "saver_context_closed": True, "tool_and_owned_model_http_clients_closed": True})
        objects.evidence.finalize()
        self.closed = True
        self.history = None


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    args = parser.parse_args()
    # 导入任何候选 SDK 前，核验固化的源码及适配器材料。
    from verify_source import verify_source
    provenance = verify_source()
    material_root = Path(__file__).resolve().parents[1]
    manifest = _decode(_read_file(str(material_root / "LAB_ADAPTER_MANIFEST.json")))
    own_path = Path(__file__).resolve().relative_to(material_root).as_posix()
    own_records = [item for item in manifest.get("files", []) if item.get("path") == own_path]
    if len(own_records) != 1 or own_records[0].get("sha256") != _sha(Path(__file__).read_bytes()):
        raise ValueError("新的常驻运行入口必须固化在其独立的冻结适配器清单中")
    binding, binding_sha256 = load_binding(args.binding)
    runtime = ResidentRuntime(binding, provenance, binding_sha256)
    opened = await runtime.open()
    print(runtime.encode({"phase": "ready", "owner": binding["owner"], "objects": opened,
        "binding_sha256": binding_sha256, "history_prepared": False}), end="", flush=True)
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                if len(line) > 4096:
                    raise ValueError("命令超过控制消息的大小上限")
                command = _decode(line)
                _fields(command, {"command_id", "action"}, {"task_id"})
                _identifier(command["command_id"])
                if command["action"] in {"prepare_history", "close"}:
                    if "task_id" in command:
                        raise ValueError("此操作不能选择目标")
                    if command["action"] == "close":
                        await runtime.close()
                        print(runtime.encode({"command_id": command["command_id"], "status": "closed"}), end="", flush=True)
                        break
                    result = await runtime.prepare_history()
                elif command["action"] in {"state", "run_task"}:
                    _identifier(command.get("task_id"))
                    result = await (runtime.state(command["task_id"]) if command["action"] == "state" else runtime.run_task(command["task_id"]))
                else:
                    raise ValueError("不支持的常驻运行命令")
                # 原始快照和结果保留在证据中，不输出到 stdout。
                public = {key: item for key, item in result.items() if key not in {"final_response", "provenance", "response_metadata"}}
                print(runtime.encode({"command_id": command["command_id"], "result": public}), end="", flush=True)
            except Exception as error:
                print(runtime.encode({"status": "rejected_or_failed", "error_type": type(error).__name__}), end="", flush=True)
    finally:
        if not runtime.closed:
            await runtime.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({"status": "startup_or_shutdown_failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
