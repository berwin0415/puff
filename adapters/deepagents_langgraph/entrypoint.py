"""连接官方 DeepAgents 图和持久化保存器的轻量容器入口。"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from verify_source import verify_source
from detailed_support import Evidence, completion, denial_from_http, existing_path, read_credentials, read_scope, validate_options
from run_control import NativeRunControl
from pause_recovery import PauseRecovery, validate_lifecycle


SECRETS: list[str] = []
BUILTINS = frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute", "task", "write_todos"})


def trusted_path(value, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or any(part == ".." or part.lower() == "old" for part in value.split("/")):
        raise ValueError("可信运行路径无效")
    path = Path(value)
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("可信运行路径不得跟随符号链接")
    if directory:
        path.mkdir(parents=True, exist_ok=True)
    if path.resolve(strict=True) != path:
        raise ValueError("可信运行路径在解析期间发生变化")
    return path


def read_token(value) -> str:
    path = trusted_path(value)
    if not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("必须提供大小受限的本地令牌文件")
    token = path.read_text().strip()
    if not token or re.search(r"\s", token):
        raise ValueError("本地令牌不能为空")
    SECRETS.append(token)
    return token


def redact(value: str) -> str:
    for token in SECRETS:
        # 编码后的 JSON 可能转义测试凭证中的引号或反斜杠。
        value = value.replace(json.dumps(token, ensure_ascii=False)[1:-1], "[redacted-local-secret]")
        value = value.replace(token, "[redacted-local-secret]")
    return value


def native_json(value):
    """保留原生消息和事件字段，并记录非 JSON 值的类型。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "_asdict"):
        return value._asdict()
    if isinstance(value, (tuple, set, frozenset)):
        return list(value)
    if isinstance(value, (datetime, Path)):
        return str(value)
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__, "message": str(value)}
    raise TypeError(f"不支持的原生事件类型：{type(value).__name__}")


def encoded(value, *, pretty=False) -> str:
    return redact(json.dumps(value, default=native_json, ensure_ascii=False, indent=2 if pretty else None)) + "\n"


def request_from_file() -> tuple[dict, bool]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--restore-business-state-only", action="store_true")
    args = parser.parse_args()
    request = json.loads(trusted_path(args.request).read_bytes())
    expected = {"run_id", "session_id", "prompt", "workspace", "state_dir", "output_dir", "tools_url", "tool_token_file", "max_output_tokens", "timeout_seconds", "max_retries"}
    # 保留原有接口约定；仅生命周期恢复省略 prompt。
    if not isinstance(request, dict):
        raise ValueError("可信运行请求无效")
    lifecycle = validate_lifecycle(request)
    required = expected - {"prompt"} if lifecycle and lifecycle["mode"] == "resume" else expected
    if not required <= set(request) or set(request) - expected - {"candidate_options", "lifecycle"}:
        raise ValueError("可信运行请求字段无效")
    for name in ("run_id", "session_id"):
        if not isinstance(request[name], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request[name]) or request[name].lower() == "old":
            raise ValueError("运行或会话标识无效")
    if (not lifecycle or lifecycle["mode"] == "new") and (not isinstance(request["prompt"], str) or not 0 < len(request["prompt"]) <= 65536):
        raise ValueError("必须提供长度受限的任务提示词")
    for name, minimum, maximum in (("max_output_tokens", 1, 16384), ("timeout_seconds", 1, 3600), ("max_retries", 0, 2)):
        if type(request[name]) is not int or not minimum <= request[name] <= maximum:
            raise ValueError("运行预算无效")
    paths = [trusted_path(request[name], directory=True) for name in ("workspace", "state_dir", "output_dir")]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise ValueError("状态、工作区和证据必须使用相互独立的目录树")
    trusted_path(request["tool_token_file"])
    if request["tools_url"] != "http://tools:8081":
        raise ValueError("必须使用限定作用域的实验工具服务")
    if "candidate_options" in request:
        request["candidate_options"] = validate_options(request["candidate_options"])
    if args.restore_business_state_only and lifecycle:
        raise ValueError("生命周期运行与仅检查业务状态属于不同入口模式")
    if args.restore_business_state_only and request.get("candidate_options", {}).get("case") != "extension":
        raise ValueError("仅扩展用例支持业务状态检查")
    return request, args.restore_business_state_only


def save_snapshot(snapshot, output, evidence, scope, extension):
    data = encoded(snapshot._asdict(), pretty=True)
    (output / "state-snapshot.json").write_text(data)
    if extension:
        from business_extension import read_business_state
        source = {"path": "state-snapshot.json", "sha256": hashlib.sha256(data.encode()).hexdigest(),
            "json_pointer": "/values/business_state"}
        evidence.save("business-state.json", {"scope": scope, "business_state": read_business_state(snapshot.values, scope),
            "source": source, "kind": "原生图状态的只读投影，不能替代检查点"})


