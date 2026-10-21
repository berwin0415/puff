"""Negative calibration of detailed orchestration and evidence interpretation.

Only synthetic data and a Docker transport double are used here. These tests
do not launch Docker, a candidate, a browser, a service or a model request, and
their passing results are not candidate capability evidence.
"""
from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import threading

import pytest

from runner import detailed
from runner.config import load_config
from scripts.run_detailed import CANDIDATES, DetailedLab, worker_resource_pins


def setup_case(tmp_path, monkeypatch, case="sensitive", candidate="deepseek_harness"):
    args = Namespace(candidate=candidate, case=case, seed=101, run_id="unit-detail",
                     experiment_id="detailed", date="2026-10-20")
    config = replace(load_config(), host_data_root=tmp_path / "data")
    paths = config.paths_for(args.experiment_id, args.run_id, candidate, "alice", "main",
                             args.run_id, case=case, date=args.date)
    scope = {"experiment_id": args.experiment_id, "variant": args.run_id,
             "user_id": "alice", "session_id": "main"}
    paths.fixture.mkdir(parents=True)
    monkeypatch.setattr(detailed, "context", lambda _args: (config, paths, scope))
    monkeypatch.setattr(detailed, "environment", lambda: {"unit_test_only": True})
    monkeypatch.setattr(detailed, "ledger_request", lambda *_args, **_kwargs: {
        "scopes": [{**scope, "token": "synthetic-fixture-token"}]})
    detailed.prepare(args)
    return args, config, paths, scope


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def save_process_evidence(args, paths, exit_code=0):
    service, prefix, _, _ = CANDIDATES[args.candidate]
    pins = {"container_name": f"harness-lab-detailed-{prefix}-{args.run_id}",
            "container_id": "a" * 64, "image_id": "sha256:" + "b" * 64,
            "image_tag": f"harness-lab-{service}:local", "memory_limit": 2 ** 31, "cpu_nano": 10 ** 9}
    identity = {"run_id": args.run_id, "candidate": args.candidate, "case": args.case,
                "candidate_exit_code": exit_code}
    worker = {"id": pins["container_id"], "name": "/" + pins["container_name"],
              "image_id": pins["image_id"], "image_tag": pins["image_tag"],
              "memory_limit": pins["memory_limit"], "cpu_nano": pins["cpu_nano"],
              "state": {"Status": "exited", "Pid": 0, "ExitCode": exit_code, "Error": "",
                        "Running": False, "Paused": False, "Restarting": False, "Dead": False, "OOMKilled": False}}
    execution = {**identity, "worker_expectation": pins, "worker_snapshot_matches": 1}
    observation = {**identity, "containers": [worker]}
    (paths.run / "orchestration-status.json").write_text(json.dumps(execution))
    (paths.run / "worker-observation.json").write_text(json.dumps(observation))
    return execution, observation


@pytest.mark.parametrize("memory,cpus", [(2 ** 31, 0.5), (str(2 ** 31), "0.5"), ("2g", "0.50"), ("2048MB", 0.5)])
def test_resolved_worker_pins_retain_only_exact_image_and_positive_resource_limits(memory, cpus):
    assert worker_resource_pins({"image": "candidate:local", "mem_limit": memory, "cpus": cpus,
                                 "environment": {"SYNTHETIC_SECRET": "must-never-be-serialized"}}) == {
        "image_tag": "candidate:local", "memory_limit": 2 ** 31, "cpu_nano": 500_000_000}


@pytest.mark.parametrize("field,value", [("mem_limit", 0), ("mem_limit", True), ("mem_limit", None),
    ("mem_limit", "unlimited"), ("cpus", 0), ("cpus", True), ("cpus", "NaN"), ("cpus", "Infinity"),
    ("cpus", "0.0000000001"), ("image", ""), ("image", "wrong image")])
def test_missing_or_malformed_worker_pins_are_rejected_before_candidate_launch(field, value):
    spec = {"image": "candidate:local", "mem_limit": 2 ** 31, "cpus": 1, field: value}
    with pytest.raises(RuntimeError):
        worker_resource_pins(spec)


@pytest.mark.parametrize("corruption", ["oom", "missing_observation", "missing_host", "missing_state", "wrong_image",
    "missing_pin", "null_resource_pin", "ambiguous_worker", "ambiguous_host", "wrong_host_identity", "boolean_exit"])
def test_process_validation_requires_complete_matching_protected_host_evidence(tmp_path, monkeypatch, corruption):
    args, _, paths, _ = setup_case(tmp_path, monkeypatch)
    execution, observation = save_process_evidence(args, paths)
    assert detailed.process_validation(args, paths)["passed"]
    original = {"status": "completed", "stop_reason": "stop", "OOMKilled": False}
    (paths.run / "candidate/result.json").write_text(json.dumps(original))
    if corruption == "oom":
        observation["containers"][0]["state"]["OOMKilled"] = True
    elif corruption == "missing_state":
        del observation["containers"][0]["state"]["OOMKilled"]
    elif corruption == "wrong_image":
        observation["containers"][0]["image_id"] = "sha256:" + "c" * 64
    elif corruption == "missing_pin":
        del execution["worker_expectation"]["image_id"]
    elif corruption == "null_resource_pin":
        execution["worker_expectation"]["memory_limit"] = None
    elif corruption == "ambiguous_worker":
        observation["containers"].append(deepcopy(observation["containers"][0]))
    elif corruption == "ambiguous_host":
        execution["worker_snapshot_matches"] = 2
    elif corruption == "wrong_host_identity":
        execution["run_id"] = "other-run"
    elif corruption == "boolean_exit":
        observation["candidate_exit_code"] = False
    (paths.run / "orchestration-status.json").write_text(json.dumps(execution))
    (paths.run / "worker-observation.json").write_text(json.dumps(observation))
    if corruption == "missing_observation":
        (paths.run / "worker-observation.json").unlink()
    elif corruption == "missing_host":
        (paths.run / "orchestration-status.json").unlink()
    assert not detailed.process_validation(args, paths)["passed"]
    assert json.loads((paths.run / "process-validation.json").read_text())["passed"] is False
    assert json.loads((paths.run / "candidate/result.json").read_text()) == original


