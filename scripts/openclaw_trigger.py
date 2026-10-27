"""宿主仅观察并发送 SIGUSR1；不在宿主运行 OpenClaw。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json


def cancel_on_openclaw_activity(call, name, run, stop, run_id, scope):
    """在完整原始记录前缀中观察到官方 llm_input 活跃事件后，调用 Lab.call。

    宿主与 Docker VM 的时钟相互独立。读取时钩子是否活跃由保留的前缀证明，不通过比较两者的墙上时钟判断。
    """
    if scope.get("variant") != run_id or name != "harness-lab-detailed-openclaw-" + run_id:
        raise ValueError('取消 OpenClaw 必须绑定本次可信运行的确切容器')
    raw = run / "candidate/raw-events.jsonl"
    entrypoint = run / "candidate/entrypoint.json"
    while not stop.wait(0.1):
        try:
            if raw.is_symlink() or entrypoint.is_symlink():
                continue
            identity = json.loads(entrypoint.read_text())
            if (identity.get("run_id") != run_id or identity.get("session_id") != scope["session_id"]
                    or identity.get("scope") != scope or identity.get("agent_id") != "lab-a"
                    or not isinstance(identity.get("session_key"), str) or not identity["session_key"]):
                continue
            snapshot = raw.read_bytes()
            if len(snapshot) > 32 * 1024 * 1024:
                return
            complete = snapshot[:snapshot.rfind(b"\n") + 1].splitlines(keepends=True)
            active = {}
            for number, line in enumerate(complete, 1):
                row = json.loads(line)
                context, event = row.get("context", {}), row.get("event", {})
                if (context.get("agentId") != "lab-a" or context.get("sessionKey") != identity["session_key"]):
                    continue
                native_run = event.get("runId") or context.get("runId")
                if not isinstance(native_run, str) or not native_run:
                    continue
                if row.get("type") == "agent_end":
                    active.pop(native_run, None)
                elif row.get("type") == "llm_input" and event.get("runId") == native_run:
                    active[native_run] = (number, line, row)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if not active:
            continue
        number, line, row = max(active.values(), key=lambda value: value[0])
        record = {"requested_at": datetime.now(timezone.utc).isoformat(), "container_name": name,
                  "candidate": "openclaw", "run_id": run_id, "session_id": scope["session_id"], "scope": scope,
                  "signal": "SIGUSR1", "native_run_id": row["event"]["runId"],
                  "session_key": identity["session_key"], "agent_id": "lab-a",
                  "trigger": {"path": "candidate/raw-events.jsonl", "line": number},
                  "trigger_line_sha256": hashlib.sha256(line).hexdigest(),
                  "observed_through": {"path": "candidate/raw-events.jsonl", "line": len(complete),
                                       "line_sha256": hashlib.sha256(complete[-1]).hexdigest(),
                                       "prefix_sha256": hashlib.sha256(b"".join(complete)).hexdigest()}}
        try:
            sent = call(["docker", "kill", "--signal", "SIGUSR1", name], capture=True, check=False, timeout=10)
            record["signal_exit_code"] = sent.returncode
        except Exception as error:
            record["error_type"] = type(error).__name__
        (run / "control-request.json").write_text(json.dumps(record) + "\n")
        return