def cold_sqlite_inventory(database):
    """不可变冷检查前，必须由外部确保没有写入者。"""
    database = existing_path(str(database))
    result = {"directory_entries": sorted(path.name for path in database.parent.iterdir()), "files": {}}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database.with_name(database.name + suffix)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if not suffix:
                raise ValueError("缺少原始原生 SQLite 数据库") from None
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("SQLite 数据库及附属文件必须是普通文件，不能是符号链接")
        if suffix in {"-wal", "-journal"} and metadata.st_size:
            raise ValueError("非空 SQLite WAL 或日志需要单独的一致性快照；已拒绝不可变冷读取")
        data = path.read_bytes()
        result["files"][path.name] = {"bytes": len(data), "mode": stat.S_IMODE(metadata.st_mode),
            "sha256": hashlib.sha256(data).hexdigest()}
    return result


async def inspect_business_checkpoint(request, scope, provenance):
    """读取已有原生检查点，不启动图或模型调用。"""
    from business_extension import read_business_state
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    output = Path(request["output_dir"])
    if any(output.iterdir()):
        raise ValueError("检查点检查必须使用全新的空证据目录")
    database = existing_path(str(Path(request["state_dir"]) / "checkpoints.sqlite"))
    if not database.is_file():
        raise ValueError("必须提供原始原生检查点数据库")
    before_files = cold_sqlite_inventory(database)
    before = before_files["files"][database.name]["sha256"]
    # 公开 SQLite 不可变 URI 仅用于已停止且只读的挂载。
    # 避免为已关闭且保留 WAL 头的数据库创建 shm；附属文件检查
    # 会拒绝非空 WAL 或日志，避免忽略可能已提交的数据。
    # 保留公开保存器的正常初始化路径，不设置私有 is_setup。
    async with aiosqlite.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        saver = AsyncSqliteSaver(connection)
        checkpoint = await saver.aget_tuple({"configurable": {"thread_id": request["session_id"]}})
        if checkpoint is None:
            raise ValueError("请求的线程没有原生检查点")
        reconstructed = read_business_state(checkpoint.checkpoint["channel_values"], scope)
        data = encoded(checkpoint._asdict(), pretty=True)
    after_files = cold_sqlite_inventory(database)
    after = after_files["files"][database.name]["sha256"]
    if before_files != after_files:
        raise ValueError("检查点检查改变了 SQLite 数据库或附属文件清单")
    (output / "native-checkpoint-reconnected.json").write_text(data)
    source = {"path": "native-checkpoint-reconnected.json", "sha256": hashlib.sha256(data.encode()).hexdigest(),
        "json_pointer": "/checkpoint/channel_values/business_state"}
    (output / "business-state-reconnected.json").write_text(encoded({"scope": scope, "business_state": reconstructed,
        "source": source, "public_api": "AsyncSqliteSaver.aget_tuple；通过冷检查附属文件门控后，使用 SQLite mode=ro&immutable=1 URI",
        "checkpoint_id": checkpoint.config["configurable"]["checkpoint_id"], "pid": os.getpid(),
        "database_sha256_before": before, "database_sha256_after": after,
        "sqlite_files_before": before_files, "sqlite_files_after": after_files,
        "graph_started": False, "model_calls": 0, "tool_http_calls": 0, "task_continuation_verified": False}, pretty=True))
    summary = {"status": "validated", "native_status": "checkpoint_read_only", "stop_reason": "native_business_state_read_without_graph",
        "session_id": request["session_id"], "scope": scope, "business_state_source": source, "provenance": provenance,
        "model_started": False, "graph_started": False, "task_continuation_verified": False}
    (output / "result.json").write_text(encoded(summary, pretty=True))
    print(encoded({key: summary[key] for key in ("status", "stop_reason", "session_id")}), end="")
    return 0