def test_extension_uses_same_base_task_with_explicit_state_query_and_fixed_rule(tmp_path, monkeypatch):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "extension")
    request = json.loads((paths.fixture / "candidate-request.json").read_text())
    assert request["candidate_options"] == {"case": "extension", "trusted_scope_file": "/config/scope.json",
                                            "business_extension": {"quantity_limit": 3}}
    assert "query_order_status" in request["prompt"]
    assert json.loads((paths.workspace / "request.json").read_text())["quantity_each"] == 2
    assert json.loads((paths.run / "input.json").read_text())["expected_answer_delivered"] is False


def cancel_evidence(scope):
    """Synthetic values with the actual scoped DSH control event fields."""
    reason = {"kind": "aborted", "reason": {"kind": "hook", "reason": "experiment_cancel"}}
    raw = [{"params": {"sessionId": "main", "event": {"type": "turn/start", "data": {"turn": 1}}}},
           {"params": {"sessionId": "main", "event": {"type": "turn/end", "data": {"turn": 1, "reason": reason}}}}]
    common = {"scope": scope, "request_id": "unit-detail:cancel:1", "turn": 1,
              "native_api": "Agent.cancel", "signal": "SIGUSR1", "action": "cancel"}
    request = {**common, "phase": "requested", "native_status_before": "running",
               "active_input_submitted": True, "api_executed": False,
               "source": {"path": "raw-events.jsonl", "line": 1}}
    api = {**common, "phase": "native_api_returned", "api_executed": True,
           "source": {"path": "raw-events.jsonl", "line": 1},
           "request_source": {"path": "control-events.jsonl", "line": 1}}
    idle = {**common, "phase": "native_idle_observed", "cancel_confirmed": True,
            "native_status_after": "idle", "native_inbox_pending": False, "turn": 1,
            "native_turn_end_reason": reason, "source": {"path": "raw-events.jsonl", "line": 2},
            "request_source": {"path": "control-events.jsonl", "line": 1},
            "api_source": {"path": "control-events.jsonl", "line": 2}, "time": "2026-10-20T00:00:01Z"}
    host = {"signal": "SIGUSR1", "signal_exit_code": 0,
            "trigger": {"path": "candidate/raw-events.jsonl", "line": 1}}
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    native = {"status": "incomplete", "native_status": "incomplete", "stop_reason": "aborted"}
    return raw, [request, api, idle], host, ledger, native


def save_cancel_evidence(paths, raw, controls, host):
    write_jsonl(paths.run / "candidate/raw-events.jsonl", raw)
    write_jsonl(paths.run / "candidate/control-events.jsonl", controls)
    (paths.run / "control-request.json").write_text(json.dumps(host))


def test_cancel_requires_requested_native_abort_and_stops_new_write_dispatch(tmp_path, monkeypatch):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "cancel")
    raw, controls, host, ledger, native = cancel_evidence(scope)
    reason = raw[1]["params"]["event"]["data"]["reason"]
    save_cancel_evidence(paths, raw, controls, host)
    assert detailed.grade_cancel(paths, scope, ledger, native)["passed"]
    raw[1]["params"]["event"]["data"]["reason"] = {"kind": "completed"}
    write_jsonl(paths.run / "candidate/raw-events.jsonl", raw)
    assert not detailed.grade_cancel(paths, scope, ledger, native)["passed"]
    raw[1]["params"]["event"]["data"]["reason"] = reason
    write_jsonl(paths.run / "candidate/raw-events.jsonl", raw)
    write_jsonl(paths.run / "tool-events.jsonl", [
        {"scope": scope, "call_id": "late", "phase": "start", "tool": "write", "arguments": {}, "time": "2026-10-20T00:00:02Z"},
        {"scope": scope, "call_id": "late", "phase": "finish", "tool": "write", "status": 400, "time": "2026-10-20T00:00:02Z"}])
    assert not detailed.grade_cancel(paths, scope, ledger, native)["passed"]


@pytest.mark.parametrize("corruption", [
    "missing_request_ids", "empty_request_ids", "blank_request_ids", "different_request_id",
    "foreign_start_session", "foreign_end_session", "different_request_turn", "different_api_turn",
    "boolean_turn", "missing_turns", "different_native_api", "wrong_action", "wrong_signal",
    "api_references_itself_as_request", "api_references_different_start", "end_before_start",
    "api_before_request", "api_after_idle", "different_host_trigger", "different_control_file",
    "different_raw_file",
])
def test_cancel_rejects_broken_native_source_chain(tmp_path, monkeypatch, corruption):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "cancel")
    raw, controls, host, ledger, native = cancel_evidence(scope)
    request, api, idle = controls
    if corruption in {"missing_request_ids", "empty_request_ids", "blank_request_ids"}:
        for row in controls:
            if corruption == "missing_request_ids":
                row.pop("request_id")
            else:
                row["request_id"] = "" if corruption == "empty_request_ids" else "   "
    elif corruption == "different_request_id":
        api["request_id"] = "other-request"
    elif corruption in {"foreign_start_session", "foreign_end_session"}:
        raw[0 if corruption == "foreign_start_session" else 1]["params"]["sessionId"] = "other-session"
    elif corruption in {"different_request_turn", "different_api_turn"}:
        (request if corruption == "different_request_turn" else api)["turn"] = 2
    elif corruption == "boolean_turn":
        api["turn"] = True
    elif corruption == "missing_turns":
        for row in controls:
            row.pop("turn")
        for row in raw:
            row["params"]["event"]["data"].pop("turn")
    elif corruption == "different_native_api":
        api["native_api"] = "Agent.dispose"
    elif corruption == "wrong_action":
        request["action"] = "pause"
    elif corruption == "wrong_signal":
        idle["signal"] = "SIGTERM"
    elif corruption == "api_references_itself_as_request":
        api["request_source"] = {"path": "control-events.jsonl", "line": 2}
    elif corruption == "api_references_different_start":
        api["source"] = {"path": "raw-events.jsonl", "line": 2}
    elif corruption == "end_before_start":
        raw.reverse()
        request["source"]["line"] = api["source"]["line"] = 2
        idle["source"]["line"] = 1
        host["trigger"]["line"] = 2
    elif corruption == "api_before_request":
        controls[:] = [api, request, idle]
        idle["request_source"]["line"] = api["request_source"]["line"] = 2
        idle["api_source"]["line"] = 1
    elif corruption == "api_after_idle":
        controls[:] = [request, idle, api]
        idle["api_source"]["line"] = 3
    elif corruption == "different_host_trigger":
        host["trigger"]["line"] = 2
    elif corruption == "different_control_file":
        idle["request_source"]["path"] = idle["api_source"]["path"] = "unrelated-controls.jsonl"
        api["request_source"]["path"] = "unrelated-controls.jsonl"
        write_jsonl(paths.run / "candidate/unrelated-controls.jsonl", controls)
    elif corruption == "different_raw_file":
        for row in controls:
            row["source"]["path"] = "unrelated-raw.jsonl"
        host["trigger"]["path"] = "candidate/unrelated-raw.jsonl"
        write_jsonl(paths.run / "candidate/unrelated-raw.jsonl", raw)
    save_cancel_evidence(paths, raw, controls, host)
    result = detailed.grade_cancel(paths, scope, ledger, native)
    assert not result["passed"]
    assert not next(check["passed"] for check in result["checks"] if check["name"] == "native_running_cancel_confirmed")


