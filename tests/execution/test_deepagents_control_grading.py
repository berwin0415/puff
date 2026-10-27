"""Docker 验收程序中的合成校准，不能作为候选能力证据。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from runner.deepagents_control import grade_drained_cancel
from scripts.native_triggers import cancel_on_deepagents_activity


def save(path, value):
    path.write_text(json.dumps(value) + "\n")


def build(tmp_path, defect=None, progressed=False):
    run, state = tmp_path / "run", tmp_path / "state"
    directory = run / "candidate"
    directory.mkdir(parents=True)
    state.mkdir()
    scope = {"experiment_id": "detailed", "variant": "synthetic-drain", "user_id": "alice", "session_id": "main"}
    identity = {"scope": scope, "run_id": scope["variant"], "session_id": "main"}
    raw, controls = [], []

    def time(second):
        return f"2026-10-20T00:00:{second:02d}+00:00"

    def append(name, rows, value):
        rows.append(value)
        data = (json.dumps(value) + "\n").encode()
        return {"path": name, "line": len(rows), "line_sha256": hashlib.sha256(data).hexdigest()}

    def graph(second, event, mode="tasks"):
        return append("raw-events.jsonl", raw, {**identity, "time": time(second), "stream_mode": mode, "data": event})

    task = {"id": "task-model", "name": "model", "input": {}, "triggers": ["branch:to:model"]}
    active_source = graph(1, task)
    host = {**identity, "candidate": "deepagents_langgraph", "signal": "SIGUSR1", "signal_exit_code": 0,
        "container_name": "harness-lab-detailed-deepagents-" + scope["variant"],
        "requested_at": "2040-01-01T00:00:00+00:00",  # 刻意构造宿主与虚拟机时钟偏移。
        "trigger": {"path": "candidate/raw-events.jsonl", "line": 1},
        "trigger_line_sha256": active_source["line_sha256"],
        "observed_through": {**active_source, "path": "candidate/raw-events.jsonl"}}
    if progressed:
        graph(2, {"id": task["id"], "name": "model", "result": {}, "error": None, "interrupts": []})
        task = {"id": "task-tools", "name": "tools", "input": {}, "triggers": ["branch:to:tools"]}
        active_source = graph(3, task)
    if defect in {"stale_admission", "stale_host"}:
        finished = graph(4, {"id": task["id"], "name": task["name"], "result": {}, "error": None, "interrupts": []})
        if defect == "stale_host":
            host["observed_through"] = {**finished, "path": "candidate/raw-events.jsonl"}
    common = {**identity, "request_id": scope["variant"] + ":drain:1", "signal": "SIGUSR1", "action": "cancel",
        "native_api": "RunControl.request_drain", "reason": "experiment_cancel",
        "semantics": "cooperative_drain_at_superstep_boundary", "immediate_cancel": False}
    request = {**common, "time": time(5), "phase": "requested", "api_executed": False,
        "active_native_task_observed": True, "active_tasks": [{"id": task["id"], "name": task["name"], "source": active_source}],
        "source": active_source, "admitted": True, "refusal_reason": None}
    if defect == "admission_without_task":
        request["active_tasks"] = []
    request_ref = append("control-events.jsonl", controls, request)
    native_api = {**common, "time": time(6), "type": "native_control_api_returned", "arguments": {"reason": "experiment_cancel"},
        "returned": None, "drain_requested": True, "drain_reason": "experiment_cancel"}
    if defect == "wrong_api":
        native_api["arguments"] = {"reason": "another"}
    native_api_ref = append("raw-events.jsonl", raw, native_api)
    api = {**common, "time": time(7), "phase": "native_api_returned", "api_executed": True,
        "request_source": request_ref, "source": native_api_ref, "drain_requested": True, "drain_reason": "experiment_cancel"}
    api_ref = append("control-events.jsonl", controls, api)
    snapshot = {"config": {"configurable": {"thread_id": "main", "checkpoint_id": "checkpoint-001"}},
        "next": ["tools"], "tasks": [["pending", "tools", None, [], None]], "values": {"messages": []}}
    if defect == "wrong_thread":
        snapshot["config"]["configurable"]["thread_id"] = "foreign-session"
    if defect == "no_pending_graph":
        snapshot["next"] = []
    save(directory / "state-snapshot.json", snapshot)
    drained = {**identity, "time": time(8), "type": "native_graph_drained", "exception_type": "langgraph.errors.GraphDrained",
        "message": "Graph drained: experiment_cancel", "reason": "experiment_cancel", "drain_requested": True, "drain_reason": "experiment_cancel"}
    if defect == "wrong_exception":
        drained["exception_type"] = "asyncio.TimeoutError"
    if defect == "wrong_reason":
        drained["reason"] = "another-reason"
    drained_ref = append("raw-events.jsonl", raw, drained)
    closed = {**common, "time": time(9), "phase": "native_drained_observed", "api_executed": True, "cancel_confirmed": True,
        "native_status_after": "drained", "source": drained_ref, "checkpoint_source": {
            "path": "state-snapshot.json", "sha256": hashlib.sha256((directory / "state-snapshot.json").read_bytes()).hexdigest(),
            "json_pointer": "/config/configurable/checkpoint_id"}, "native_exception_reason": "experiment_cancel",
        "checkpoint_id": "checkpoint-001", "native_next": snapshot["next"], "native_tasks": snapshot["tasks"],
        "request_source": request_ref, "api_source": api_ref, "inflight_operations_may_finish_after_request": True,
        "task_continuation_verified": False}
    if defect == "bad_checkpoint_hash":
        closed["checkpoint_source"]["sha256"] = "0" * 64
    if defect == "resume_claim":
        closed["task_continuation_verified"] = True
    if defect == "source_hash":
        closed["source"] = {**drained_ref, "line_sha256": "0" * 64}
    append("control-events.jsonl", controls, closed)
    original = {**identity, "status": "drained", "native_status": "drained", "stop_reason": "native_cooperative_drain",
        "control_observation": deepcopy(closed), "checkpoint_id": "checkpoint-001", "native_next": snapshot["next"],
        "native_tasks": snapshot["tasks"], "native_result": snapshot["values"], "cancel_confirmed": True,
        "immediate_cancel": False, "task_continuation_verified": False,
        "native_exception": {"type": drained["exception_type"], "reason": drained["reason"], "message": drained["message"]},
        "native_terminal": {"is_streaming": False, "error_message": None, "pending_graph_nodes": snapshot["next"]}}
    if defect in {"natural_complete", "timeout"}:
        original["status"] = original["native_status"] = "completed" if defect == "natural_complete" else "timed_out"
    if defect == "changed_result_state":
        original["native_result"] = {"messages": ["substituted"]}
    if defect == "foreign_scope":
        raw[0]["scope"] = {**scope, "user_id": "bob"}
    if defect == "host_signal_failed":
        host["signal_exit_code"] = 1
    if defect == "new_native_task":
        graph(10, {"id": "unexpected", "name": "tools", "input": {}, "triggers": []})
    for name, rows in (("raw-events.jsonl", raw), ("control-events.jsonl", controls)):
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    save(directory / "result.json", original)
    save(run / "control-request.json", host)
    with sqlite3.connect(state / "checkpoints.sqlite") as db:
        db.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)")
        db.execute("INSERT INTO checkpoints VALUES (?, '', ?)", ("main", "different" if defect == "different_db_checkpoint" else "checkpoint-001"))
    native = {key: original[key] for key in ("status", "native_status", "stop_reason")}
    native["error_present"] = False
    ledger = {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}
    return SimpleNamespace(paths=SimpleNamespace(run=run, state=state), scope=scope, ledger=ledger, native=native, raw=raw)


@pytest.mark.parametrize("progressed", [False, True])
def test_native_drain_chain_accepts_actual_task_progress_and_host_clock_offset(tmp_path, progressed):
    item = build(tmp_path, progressed=progressed)
    assert grade_drained_cancel(item.paths, item.scope, item.ledger, item.native)["passed"]


@pytest.mark.parametrize("defect", ["stale_admission", "stale_host", "admission_without_task", "wrong_api", "wrong_thread",
    "no_pending_graph", "wrong_exception", "wrong_reason", "bad_checkpoint_hash", "resume_claim", "source_hash",
    "natural_complete", "timeout", "changed_result_state", "foreign_scope", "host_signal_failed", "new_native_task", "different_db_checkpoint"])
def test_native_drain_rejects_invalid_claims(tmp_path, defect):
    item = build(tmp_path, defect=defect)
    assert not grade_drained_cancel(item.paths, item.scope, item.ledger, item.native)["passed"]


@pytest.mark.parametrize("tool", ["write", "write_file", "sensitive_export"])
@pytest.mark.parametrize("after_ack", [False, True])
def test_independent_audit_distinguishes_inflight_work_and_new_writes(tmp_path, tool, after_ack):
    item = build(tmp_path)
    events = [{"time": "2026-10-20T00:00:10+00:00" if after_ack else "2026-10-20T00:00:04+00:00",
        "scope": item.scope, "call_id": "http-one", "phase": "start", "tool": tool, "arguments": {}},
        {"time": "2026-10-20T00:00:11+00:00", "scope": item.scope, "call_id": "http-one", "phase": "finish", "tool": tool, "status": 200}]
    (item.paths.run / "tool-events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    result = grade_drained_cancel(item.paths, item.scope, item.ledger, item.native)
    assert result["passed"] is (not after_ack)


def test_unpaired_audit_cannot_be_mistaken_for_no_new_write(tmp_path):
    item = build(tmp_path)
    (item.paths.run / "tool-events.jsonl").write_text(json.dumps({"time": "2026-10-20T00:00:04+00:00", "scope": item.scope,
        "call_id": "unsettled", "phase": "start", "tool": "write", "arguments": {}}) + "\n")
    assert not grade_drained_cancel(item.paths, item.scope, item.ledger, item.native)["passed"]


def test_host_trigger_uses_real_active_native_task_and_records_full_observation(tmp_path):
    item = build(tmp_path)
    raw = item.paths.run / "candidate/raw-events.jsonl"
    raw.write_text(json.dumps(item.raw[0]) + "\n")
    calls = []
    class Stop:
        def wait(self, _timeout):
            return False
    class Lab:
        def call(self, args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0)
    name = "harness-lab-detailed-deepagents-" + item.scope["variant"]
    cancel_on_deepagents_activity(Lab(), name, item.paths.run, Stop(), item.scope["variant"], item.scope)
    host = json.loads((item.paths.run / "control-request.json").read_text())
    assert calls[0][0] == ["docker", "kill", "--signal", "SIGUSR1", name]
    assert host["trigger"]["line"] == host["observed_through"]["line"] == 1
    assert host["trigger_line_sha256"] == host["observed_through"]["line_sha256"]


@pytest.mark.parametrize("case", ["finished", "partial", "foreign"])
def test_host_trigger_does_not_signal_finished_or_invalid_native_tasks(tmp_path, case):
    item = build(tmp_path)
    raw = item.paths.run / "candidate/raw-events.jsonl"
    start = item.raw[0]
    finish = {**start, "data": {"id": start["data"]["id"], "result": {}, "error": None, "interrupts": []}}
    if case == "finished":
        raw.write_text(json.dumps(start) + "\n" + json.dumps(finish) + "\n")
    elif case == "partial":
        raw.write_text(json.dumps(start))
    else:
        raw.write_text(json.dumps({**start, "session_id": "foreign"}) + "\n")
    class Stop:
        calls = 0
        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 1
    class Lab:
        def call(self, *_args, **_kwargs):
            raise AssertionError("已完成任务不能触发信号")
    cancel_on_deepagents_activity(Lab(), "unused", item.paths.run, Stop(), item.scope["variant"], item.scope)
