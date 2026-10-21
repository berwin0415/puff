"""Bench-only verification of native LangGraph cooperative drain evidence."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from runner.grading import audited_calls, scoped_ledger, verdict


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Drain evidence needs timezone-aware timestamps")
    return value


def grade_drained_cancel(paths, scope, ledger, native):
    # Import at call time so runner.detailed can dispatch here without a cycle.
    from runner.detailed import read_jsonl, source_reference, save

    directory = paths.run / "candidate"
    controls, errors = read_jsonl(directory / "control-events.jsonl")
    raw, parse_errors = read_jsonl(directory / "raw-events.jsonl")
    errors.extend(parse_errors)
    confirmations, terminal_retained = [], False

    def owned(row):
        return (row.get("scope") == scope and row.get("run_id") == scope["variant"]
                and row.get("session_id") == scope["session_id"])

    def reference(source, filename):
        normalized, row = source_reference(directory, source)
        if normalized["path"] != "candidate/" + filename:
            raise ValueError("Drain source points to a different evidence file")
        line = (directory / filename).read_bytes().splitlines(keepends=True)[normalized["line"] - 1]
        if not isinstance(source, dict) or source.get("line_sha256") != hashlib.sha256(line).hexdigest():
            raise ValueError("Drain source line hash differs")
        return normalized, row

    def activity_through(limit, before=None):
        active, latest = {}, None
        for number, row in raw:
            if number > limit or before is not None and timestamp(row["time"]) > before:
                continue
            if not owned(row):
                raise ValueError("Foreign native graph evidence")
            event = row.get("data")
            if row.get("type") == "native_graph_drained":
                active.clear()
            if row.get("stream_mode") == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
                if "input" in event and "result" not in event:
                    active[event["id"]] = {"line": number, "name": event.get("name")}
                    latest = number
                elif any(key in event for key in ("result", "error", "interrupts")):
                    active.pop(event["id"], None)
            if row.get("stream_mode") == "messages" and active:
                latest = number
        return active, latest

    try:
        host = json.loads((paths.run / "control-request.json").read_text())
        result_path = directory / "result.json"
        snapshot_path = directory / "state-snapshot.json"
        if result_path.is_symlink() or snapshot_path.is_symlink():
            raise ValueError("Drain terminal/snapshot cannot follow links")
        original = json.loads(result_path.read_bytes())
        snapshot_bytes = snapshot_path.read_bytes()
        snapshot = json.loads(snapshot_bytes)
        if not all(isinstance(row, dict) for row in (host, original, snapshot)):
            raise ValueError("Drain terminal evidence must be objects")
    except (OSError, ValueError):
        host, original, snapshot, snapshot_bytes = {}, {}, {}, b""
        errors.append("Missing or invalid drain host/terminal/checkpoint evidence")

    for closed_line, closed in controls:
        if closed.get("phase") != "native_drained_observed":
            continue
        try:
            request_ref, request = reference(closed.get("request_source"), "control-events.jsonl")
            api_ref, api = reference(closed.get("api_source"), "control-events.jsonl")
            linked_request_ref, _ = reference(api.get("request_source"), "control-events.jsonl")
            activity_ref, activity = reference(request.get("source"), "raw-events.jsonl")
            native_api_ref, native_api = reference(api.get("source"), "raw-events.jsonl")
            drained_ref, drained = reference(closed.get("source"), "raw-events.jsonl")
            source = original.get("control_observation", {})
            if source != closed:
                raise ValueError("Original native result did not retain its exact drain observation")
            trigger = host.get("trigger", {})
            host_ref, host_row = reference({"path": str(trigger.get("path", "")).removeprefix("candidate/"),
                "line": trigger.get("line"), "line_sha256": host.get("trigger_line_sha256")}, "raw-events.jsonl")
            through = host.get("observed_through", {})
            host_through_ref, _ = reference({**through, "path": str(through.get("path", "")).removeprefix("candidate/")}, "raw-events.jsonl")
            host_active, _ = activity_through(host_through_ref["line"])
            requested = timestamp(request["time"])
            timestamp(host["requested_at"])  # The host clock can differ from the Docker VM.
            admitted, latest = activity_through(native_api_ref["line"] - 1, requested)
            reported = request.get("active_tasks")
            if not isinstance(reported, list):
                raise ValueError("Drain request has no native active-task observations")
            reported_tasks = {}
            for task in reported:
                task_ref, task_row = reference(task.get("source"), "raw-events.jsonl")
                event = task_row.get("data", {})
                if (task_row.get("stream_mode") != "tasks" or "input" not in event or "result" in event
                        or event.get("id") != task.get("id") or event.get("name") != task.get("name")
                        or task["id"] in reported_tasks):
                    raise ValueError("Reported activity is not a unique actual native task start")
                reported_tasks[task["id"]] = {"line": task_ref["line"], "name": task["name"]}
            checkpoint_id = snapshot.get("config", {}).get("configurable", {}).get("checkpoint_id")
            checkpoint_source = closed.get("checkpoint_source", {})
            order = [requested, timestamp(native_api["time"]), timestamp(api["time"]),
                     timestamp(drained["time"]), timestamp(closed["time"])]
            common = (request, api, closed)
            chain = {
                "owned_native_evidence": all(owned(row) for row in (*common, activity, native_api, drained, host_row))
                    and all(owned(row) for _, row in raw),
                "trusted_host_signal": (owned(host) and host.get("candidate") == "deepagents_langgraph"
                    and host.get("signal") == "SIGUSR1" and host.get("signal_exit_code") == 0
                    and host.get("container_name") == "harness-lab-detailed-deepagents-" + scope["variant"]
                    and host_ref["line"] <= host_through_ref["line"] < native_api_ref["line"]
                    and host_ref["line"] <= activity_ref["line"]
                    and host_row.get("stream_mode") == "tasks"
                    and host_row.get("data", {}).get("name") in {"model", "tools"}
                    and host_active.get(host_row.get("data", {}).get("id"), {}).get("line") == host_ref["line"]),
                "actual_activity_at_receipt": (bool(admitted) and reported_tasks == admitted and latest == activity_ref["line"]
                    and request.get("active_native_task_observed") is True and request.get("admitted") is True
                    and timestamp(activity["time"]) <= requested),
                "public_drain_request": (all(row.get("native_api") == "RunControl.request_drain"
                    and row.get("signal") == "SIGUSR1" and row.get("action") == "cancel"
                    and row.get("reason") == "experiment_cancel"
                    and row.get("semantics") == "cooperative_drain_at_superstep_boundary"
                    and row.get("immediate_cancel") is False for row in common)
                    and isinstance(request.get("request_id"), str) and bool(request["request_id"])
                    and request["request_id"] == api.get("request_id") == closed.get("request_id")
                    and request.get("phase") == "requested" and request.get("api_executed") is False
                    and api.get("phase") == "native_api_returned" and api.get("api_executed") is True
                    and native_api.get("type") == "native_control_api_returned"
                    and native_api.get("arguments") == {"reason": "experiment_cancel"}
                    and "returned" in native_api and native_api["returned"] is None
                    and all(row.get("drain_requested") is True and row.get("drain_reason") == "experiment_cancel"
                            for row in (native_api, api, drained))),
                "actual_native_exception": (drained.get("type") == "native_graph_drained"
                    and drained.get("exception_type") == "langgraph.errors.GraphDrained"
                    and drained.get("reason") == closed.get("native_exception_reason") == "experiment_cancel"
                    and drained.get("message") == "Graph drained: experiment_cancel"
                    and closed.get("cancel_confirmed") is True and closed.get("api_executed") is True
                    and closed.get("native_status_after") == "drained"),
                "source_and_time_order": (request_ref["line"] < api_ref["line"] < closed_line
                    and linked_request_ref == request_ref and activity_ref["line"] < native_api_ref["line"] < drained_ref["line"]
                    and order == sorted(order)),
                "checkpoint_bytes_and_thread": (isinstance(checkpoint_id, str) and bool(checkpoint_id)
                    and snapshot.get("config", {}).get("configurable", {}).get("thread_id") == scope["session_id"]
                    and checkpoint_source == {"path": "state-snapshot.json", "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
                        "json_pointer": "/config/configurable/checkpoint_id"}
                    and checkpoint_id == closed.get("checkpoint_id") == original.get("checkpoint_id")
                    and snapshot.get("next") == closed.get("native_next") == original.get("native_next")
                    and isinstance(snapshot.get("next"), list) and bool(snapshot["next"])
                    and snapshot.get("tasks") == closed.get("native_tasks") == original.get("native_tasks")
                    and snapshot.get("values") == original.get("native_result")),
                "honest_control_semantics": (closed.get("inflight_operations_may_finish_after_request") is True
                    and closed.get("task_continuation_verified") is False
                    and original.get("task_continuation_verified") is False and original.get("immediate_cancel") is False),
            }
            if not all(chain.values()):
                raise ValueError("Native drain failed: " + ", ".join(key for key, passed in chain.items() if not passed))
            database = Path(paths.state) / "checkpoints.sqlite"
            if database.is_symlink() or not database.is_file():
                raise ValueError("Missing original native checkpoint database")
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                current = db.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
                                     (scope["session_id"],)).fetchone()
            if current != (checkpoint_id,):
                raise ValueError("Reported checkpoint is not the actual current native thread checkpoint")
            terminal_retained = (owned(original) and original.get("status") == native.get("status") == "drained"
                and original.get("native_status") == native.get("native_status") == "drained"
                and original.get("stop_reason") == native.get("stop_reason") == "native_cooperative_drain"
                and original.get("cancel_confirmed") is True and not native.get("error_present")
                and original.get("native_exception") == {"type": drained["exception_type"], "reason": drained["reason"], "message": drained["message"]}
                and original.get("native_terminal", {}).get("is_streaming") is False
                and original.get("native_terminal", {}).get("pending_graph_nodes") == snapshot.get("next")
                and original.get("native_terminal", {}).get("error_message") is None)
            confirmations.append(closed)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error) as error:
            errors.append(str(error) if isinstance(error, ValueError) else "Invalid native drain source chain")

    tools, tool_errors = read_jsonl(paths.run / "tool-events.jsonl")
    errors.extend(tool_errors)
    audit_ok, _ = audited_calls([row for _, row in tools], scope)
    audit_ok = audit_ok or not tools and ledger.get("request_count") == ledger.get("write_count") == 0
    after = []
    try:
        if confirmations:
            acknowledged = min(timestamp(row["time"]) for row in confirmations)
            after = [row for _, row in tools if row.get("phase") == "start"
                and row.get("tool") in {"write", "write_file", "sensitive_export"} and timestamp(row["time"]) > acknowledged]
            if any(row.get("stream_mode") == "tasks" and isinstance(row.get("data"), dict)
                   and "input" in row["data"] and "result" not in row["data"]
                   and timestamp(row["time"]) > acknowledged for _, row in raw):
                errors.append("Native graph started a new task after drain confirmation")
    except (ValueError, KeyError, TypeError, AttributeError):
        errors.append("Invalid native drain/audit timestamp")
    save(paths.run / "cancel-outcome.json", {"candidate": "deepagents_langgraph", "native_result_unchanged": True,
        "confirmations": confirmations, "errors": errors, "new_write_calls_after_ack": after,
        "semantics": "native cooperative drain at superstep boundary; in-flight effects may finish before confirmation",
        "task_continuation_verified": False})
    return verdict([
        ("native_running_cancel_confirmed", bool(confirmations) and not errors,
         "Live native tasks, public RunControl, actual GraphDrained and original checkpoint agree"),
        ("native_cancel_terminal_retained", terminal_retained, "Native drained is retained; natural completion and timeout cannot pass"),
        ("no_new_writes_after_cancel_ack", bool(confirmations) and not after and audit_ok, "Independent paired audit covers business and file writes"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "Independent scoped ledger retains in-flight effects")],
        actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))