def sdk_cancel_evidence(scope):
    """Actual SDK bridge schema; every projection has separate copied values."""
    identity = {"run_id": scope["variant"], "session_id": scope["session_id"], "scope": scope}
    terminal = {"is_complete": True, "current_turn": 1, "interruptions": [], "final_output_present": False,
                "agent_end_events": 0, "response_count": 0, "run_loop_exception": None}
    activity = {**identity, "time": "2026-10-20T00:00:00.100000Z", "native_current_turn": 1,
                "native_is_complete": False, "type": "llm_start",
                "event": {"agent_name": "harness-lab-agent", "system_prompt": "task", "input_items": []}}
    observation = {**identity, "time": "2026-10-20T00:00:00.400000Z", "native_current_turn": 1,
                   "native_is_complete": True, "type": "public_stream_observation",
                   "event": {"api": "RunResultStreaming.stream_events", "stream_exhausted": True,
                             "native_terminal": deepcopy(terminal)}}
    ref = lambda name, line: {"path": name, "line": line}
    common = {**identity, "request_id": "unit-detail:cancel:1", "requested_at": "2026-10-20T00:00:00.200000Z",
              "turn": 1, "native_api": "RunResultStreaming.cancel", "cancel_mode": "immediate",
              "signal": "SIGUSR1", "action": "cancel", "active_input_submitted": True,
              "native_is_complete_before": False, "active_activity": "llm"}
    request = {**common, "phase": "requested", "time": "2026-10-20T00:00:00.200001Z",
               "api_executed": False, "cancel_confirmed": False, "source": ref("raw-events.jsonl", 1)}
    api = {**common, "phase": "native_api_returned", "time": "2026-10-20T00:00:00.300000Z",
           "api_executed": True, "cancel_confirmed": False, "native_is_complete_after": True,
           "source": ref("raw-events.jsonl", 1), "request_source": ref("control-events.jsonl", 1)}
    closed = {**common, "phase": "native_stream_closed", "time": "2026-10-20T00:00:00.400001Z",
              "api_executed": True, "cancel_confirmed": True, "stream_exhausted": True,
              "source": ref("raw-events.jsonl", 2), "activity_source": ref("raw-events.jsonl", 1),
              "request_source": ref("control-events.jsonl", 1), "api_source": ref("control-events.jsonl", 2),
              "native_terminal": deepcopy(terminal)}
    host = {**identity, "candidate": "openai_agents_sdk", "requested_at": "2026-10-20T00:00:00.150000Z",
            "signal": "SIGUSR1", "signal_exit_code": 0,
            "container_name": "harness-lab-detailed-agents-sdk-" + scope["variant"],
            "trigger": ref("candidate/raw-events.jsonl", 1), "observed_through": ref("raw-events.jsonl", 1)}
    original = {"status": "cancelled", "stop_reason": "native_immediate_cancel_stream_exhausted",
                "session_id": scope["session_id"], "scope": scope, "final_response": None, "stream_exhausted": True,
                "native_terminal": deepcopy(terminal), "native_terminal_source": ref("raw-events.jsonl", 2),
                "experiment_cancel": {"requested": True, "api_executed": True, "confirmed": True,
                                      "source": ref("control-events.jsonl", 3)}}
    return [activity, observation], [request, api, closed], host, original


def save_sdk_cancel_evidence(paths, raw, controls, host, original, *, bind=True):
    def line_hash(row):
        return hashlib.sha256((json.dumps(row) + "\n").encode()).hexdigest()

    def bind_reference(reference):
        if not isinstance(reference, dict):
            return
        entries = raw if reference.get("path") == "raw-events.jsonl" else controls if reference.get("path") == "control-events.jsonl" else []
        number = reference.get("line", 0)
        if type(number) is int and 0 < number <= len(entries):
            reference["line_sha256"] = line_hash(entries[number - 1])

    if bind:
        for row in controls:
            for name in ("source", "activity_source"):
                bind_reference(row.get(name))
        # Reordered acyclic references still receive correct byte hashes, so
        # order/phase negatives cannot pass merely due to a stale test hash.
        for _ in range(len(controls) + 1):
            for row in controls:
                for name in ("request_source", "api_source"):
                    bind_reference(row.get(name))
        bind_reference(original.get("native_terminal_source"))
        bind_reference(original.get("experiment_cancel", {}).get("source"))
        bind_reference(host.get("observed_through"))
        line = host["trigger"]["line"]
        if type(line) is int and 0 < line <= len(raw):
            host["trigger_line_sha256"] = line_hash(raw[line - 1])
    save_cancel_evidence(paths, raw, controls, host)
    (paths.run / "candidate/result.json").write_text(json.dumps(original))


@pytest.mark.parametrize("in_flight", [False, True])
def test_sdk_cancel_requires_public_stream_settlement_and_accepts_in_flight_audit(tmp_path, monkeypatch, in_flight):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "cancel", "openai_agents_sdk")
    raw, controls, host, original = sdk_cancel_evidence(scope)
    save_sdk_cancel_evidence(paths, raw, controls, host, original)
    if in_flight:
        write_jsonl(paths.run / "tool-events.jsonl", [
            {"scope": scope, "call_id": "earlier", "phase": "start", "tool": "write", "arguments": {},
             "time": "2026-10-20T00:00:00.190000Z"},
            {"scope": scope, "call_id": "earlier", "phase": "finish", "tool": "write", "status": 400,
             "time": "2026-10-20T00:00:00.600000Z"}])
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    native = detailed.native_terminal(paths)
    assert detailed.grade_sdk_cancel(paths, scope, ledger, native)["passed"]
    assert json.loads((paths.run / "candidate/result.json").read_text()) == original


