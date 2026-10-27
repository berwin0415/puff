"""仅用于 I23 绑定：通过公开图 API 恢复原始 SQLite 最新检查点。

此处不生成暂停索引或检查点，不重试任务、查询回执或重建状态。
故障引用由可信实验台管理。普通 I22 入口不导入此模块。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from pause_recovery import _json, _read_file, runtime_identity_sha256


LOCK = ".i23-chain.lock"
SCHEMA = "harness-lab-crash-binding/1"
FIELDS = frozenset(("schema", "candidate", "mode", "chain_id", "attempt_id", "scope", "session_id",
    "checkpoint_ns", "source_export_sha256", "adapter_export_sha256", "runtime_sha256",
    "candidate_image_id", "model_token_sha256", "budget", "recovery"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def crash_projection(value):
    """将公开原生值记录为数据，绝不将此投影回灌。

    固定图会持久化已推送工具任务的 Send 对象，普通事件编码器
    不支持这些对象。保留完整参数供独立实体核查，不修改原始
    保存器元组、图快照或值为 None 的恢复输入。
    """
    if type(value).__module__ == "langgraph.types" and type(value).__name__ == "Send":
        from langgraph.types import Send
        if type(value) is not Send or not isinstance(value.node, str) or not value.node or value.timeout is not None:
            raise TypeError("不支持的 I23 原生 Send 结构")
        return {"native_send": {"node": value.node, "arg": crash_projection(value.arg), "timeout": None}}
    if isinstance(value, dict):
        return {key: crash_projection(item) for key, item in value.items()}
    if isinstance(value, list):
        return [crash_projection(item) for item in value]
    if isinstance(value, tuple):
        return tuple(crash_projection(item) for item in value)
    # 消息及其他普通公开值继续使用已有的严格
    # 事件编码器；未知对象仍与之前一样被拒绝。
    return value


def tree(root):
    """对原始文件计算哈希，不执行 fsync、打开 SQLite 或触发恢复副作用。"""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or any(p.is_symlink() for p in root.parents):
        raise ValueError("I23 原生目录树无效")
    result, total = {}, 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("I23 原生文件必须是普通文件且无额外硬链接")
        data = _read_file(path, 64 * 1024**2)
        total += len(data)
        if len(result) >= 4096 or total > 256 * 1024**2:
            raise ValueError("I23 原生目录树超出限制")
        result[path.relative_to(root).as_posix()] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return result


def validate_request(request):
    value = request.get("fault_recovery")
    if (not isinstance(value, dict) or set(value) != {"mode", "chain_id", "attempt_id", "binding_file"}
            or (value["mode"], value["attempt_id"]) not in {("new", "worker-a"), ("crash-resume", "worker-b")}
            or value["chain_id"] != request.get("run_id") or value["binding_file"] != "/config/native-crash.json"
            or request.get("candidate_options") != {"case": "tools", "trusted_scope_file": "/config/scope.json"}
            or "lifecycle" in request or "followup" in request):
        raise ValueError("独立 I23 请求无效")
    if value["mode"] == "crash-resume" and "prompt" in request:
        raise ValueError("故障恢复不能接收新的提示词")
    return value


class CrashRecovery:
    def __init__(self, request, scope, provenance, model_token, started):
        self.request, self.scope, self.started = request, scope, started
        self.control = validate_request(request)
        self.descriptor = None
        path = Path(self.control["binding_file"])
        if any(p.is_symlink() for p in (path, *path.parents)) or not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError("I23 绑定必须使用精确匹配的只读文件挂载")
        self.binding = binding = _json(_read_file(path, 4 * 1024**2))
        if (not isinstance(binding, dict) or set(binding) != FIELDS or binding["schema"] != SCHEMA
                or binding["candidate"] != "deepagents_langgraph"
                or any(binding[key] != self.control[key] for key in ("mode", "chain_id", "attempt_id"))
                or binding["scope"] != scope or scope.get("experiment_id") != "fault-recovery"
                or scope.get("variant") != binding["chain_id"] or scope.get("session_id") != request["session_id"]
                or binding["session_id"] != request["session_id"] or binding["checkpoint_ns"] != ""
                or any(binding[key] != provenance[key] for key in ("source_export_sha256", "adapter_export_sha256"))
                or binding["candidate_image_id"] != os.environ.get("HARNESS_I23_IMAGE_ID")
                or binding["model_token_sha256"] != hashlib.sha256(model_token.encode()).hexdigest()):
            raise ValueError("I23 原始作用域、运行时或材料绑定不匹配")
        budget = binding["budget"]
        if not isinstance(budget, dict) or set(budget) != {"active_limit_seconds", "used_active_seconds", "remaining_seconds"}:
            raise ValueError("I23 活跃执行预算无效")
        used, remaining = budget["used_active_seconds"], budget["remaining_seconds"]
        if (budget["active_limit_seconds"] != 300 or type(used) not in (float, int) or not math.isfinite(used)
                or not 0 <= used < 300 or type(remaining) is not int or not 1 <= remaining <= 300 - used
                or remaining != request["timeout_seconds"]):
            raise ValueError("I23 预算被重置或已耗尽")
        self.remaining()
        if any(Path(request["output_dir"]).iterdir()):
            raise ValueError("I23 每次尝试的输出目录必须为空")
        self.state, self.workspace = Path(request["state_dir"]), Path(request["workspace"])
        if self.control["mode"] == "new":
            if binding["recovery"] is not None or used != 0 or any(self.state.iterdir()):
                raise ValueError("新 I23 任务必须从空原生状态开始")
        else:
            recovery = binding["recovery"]
            if (not isinstance(recovery, dict) or set(recovery) != {"boundary_sha256", "audit_sha256", "checkpoint_id",
                    "pending_write_keys", "state_files", "workspace_files"}
                    or not isinstance(recovery["checkpoint_id"], str) or not recovery["checkpoint_id"]
                    or recovery["state_files"] != tree(self.state) or recovery["workspace_files"] != tree(self.workspace)
                    or "checkpoints.sqlite" not in recovery["state_files"]
                    or "pause-index.json" in recovery["state_files"]):
                raise ValueError("I23 恢复必须使用未经修改的自然故障文件")
        self.descriptor = os.open(self.state / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
                raise ValueError("I23 进程锁无效")
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def remaining(self):
        remaining = self.request["timeout_seconds"] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("I23 连续活跃执行预算已耗尽")
        return remaining

    def runtime(self, definitions):
        actual = runtime_identity_sha256(model_name=os.environ["MODEL_NAME"],
            max_output_tokens=self.request["max_output_tokens"], max_retries=self.request["max_retries"],
            candidate_options=self.request["candidate_options"], tool_definitions=definitions)
        if actual != self.binding["runtime_sha256"]:
            raise ValueError("I23 实际工具、模型或图接口发生变化")
        return actual

    def config(self):
        # 省略 checkpoint_id，使用 LangGraph 原生最新检查点和待写入
        # 语义；指定历史检查点可能重放某个步骤。
        return {"configurable": {"thread_id": self.request["session_id"], "checkpoint_ns": ""}, "recursion_limit": 64}

    async def check_resume(self, graph, saver):
        original = self.binding["recovery"]
        config = self.config()
        saved = await saver.aget_tuple(config)
        if saved is None or saved.config.get("configurable", {}).get("checkpoint_id") != original["checkpoint_id"]:
            raise ValueError("I23 公开保存器未读到已核查的自然最新检查点")
        keys = sorted([task_id, channel] for task_id, channel, _ in saved.pending_writes)
        if keys != original["pending_write_keys"]:
            raise ValueError("I23 公开保存器的待写入记录与 SQLite 实体核查结果不一致")
        snapshot = await graph.aget_state(config)
        if (snapshot.config.get("configurable", {}).get("checkpoint_id") != original["checkpoint_id"]
                or not snapshot.next or snapshot.interrupts or any(task.error or task.interrupts for task in snapshot.tasks)):
            raise ValueError("I23 公开图接口未暴露可执行的未完成故障检查点")
        return crash_projection({"schema": "harness-lab-native-crash-resume/1", "scope": self.scope,
            "chain_id": self.binding["chain_id"], "attempt_id": "worker-b", "native_api": "AsyncSqliteSaver.aget_tuple; graph.aget_state; graph.astream(None)",
            "boundary_sha256": original["boundary_sha256"], "physical_audit_sha256": original["audit_sha256"],
            "checkpoint_id": original["checkpoint_id"], "checkpoint_tuple": saved._asdict(),
            "snapshot_before_resume": snapshot._asdict(), "pending_write_keys": keys,
            "config": config, "input": None, "new_prompt_submitted": False,
            "pause_index_used": False, "original_files_checked_before_saver_open": True})

    def close(self):
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None
