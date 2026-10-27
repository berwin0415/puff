"""仅在 bench 中验证原生 LangGraph 协作 drain 证据。"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from runner.grading import audited_calls, scoped_ledger, verdict


def timestamp(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("drain 证据时间戳必须包含时区")
    return value


def grade_drained_cancel(paths, scope, ledger, native):
    # 调用时才导入，避免 runner.detailed 分派到此处时循环导入。
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
            raise ValueError("drain 来源指向不同的证据文件")
        line = (directory / filename).read_bytes().splitlines(keepends=True)[normalized["line"] - 1]
        if not isinstance(source, dict) or source.get("line_sha256") != hashlib.sha256(line).hexdigest():
            raise ValueError("drain 来源行哈希不一致")
        return normalized, row

    def activity_through(limit, before=None):
        active, latest = {}, None
        for number, row in raw:
            if number > limit or before is not None and timestamp(row["time"]) > before:
                continue
            if not owned(row):
                raise ValueError("发现其他任务的原生图证据")
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
            raise ValueError("drain 终态/快照不能跟随链接")
        original = json.loads(result_path.read_bytes())
        snapshot_bytes = snapshot_path.read_bytes()
        snapshot = json.loads(snapshot_bytes)
        if not all(isinstance(row, dict) for row in (host, original, snapshot)):
            raise ValueError("drain 终态证据必须为对象")
    except (OSError, ValueError):
        host, original, snapshot, snapshot_bytes = {}, {}, {}, b""
        errors.append("drain 宿主机、终态或检查点证据缺失或无效")

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
                raise ValueError("原始原生结果未保留精确 drain 观测")
            trigger = host.get("trigger", {})
            host_ref, host_row = reference({"path": str(trigger.get("path", "")).removeprefix("candidate/"),
                "line": trigger.get("line"), "line_sha256": host.get("trigger_line_sha256")}, "raw-events.jsonl")
            through = host.get("observed_through", {})
            host_through_ref, _ = reference({**through, "path": str(through.get("path", "")).removeprefix("candidate/")}, "raw-events.jsonl")
            host_active, _ = activity_through(host_through_ref["line"])
            requested = timestamp(request["time"])
            timestamp(host["requested_at"])  # 宿主机时钟可能与 Docker VM 不同。
            admitted, latest = activity_through(native_api_ref["line"] - 1, requested)
            reported = request.get("active_tasks")
            if not isinstance(reported, list):
                raise ValueError("drain 请求没有原生活跃任务观测")
            reported_tasks = {}
            for task in reported:
                task_ref, task_row = reference(task.get("source"), "raw-events.jsonl")
                event = task_row.get("data", {})
                if (task_row.get("stream_mode") != "tasks" or "input" not in event or "result" in event
                        or event.get("id") != task.get("id") or event.get("name") != task.get("name")
                        or task["id"] in reported_tasks):
                    raise ValueError("报告的活动不是唯一真实原生任务启动")
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
                raise ValueError("原生 drain 失败：" + ", ".join(key for key, passed in chain.items() if not passed))
            database = Path(paths.state) / "checkpoints.sqlite"
            if database.is_symlink() or not database.is_file():
                raise ValueError("缺少原始原生检查点数据库")
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                current = db.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
                                     (scope["session_id"],)).fetchone()
            if current != (checkpoint_id,):
                raise ValueError("报告检查点不是当前原生线程的真实检查点")
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
            errors.append(str(error) if isinstance(error, ValueError) else "无效原生 drain 来源链")

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
                errors.append("原生图在 drain 确认后启动了新任务")
    except (ValueError, KeyError, TypeError, AttributeError):
        errors.append("原生 drain 或审计时间戳无效")
    save(paths.run / "cancel-outcome.json", {"candidate": "deepagents_langgraph", "native_result_unchanged": True,
        "confirmations": confirmations, "errors": errors, "new_write_calls_after_ack": after,
        "semantics": "在超步边界进行原生协作 drain；在途副作用可能在确认前完成",
        "task_continuation_verified": False})
    return verdict([
        ("native_running_cancel_confirmed", bool(confirmations) and not errors,
         "运行中原生任务、公共 RunControl、实际 GraphDrained 和原始检查点相符"),
        ("native_cancel_terminal_retained", terminal_retained, "保留原生 drained；自然完成或超时不能算通过"),
        ("no_new_writes_after_cancel_ack", bool(confirmations) and not after and audit_ok, "独立配对审计覆盖业务和文件写入"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "独立作用域账本保留在途副作用")],
        actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))