@pytest.mark.parametrize("progress", ["same_turn", "host_vm_clock_offset", "expired_host_hook", "different_host_turn"])
def test_sdk_cancel_separately_verifies_host_hook_and_later_admission_hook(tmp_path, monkeypatch, progress):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "cancel", "openai_agents_sdk")
    raw, controls, host, original = sdk_cancel_evidence(scope)
    observation = raw.pop()
    end = {**deepcopy(raw[0]), "type": "llm_end", "time": "2026-10-20T00:00:00.160000Z"}
    tool = {**deepcopy(raw[0]), "type": "tool_start", "time": "2026-10-20T00:00:00.170000Z",
            "event": {"agent_name": "harness-lab-agent", "tool_name": "read_file", "call_id": "sdk-tool-1"}}
    raw.extend([end, tool, observation])
    request, api, closed = controls
    for row in controls:
        row["active_activity"] = "tool"
    request["source"]["line"] = api["source"]["line"] = closed["activity_source"]["line"] = 3
    closed["source"]["line"] = original["native_terminal_source"]["line"] = 4
    if progress == "expired_host_hook":
        end["time"] = "2026-10-20T00:00:00.140000Z"
        host["observed_through"]["line"] = 2
    elif progress == "different_host_turn":
        raw[0]["native_current_turn"] = 2
    elif progress == "host_vm_clock_offset":
        host["requested_at"] = "2026-10-19T23:59:30Z"
    save_sdk_cancel_evidence(paths, raw, controls, host, original)
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    result = detailed.grade_sdk_cancel(paths, scope, ledger, detailed.native_terminal(paths))
    assert result["passed"] is (progress in {"same_turn", "host_vm_clock_offset"})


@pytest.mark.parametrize("corruption", [
    "empty_request_id", "foreign_hook_session", "foreign_observation_run", "different_api_turn", "boolean_turn",
    "different_api", "after_turn_mode", "inactive_request", "not_official_hook", "stale_hook",
    "api_not_executed", "api_wrong_request_source", "api_wrong_activity_source", "api_after_close",
    "stream_not_exhausted", "public_error", "public_final_output", "public_not_complete", "natural_agent_end",
    "summary_terminal_differs", "missing_terminal_source", "wrong_terminal_source", "summary_claim_only",
    "line_hash_mismatch", "host_wrong_scope", "host_signal_failed", "late_write", "late_tool_hook",
    "missing_host_snapshot", "host_snapshot_after_admission", "host_snapshot_hash_mismatch",
])
def test_sdk_cancel_rejects_claims_without_actual_public_source_chain(tmp_path, monkeypatch, corruption):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "cancel", "openai_agents_sdk")
    raw, controls, host, original = sdk_cancel_evidence(scope)
    request, api, closed = controls
    if corruption == "empty_request_id":
        for row in controls:
            row["request_id"] = ""
    elif corruption == "foreign_hook_session":
        raw[0]["session_id"] = "another-session"
    elif corruption == "foreign_observation_run":
        raw[1]["run_id"] = "another-run"
    elif corruption in {"different_api_turn", "boolean_turn"}:
        api["turn"] = 2 if corruption == "different_api_turn" else True
    elif corruption == "different_api":
        api["native_api"] = "asyncio.Task.cancel"
    elif corruption == "after_turn_mode":
        for row in controls:
            row["cancel_mode"] = "after_turn"
    elif corruption == "inactive_request":
        request["native_is_complete_before"] = True
    elif corruption == "not_official_hook":
        raw[0]["type"] = "provider_wire_event"
    elif corruption == "stale_hook":
        raw.insert(1, {**deepcopy(raw[0]), "time": "2026-10-20T00:00:00.180000Z", "type": "llm_end"})
        closed["source"]["line"] = original["native_terminal_source"]["line"] = 3
    elif corruption == "api_not_executed":
        api["api_executed"] = False
    elif corruption == "api_wrong_request_source":
        api["request_source"]["line"] = 2
    elif corruption == "api_wrong_activity_source":
        api["source"]["line"] = 2
    elif corruption == "api_after_close":
        controls[:] = [request, closed, api]
        closed["api_source"]["line"] = 3
        original["experiment_cancel"]["source"]["line"] = 2
    elif corruption == "stream_not_exhausted":
        raw[1]["event"]["stream_exhausted"] = False
    elif corruption in {"public_error", "public_final_output", "public_not_complete"}:
        key, value = {"public_error": ("run_loop_exception", {"error_type": "RuntimeError", "error": "actual failure"}),
                      "public_final_output": ("final_output_present", True),
                      "public_not_complete": ("is_complete", False)}[corruption]
        for terminal in (raw[1]["event"]["native_terminal"], closed["native_terminal"], original["native_terminal"]):
            terminal[key] = value
    elif corruption == "natural_agent_end":
        raw.append({**deepcopy(raw[0]), "time": "2026-10-20T00:00:00.390000Z", "type": "agent_end"})
    elif corruption == "summary_terminal_differs":
        original["native_terminal"]["response_count"] = 4
    elif corruption == "missing_terminal_source":
        original.pop("native_terminal_source")
    elif corruption == "wrong_terminal_source":
        original["native_terminal_source"]["line"] = 1
    elif corruption == "summary_claim_only":
        controls.clear()
    elif corruption == "host_wrong_scope":
        host["scope"] = {**scope, "user_id": "other-user"}
    elif corruption == "host_signal_failed":
        host["signal_exit_code"] = 1
    elif corruption == "missing_host_snapshot":
        host.pop("observed_through")
    elif corruption == "host_snapshot_after_admission":
        host["observed_through"]["line"] = 2
    elif corruption == "late_write":
        write_jsonl(paths.run / "tool-events.jsonl", [
            {"scope": scope, "call_id": "late", "phase": "start", "tool": "write", "arguments": {},
             "time": "2026-10-20T00:00:00.600000Z"},
            {"scope": scope, "call_id": "late", "phase": "finish", "tool": "write", "status": 400,
             "time": "2026-10-20T00:00:00.700000Z"}])
    elif corruption == "late_tool_hook":
        raw.append({**deepcopy(raw[0]), "time": "2026-10-20T00:00:00.600000Z", "type": "tool_start",
                    "event": {"agent_name": "harness-lab-agent", "tool_name": "write", "call_id": "late"}})
    save_sdk_cancel_evidence(paths, raw, controls, host, original)
    if corruption == "line_hash_mismatch":
        raw[0]["event"]["system_prompt"] = "changed after source was bound"
        save_sdk_cancel_evidence(paths, raw, controls, host, original, bind=False)
    elif corruption == "host_snapshot_hash_mismatch":
        host["observed_through"]["line_sha256"] = "0" * 64
        save_sdk_cancel_evidence(paths, raw, controls, host, original, bind=False)
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    assert not detailed.grade_sdk_cancel(paths, scope, ledger, detailed.native_terminal(paths))["passed"]


