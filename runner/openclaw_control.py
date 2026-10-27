"""独立判定 OpenClaw 取消证据，不导入候选框架或加载其运行时。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("OpenClaw 证据时间戳必须包含时区")
    return parsed


def _json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("OpenClaw 证据必须是普通文件：" + path.name)
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("OpenClaw 证据必须是 JSON 对象：" + path.name)
    return value


def grade_openclaw_cancel(paths, scope, ledger, native):
    """保留 failed/aborted，并证明 HTTP 已断连且原生执行已结束。

    在函数内导入，避免 runner.detailed 分派到此处时发生循环导入。
    合成测试只校准证据拒绝逻辑，不能证明候选框架的能力。"""
    from runner.detailed import read_jsonl, source_reference
    from runner.experiment import save
    from runner.grading import audited_calls, same_scope, scoped_ledger, verdict

    directory = paths.run / "candidate"
    controls, errors = read_jsonl(directory / "control-events.jsonl")
    raw_rows, raw_errors = read_jsonl(directory / "raw-events.jsonl")
    rpc_rows, rpc_errors = read_jsonl(directory / "gateway-control-events.jsonl")
    transport_rows, transport_errors = read_jsonl(directory / "http-transport-events.jsonl")
    errors.extend(raw_errors + rpc_errors + transport_errors)
    confirmations, terminal_retained = [], False

    def reference(value, filename):
        ref, row = source_reference(directory, value)
        if ref["path"] != "candidate/" + filename:
            raise ValueError("OpenClaw 来源引用了不同的证据文件")
        return ref, row

    def line_hash(ref):
        filename = ref["path"].removeprefix("candidate/")
        return hashlib.sha256((directory / filename).read_bytes().splitlines(keepends=True)[ref["line"] - 1]).hexdigest()

    def hook_run(row):
        return row.get("event", {}).get("runId") or row.get("context", {}).get("runId")

    def owned_hook(row, run_id, session_key, native_session):
        context = row.get("context", {})
        event_session = row.get("event", {}).get("sessionId")
        return (context.get("agentId") == "lab-a" and context.get("sessionKey") == session_key
                and context.get("sessionId") == native_session and hook_run(row) == run_id
                and context.get("runId", run_id) == run_id
                and (event_session is None or event_session == native_session))

    def rpc(value, method, params):
        returned_ref, returned = reference(value, "gateway-control-events.jsonl")
        requested_ref, requested = reference(returned.get("request_source"), "gateway-control-events.jsonl")
        result = returned.get("result")
        if not (requested.get("phase") == "requested" and returned.get("phase") == "returned"
                and all(row.get("method") == method and row.get("params") == params
                        and same_scope(row.get("scope"), scope) for row in (requested, returned))
                and requested_ref["line"] < returned_ref["line"]
                and _timestamp(requested["time"]) <= _timestamp(returned["time"])
                and type(returned.get("exit_code")) is int and returned["exit_code"] == 0
                and returned.get("exit_signal") is None and returned.get("error") is None
                and returned.get("output_truncated") is False
                and returned.get("evidence_kind") == "official_cli_stdout_json_not_websocket_wire"
                and isinstance(result, dict) and json.loads(returned["stdout"]) == result
                and isinstance(returned.get("stderr"), str)):
            raise ValueError("OpenClaw 公共 CLI 请求/响应与留存 stdout 不一致")
        return requested_ref, requested, returned_ref, returned, result

    try:
        if directory.is_symlink():
            raise ValueError("OpenClaw 候选证据目录不能是符号链接")
        host = _json(paths.run / "control-request.json")
        original = _json(directory / "result.json")
        entry = _json(directory / "entrypoint.json")
        _timestamp(host["requested_at"])  # 不将宿主机时钟与 VM 时间比较。
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        host, original, entry = {}, {}, {}
        errors.append(str(error))

    for idle_line, idle in controls:
        if idle.get("phase") != "native_idle_observed":
            continue
        try:
            request_ref, request = reference(idle.get("request_source"), "control-events.jsonl")
            api_ref, api = reference(idle.get("api_source"), "control-events.jsonl")
            api_request_ref, _ = reference(api.get("request_source"), "control-events.jsonl")
            admission_ref, admission = reference(request.get("source"), "raw-events.jsonl")
            api_admission_ref, _ = reference(api.get("source"), "raw-events.jsonl")
            end_ref, end = reference(idle.get("source"), "raw-events.jsonl")
            trigger = host.get("trigger", {})
            prefix = host.get("observed_through", {})
            if trigger.get("path") != prefix.get("path") or trigger.get("path") != "candidate/raw-events.jsonl":
                raise ValueError("OpenClaw 宿主机观测必须引用实际原始文件")
            host_ref, host_hook = reference({"path": "raw-events.jsonl", "line": trigger.get("line")}, "raw-events.jsonl")
            prefix_ref, _ = reference({"path": "raw-events.jsonl", "line": prefix.get("line")}, "raw-events.jsonl")
            prefix_bytes = b"".join((directory / "raw-events.jsonl").read_bytes().splitlines(keepends=True)[:prefix_ref["line"]])
            run_id, session_key = request.get("native_run_id"), entry.get("session_key")
            native_session = admission.get("context", {}).get("sessionId")
            request_id = request.get("request_id")
            before = rpc(request.get("native_before_source"), "agent.wait", {"runId": run_id, "timeoutMs": 0})
            transport_id = scope["variant"] + ":http:1"
            abort_params = {"method": "POST", "path": "/v1/chat/completions", "transport_id": transport_id, "runId": run_id}
            disconnect_ref, disconnect = reference(api.get("native_api_source"), "http-transport-events.jsonl")
            disconnect_request_ref, disconnect_request = reference(disconnect.get("request_source"), "http-transport-events.jsonl")
            run_ref, run_observed = reference(disconnect_request.get("run_source"), "http-transport-events.jsonl")
            run_payload_ref, run_payload = reference(run_observed.get("source"), "raw-events.jsonl")
            response_ref, response = reference(run_observed.get("response_source"), "http-transport-events.jsonl")
            http_request_ref, http_request = reference(response.get("request_source"), "http-transport-events.jsonl")
            stream_ref, stream = reference(idle.get("http_transport_source"), "http-transport-events.jsonl")
            stream_disconnect_ref, _ = reference(stream.get("disconnect_source"), "http-transport-events.jsonl")
            stream_run_ref, _ = reference(stream.get("run_source"), "http-transport-events.jsonl")
            stream_request_ref, _ = reference(stream.get("request_source"), "http-transport-events.jsonl")
            after = rpc(idle.get("native_after_source"), "agent.wait", {"runId": run_id, "timeoutMs": 15000})
            end_controls = [(line, row) for line, row in controls if row.get("phase") == "native_turn_end_observed"
                            and row.get("request_id") == request_id]
            if len(end_controls) != 1:
                raise ValueError("OpenClaw 必须具有一条可追溯来源的原生 agent_end 观测")
            end_line, end_control = end_controls[0]
            end_control_ref, _ = reference(end_control.get("source"), "raw-events.jsonl")
            end_request_ref, _ = reference(end_control.get("request_source"), "control-events.jsonl")
            end_api_ref, _ = reference(end_control.get("api_source"), "control-events.jsonl")
            assistant = next((message for message in reversed(end.get("event", {}).get("messages", []))
                              if message.get("role") == "assistant"), {})
            latest_input_line = max(line for line, row in raw_rows if row.get("type") == "llm_input"
                                    and owned_hook(row, run_id, session_key, native_session))
            end_candidates = [(line, row) for line, row in raw_rows if line > latest_input_line
                              and row.get("type") == "agent_end" and owned_hook(row, run_id, session_key, native_session)]

            def terminal_order(item):
                line, row = item
                times = [message.get("timestamp") for message in row.get("event", {}).get("messages", [])
                         if message.get("role") == "assistant"]
                return max([0] + [value for value in times if type(value) in (int, float) and math.isfinite(value)]), line

            final_end = max(end_candidates, key=terminal_order) if end_candidates else None
            rows = (request, api, end_control, idle)
            vm_order = [_timestamp(admission["time"]), _timestamp(before[1]["time"]), _timestamp(before[3]["time"]),
                        _timestamp(request["time"]), _timestamp(disconnect_request["time"]), _timestamp(disconnect["time"]),
                        _timestamp(api["time"]), _timestamp(after[1]["time"]), _timestamp(after[3]["time"]),
                        _timestamp(idle["time"])]
            before_result, abort_result, after_result = before[4], disconnect.get("result"), after[4]
            ended_at = after_result.get("endedAt")
            live_host = not any(host_ref["line"] < line <= prefix_ref["line"] and row.get("type") == "agent_end"
                                and owned_hook(row, run_id, session_key, native_session) for line, row in raw_rows)
            live_admission = not any(admission_ref["line"] < line < end_ref["line"] and row.get("type") == "agent_end"
                                    and owned_hook(row, run_id, session_key, native_session)
                                    and _timestamp(row["time"]) <= _timestamp(disconnect_request["time"]) for line, row in raw_rows)

            wire = directory / "http-response.sse"
            if wire.is_symlink() or not wire.is_file():
                raise ValueError("OpenClaw SSE 必须是普通文件")
            text = wire.read_text().replace("\r\n", "\n")
            frames = text.split("\n\n")
            payloads, done = [], False
            # 断连可能发生在 JSON 帧任意位置。保留绑定哈希的原始传输尾部，
            # 仅将完整解析帧与原始钩子比较。
            for frame in frames[:-1]:
                data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
                if not data:
                    continue
                if done:
                    raise ValueError("OpenClaw SSE 在 DONE 后仍有数据")
                if data == "[DONE]":
                    done = True
                else:
                    value = json.loads(data)
                    if not isinstance(value, dict):
                        raise ValueError("OpenClaw SSE 数据必须是对象")
                    payloads.append(value)
            wire_ids = {row["id"] for row in payloads if isinstance(row.get("id"), str)}
            wire_errors = [row["error"] for row in payloads if row.get("error")]
            raw_payloads = [row.get("event") for _, row in raw_rows if row.get("type") == "gateway_http_chunk"]
            chain = {
                "host_signal_and_live_prefix": (host.get("candidate") == "openclaw" and host.get("run_id") == scope["variant"]
                    and host.get("session_id") == scope["session_id"] and host.get("scope") == scope
                    and host.get("container_name") == "harness-lab-detailed-openclaw-" + scope["variant"]
                    and host.get("signal") == "SIGUSR1" and type(host.get("signal_exit_code")) is int
                    and host["signal_exit_code"] == 0 and host.get("native_run_id") == run_id
                    and host.get("session_key") == session_key and host.get("agent_id") == "lab-a"
                    and host_ref["line"] <= prefix_ref["line"] and host_ref["line"] <= admission_ref["line"]
                    and line_hash(host_ref) == host.get("trigger_line_sha256")
                    and line_hash(prefix_ref) == prefix.get("line_sha256")
                    and hashlib.sha256(prefix_bytes).hexdigest() == prefix.get("prefix_sha256")
                    and host_hook.get("type") == "llm_input" and live_host),
                "trusted_identity": (entry.get("run_id") == scope["variant"] and entry.get("session_id") == scope["session_id"]
                    and entry.get("scope") == scope and entry.get("agent_id") == "lab-a"
                    and isinstance(session_key, str) and bool(session_key) and isinstance(run_id, str) and bool(run_id)
                    and isinstance(native_session, str) and bool(native_session)
                    and all(same_scope(row.get("scope"), scope) and row.get("native_run_id") == run_id
                            and row.get("session_key") == session_key for row in rows)
                    and all(owned_hook(row, run_id, session_key, native_session) for row in (host_hook, admission, end))),
                "request_and_public_api": (request.get("phase") == "requested" and api.get("phase") == "native_api_returned"
                    and request.get("active_input_submitted") is True and request.get("api_executed") is False
                    and all(row.get("native_api") == "Gateway.HTTP.client_disconnect" and row.get("native_status_api") == "agent.wait"
                            and row.get("signal") == "SIGUSR1" and row.get("action") == "cancel" for row in rows)
                    and isinstance(request_id, str) and bool(request_id.strip())
                    and all(row.get("request_id") == request_id for row in rows)
                    and api.get("api_executed") is True and idle.get("api_executed") is True
                    and all(row.get("requested_at") == request.get("requested_at") for row in rows)
                    and _timestamp(request["requested_at"]) <= _timestamp(before[1]["time"])
                    and admission.get("type") == "llm_input" and live_admission
                    and request.get("native_params") == abort_params
                    and before_result == request.get("native_before_result")
                    and before_result.get("runId") == run_id and before_result.get("status") in {"timeout", "pending"}
                    and "endedAt" not in before_result and "stopReason" not in before_result
                    and all(row.get("native_status_before") == before_result["status"] for row in rows)
                    and api.get("native_status_after") is None
                    and api.get("native_api_result") == abort_result
                    and abort_result == {"transport_id": transport_id, "runId": run_id,
                                         "disconnect_requested": True, "signal_aborted": True}),
                "actual_http_disconnect_chain": (http_request.get("phase") == "requested"
                    and http_request.get("method") == "POST" and http_request.get("path") == "/v1/chat/completions"
                    and response.get("phase") == "response_received" and response.get("status") == 200
                    and isinstance(response.get("content_type"), str) and "text/event-stream" in response["content_type"]
                    and run_observed.get("phase") == "run_observed" and run_observed.get("run_id") == run_id
                    and run_payload.get("type") == "gateway_http_chunk" and run_payload.get("event", {}).get("id") == run_id
                    and disconnect_request.get("phase") == "disconnect_requested"
                    and disconnect_request.get("run_id") == disconnect.get("run_id") == stream.get("run_id") == run_id
                    and disconnect_request.get("params") == abort_params and disconnect_request.get("signal_was_aborted") is False
                    and disconnect.get("phase") == "disconnect_returned" and disconnect.get("terminal_confirmation") is False
                    and all(row.get("request_id") == request_id for row in (disconnect_request, disconnect, stream))
                    and all(same_scope(row.get("scope"), scope) and row.get("session_key") == session_key
                            and row.get("transport_id") == transport_id for row in (http_request, response, run_observed, disconnect_request, disconnect, stream))
                    and http_request_ref["line"] < response_ref["line"] < run_ref["line"] < disconnect_request_ref["line"] < disconnect_ref["line"] < stream_ref["line"]
                    and stream_disconnect_ref == disconnect_request_ref and stream_run_ref == run_ref and stream_request_ref == http_request_ref
                    and [_timestamp(row["time"]) for row in (http_request, response, run_observed, disconnect_request, disconnect, stream)]
                        == sorted(_timestamp(row["time"]) for row in (http_request, response, run_observed, disconnect_request, disconnect, stream))
                    and _timestamp(run_observed["time"]) <= _timestamp(request["time"])
                    and _timestamp(stream["time"]) <= _timestamp(idle["time"])
                    and all(row.get("method") == "agent.wait" for _, row in rpc_rows)),
                "raw_sources_and_vm_order": (request_ref["line"] < api_ref["line"] < end_line < idle_line
                    and admission_ref["line"] < end_ref["line"] and api_request_ref == request_ref
                    and api_admission_ref == admission_ref and end_control_ref == end_ref
                    and end_request_ref == request_ref and end_api_ref == api_ref
                    and before[2]["line"] < after[0]["line"]
                    and vm_order == sorted(vm_order)
                    and _timestamp(disconnect_request["time"]) <= _timestamp(end["time"]) <= _timestamp(end_control["time"]) <= _timestamp(idle["time"])
                    and run_payload_ref["line"] < end_ref["line"]),
                "actual_aborted_agent_end": (end.get("type") == "agent_end" and end.get("event", {}).get("success") is False
                    and assistant.get("stopReason") == "aborted"
                    and end_control.get("native_event") == "agent_end" and end_control.get("native_terminal") == end.get("event")
                    and end_control.get("native_assistant_stop_reason") == idle.get("native_assistant_stop_reason") == "aborted"),
                "final_native_terminal": final_end is not None and final_end[0] == end_ref["line"],
                "no_later_native_input": not any(line > end_ref["line"] and row.get("type") == "llm_input"
                    and owned_hook(row, run_id, session_key, native_session) for line, row in raw_rows),
                "public_terminal_without_idle_claim": (after_result == idle.get("native_after_result")
                    and after_result.get("runId") == run_id and after_result.get("status") == "error"
                    and after_result.get("stopReason") == "aborted"
                    and type(ended_at) in (int, float) and math.isfinite(ended_at)
                    and after_result.get("pendingError") is not True and after_result.get("yielded") is not True
                    and round(_timestamp(disconnect_request["time"]).timestamp() * 1000) <= ended_at <= round(_timestamp(after[3]["time"]).timestamp() * 1000)
                    and idle.get("native_status_after") == after_result["status"]
                    and idle.get("stage") == "public_agent_wait_terminal_and_http_client_disconnected"
                    and idle.get("native_state_kind") == "run_terminal_not_Agent_idle"
                    and "native_inbox_pending" in idle and idle["native_inbox_pending"] is None
                    and idle.get("cancel_confirmed") is True),
                "original_http_client_stream_settled": (idle.get("http_stream_settled") is True
                    and idle.get("http_stream_source") == {"path": "http-response.sse"}
                    and stream.get("phase") == "stream_settled" and stream.get("outcome") == "client_abort"
                    and stream.get("terminal_confirmation") is False and stream.get("done") is False and not done
                    and isinstance(stream.get("body_read_error"), dict) and stream["body_read_error"].get("name") == "AbortError"
                    and stream.get("wire_sha256") == hashlib.sha256(wire.read_bytes()).hexdigest()
                    and stream.get("gateway_sse_frames") == len(payloads)
                    and bool(payloads) and wire_ids == {run_id} and raw_payloads == payloads
                    and original.get("gateway_run_id") == run_id and original.get("gateway_sse_frames") == len(payloads)
                    and original.get("gateway_errors", []) == wire_errors
                    and original.get("gateway_transport_error") == stream.get("body_read_error")
                    and original.get("gateway_http_stream") == {"outcome": "client_abort", "done": False,
                        "source": {"path": "http-transport-events.jsonl", "line": stream_ref["line"]}}),
            }
            if not all(chain.values()):
                raise ValueError("OpenClaw 取消失败：" + ", ".join(key for key, passed in chain.items() if not passed))
            terminal = original.get("native_terminal", {})
            terminal_retained = (original.get("status") == native.get("status") == "failed"
                and original.get("stop_reason") == native.get("stop_reason") == "native_agent_error"
                and original.get("session_id") == scope["session_id"] and original.get("session_key") == session_key
                and not native.get("error_present") and native.get("normal_end") is False
                and native.get("native_terminal") == terminal and terminal.get("last_assistant_stop_reason") == "aborted"
                and terminal.get("success") == end["event"].get("success")
                and terminal.get("error") == (end["event"].get("error") or None)
                and type(terminal.get("observed_terminal_events")) is int
                and terminal["observed_terminal_events"] == len(end_candidates))
            confirmations.append({**idle, "source_checks": chain, "native_end_source": end_ref,
                                  "host_trigger_source": host_ref, "host_observed_through": prefix_ref,
                                  "admission_source": admission_ref, "api_ack_at": disconnect["time"],
                                  "api_ack_source": disconnect_ref, "disconnect_requested_at": disconnect_request["time"],
                                  "sse_sha256": hashlib.sha256(wire.read_bytes()).hexdigest()})
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            errors.append(str(error) if isinstance(error, ValueError) else "无效 OpenClaw 取消来源链")

    tool_rows, tool_errors = read_jsonl(paths.run / "tool-events.jsonl")
    errors.extend(tool_errors)
    audit_ok, pairs = audited_calls([row for _, row in tool_rows], scope)
    audit_ok = audit_ok or not tool_rows and ledger.get("request_count") == ledger.get("write_count") == 0
    after_confirm, after_api, after_disconnect, in_flight, disconnect_observations = [], [], [], [], []
    try:
        # 即使没有原生确认，也报告请求后的分派。
        # 这些传输观测不能用于判定取消通过。
        for line, returned in transport_rows:
            if returned.get("phase") != "disconnect_returned":
                continue
            _, requested = reference(returned.get("request_source"), "http-transport-events.jsonl")
            result = returned.get("result", {})
            if (requested.get("phase") == "disconnect_requested" and requested.get("signal_was_aborted") is False
                    and all(same_scope(row.get("scope"), scope) and row.get("session_key") == entry.get("session_key")
                            and row.get("run_id") == host.get("native_run_id")
                            and row.get("transport_id") == scope["variant"] + ":http:1" for row in (requested, returned))
                    and returned.get("request_id") == requested.get("request_id")
                    and result.get("disconnect_requested") is True and result.get("signal_aborted") is True
                    and _timestamp(requested["time"]) <= _timestamp(returned["time"])):
                disconnect_observations.append({"requested_at": requested["time"], "returned_at": returned["time"],
                    "source": {"path": "candidate/http-transport-events.jsonl", "line": line}, "terminal_confirmation": False})
        confirmed_at = min((_timestamp(row["time"]) for row in confirmations), default=None)
        api_at = min((_timestamp(row["returned_at"]) for row in disconnect_observations), default=None)
        disconnect_at = min((_timestamp(row["requested_at"]) for row in disconnect_observations), default=None)
        for _, row in tool_rows:
            at = _timestamp(row["time"])
            if row.get("phase") == "start" and row.get("tool") in {"write", "write_file", "sensitive_export"}:
                if confirmed_at is not None and at > confirmed_at:
                    after_confirm.append(row)
                if api_at is not None and at > api_at:
                    after_api.append(row)
                if disconnect_at is not None and at > disconnect_at:
                    after_disconnect.append(row)
        if confirmed_at is not None:
            in_flight = [{"start": start, "finish": finish} for start, finish in pairs
                         if _timestamp(start["time"]) <= confirmed_at < _timestamp(finish["time"])]
        requests_audited = all(any(start.get("tool") == row.get("action")
            and start.get("arguments", {}).get("operation_id") == row.get("operation_id") for start, _ in pairs)
            for row in ledger.get("requests", []))
        writes_audited = all(any(start.get("tool") == "write"
            and start.get("arguments", {}).get("operation_id") == row.get("operation_id") for start, _ in pairs)
            for row in ledger.get("writes", []))
        audit_ok = audit_ok and requests_audited and writes_audited
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        errors.append("OpenClaw 取消或独立审计时间戳无效")
        audit_ok = False
    save(paths.run / "cancel-outcome.json", {"candidate": "openclaw", "native_result_unchanged": True,
         "confirmations": confirmations, "errors": errors, "new_write_calls_after_confirmation": after_confirm,
         "new_write_calls_after_api_ack_observation": after_api, "in_flight_calls_at_confirmation": in_flight,
         "disconnect_observations": disconnect_observations, "new_write_calls_after_disconnect_request": after_disconnect,
         "confirmation_interval_evaluated": bool(confirmations),
         "in_flight_effects": "已分派副作用保留在完整独立 ledger.json 中；API 接收不等于终态确认"})
    return verdict([
        ("native_running_cancel_confirmed", len(confirmations) == 1 and not errors,
         "可信运行中原始前缀、匹配的 HTTP 断连、真实 aborted agent_end、公共终态等待及客户端读取结束均相符"),
        ("native_cancel_terminal_retained", terminal_retained,
         "真实 failed/native_agent_error 和 assistant aborted 连同可追溯来源的原生终态一并保留"),
        ("no_new_writes_after_cancel_ack", len(confirmations) == 1 and not after_confirm and audit_ok,
         "独立检查强终态及 SSE 确认后的 write/write_file/sensitive_export 启动；API 接收确认区间单独报告"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "完整独立账本保留在途副作用")],
        actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))