async def main() -> int:
    process_started = time.monotonic()
    # 导入任何候选 SDK 之前，先核验镜像来源。
    provenance = verify_source()
    request, restore_business_only = request_from_file()
    options = request.get("candidate_options")
    scope = read_scope(options["trusted_scope_file"], request["session_id"]) if options else None
    if restore_business_only:
        return await inspect_business_checkpoint(request, scope, provenance)
    if os.environ.get("MODEL_API_BASE_URL") != "http://model-proxy:8082/v1" or not os.environ.get("MODEL_NAME"):
        raise ValueError("候选必须使用计量代理和选定模型")
    token = os.environ.get("MODEL_API_KEY")
    if token:
        SECRETS.append(token)
    else:
        token = read_token(os.environ.get("MODEL_RUN_TOKEN_FILE"))
    tool_token = read_token(request["tool_token_file"])
    extension = options["business_extension"] if options and options["case"] == "extension" else None
    credentials = read_credentials(options["browser"]["credentials_file"], scope["user_id"]) if options and options["case"] == "browser" else None
    if credentials:
        SECRETS.append(credentials["password"])
        existing_path(options["browser"]["artifacts_dir"])
    output = Path(request["output_dir"])
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
    from langchain_core.messages import AIMessage
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.runtime import RunControl
    from langgraph.errors import GraphDrained
    from http_tools import HttpTools

    model_name = os.environ["MODEL_NAME"]
    profile_key = "openai:" + model_name
    register_harness_profile(profile_key, HarnessProfile(
        excluded_tools=BUILTINS,
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ))
    model = ChatOpenAI(
        model=model_name,
        base_url=os.environ["MODEL_API_BASE_URL"],
        api_key=token,
        use_responses_api=False,
        max_tokens=request["max_output_tokens"],
        max_retries=request["max_retries"],
        timeout=request["timeout_seconds"],
    )
    started = process_started if request.get("lifecycle") else time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    evidence = Evidence(output, encoded, scope)
    lifecycle = None
    control = NativeRunControl(RunControl(), evidence, request["run_id"], scope, lifecycle=request.get("lifecycle"))
    saver_closed = tools_closed = False

    def observe_http(event):
        if lifecycle:
            lifecycle.observe_http(event)
        source = evidence.append("http-tool-events.jsonl" if extension else "http-events.jsonl", {"time": evidence.now(), "scope": scope, **event})
        if event.get("phase") == "finish" and denial_from_http(event["tool"], event["status"], event.get("result")):
            evidence.append("policy-decisions.jsonl", {"time": evidence.now(), "scope": scope,
                "call_id": event["call_id"], "tool": event["tool"], "operation_id": event["arguments"].get("operation_id"),
                "decision": "deny", "reason": "sensitive_operation_denied", "approval_required": False,
                "layer": "tool_execution", "decision_origin": "fixture_execution", "source": source})
        return source

    if credentials:
        from browser_tools import BrowserTools
        tools = BrowserTools(options["browser"], request["workspace"], evidence, request["timeout_seconds"])
        prompt = request["prompt"] + "\n本轮测试登录凭证（可信后端单独注入）：\n" + json.dumps(credentials, ensure_ascii=False)
        system_prompt = "通过阅读页面并自行选择浏览器基础操作来完成用户目标。只能使用提供的浏览器工具。普通授权操作自动执行。不要使用脚本、任意 HTTP 请求或业务 API 捷径。下载网站实际提供的 CSV；浏览器下载产物会复制到任务工作区。"
    else:
        tools = HttpTools(request["tools_url"], tool_token, request["timeout_seconds"], on_event=observe_http if options else None,
            evidence=evidence, extension=extension, scope=scope)
        prompt = request.get("prompt")
        system_prompt = "使用提供的 lab_* 工具完成用户任务。这些是共享实验工具，以其输入和输出为准。"
    summary = {"status": "failed", "native_status": "not_started", "run_id": request["run_id"],
        "session_id": request["session_id"], "scope": scope, "stop_reason": "runtime_error"}
    try:
        lifecycle = PauseRecovery(request, scope, provenance, native_json, started) if request.get("lifecycle") else None
        async with AsyncSqliteSaver.from_conn_string(str(Path(request["state_dir"]) / "checkpoints.sqlite")) as saver:
            remaining = request["timeout_seconds"] - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("初始化时已达到活跃任务执行时限")
            async with asyncio.timeout(remaining):
                provided_tools = await tools.load()
            if lifecycle:
                runtime = lifecycle.bind_runtime(provided_tools, system_prompt, BUILTINS)
                evidence.save("lifecycle-runtime.json", runtime)
            if extension:
                from business_extension import state_schema
                business_schema = state_schema()
            graph = create_deep_agent(
                model=model,
                tools=provided_tools,
                system_prompt=system_prompt,
                checkpointer=saver,
                interrupt_on=None,
                **({"state_schema": business_schema} if extension else {}),
            )
            config = {"configurable": {"thread_id": request["session_id"]}, "recursion_limit": 64}
            if lifecycle:
                config = lifecycle.execution_config()
            if lifecycle and lifecycle.lifecycle["mode"] == "resume":
                resumed = await lifecycle.check_resume(graph, config)
                evidence.save("native-resume-binding.json", {**resumed, "scope": scope,
                    "chain_id": lifecycle.binding["chain_id"], "attempt_id": lifecycle.binding["attempt_id"],
                    "native_state_kind": "原始 SQLite 检查点；不重建状态"})
            else:
                previous = await graph.aget_state(config)
                if previous.values:
                    raise ValueError("新会话必须从空原生状态开始")
            (output / "entrypoint.json").write_text(encoded({
                "run_id": request["run_id"], "session_id": request["session_id"], "started_at": started_at,
                "factory": "deepagents.create_deep_agent", "model": model_name, "profile_key": profile_key,
                "provenance": provenance, "tool_name_map": tools.names,
                "variant": "detailed" if options else "screening", "case": options["case"] if options else "tools", "scope": scope,
                "builtin_tool_exclusions": sorted(BUILTINS),
                "checkpointer": "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver",
                "durability": "sync", "lifecycle": request.get("lifecycle"),
                "controls": {"pause": "SIGUSR2 → 公开 RunControl.request_drain(experiment_pause)；仅在保存器关闭后生成索引" if lifecycle else "not_implemented_by_this_entry",
                    "resume": "核验受保护的原始检查点和当前最新检查点；调用不含 checkpoint_id 的 astream(None)" if lifecycle else "not_yet_verified",
                    "cancel": "SIGUSR1 → 公开 RunControl.request_drain；确认真实 GraphDrained 和检查点",
                    "cancel_semantics": "原生机制在超级步边界协作排空；在途工作可能执行完毕，不会立即停止",
                    "timeout": "使用 Python asyncio.timeout 兜底；不代表已确认原生取消"},
                "business_extension": {"state_channel": "business_state", "state_update": "公开 ToolRuntime + Command(update=...) + ToolMessage",
                    "quantity_limit": extension["quantity_limit"], "query_order_status": "仅转发原始回执"} if extension else None,
            }, pretty=True))
            try:
                remaining = request["timeout_seconds"] - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("图启动前已达到活跃任务执行时限")
                async with asyncio.timeout(remaining):
                    initial = None if lifecycle and lifecycle.lifecycle["mode"] == "resume" else {"messages": [{"role": "user", "content": prompt}]}
                    if extension and initial is not None:
                        from business_extension import new_business_state
                        initial["business_state"] = new_business_state(scope)
                    control.install()
                    control.active = True
                    async for mode, event in graph.astream(
                        initial,
                        config=config,
                        stream_mode=["updates", "messages", "tasks", "checkpoints"],
                        durability="sync",
                        control=control.control,
                    ):
                        source = evidence.append("raw-events.jsonl", {"time": evidence.now(),
                            "run_id": request["run_id"], "session_id": request["session_id"], "scope": scope,
                            "stream_mode": mode, "data": event})
                        control.observe(mode, event, source)
                        if mode == "messages" and isinstance(event, (tuple, list)) and event:
                            metadata = getattr(event[0], "response_metadata", {})
                            if metadata.get("finish_reason") is not None:
                                evidence.append("model-terminals.jsonl", {"source": source, "response_metadata": metadata})
                    control.active = False
                snapshot = await graph.aget_state(config)
                save_snapshot(snapshot, output, evidence, scope, extension)
                messages = snapshot.values.get("messages", [])
                last = messages[-1] if messages else None
                native = completion(last_is_ai=isinstance(last, AIMessage),
                    tool_calls=getattr(last, "tool_calls", []), invalid_tool_calls=getattr(last, "invalid_tool_calls", []),
                    metadata=getattr(last, "response_metadata", {}), pending=snapshot.next,
                    task_errors=[task.error for task in snapshot.tasks if task.error],
                    interrupts=[interrupt for task in snapshot.tasks for interrupt in task.interrupts])
                summary.update({
                    "status": "denied" if tools.denied and native["native_status"] == "completed" else native["native_status"],
                    **native,
                    "experiment_policy_status": "execution_denied" if tools.denied else "no_execution_denial_observed",
                    "final_response": last.content if isinstance(last, AIMessage) else None,
                    "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id"),
                    "native_result": snapshot.values,
                    "native_terminal": {"finish_reason": native["finish_reason"], "response_metadata": native["response_metadata"],
                        "pending_tool_calls": bool(getattr(last, "tool_calls", []) or getattr(last, "invalid_tool_calls", [])),
                        "is_streaming": False, "error_message": "native_task_error" if native["native_status"] == "failed" else None},
                    "browser_download": getattr(tools, "download", None),
                    "business_rule_denial_observed": getattr(tools, "quantity_denied", False),
                })
            except GraphDrained as error:
                # 原生图已协作停止；读取其
                # 实际最后检查点；不伪造中止事件或恢复操作。
                control.active = False
                snapshot = await graph.aget_state(config)
                save_snapshot(snapshot, output, evidence, scope, extension)
                observation = control.drained(error, snapshot)
                if lifecycle and observation["pause_boundary_observed"]:
                    await lifecycle.prepare_pause(graph, config, control, observation)
                elif lifecycle and error.reason == "experiment_pause":
                    raise ValueError("原生暂停请求未到达干净、未完成的安全边界")
                summary.update({"status": "drained", "native_status": "drained", "stop_reason": "native_cooperative_drain",
                    "cancel_confirmed": observation["cancel_confirmed"], "immediate_cancel": False,
                    "native_exception": {"type": type(error).__module__ + "." + type(error).__qualname__, "reason": error.reason, "message": str(error)},
                    "checkpoint_id": observation["checkpoint_id"], "native_next": snapshot.next,
                    "native_tasks": snapshot.tasks, "native_result": snapshot.values,
                    "control_observation": observation, "task_continuation_verified": False,
                    "native_terminal": {"is_streaming": False, "error_message": None,
                        "pending_graph_nodes": snapshot.next, "pending_tool_calls": any(getattr(message, "tool_calls", []) for message in snapshot.values.get("messages", [])[-1:])}})
            except Exception as error:
                control.active = False
                summary.update({
                    "status": "timed_out" if isinstance(error, TimeoutError) else "failed",
                    "native_status": "timed_out" if isinstance(error, TimeoutError) else "failed",
                    "stop_reason": "active_timeout" if isinstance(error, TimeoutError) else "runtime_error",
                    "error_type": type(error).__name__, "error": str(error),
                })
                # 原生循环失败时，持久化实际最后一个检查点。
                snapshot = await graph.aget_state(config)
                save_snapshot(snapshot, output, evidence, scope, extension)
                summary["native_result"] = snapshot.values
                messages = snapshot.values.get("messages", [])
                last = messages[-1] if messages else None
                summary["native_terminal"] = {"finish_reason": getattr(last, "response_metadata", {}).get("finish_reason"),
                    "response_metadata": getattr(last, "response_metadata", {}),
                    "pending_tool_calls": bool(getattr(last, "tool_calls", []) or getattr(last, "invalid_tool_calls", [])),
                    "is_streaming": False, "error_message": str(error)}
        saver_closed = True
    except Exception as error:
        summary.update({"status": "timed_out" if isinstance(error, TimeoutError) else "failed",
            "native_status": "timed_out" if isinstance(error, TimeoutError) else "failed",
            "stop_reason": "active_timeout" if isinstance(error, TimeoutError) else "runtime_error",
            "error_type": type(error).__name__, "error": str(error)})
    finally:
        try:
            control.close()
            try:
                await tools.close()
                tools_closed = True
            except Exception as error:
                summary.update({"status": "failed", "stop_reason": "tool_cleanup_error", "error_type": type(error).__name__, "error": str(error)})
            if lifecycle:
                try:
                    evidence.finalize()
                    if lifecycle.pending is not None and summary["status"] == "drained":
                        pause_index = lifecycle.publish(saver_closed=saver_closed, tools_closed=tools_closed)
                        summary.update({"status": "paused", "native_status": "drained", "stop_reason": "native_safe_pause",
                            "pause_confirmed": True, "pause_index": pause_index,
                            "task_continuation_verified": False})
                except Exception as error:
                    summary.update({"status": "timed_out" if isinstance(error, TimeoutError) else "failed",
                        "stop_reason": "pause_persistence_error" if lifecycle.pending else "evidence_finalize_error",
                        "pause_confirmed": False, "error_type": type(error).__name__, "error": str(error)})
            else:
                evidence.finalize()
        finally:
            if lifecycle:
                lifecycle.close()
    summary["elapsed_seconds"] = time.monotonic() - started
    (output / "result.json").write_text(encoded(summary, pretty=True))
    print(encoded({"status": summary["status"], "stop_reason": summary["stop_reason"], "session_id": request["session_id"]}), end="")
    return 0 if summary["status"] in {"completed", "denied"} or summary["status"] == "drained" and summary.get("cancel_confirmed") is True or summary["status"] == "paused" and summary.get("pause_confirmed") is True else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        print(redact(f"{type(error).__name__}: {error}"), file=sys.stderr)
        raise SystemExit(1) from None