@pytest.mark.parametrize("exit_code", [1, 0])
@pytest.mark.parametrize("process_error", [None, "oom", "missing_observation"])
def test_sdk_cancel_grade_requires_expected_exit_one_and_allows_zero_model_requests(tmp_path, monkeypatch, exit_code, process_error):
    args, config, paths, scope = setup_case(tmp_path, monkeypatch, "cancel", "openai_agents_sdk")
    raw, controls, host, original = sdk_cancel_evidence(scope)
    save_sdk_cancel_evidence(paths, raw, controls, host, original)
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    monkeypatch.setattr(detailed, "ledger_request", lambda *_args, **_kwargs: ledger)
    _, observation = save_process_evidence(args, paths, exit_code)
    if process_error == "oom":
        observation["containers"][0]["state"]["OOMKilled"] = True
        (paths.run / "worker-observation.json").write_text(json.dumps(observation))
    elif process_error == "missing_observation":
        (paths.run / "worker-observation.json").unlink()
    with sqlite3.connect(paths.budget / "calls.sqlite") as db:
        db.executescript("CREATE TABLE budget (id INTEGER, cap INTEGER); CREATE TABLE calls "
                         "(id INTEGER, started_at TEXT, path TEXT, outcome TEXT, upstream_status INTEGER);")
        db.execute("INSERT INTO budget VALUES(1,?)", (config.budget.max_model_calls,))
    assert detailed.grade(args) == (0 if exit_code == 1 and process_error is None else 1)
    verdict = json.loads((paths.run / "verdict.json").read_text())
    assert verdict["metrics"]["model_requests"] == 0
    assert next(check for check in verdict["checks"] if check["name"] == "candidate_process_exit")["passed"] is (exit_code == 1 and process_error is None)
    assert json.loads((paths.run / "candidate/result.json").read_text()) == original


def test_extension_state_and_alias_evidence_reject_missing_query_or_native_tampering(tmp_path, monkeypatch):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "extension")
    operation = "order-unit"
    receipt = {**scope, "operation_id": operation, "id": 1, "quantity": 2}
    state = {"schema_version": 1, "operation_id": operation, "receipt_confirmed": True,
             "operations": {operation: {"operation_id": operation, "receipt_confirmed": True,
                                         "write_count": 1, "receipts": [receipt],
                                         "source": {"path": "http-tool-events.jsonl", "line": 1}}}}
    native = paths.run / "candidate/native-state.json"
    native.write_text(json.dumps({"context": {"business": state}}))
    projection = {"scope": scope, "business_state": state, "source": {"path": "native-state.json",
        "sha256": hashlib.sha256(native.read_bytes()).hexdigest(), "json_pointer": "/context/business"}}
    (paths.run / "candidate/business-state.json").write_text(json.dumps(projection))
    actual = {"operation_id": operation, "write_count": 1, "receipts": [receipt]}
    arguments = {"operation_id": operation}
    write_jsonl(paths.run / "candidate/http-tool-events.jsonl", [{"scope": scope, "call_id": "native-1",
        "tool": "receipt", "exposed_tool": "query_order_status", "arguments": arguments,
        "phase": "finish", "status": 200, "result": {"result": actual}}])
    write_jsonl(paths.run / "tool-events.jsonl", [
        {"scope": scope, "call_id": "trusted-1", "tool": "receipt", "phase": "start", "arguments": arguments},
        {"scope": scope, "call_id": "trusted-1", "tool": "receipt", "phase": "finish", "status": 200, "result": actual}])

    def grade():
        result = {"passed": True, "checks": []}
        detailed.extension_checks(result, paths, scope, {"writes": [receipt]}, {"selected": [{"operation_id": operation}]})
        return result

    assert grade()["passed"]
    event_path = paths.run / "candidate/http-tool-events.jsonl"
    event = json.loads(event_path.read_text())
    event["tool"] = "write"
    write_jsonl(event_path, [event])
    assert not grade()["passed"]
    event["tool"] = "receipt"
    write_jsonl(event_path, [event])
    write_jsonl(paths.run / "candidate/http-tool-events.jsonl", [])
    assert not grade()["passed"]
    native.write_text(json.dumps({"context": {"business": {**state, "receipt_confirmed": False}}}))
    with pytest.raises(ValueError, match="hash"):
        detailed.business_projection(paths.run / "candidate", scope)


@pytest.mark.parametrize("native_hook", [False, True])
def test_quantity_denial_needs_real_attempt_no_http_and_no_substitute_write(tmp_path, monkeypatch, native_hook):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "extension-denied")
    operation = "quantity-denied-item-101-08"
    raw = {"scope": scope, "call_id": "blocked-1", "tool": "write", "phase": "finish",
           "arguments": {"operation_id": operation, "quantity": 4, "item_id": "item-101-08"},
           "http_forwarded": False}
    raw.update({"native_hook_result": {"block": True}} if native_hook else {"status": 403})
    write_jsonl(paths.run / "candidate/business-tool-events.jsonl", [raw])
    decision = {"scope": scope, "call_id": "blocked-1", "tool": "write", "operation_id": operation, "quantity": 4,
        "quantity_limit": 3, "decision": "deny", "reason": "quantity_limit_exceeded", "approval_required": False,
        "http_forwarded": False, "layer": "candidate_execution", "source": {"path": "business-tool-events.jsonl", "line": 1}}
    write_jsonl(paths.run / "candidate/business-rule-decisions.jsonl", [decision])
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    assert detailed.grade_quantity_denial(paths, scope, ledger, operation)["passed"]
    raw["tool"] = "sensitive_export"
    write_jsonl(paths.run / "candidate/business-tool-events.jsonl", [raw])
    assert not detailed.grade_quantity_denial(paths, scope, ledger, operation)["passed"]
    raw["tool"] = "write"
    write_jsonl(paths.run / "tool-events.jsonl", [{"scope": scope, "tool": "write", "phase": "rejected", "status": 400}])
    assert not detailed.grade_quantity_denial(paths, scope, ledger, operation)["passed"]
    write_jsonl(paths.run / "tool-events.jsonl", [])
    raw["arguments"]["quantity"] = 2
    write_jsonl(paths.run / "candidate/business-tool-events.jsonl", [raw])
    assert not detailed.grade_quantity_denial(paths, scope, ledger, operation)["passed"]
    raw["arguments"]["quantity"] = 4
    write_jsonl(paths.run / "candidate/business-tool-events.jsonl", [raw])
    ledger["requests"] = [{**scope, "operation_id": "substitute", "action": "write"}]
    ledger["request_count"] = 1
    assert not detailed.grade_quantity_denial(paths, scope, ledger, operation)["passed"]


