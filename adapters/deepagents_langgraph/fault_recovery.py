"""I23 binding only: resume the original SQLite head through public graph APIs.

No pause-index, checkpoint manufacture, task retry, receipt lookup or state
reconstruction is performed here. The trusted bench owns the crash reference.
The ordinary I22 entrypoint does not import this module.
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
    """Record public native values as data; never feed this projection back.

    The fixed graph persists Send objects for pushed tool tasks. Its ordinary
    event encoder does not support those objects. Keep their full arguments
    for the independent physical audit, without changing the original saver
    tuple, graph snapshot or None resume input.
    """
    if type(value).__module__ == "langgraph.types" and type(value).__name__ == "Send":
        from langgraph.types import Send
        if type(value) is not Send or not isinstance(value.node, str) or not value.node or value.timeout is not None:
            raise TypeError("Unsupported I23 native Send shape")
        return {"native_send": {"node": value.node, "arg": crash_projection(value.arg), "timeout": None}}
    if isinstance(value, dict):
        return {key: crash_projection(item) for key, item in value.items()}
    if isinstance(value, list):
        return [crash_projection(item) for item in value]
    if isinstance(value, tuple):
        return tuple(crash_projection(item) for item in value)
    # Messages and other ordinary public values retain the existing strict
    # event encoder. Unknown objects still fail there, as before.
    return value


def tree(root):
    """Hash original files without fsync, SQLite open or recovery side effects."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or any(p.is_symlink() for p in root.parents):
        raise ValueError("Invalid I23 native tree")
    result, total = {}, 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("I23 native files must be regular and unlinked")
        data = _read_file(path, 64 * 1024**2)
        total += len(data)
        if len(result) >= 4096 or total > 256 * 1024**2:
            raise ValueError("I23 native tree exceeds bound")
        result[path.relative_to(root).as_posix()] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return result


def validate_request(request):
    value = request.get("fault_recovery")
    if (not isinstance(value, dict) or set(value) != {"mode", "chain_id", "attempt_id", "binding_file"}
            or (value["mode"], value["attempt_id"]) not in {("new", "worker-a"), ("crash-resume", "worker-b")}
            or value["chain_id"] != request.get("run_id") or value["binding_file"] != "/config/native-crash.json"
            or request.get("candidate_options") != {"case": "tools", "trusted_scope_file": "/config/scope.json"}
            or "lifecycle" in request or "followup" in request):
        raise ValueError("Invalid independent I23 request")
    if value["mode"] == "crash-resume" and "prompt" in request:
        raise ValueError("Crash resume must not accept a new prompt")
    return value


class CrashRecovery:
    def __init__(self, request, scope, provenance, model_token, started):
        self.request, self.scope, self.started = request, scope, started
        self.control = validate_request(request)
        self.descriptor = None
        path = Path(self.control["binding_file"])
        if any(p.is_symlink() for p in (path, *path.parents)) or not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise ValueError("I23 binding requires its exact read-only file mount")
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
            raise ValueError("I23 original scope/runtime/material binding mismatch")
        budget = binding["budget"]
        if not isinstance(budget, dict) or set(budget) != {"active_limit_seconds", "used_active_seconds", "remaining_seconds"}:
            raise ValueError("Invalid I23 active budget")
        used, remaining = budget["used_active_seconds"], budget["remaining_seconds"]
        if (budget["active_limit_seconds"] != 300 or type(used) not in (float, int) or not math.isfinite(used)
                or not 0 <= used < 300 or type(remaining) is not int or not 1 <= remaining <= 300 - used
                or remaining != request["timeout_seconds"]):
            raise ValueError("I23 budget reset or exhausted")
        self.remaining()
        if any(Path(request["output_dir"]).iterdir()):
            raise ValueError("I23 attempt output must start empty")
        self.state, self.workspace = Path(request["state_dir"]), Path(request["workspace"])
        if self.control["mode"] == "new":
            if binding["recovery"] is not None or used != 0 or any(self.state.iterdir()):
                raise ValueError("New I23 task requires empty native state")
        else:
            recovery = binding["recovery"]
            if (not isinstance(recovery, dict) or set(recovery) != {"boundary_sha256", "audit_sha256", "checkpoint_id",
                    "pending_write_keys", "state_files", "workspace_files"}
                    or not isinstance(recovery["checkpoint_id"], str) or not recovery["checkpoint_id"]
                    or recovery["state_files"] != tree(self.state) or recovery["workspace_files"] != tree(self.workspace)
                    or "checkpoints.sqlite" not in recovery["state_files"]
                    or "pause-index.json" in recovery["state_files"]):
                raise ValueError("I23 resume requires the unmodified natural crash files")
        self.descriptor = os.open(self.state / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
                raise ValueError("Invalid I23 process lock")
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def remaining(self):
        remaining = self.request["timeout_seconds"] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("I23 continuous active budget exhausted")
        return remaining

    def runtime(self, definitions):
        actual = runtime_identity_sha256(model_name=os.environ["MODEL_NAME"],
            max_output_tokens=self.request["max_output_tokens"], max_retries=self.request["max_retries"],
            candidate_options=self.request["candidate_options"], tool_definitions=definitions)
        if actual != self.binding["runtime_sha256"]:
            raise ValueError("I23 actual tool/model/graph surface changed")
        return actual

    def config(self):
        # Omitting checkpoint_id uses LangGraph's native current-head and pending
        # write semantics. Supplying a historical checkpoint could replay a step.
        return {"configurable": {"thread_id": self.request["session_id"], "checkpoint_ns": ""}, "recursion_limit": 64}

    async def check_resume(self, graph, saver):
        original = self.binding["recovery"]
        config = self.config()
        saved = await saver.aget_tuple(config)
        if saved is None or saved.config.get("configurable", {}).get("checkpoint_id") != original["checkpoint_id"]:
            raise ValueError("I23 public saver did not read the audited natural head")
        keys = sorted([task_id, channel] for task_id, channel, _ in saved.pending_writes)
        if keys != original["pending_write_keys"]:
            raise ValueError("I23 public saver pending writes differ from physical SQLite audit")
        snapshot = await graph.aget_state(config)
        if (snapshot.config.get("configurable", {}).get("checkpoint_id") != original["checkpoint_id"]
                or not snapshot.next or snapshot.interrupts or any(task.error or task.interrupts for task in snapshot.tasks)):
            raise ValueError("I23 public graph did not expose an executable unfinished crash head")
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
