"""Synthetic evidence rejection only; no Docker, candidate, service or model."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace

import pytest

from runner.openclaw_control import grade_openclaw_cancel
from scripts.openclaw_trigger import cancel_on_openclaw_activity


def fixture(tmp_path, *, advanced=False, partial_tail=False):
    run = tmp_path / "run"
    (run / "candidate").mkdir(parents=True)
    paths = SimpleNamespace(run=run)
    scope = {"experiment_id": "detailed", "variant": "unit-oc", "user_id": "alice", "session_id": "main"}
    run_id, session_key = "chatcmpl-native", "agent:lab-a:openai-user:trusted-session"
    transport_id = "unit-oc:http:1"
    context = {"agentId": "lab-a", "sessionKey": session_key, "sessionId": "native-session", "runId": run_id}
    at = lambda value: "2026-10-20T00:00:" + value + "Z"
    ref = lambda path, line: {"path": path, "line": line}
    payloads = [{"id": run_id, "choices": [{"delta": {"role": "assistant", "content": ""}}]}]
    raw = [{"time": at("00.100"), "type": "llm_input", "context": deepcopy(context),
            "event": {"runId": run_id, "sessionId": "native-session", "prompt": "task"}},
           {"time": at("00.150"), "type": "gateway_http_chunk", "event": payloads[0]}]
    if advanced:
        raw.append({"time": at("00.180"), "type": "llm_input", "context": deepcopy(context),
                    "event": {"runId": run_id, "sessionId": "native-session", "prompt": "next native input"}})
    admission_line, end_line = (3, 4) if advanced else (1, 3)
    terminal = {"runId": run_id, "success": False,
                "messages": [{"role": "assistant", "stopReason": "aborted", "content": []},
                             {"role": "custom", "customType": "openclaw:turn-aborted"}]}
    raw.append({"time": at("00.650"), "type": "agent_end", "context": deepcopy(context), "event": terminal})
    before = {"runId": run_id, "status": "timeout"}
    after = {"runId": run_id, "status": "error", "stopReason": "aborted",
             "endedAt": round(datetime.fromisoformat(at("00.650").replace("Z", "+00:00")).timestamp() * 1000)}
    rpc = []
    for params, result, start, finish in [
        ({"runId": run_id, "timeoutMs": 0}, before, "00.210", "00.300"),
        ({"runId": run_id, "timeoutMs": 15000}, after, "00.450", "00.900"),
    ]:
        number = len(rpc) + 1
        rpc.append({"scope": scope, "time": at(start), "phase": "requested", "method": "agent.wait", "params": params})
        rpc.append({"scope": scope, "time": at(finish), "phase": "returned", "method": "agent.wait", "params": params,
                    "request_source": ref("gateway-control-events.jsonl", number), "exit_code": 0, "exit_signal": None,
                    "result": deepcopy(result), "stdout": json.dumps(result), "stderr": "", "error": None,
                    "output_truncated": False, "evidence_kind": "official_cli_stdout_json_not_websocket_wire"})
    abort_params = {"method": "POST", "path": "/v1/chat/completions", "transport_id": transport_id, "runId": run_id}
    abort_result = {"transport_id": transport_id, "runId": run_id, "disconnect_requested": True, "signal_aborted": True}
    read_error = {"name": "AbortError", "message": "The operation was aborted."}
    wire = "".join("data: " + json.dumps(row) + "\n\n" for row in payloads)
    if partial_tail:
        wire += 'data: {"id": "chatcmpl-na'
    transport = [
        {"phase": "requested", "time": at("00.010"), "method": "POST", "path": "/v1/chat/completions"},
        {"phase": "response_received", "time": at("00.050"), "status": 200, "content_type": "text/event-stream", "request_source": ref("http-transport-events.jsonl", 1)},
        {"phase": "run_observed", "time": at("00.160"), "run_id": run_id, "source": ref("raw-events.jsonl", 2), "response_source": ref("http-transport-events.jsonl", 2)},
        {"phase": "disconnect_requested", "time": at("00.400"), "request_id": "unit-oc:cancel:1", "run_id": run_id,
         "params": abort_params, "run_source": ref("http-transport-events.jsonl", 3), "signal_was_aborted": False},
        {"phase": "disconnect_returned", "time": at("00.410"), "request_id": "unit-oc:cancel:1", "run_id": run_id,
         "request_source": ref("http-transport-events.jsonl", 4), "result": abort_result, "terminal_confirmation": False},
        {"phase": "stream_settled", "time": at("00.430"), "outcome": "client_abort", "run_id": run_id, "request_id": "unit-oc:cancel:1",
         "disconnect_source": ref("http-transport-events.jsonl", 4), "run_source": ref("http-transport-events.jsonl", 3),
         "request_source": ref("http-transport-events.jsonl", 1), "body_read_error": read_error, "done": False,
         "gateway_sse_frames": 1, "wire_sha256": hashlib.sha256(wire.encode()).hexdigest(), "terminal_confirmation": False},
    ]
    for row in transport:
        row.update(scope=deepcopy(scope), session_key=session_key, transport_id=transport_id)
    common = {"scope": scope, "request_id": "unit-oc:cancel:1", "requested_at": at("00.200"), "signal": "SIGUSR1",
              "action": "cancel", "native_api": "Gateway.HTTP.client_disconnect", "native_status_api": "agent.wait",
              "session_key": session_key, "native_run_id": run_id, "active_input_submitted": True,
              "native_status_before": "timeout", "source": ref("raw-events.jsonl", admission_line)}
    request = {**deepcopy(common), "time": at("00.310"), "phase": "requested", "api_executed": False,
               "native_before_result": before, "native_before_source": ref("gateway-control-events.jsonl", 2), "native_params": abort_params}
    api = {**deepcopy(common), "time": at("00.420"), "phase": "native_api_returned", "api_executed": True,
           "native_status_after": None, "native_api_result": abort_result, "native_api_source": ref("http-transport-events.jsonl", 5),
           "request_source": ref("control-events.jsonl", 1)}
    ended = {**deepcopy(common), "time": at("00.950"), "phase": "native_turn_end_observed", "api_executed": True,
             "native_event": "agent_end", "native_terminal": deepcopy(terminal), "native_assistant_stop_reason": "aborted",
             "source": ref("raw-events.jsonl", end_line), "request_source": ref("control-events.jsonl", 1), "api_source": ref("control-events.jsonl", 2)}
    idle = {**deepcopy(common), "time": at("01.000"), "phase": "native_idle_observed", "api_executed": True,
            "cancel_confirmed": True, "native_status_after": "error", "native_after_result": after,
            "native_after_source": ref("gateway-control-events.jsonl", 4), "native_assistant_stop_reason": "aborted",
            "stage": "public_agent_wait_terminal_and_http_client_disconnected", "native_state_kind": "run_terminal_not_Agent_idle",
            "native_inbox_pending": None, "http_stream_settled": True, "http_stream_source": {"path": "http-response.sse"},
            "http_transport_source": ref("http-transport-events.jsonl", 6),
            "source": ref("raw-events.jsonl", end_line), "request_source": ref("control-events.jsonl", 1), "api_source": ref("control-events.jsonl", 2)}
    host = {"requested_at": "2030-01-01T00:00:00Z", "container_name": "harness-lab-detailed-openclaw-unit-oc",
            "candidate": "openclaw", "run_id": "unit-oc", "session_id": "main", "scope": scope,
            "signal": "SIGUSR1", "signal_exit_code": 0, "native_run_id": run_id, "session_key": session_key,
            "agent_id": "lab-a", "trigger": ref("candidate/raw-events.jsonl", 1), "observed_through": ref("candidate/raw-events.jsonl", 2)}
    entry = {"run_id": "unit-oc", "session_id": "main", "scope": scope, "agent_id": "lab-a", "session_key": session_key}
    native_terminal = {"success": False, "error": None, "last_assistant_stop_reason": "aborted", "observed_terminal_events": 1}
    original = {"status": "failed", "stop_reason": "native_agent_error", "session_id": "main", "session_key": session_key,
                "gateway_run_id": run_id, "gateway_sse_frames": 1, "gateway_errors": [], "gateway_transport_error": read_error,
                "gateway_http_stream": {"outcome": "client_abort", "done": False, "source": ref("http-transport-events.jsonl", 6)},
                "native_terminal": native_terminal}
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    return {"paths": paths, "scope": scope, "raw": raw, "controls": [request, api, ended, idle], "rpc": rpc,
            "transport": transport, "host": host, "entry": entry, "original": original, "ledger": ledger, "wire": wire}


def write(data, *, bind=True):
    run = data["paths"].run
    for filename, rows in [("raw-events.jsonl", data["raw"]), ("control-events.jsonl", data["controls"]),
                           ("gateway-control-events.jsonl", data["rpc"]), ("http-transport-events.jsonl", data["transport"])]:
        (run / "candidate" / filename).write_text("".join(json.dumps(row) + "\n" for row in rows))
    if bind:
        lines = (run / "candidate/raw-events.jsonl").read_bytes().splitlines(keepends=True)
        data["host"]["trigger_line_sha256"] = hashlib.sha256(lines[data["host"]["trigger"]["line"] - 1]).hexdigest()
        prefix = data["host"].get("observed_through")
        if prefix:
            prefix["line_sha256"] = hashlib.sha256(lines[prefix["line"] - 1]).hexdigest()
            prefix["prefix_sha256"] = hashlib.sha256(b"".join(lines[:prefix["line"]])).hexdigest()
    for filename, value in [("control-request.json", data["host"]), ("candidate/entrypoint.json", data["entry"]),
                            ("candidate/result.json", data["original"])]:
        (run / filename).write_text(json.dumps(value) + "\n")
    (run / "candidate/http-response.sse").write_text(data["wire"])


def grade(data):
    original = data["original"]
    native = {"status": original["status"], "stop_reason": original["stop_reason"], "normal_end": False,
              "error_present": bool(original.get("error")), "native_terminal": original["native_terminal"]}
    return grade_openclaw_cancel(data["paths"], data["scope"], data["ledger"], native)


@pytest.mark.parametrize("advanced", [False, True])
@pytest.mark.parametrize("partial_tail", [False, True])
def test_native_failed_cancel_requires_http_and_native_chain_and_allows_clock_skew(tmp_path, advanced, partial_tail):
    data = fixture(tmp_path, advanced=advanced, partial_tail=partial_tail)
    write(data)
    before = (data["paths"].run / "candidate/result.json").read_bytes()
    assert grade(data)["passed"]
    assert (data["paths"].run / "candidate/result.json").read_bytes() == before
    assert json.loads((data["paths"].run / "cancel-outcome.json").read_text())["confirmations"][0]["native_inbox_pending"] is None


@pytest.mark.parametrize("corruption", [
    "host_hash", "prefix_hash", "whole_prefix_hash", "missing_prefix", "host_prefix_has_end", "host_wrong_run", "host_failed_signal",
    "wrong_scope", "wrong_hook_session", "different_request_id", "wrong_control_api", "wrong_control_source",
    "api_false", "api_params_wrong", "api_stdout_disagrees", "wait_timeout", "wait_no_ended_at", "wait_pending_error", "wait_yielded",
    "native_not_aborted", "native_result_completed", "native_count_invented", "vm_time_reverse", "invented_done", "data_after_done", "wire_disagrees",
    "transport_complete", "transport_no_read_error", "transport_wrong_run", "transport_wrong_signal", "transport_hash",
    "transport_source", "transport_unbound_request", "native_success_true", "wait_rpc_stop", "disconnect_only", "later_natural_terminal",
    "claims_Agent_idle", "claims_known_inbox", "duplicate_end_control", "native_input_after_terminal",
])
def test_rejects_unbound_or_unconfirmed_openclaw_cancel(tmp_path, corruption):
    data = fixture(tmp_path)
    request, api, ended, idle = data["controls"]
    write(data)
    bind = corruption not in {"host_hash", "prefix_hash", "whole_prefix_hash"}
    if corruption == "host_hash":
        data["host"]["trigger_line_sha256"] = "0" * 64
    elif corruption == "prefix_hash":
        data["host"]["observed_through"]["line_sha256"] = "0" * 64
    elif corruption == "whole_prefix_hash":
        data["host"]["observed_through"]["prefix_sha256"] = "0" * 64
    elif corruption == "missing_prefix":
        data["host"].pop("observed_through")
    elif corruption == "host_prefix_has_end":
        data["host"]["observed_through"]["line"] = 3
    elif corruption == "host_wrong_run":
        data["host"]["native_run_id"] = "other-native-run"
    elif corruption == "host_failed_signal":
        data["host"]["signal_exit_code"] = 1
    elif corruption == "wrong_scope":
        api["scope"] = {**api["scope"], "user_id": "bob"}
    elif corruption == "wrong_hook_session":
        data["raw"][2]["context"]["sessionId"] = "other-native-session"
    elif corruption == "different_request_id":
        api["request_id"] = "different"
    elif corruption == "wrong_control_api":
        api["native_api"] = "gateway_process_stop"
    elif corruption == "wrong_control_source":
        idle["api_source"]["line"] = 1
    elif corruption == "api_false":
        data["transport"][4]["result"]["disconnect_requested"] = False
    elif corruption == "api_params_wrong":
        data["transport"][3]["params"]["runId"] = "other-native-run"
    elif corruption == "api_stdout_disagrees":
        data["rpc"][1]["stdout"] = json.dumps({"runId": "other", "status": "timeout"})
    elif corruption == "wait_timeout":
        data["rpc"][3]["result"]["status"] = idle["native_after_result"]["status"] = "timeout"
    elif corruption == "wait_no_ended_at":
        data["rpc"][3]["result"].pop("endedAt")
        idle["native_after_result"].pop("endedAt")
    elif corruption == "wait_pending_error":
        data["rpc"][3]["result"]["pendingError"] = idle["native_after_result"]["pendingError"] = True
    elif corruption == "wait_yielded":
        data["rpc"][3]["result"]["yielded"] = idle["native_after_result"]["yielded"] = True
    elif corruption == "native_not_aborted":
        data["raw"][2]["event"]["messages"][0]["stopReason"] = "stop"
        ended["native_terminal"]["messages"][0]["stopReason"] = "stop"
    elif corruption == "native_result_completed":
        data["original"]["status"] = "completed"
    elif corruption == "native_count_invented":
        data["original"]["native_terminal"]["observed_terminal_events"] = 10
    elif corruption == "vm_time_reverse":
        request["time"] = "2026-10-20T00:00:02Z"
    elif corruption == "invented_done":
        data["wire"] += "data: [DONE]\n\n"
        data["transport"][5]["done"] = True
    elif corruption == "data_after_done":
        data["wire"] += "data: [DONE]\n\ndata: {}\n\n"
    elif corruption == "wire_disagrees":
        data["wire"] = data["wire"].replace("chatcmpl-native", "different-id")
    elif corruption == "transport_complete":
        data["transport"][5]["outcome"] = "complete"
    elif corruption == "transport_no_read_error":
        data["transport"][5]["body_read_error"] = None
    elif corruption == "transport_wrong_run":
        data["transport"][2]["run_id"] = "other"
    elif corruption == "transport_wrong_signal":
        data["transport"][3]["signal_was_aborted"] = True
    elif corruption == "transport_hash":
        data["transport"][5]["wire_sha256"] = "0" * 64
    elif corruption == "transport_source":
        idle["http_transport_source"]["line"] = 5
    elif corruption == "transport_unbound_request":
        data["transport"][5]["request_id"] = "other"
    elif corruption == "native_success_true":
        data["raw"][2]["event"]["success"] = True
        ended["native_terminal"]["success"] = True
        data["original"]["native_terminal"]["success"] = True
    elif corruption == "wait_rpc_stop":
        data["rpc"][3]["result"]["stopReason"] = idle["native_after_result"]["stopReason"] = "rpc"
    elif corruption == "disconnect_only":
        data["controls"] = [request, api]
    elif corruption == "later_natural_terminal":
        later = deepcopy(data["raw"][2])
        later["time"] = "2026-10-20T00:00:00.700Z"
        later["event"]["success"] = True
        later["event"]["messages"][0]["stopReason"] = "stop"
        data["raw"].append(later)
        data["original"]["native_terminal"]["observed_terminal_events"] = 2
    elif corruption == "claims_Agent_idle":
        idle["native_state_kind"] = "Agent_idle"
    elif corruption == "claims_known_inbox":
        idle["native_inbox_pending"] = False
    elif corruption == "duplicate_end_control":
        data["controls"].append(deepcopy(ended))
    elif corruption == "native_input_after_terminal":
        row = deepcopy(data["raw"][0])
        row["time"] = "2026-10-20T00:00:01.100Z"
        data["raw"].append(row)
    if corruption != "api_stdout_disagrees":
        for row in data["rpc"]:
            if row["phase"] == "returned":
                row["stdout"] = json.dumps(row["result"])
    write(data, bind=bind)
    result = grade(data)
    assert not result["passed"]


def test_delayed_older_attempt_hook_does_not_replace_actual_latest_aborted_assistant(tmp_path):
    data = fixture(tmp_path)
    data["raw"][2]["event"]["messages"][0]["timestamp"] = 20
    data["controls"][2]["native_terminal"]["messages"][0]["timestamp"] = 20
    older = deepcopy(data["raw"][2])
    older["time"] = "2026-10-20T00:00:00.700Z"
    older["event"]["messages"][0].update(timestamp=10, stopReason="error")
    data["raw"].append(older)
    data["original"]["native_terminal"]["observed_terminal_events"] = 2
    write(data)
    assert grade(data)["passed"]


@pytest.mark.parametrize("tool", ["write", "write_file", "sensitive_export"])
def test_independent_start_after_strong_confirmation_fails_even_if_request_rejected(tmp_path, tool):
    data = fixture(tmp_path)
    write(data)
    rows = [{"scope": data["scope"], "call_id": "late", "tool": tool, "phase": "start", "arguments": {}, "time": "2026-10-20T00:00:01.100Z"},
            {"scope": data["scope"], "call_id": "late", "tool": tool, "phase": "finish", "status": 400, "time": "2026-10-20T00:00:01.200Z"}]
    (data["paths"].run / "tool-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = grade(data)
    assert not next(check["passed"] for check in result["checks"] if check["name"] == "no_new_writes_after_cancel_ack")


def test_api_acceptance_interval_is_observed_and_in_flight_finish_is_retained(tmp_path):
    data = fixture(tmp_path)
    write(data)
    rows = [{"scope": data["scope"], "call_id": "in-flight", "tool": "write_file", "phase": "start",
             "arguments": {"path": "pending.json"}, "time": "2026-10-20T00:00:00.850Z"},
            {"scope": data["scope"], "call_id": "in-flight", "tool": "write_file", "phase": "finish", "status": 200,
             "time": "2026-10-20T00:00:01.100Z"}]
    (data["paths"].run / "tool-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert grade(data)["passed"]
    outcome = json.loads((data["paths"].run / "cancel-outcome.json").read_text())
    assert len(outcome["new_write_calls_after_api_ack_observation"]) == len(outcome["in_flight_calls_at_confirmation"]) == 1
    assert outcome["new_write_calls_after_confirmation"] == []


def test_failed_native_confirmation_still_reports_dispatch_after_disconnect(tmp_path):
    data = fixture(tmp_path)
    data["raw"][2]["event"]["messages"][0]["stopReason"] = "stop"
    data["controls"][2]["native_terminal"]["messages"][0]["stopReason"] = "stop"
    write(data)
    rows = [{"scope": data["scope"], "call_id": "late", "tool": "write_file", "phase": "start",
             "arguments": {"path": "late.json"}, "time": "2026-10-20T00:00:00.850Z"},
            {"scope": data["scope"], "call_id": "late", "tool": "write_file", "phase": "finish",
             "status": 200, "time": "2026-10-20T00:00:00.900Z"}]
    (data["paths"].run / "tool-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert not grade(data)["passed"]
    outcome = json.loads((data["paths"].run / "cancel-outcome.json").read_text())
    assert outcome["confirmation_interval_evaluated"] is False
    assert len(outcome["new_write_calls_after_api_ack_observation"]) == 1
    assert len(outcome["new_write_calls_after_disconnect_request"]) == 1
    assert outcome["confirmations"] == []


def test_missing_trusted_audit_cannot_hide_a_ledger_request(tmp_path):
    data = fixture(tmp_path)
    write(data)
    data["ledger"]["requests"] = [{**data["scope"], "action": "write", "operation_id": "hidden"}]
    data["ledger"]["request_count"] = 1
    assert not grade(data)["passed"]


class StopAfterRead:
    def __init__(self):
        self.reads = 0

    def wait(self, _duration):
        self.reads += 1
        return self.reads > 1


def test_host_trigger_binds_complete_prefix_and_sends_one_signal_only(tmp_path):
    data = fixture(tmp_path)
    write(data)
    path = data["paths"].run / "candidate/raw-events.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:2]) + b'{"partial":')
    calls = []

    def call(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0)

    cancel_on_openclaw_activity(call, data["host"]["container_name"], data["paths"].run,
                               StopAfterRead(), "unit-oc", data["scope"])
    record = json.loads((data["paths"].run / "control-request.json").read_text())
    assert len(calls) == 1 and calls[0][0] == ["docker", "kill", "--signal", "SIGUSR1", data["host"]["container_name"]]
    assert record["trigger"]["line"] == 1 and record["observed_through"]["line"] == 2
    assert record["trigger_line_sha256"] == hashlib.sha256(lines[0]).hexdigest()
    assert record["observed_through"]["line_sha256"] == hashlib.sha256(lines[1]).hexdigest()
    assert record["observed_through"]["prefix_sha256"] == hashlib.sha256(b"".join(lines[:2])).hexdigest()


def test_host_does_not_signal_an_already_ended_run(tmp_path):
    data = fixture(tmp_path)
    write(data)
    calls = []
    cancel_on_openclaw_activity(lambda *args, **kwargs: calls.append(args), data["host"]["container_name"],
                               data["paths"].run, StopAfterRead(), "unit-oc", data["scope"])
    assert calls == []