@pytest.mark.parametrize("candidate", ["deepseek_harness", "openclaw", "deepagents_langgraph", "openai_agents_sdk"])
def test_browser_prepare_separates_credentials_and_keeps_frozen_prompt(tmp_path, monkeypatch, capsys, candidate):
    args, _, paths, scope = setup_case(tmp_path, monkeypatch, "browser", candidate)
    credentials = json.loads((paths.fixture / "browser-credentials.json").read_text())
    request = json.loads((paths.fixture / "candidate-request.json").read_text())
    recorded = (paths.run / "input.json").read_text()
    assert credentials["password"] not in recorded
    assert credentials["password"] not in capsys.readouterr().out
    assert "synthetic-fixture-token" not in (paths.fixture / "browser-credentials.json").read_text()
    assert request["prompt"] == detailed.browser_prompt(args.seed, scope, "http://site:8081", "alice")
    assert request["candidate_options"]["case"] == "browser"
    assert json.loads(recorded)["business_http_tools_exposed"] is False
    assert (paths.run / "browser").is_dir()
    assert not list(paths.workspace.iterdir())
    with pytest.raises(FileExistsError):
        detailed.prepare(args)


@pytest.mark.parametrize("case", ["tools", "sensitive"])
def test_nonbrowser_mount_placeholders_and_old_request_protocol(tmp_path, monkeypatch, case):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, case, "pi_agent_core")
    assert json.loads((paths.fixture / "browser-credentials.json").read_text()) == {}
    assert json.loads((paths.fixture / "scope.json").read_text()) == scope
    assert len(json.loads((paths.fixture / "candidate-request.json").read_text())) == 11
    assert (paths.run / "browser").is_dir()


def fixture_denial(scope):
    return [{"scope": scope, "call_id": "call-1", "phase": "start", "tool": "sensitive_export",
             "arguments": {"operation_id": "sensitive-export-1"}},
            {"scope": scope, "call_id": "call-1", "phase": "finish", "tool": "sensitive_export",
             "status": 403, "fixture_status": 403,
             "details": {"reason": "sensitive_operation_denied", "approval_required": False}}]


def denial_ledger(scope):
    return {"writes": [], "write_count": 0, "faults": [], "request_count": 1,
            "requests": [{**scope, "operation_id": "sensitive-export-1", "action": "sensitive_export", "status": 403}]}


def test_fixture_denial_requires_real_paired_audit_and_ledger(tmp_path, monkeypatch):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch)
    events = fixture_denial(scope)
    write_jsonl(paths.run / "tool-events.jsonl", events)
    decisions, errors = detailed.sensitive_decisions(paths, scope, denial_ledger(scope), "sensitive-export-1")
    assert not errors and decisions[0]["layer"] == "fixture_execution"
    assert decisions[0]["source"]["line"] == 2
    assert not detailed.sensitive_decisions(paths, scope, {"requests": []}, "sensitive-export-1")[0]
    events[1]["details"]["reason"] = "Some textual refusal"
    write_jsonl(paths.run / "tool-events.jsonl", events)
    assert not detailed.sensitive_decisions(paths, scope, denial_ledger(scope), "sensitive-export-1")[0]
    write_jsonl(paths.run / "tool-events.jsonl", events[1:])
    decisions, errors = detailed.sensitive_decisions(paths, scope, denial_ledger(scope), "sensitive-export-1")
    assert not decisions and errors


@pytest.mark.parametrize(("native_status", "native_stop", "exit_code", "expected"), [
    ("completed", "stop", 0, "denied"),
    ("failed", "error", 1, "failed"),
    ("timeout", "timeout", 124, "timeout"),
    ("completed", "stop", 124, "completed"),
])
@pytest.mark.parametrize("process_error", [None, "oom", "missing_observation"])
def test_sensitive_outcome_never_masks_native_or_host_failure(tmp_path, monkeypatch, native_status, native_stop, exit_code, expected, process_error):
    args, config, paths, scope = setup_case(tmp_path, monkeypatch)
    ledger = denial_ledger(scope)
    monkeypatch.setattr(detailed, "ledger_request", lambda *_args, **_kwargs: ledger)
    write_jsonl(paths.run / "tool-events.jsonl", fixture_denial(scope))
    original = {"status": native_status, "stop_reason": native_stop}
    (paths.run / "candidate/result.json").write_text(json.dumps(original))
    _, observation = save_process_evidence(args, paths, exit_code)
    if process_error == "oom":
        observation["containers"][0]["state"]["OOMKilled"] = True
        (paths.run / "worker-observation.json").write_text(json.dumps(observation))
    elif process_error == "missing_observation":
        (paths.run / "worker-observation.json").unlink()
    with sqlite3.connect(paths.budget / "calls.sqlite") as db:
        db.executescript("CREATE TABLE budget (id INTEGER, cap INTEGER); CREATE TABLE calls "
                         "(id INTEGER, started_at TEXT, path TEXT, outcome TEXT, upstream_status INTEGER);")
        db.execute("INSERT INTO budget VALUES(1,?)", (config.budget.max_model_calls,))
        db.execute("INSERT INTO calls VALUES(1,'unit-test','/chat/completions','forwarded',200)")
    grade_exit = detailed.grade(args)
    assert json.loads((paths.run / "sensitive-outcome.json").read_text())["experiment_status"] == (expected if process_error is None else native_status)
    assert json.loads((paths.run / "candidate/result.json").read_text()) == original
    assert grade_exit == (0 if native_status == "completed" and exit_code == 0 and process_error is None else 1)


