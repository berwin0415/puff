"""Host-only triggers for native controls; never execute or grade an Agent."""
from datetime import datetime, timezone
import hashlib
import json


def cancel_on_deepagents_activity(lab, name, run, stop, run_id, scope):
    """Send one SIGUSR1 after observing an active native model/tools task.

    The candidate rechecks liveness at signal receipt. Host and candidate may
    legitimately observe different tasks as the graph progresses.
    """
    raw = run / "candidate/raw-events.jsonl"
    while not stop.wait(0.1):
        try:
            lines = raw.read_bytes().splitlines(keepends=True)
        except OSError:
            continue
        active, through = {}, None
        for number, line in enumerate(lines, 1):
            if not line.endswith(b"\n"):
                continue
            try:
                row = json.loads(line)
                if (not isinstance(row, dict) or row.get("run_id") != run_id
                        or row.get("session_id") != scope["session_id"] or row.get("scope") != scope):
                    continue
                through = {"path": "candidate/raw-events.jsonl", "line": number,
                           "line_sha256": hashlib.sha256(line).hexdigest()}
                if row.get("type") == "native_graph_drained":
                    active.clear()
                event = row.get("data")
                if row.get("stream_mode") != "tasks" or not isinstance(event, dict) or not isinstance(event.get("id"), str):
                    continue
                if "input" in event and "result" not in event:
                    active[event["id"]] = (number, line, event.get("name"))
                elif any(key in event for key in ("result", "error", "interrupts")):
                    active.pop(event["id"], None)
            except (ValueError, TypeError):
                continue
        candidates = [item for item in active.values() if item[2] in {"model", "tools"}]
        if not candidates:
            continue
        number, line, _ = max(candidates)
        record = {"requested_at": datetime.now(timezone.utc).isoformat(), "container_name": name,
            "candidate": "deepagents_langgraph", "run_id": run_id, "session_id": scope["session_id"],
            "scope": scope, "signal": "SIGUSR1",
            "trigger": {"path": "candidate/raw-events.jsonl", "line": number},
            "trigger_line_sha256": hashlib.sha256(line).hexdigest(), "observed_through": through}
        try:
            sent = lab.call(["docker", "kill", "--signal", "SIGUSR1", name], capture=True, check=False, timeout=10)
            record["signal_exit_code"] = sent.returncode
        except Exception as error:
            record["error_type"] = type(error).__name__
        (run / "control-request.json").write_text(json.dumps(record) + "\n")
        return