@pytest.mark.parametrize("wire", [False, True])
def test_browser_index_requires_real_successful_raw_reference(tmp_path, monkeypatch, wire):
    _, _, paths, scope = setup_case(tmp_path, monkeypatch, "browser")
    native = {"scope": scope, "call_id": "b1", "native_tool": "mcp__browser__browser_click"}
    if wire:
        native["direction"] = "inbound"
        native["message"] = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
    else:
        native.update({"phase": "finish", "status": 200, "result": {"content": []}})
    action = {"scope": scope, "tool": "browser_click", "native_tool": native["native_tool"],
              "call_id": "b1", "phase": "finish", "status": 200,
              "source": {"path": "mcp-events.jsonl", "line": 1}}
    write_jsonl(paths.run / "candidate/mcp-events.jsonl", [native])
    write_jsonl(paths.run / "candidate/browser-actions.jsonl", [action])
    indexed, errors = detailed.browser_index(paths, scope)
    assert not errors and indexed[0]["source"]["line"] == 1
    assert indexed[0]["native_source"]["path"] == "candidate/mcp-events.jsonl"
    write_jsonl(paths.run / "candidate/browser-actions.jsonl", [action, action])
    assert detailed.browser_index(paths, scope)[1]
    write_jsonl(paths.run / "candidate/browser-actions.jsonl", [action])
    missing = dict(native)
    missing.pop("call_id")
    write_jsonl(paths.run / "candidate/mcp-events.jsonl", [missing])
    assert not detailed.browser_index(paths, scope)[0]
    if wire:
        native["message"] = {"id": 1, "error": {"code": -1, "message": "failure"}}
    else:
        native.update({"phase": "error", "status": 500, "result": None})
    write_jsonl(paths.run / "candidate/mcp-events.jsonl", [native])
    indexed, errors = detailed.browser_index(paths, scope)
    assert not indexed and errors
    action["source"]["path"] = "../site-actions.jsonl"
    write_jsonl(paths.run / "candidate/browser-actions.jsonl", [action])
    assert detailed.browser_index(paths, scope)[1]


@pytest.mark.parametrize("reason", ["max-tokens", "blocked", "interrupted", "missing_turn_end", None])
def test_denial_cannot_hide_incomplete_native_turn(tmp_path, monkeypatch, reason):
    _, _, paths, _ = setup_case(tmp_path, monkeypatch)
    result_file = paths.run / "candidate/result.json"
    result_file.write_text(json.dumps({"status": "denied", "native_status": "incomplete", "stop_reason": reason}))
    assert detailed.native_terminal(paths)["normal_end"] is False
    result_file.write_text(json.dumps({"status": "denied", "native_status": "incomplete", "stop_reason": "stop"}))
    assert detailed.native_terminal(paths)["normal_end"] is False


class FakeDetailedLab(DetailedLab):
    def __init__(self, tmp_path):
        super().__init__()
        self.config = replace(self.config, host_data_root=tmp_path / "data",
                              sampling=replace(self.config.sampling, interval_seconds=0.001))
        self.detailed_override = tmp_path / "detailed.override.yml"
        self.detailed_override.write_text("services: {}\n")
        self.commands = []
        self.worker = None

    def containers(self):
        return [deepcopy(self.worker)] if self.worker else []

    def call(self, args, *, capture=False, check=True, timeout=None):
        self.commands.append(tuple(args))
        tail = args[len(self.prefix):] if args[:len(self.prefix)] == self.prefix else None
        output = ""
        if tail and tail[:3] == ["--profile", "model", "--profile"] and tail[4:] == ["config", "--format", "json"]:
            # This must be true before resolving a required-variable override.
            assert all(self.env.get(name) for name in ("LAB_FIXTURE_HOST", "LAB_EVIDENCE_HOST", "LAB_STATE_HOST"))
            service = tail[3]
            output = json.dumps({"services": {"model-proxy": {"environment": {
                "MODEL_NAME": "unit-model", "MODEL_API_KEY": "must-never-be-serialized"}},
                service: {"image": f"harness-lab-{service}:local", "mem_limit": 2 ** 31, "cpus": 1,
                          "environment": {"SYNTHETIC_SECRET": "must-never-be-serialized"}}}})
        elif args[:5] == ["docker", "image", "inspect", "--format", "{{.Id}}"]:
            output = "sha256:" + "b" * 64 + "\n"
        elif tail and "runner.detailed" in tail and "prepare" in tail:
            for name in ("LAB_EVIDENCE_HOST", "LAB_WORKSPACE_HOST", "LAB_STATE_HOST", "LAB_BUDGET_HOST"):
                Path(self.env[name]).mkdir(parents=True, exist_ok=False)
        elif tail and tail[0] == "run" and "--name" in tail:
            name, service = tail[tail.index("--name") + 1], tail[-1]
            self.worker = {"id": "a" * 64, "name": "/" + name, "image_id": "sha256:" + "b" * 64,
                           "image_tag": f"harness-lab-{service}:local", "memory_limit": 2 ** 31, "cpu_nano": 10 ** 9,
                           "state": {"Status": "exited", "Pid": 0, "ExitCode": 0, "Error": "",
                                     "Running": False, "Paused": False, "Restarting": False, "Dead": False, "OOMKilled": False}}
        elif tail and "runner.detailed" in tail and "grade" in tail:
            # Other task grading is outside this transport double; exercise
            # the real protected-host process reader against retained records.
            grading_args = Namespace(**{key.replace("-", "_"): tail[tail.index("--" + key) + 1]
                                       for key in ("run-id", "candidate", "case")})
            report = detailed.process_validation(grading_args, Namespace(run=Path(self.env["LAB_EVIDENCE_HOST"])))
            return subprocess.CompletedProcess(args, 0 if report["passed"] else 1, "", "")
        return subprocess.CompletedProcess(args, 0, output, "")

    def sample(self, event, samples, evidence):
        return


@pytest.mark.parametrize("corruption", [None, "wrong_run", "wrong_session", "wrong_scope", "complete", "boolean_turn", "ended", "closed", "not_hook"])
def test_sdk_host_trigger_uses_live_owned_official_hook_only(tmp_path, corruption):
    lab = FakeDetailedLab(tmp_path)
    run = tmp_path / "trigger"
    (run / "candidate").mkdir(parents=True)
    scope = {"experiment_id": "detailed", "variant": "unit-detail", "user_id": "alice", "session_id": "main"}
    raw = sdk_cancel_evidence(scope)[0][:1]
    if corruption == "wrong_run":
        raw[0]["run_id"] = "other-run"
    elif corruption == "wrong_session":
        raw[0]["session_id"] = "other-session"
    elif corruption == "wrong_scope":
        raw[0]["scope"] = {**scope, "user_id": "other-user"}
    elif corruption == "complete":
        raw[0]["native_is_complete"] = True
    elif corruption == "boolean_turn":
        raw[0]["native_current_turn"] = True
    elif corruption in {"ended", "closed"}:
        raw.append({**deepcopy(raw[0]), "type": "llm_end" if corruption == "ended" else "public_stream_observation"})
    elif corruption == "not_hook":
        raw[0]["type"] = "provider_wire_event"
    write_jsonl(run / "candidate/raw-events.jsonl", raw)

    class OnePoll:
        count = 0

        def wait(self, _seconds):
            self.count += 1
            return self.count > 1

    lab.cancel_on_live_turn("sdk-test-container", run, OnePoll(), "openai_agents_sdk", "unit-detail", scope)
    assert bool(lab.commands) is (corruption is None)
    if corruption is None:
        record = json.loads((run / "control-request.json").read_text())
        assert record["scope"] == scope and record["signal_exit_code"] == 0
        assert record["trigger_line_sha256"] == hashlib.sha256((run / "candidate/raw-events.jsonl").read_bytes()).hexdigest()
        assert lab.commands == [("docker", "kill", "--signal", "SIGUSR1", "sdk-test-container")]
    else:
        assert not (run / "control-request.json").exists()


def test_sdk_cancel_orchestration_joins_control_evidence_before_grading(tmp_path):
    class CancelDetailedLab(FakeDetailedLab):
        def __init__(self, path):
            super().__init__(path)
            self.release = threading.Event()

        def cancel_on_live_turn(self, name, run, stop, *args):
            assert self.release.wait(2)
            (run / "control-request.json").write_text(json.dumps({"sent": True}))

        def call(self, args, **kwargs):
            result = super().call(args, **kwargs)
            if "openai-agents-sdk" in args and "--name" in args:
                self.release.set()
                self.worker["state"]["ExitCode"] = 1
                return subprocess.CompletedProcess(args, 1, "", "")
            if "runner.detailed" in args and "grade" in args:
                assert json.loads((Path(self.env["LAB_EVIDENCE_HOST"]) / "control-request.json").read_text()) == {"sent": True}
            return result

    lab = CancelDetailedLab(tmp_path)
    assert lab.run_detailed("openai_agents_sdk", "cancel", 101, "sdk-cancel-unit") == 0
    evidence = Path(lab.env["LAB_EVIDENCE_HOST"])
    assert json.loads((evidence / "orchestration-status.json").read_text())["candidate_exit_code"] == 1
    assert json.loads((evidence / "process-validation.json").read_text())["passed"]


def test_browser_orchestration_has_no_business_http_service_and_never_logs_resolved_key(tmp_path):
    lab = FakeDetailedLab(tmp_path)
    assert lab.run_detailed("deepseek_harness", "browser", 101, "browser-unit") == 0
    assert not any("tools" in command for command in lab.commands)
    evidence = Path(lab.env["LAB_EVIDENCE_HOST"])
    assert all("must-never-be-serialized" not in file.read_text() for file in evidence.iterdir() if file.is_file())
    grade = next(i for i, command in enumerate(lab.commands) if "runner.detailed" in command and "grade" in command)
    assert lab.commands.index(("docker", "kill", "harness-lab-detailed-dsh-browser-unit")) < grade
    assert tuple(lab.prefix + ["stop", "--timeout", "3", "model-proxy"]) in lab.commands[:grade]
    execution = json.loads((evidence / "orchestration-status.json").read_text())
    observation = json.loads((evidence / "worker-observation.json").read_text())
    report = json.loads((evidence / "process-validation.json").read_text())
    assert execution["worker_expectation"]["container_id"] == observation["containers"][0]["id"]
    assert execution["worker_expectation"]["image_id"] == "sha256:" + "b" * 64
    assert "not a pre-start ID pin" in report["identity_pin_provenance"]["container_id"]
    assert report["passed"] and len(report["sources"]["worker_observation"]["sha256"]) == 64
    image_inspect = next(i for i, command in enumerate(lab.commands) if command[:3] == ("docker", "image", "inspect"))
    candidate_launch = next(i for i, command in enumerate(lab.commands) if "--name" in command)
    assert image_inspect < candidate_launch < grade


@pytest.mark.parametrize("fault", ["oom", "ambiguous_name"])
def test_host_docker_fault_cannot_be_masked_by_successful_compose_exit(tmp_path, fault):
    class FaultDetailedLab(FakeDetailedLab):
        def containers(self):
            rows = super().containers()
            if rows and fault == "oom":
                rows[0]["state"]["OOMKilled"] = True
            elif rows and fault == "ambiguous_name":
                rows.append(deepcopy(rows[0]))
            return rows

    lab = FaultDetailedLab(tmp_path)
    assert lab.run_detailed("deepseek_harness", "tools", 101, "fault-unit") == 1
    evidence = Path(lab.env["LAB_EVIDENCE_HOST"])
    assert json.loads((evidence / "orchestration-status.json").read_text())["candidate_exit_code"] == 0
    assert json.loads((evidence / "process-validation.json").read_text())["passed"] is False


def test_unsupported_cases_do_not_launch_any_docker_command(tmp_path):
    lab = FakeDetailedLab(tmp_path)
    with pytest.raises(ValueError, match="not connected"):
        lab.run_detailed("pi_agent_core", "browser", 101, "unsupported-unit")
    with pytest.raises(ValueError, match="not connected"):
        lab.run_detailed("codex_sdk", "browser", 101, "unsupported-unit")
    assert lab.commands == []


def test_evidence_io_failure_still_stops_paid_execution_and_cleans_all_browser_services(tmp_path, monkeypatch):
    lab = FakeDetailedLab(tmp_path)
    original = Path.write_text

    def fail_write(path, content, *args, **kwargs):
        if path.name == "orchestration-status.json":
            lab.commands.append(("injected_disk_error",))
            raise OSError("Synthetic evidence storage failure")
        return original(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="Synthetic evidence storage failure"):
        lab.run_detailed("deepseek_harness", "browser", 101, "io-failure-unit")
    failure = lab.commands.index(("injected_disk_error",))
    assert ("docker", "kill", "harness-lab-detailed-dsh-io-failure-unit") in lab.commands[:failure]
    assert tuple(lab.prefix + ["stop", "--timeout", "3", "model-proxy"]) in lab.commands[:failure]
    for service in ("browser", "site", "model-proxy", "fixtures"):
        assert tuple(lab.prefix + ["rm", "-f", service]) in lab.commands[failure:]
