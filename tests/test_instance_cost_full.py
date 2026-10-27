"""I25 成本流程边界校准初稿，只在固定 Docker 内执行。

合成记录不证明真实任务、活动重叠、资源节省、崩溃恢复或候选能力。
不调用 SDK 或 Docker。
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from runner import instance_cost_full as flow
from runner.instance_cost import CAPS, CPU, MEMORY, canonical, sha
from scripts.run_instance_cost_full import CostFlowLab
from scripts import run_instance_cost_full as full_host


def plan(**changes):
    options = dict(trial_id="cost-calibration", pair_id="same-input", strategy="on-demand", users=2,
        candidate_image="sha256:" + "1" * 64, bench_image="sha256:" + "2" * 64,
        services_image="sha256:" + "3" * 64, seed=101, model_name="synthetic-not-called")
    options.update(changes)
    return flow.make_flow_plan(**options)


def test_full_source_plan_remains_honest_about_product_backend_and_native_recovery():
    value = plan()
    assert value["fixed_allocations"][1]["allocated_cpu_nano"] == CPU
    assert value["allocation_role_alias"] == {"observer": "backend"}
    assert value["i25_complete"] is False and value["formal_i25_eligible"] is False
    assert '不声称产品身份' in value["orchestration_trust"]
    assert '所有者专属工具和代理' in value["subset_service_policy"]
    assert value["business_tasks_planned_to_execute"] == 6
    assert value["business_tasks_bound"] == 8
    assert any("不确定写入" in row for row in value["remaining_work"])


@pytest.mark.parametrize("users", (5, 10))
def test_large_resident_plan_refuses_before_docker_but_on_demand_retains_two_active(users):
    assert plan(strategy="resident", users=users)["full_fixed_allocation_allowed"] is False
    value = plan(users=users)
    assert value["full_fixed_allocation_allowed"] is True
    assert value["fixed_allocations"][1]["counts"]["worker"] == 2


@pytest.mark.parametrize("users", (5, 10))
def test_explicit_rotation_variant_binds_new_tasks_but_keeps_two_active_and_all_limitations(users):
    value = plan(users=users, rotate_inactive_users=True)
    assert value["flow_variant"] == "inactive-user-rotation"
    assert value["business_tasks_bound"] == 10 and value["business_tasks_planned_to_execute"] == 8
    assert value["max_active_tasks"] == 2
    assert value["fixed_allocations"][1]["counts"]["worker"] == 2
    assert value["i25_complete"] is value["formal_i25_eligible"] is False
    assert flow.owners_for_kind(value, flow.INACTIVE_WAKE_KIND) == ["user-003", "user-004"]
    with pytest.raises(ValueError, match='独立完整流程变体'):
        plan(users=users, rotate_inactive_users=True, baseline_only=True)


def rotation_materials(tmp_path, monkeypatch, *, users=5):
    value = plan(users=users, rotate_inactive_users=True)
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected/plan.json").write_bytes(canonical(value))
    def admin(root, method, route, **kwargs):
        body = kwargs["json"]
        owner = body["users"][0]
        return {"scopes": [{"experiment_id": body["experiment_id"], "variant": body["variant"], **owner,
            "token": "synthetic-tool-token-" + body["variant"]}]}
    monkeypatch.setattr(flow, "admin", admin)
    monkeypatch.setattr("runner.grading.prepare_tools_inputs", lambda workspace, scope: {"prompt": "synthetic fixed tools task"})
    flow.prepare(tmp_path)
    return value, flow.read(tmp_path / "protected/tasks.json"), flow.read(tmp_path / "protected/users.json")["users"]


@pytest.mark.parametrize("users", (5, 10))
def test_rotation_preparation_creates_owner_specific_fresh_scopes_budgets_and_native_history_bindings(tmp_path, monkeypatch, users):
    value, tasks, records = rotation_materials(tmp_path, monkeypatch, users=users)
    assert len(records) == users and len(tasks) == 10
    assert {task["owner"] for task in tasks if task["kind"] == flow.INACTIVE_WAKE_KIND} == {"user-003", "user-004"}
    assert all(task["kind"] == flow.INACTIVE_WAKE_KIND for task in tasks if task["owner"] in {"user-003", "user-004"})
    assert flow.verify_rotation_materials(tmp_path, value, tasks, records) is True
    for user in records[4:]:
        assert flow.read(tmp_path / "users" / user["owner"] / "config/resident.json")["tasks"] == []


@pytest.mark.parametrize("change", ("missing-task", "scope", "reused-budget", "reused-store", "tool-binding"))
def test_rotation_cannot_borrow_an_existing_task_scope_budget_or_store(tmp_path, monkeypatch, change):
    value, tasks, records = rotation_materials(tmp_path, monkeypatch)
    task = next(item for item in tasks if item["task_id"] == "inactive-wake-user-003")
    home = tmp_path / "users/user-003"
    if change == "missing-task":
        tasks.remove(task)
    elif change == "scope":
        task["scope"]["session_id"] = "wake-user-001"
    elif change == "reused-budget":
        token = (tmp_path / "users/user-001/config/active-model.token").read_bytes()
        (home / "config/inactive-wake-model.token").write_bytes(token)
        (home / "budgets/inactive-wake/run.token").write_bytes(token)
        task["model_token_sha256"] = sha(token.strip())
    elif change == "reused-store":
        path = home / "config/resident.json"
        binding = flow.read(path)
        binding["tasks"][0]["state_dir"] = "/state/history"
        path.write_bytes(canonical(binding))
        next(user for user in records if user["owner"] == "user-003")["binding_sha256"] = sha(path.read_bytes())
    else:
        path = home / "config/inactive-wake-tool-binding.json"
        binding = flow.read(path)
        binding["scope"]["user_id"] = "user-001"
        path.write_bytes(canonical(binding))
    assert flow.verify_rotation_materials(tmp_path, value, tasks, records) is False


def rotation_history_fixture(tmp_path):
    owner = "user-003"
    user = {"owner": owner, "history_task_id": "history-" + owner, "sentinel": "synthetic-original-user-003",
        "scope": {"experiment_id": "instance-cost", "variant": "rotation-history", "user_id": owner, "session_id": "main"}}
    directory = tmp_path / "users" / owner / "evidence/history"
    directory.mkdir(parents=True)
    native = {"values": {"messages": [{"id": "prepared-history-history-user-003", "content": user["sentinel"]}]},
        "next": [], "tasks": [], "interrupts": [],
        "config": {"configurable": {"thread_id": "main", "checkpoint_id": "original-history-checkpoint"}}}
    summaries = []
    for number in range(3):
        path = directory / f"native-state-synthetic-{number}.json"
        path.write_bytes(canonical(native))
        summaries.append({"checkpoint_id": "original-history-checkpoint", "scope": user["scope"],
            "source": {"path": path.name, "sha256": sha(path.read_bytes()), "public_api": "CompiledStateGraph.aget_state"}})
    records = [{"owner": owner, "task_id": user["history_task_id"], "worker_id": "b" * 64,
        "stage": stage, "started_monotonic": start, "finished_monotonic": end,
        "read_succeeded": True, "restored": summary} for stage, start, end, summary in (
            ("before-task", 8, 9, summaries[1]), ("after-task", 13, 14, summaries[2]))]
    prepared = {"preparation_result": summaries[0], "after_exit": {"id": "a" * 64}}
    timing = {"worker_id": "b" * 64, "command_submitted_monotonic": 10, "response_observed_monotonic": 12}
    return user, records, timing, prepared


def test_previously_inactive_user_reads_same_original_history_before_and_after_new_task(tmp_path):
    user, records, timing, prepared = rotation_history_fixture(tmp_path)
    assert flow.verify_rotation_history(tmp_path, user, records, timing, prepared) is True


@pytest.mark.parametrize("change", ("recreated-history", "other-owner", "same-preparation-worker", "late-before", "early-after", "missing-read", "read-failed"))
def test_rotation_history_rejects_relabelled_recreated_or_unordered_evidence(tmp_path, change):
    user, records, timing, prepared = rotation_history_fixture(tmp_path)
    if change == "recreated-history":
        records[1]["restored"]["checkpoint_id"] = "newly-created-history"
    elif change == "other-owner":
        records[1]["restored"]["scope"] = {**user["scope"], "user_id": "user-004"}
    elif change == "same-preparation-worker":
        prepared["after_exit"]["id"] = timing["worker_id"]
    elif change == "late-before":
        records[0]["finished_monotonic"] = 11
    elif change == "early-after":
        records[1]["started_monotonic"] = 11
    elif change == "missing-read":
        records.pop()
    else:
        records[1]["read_succeeded"] = False
    if change == "other-owner":
        with pytest.raises(ValueError):
            flow.verify_rotation_history(tmp_path, user, records, timing, prepared)
    else:
        assert flow.verify_rotation_history(tmp_path, user, records, timing, prepared) is False


def test_inactive_wave_uses_only_new_pair_and_reads_history_even_when_business_reply_fails(tmp_path, monkeypatch):
    value, tasks, users = rotation_materials(tmp_path, monkeypatch)
    lab = CostFlowLab(tmp_path, value)
    rotated = [user for user in users if user["owner"] in {"user-003", "user-004"}]
    lab.task_timings = [{"owner": owner, "kind": kind, "worker_id": "a" * 64,
        "response_observed_monotonic": 0} for kind in ("active", "wake") for owner in ("user-001", "user-002")]
    commands, retired, closed = [], [], []
    monkeypatch.setattr(lab, "observations", lambda: [{"role": "fixtures"}, {"role": "observer"}])
    monkeypatch.setattr(lab, "phase_to", lambda phase, kind=None: None)
    monkeypatch.setattr(lab, "probe", lambda: {"time": "synthetic-observer-marker", "tasks": []})
    monkeypatch.setattr(lab, "services_for", lambda kind, owners: [kind + "-" + owner for owner in owners])
    monkeypatch.setattr(lab, "retire", retired.append)
    monkeypatch.setattr(lab, "close_worker", lambda worker: closed.append(worker.user["owner"]))
    class Worker:
        def __init__(self, user):
            self.user, self.id = user, ("b" if user["owner"] == "user-003" else "c") * 64
        def command(self, action, *, task_id):
            commands.append((self.user["owner"], action, task_id))
            if action == "run_task" and self.user["owner"] == "user-003":
                raise RuntimeError("synthetic-business-command-failure")
            return {"result": {"checkpoint_id": "synthetic-only"}}
    monkeypatch.setattr(lab, "worker", lambda user, stage, online=False: Worker(user))
    records = lab.wave(flow.INACTIVE_WAKE_KIND, rotated)
    assert len(records) == 2 and {row["owner"] for row in records} == {"user-003", "user-004"}
    assert next(row for row in records if row["owner"] == "user-003")["error_type"] == "RuntimeError"
    for owner in ("user-003", "user-004"):
        own = [(action, task) for user, action, task in commands if user == owner]
        assert own == [("state", "history-" + owner), ("run_task", "inactive-wake-" + owner), ("state", "history-" + owner)]
    assert lab.workers == {} and closed == ["user-003", "user-004"]
    assert len(retired) == 2 and len(lab.rotation_history_reads) == 4
    assert all(row["read_succeeded"] for row in lab.rotation_history_reads)
    assert lab.rotation_start["worker_ids_before"] == []
    assert all(row["empty_execution_directories"] and row["budget_contains_only_new_token"]
        for row in lab.rotation_start["new_task_materials"])


def test_rotation_business_missing_result_is_preserved_without_replaying_or_losing_history_reads(tmp_path, monkeypatch):
    value, _, users = rotation_materials(tmp_path, monkeypatch)
    lab = CostFlowLab(tmp_path, value)
    monkeypatch.setattr(lab, "phase_to", lambda *_: None)
    monkeypatch.setattr(lab, "worker", lambda *args, **kwargs: pytest.fail("缺少结果时不得启动替代任务"))
    rotated = [user for user in users if user["owner"] in {"user-003", "user-004"}]
    lab.rotation_history_reads = [{"original": "must-be-retained"}]
    lab.business_readback(rotated, kind=flow.INACTIVE_WAKE_KIND)
    assert len(lab.cold_reads) == 2 and all(row["read_succeeded"] is False for row in lab.cold_reads)
    assert lab.rotation_history_reads == [{"original": "must-be-retained"}]


def test_inactive_wave_has_a_separate_observer_control_and_only_its_frozen_tasks(tmp_path, monkeypatch):
    value, tasks, _ = rotation_materials(tmp_path, monkeypatch)
    selected = []
    control_file(tmp_path, 1, phase=flow.INACTIVE_WAKE_KIND, kind=flow.INACTIVE_WAKE_KIND)
    def activity(root, task):
        selected.append(task["task_id"])
        return {"task_id": task["task_id"], "scope": task["scope"], "live": {}, "successful_tool_times": []}
    monkeypatch.setattr(flow, "native_activity", activity)
    monkeypatch.setattr(flow.time, "sleep", lambda _: (tmp_path / "protected/observer/stop.json").write_bytes(canonical({"requested": True})))
    flow.observe(tmp_path)
    assert set(selected) == {"inactive-wake-user-003", "inactive-wake-user-004"} and len(selected) == 2
    assert not (tmp_path / "protected/observer/errors.jsonl").exists()
    assert flow.rows(tmp_path / "protected/observer/live.jsonl")[0]["kind"] == flow.INACTIVE_WAKE_KIND


def rotation_samples():
    samples = [{"sample_sequence": index + 1, "phase": phase,
        "host_before_observer_monotonic": start, "host_started_monotonic": start + .25,
        "host_finished_monotonic": start + .5, "host_after_observer_monotonic": start + .75}
        for index, (phase, start) in enumerate((("initial-idle", 1.), ("starting-wake", 3.), ("closing-wake", 5.),
            ("starting-inactive-wake", 7.), ("inactive-wake", 9.), ("closing-fault", 11.)))]
    status = {"started_monotonic": 0., "finished_monotonic": 12., "samples_written": len(samples), "completed": True}
    return samples, status


def test_rotation_sampling_covers_control_and_closing_phases_with_start_and_stop_edges():
    samples, status = rotation_samples()
    assert flow.rotation_sampling_check(samples, status) is True


@pytest.mark.parametrize("change", ("empty", "missing-middle", "missing-tail", "missing-head", "missing-fields",
    "boolean-sequence", "clock-backwards", "end-before-start", "nan", "infinite", "boolean-clock", "control-gap",
    "long-sample", "start-gap", "stop-gap", "sampler-write-failed", "sample-error", "status-missing"))
def test_rotation_rejects_missing_samples_invalid_clocks_or_long_gaps_in_any_phase(change):
    samples, status = rotation_samples()
    if change == "empty":
        samples = []
    elif change in {"missing-middle", "missing-tail", "missing-head"}:
        samples.pop({"missing-middle": 2, "missing-tail": -1, "missing-head": 0}[change])
    elif change == "missing-fields":
        samples[2].pop("host_after_observer_monotonic")
    elif change == "boolean-sequence":
        samples[0]["sample_sequence"] = True
    elif change in {"clock-backwards", "end-before-start", "nan", "infinite", "boolean-clock"}:
        field, replacement = {"clock-backwards": ("host_before_observer_monotonic", 1.),
            "end-before-start": ("host_finished_monotonic", 4.), "nan": ("host_started_monotonic", float("nan")),
            "infinite": ("host_after_observer_monotonic", float("inf")), "boolean-clock": ("host_before_observer_monotonic", True)}[change]
        samples[2][field] = replacement
    elif change == "control-gap":
        for row in samples[3:]:
            for key in ("host_before_observer_monotonic", "host_started_monotonic", "host_finished_monotonic", "host_after_observer_monotonic"):
                row[key] += 10
        status["finished_monotonic"] += 10
    elif change == "long-sample":
        samples[2]["host_after_observer_monotonic"] = 10.5
    elif change == "start-gap":
        status["started_monotonic"] = -5
    elif change == "stop-gap":
        status["finished_monotonic"] = 20
    elif change == "sampler-write-failed":
        status["completed"] = False
    elif change == "sample-error":
        samples[1]["error"] = "synthetic_failure"
    else:
        status = {}
    assert flow.rotation_sampling_check(samples, status) is False


def test_rotation_resource_log_write_failure_cannot_be_marked_as_completed(tmp_path, monkeypatch):
    (tmp_path / "protected/flow-resource-samples.jsonl").mkdir(parents=True)
    lab = CostFlowLab(tmp_path, plan(users=5, rotate_inactive_users=True))
    with pytest.raises(OSError):
        lab.sample_loop()
    assert lab.sampling_status["completed"] is False
    assert lab.sampling_status["samples_written"] == 0
    assert lab.sampling_status["finished_monotonic"] >= lab.sampling_status["started_monotonic"]


@pytest.mark.parametrize("users", (5, 10))
def test_rotation_resident_cli_retains_plan_and_prestart_refusal_without_creating_docker(tmp_path, monkeypatch, users):
    output = tmp_path / "new-refused-trial"
    monkeypatch.setattr(full_host, "validate_data_root", lambda *_: tmp_path)
    monkeypatch.setattr(full_host, "CostFlowLab", lambda *_: pytest.fail("常驻分配必须在调用 Docker 前拒绝"))
    monkeypatch.setattr("sys.argv", ["run_instance_cost_full.py", "run", "--trial-id", "rotation-refusal",
        "--pair-id", "rotation-pair", "--strategy", "resident", "--users", str(users),
        "--candidate-image", "sha256:" + "1" * 64, "--bench-image", "sha256:" + "2" * 64,
        "--services-image", "sha256:" + "3" * 64, "--model-name", "synthetic-model", "--seed", "101",
        "--rotate-inactive-users", "--output-root", str(output)])
    assert full_host.main() == 2
    value = flow.read(output / "protected/plan.json")
    refusal = flow.read(output / "protected/refusal.json")
    assert value["users"] == users and value["flow_variant"] == "inactive-user-rotation"
    assert value["fixed_allocations"][1]["counts"]["worker"] == users
    assert refusal["Docker_started"] is False and refusal["observed_RSS"] is None
    assert refusal["i25_complete"] is False


def native_record(time, event):
    return {"time": time, "task_id": "active-user-001", "scope": {"user_id": "synthetic"}, "stream_mode": "tasks", "data": event}


def test_live_activity_requires_original_native_start_and_paired_successful_tool(tmp_path):
    task = {"task_id": "active-user-001", "owner": "user-001", "kind": "active", "scope": {"user_id": "synthetic"}}
    raw = tmp_path / "users/user-001/evidence/active/raw-events.jsonl"
    trusted = tmp_path / "users/user-001/trusted/active/tool-events.jsonl"
    raw.parent.mkdir(parents=True)
    trusted.parent.mkdir(parents=True)
    raw.write_bytes(canonical(native_record("2026-10-20T00:00:00+00:00", {"id": "model-1", "name": "model", "input": {}})) + b"\n")
    trusted.write_bytes(canonical({"time": "2026-10-20T00:00:01+00:00", "scope": task["scope"],
        "phase": "finish", "call_id": "unpaired", "tool": "read_file", "status": 200}) + b"\n")
    first = flow.native_activity(tmp_path, task)
    assert first["live"] and first["successful_tool_times"] == []
    with raw.open("ab") as stream:
        stream.write(canonical(native_record("2026-10-20T00:00:02+00:00", {"id": "unknown-model", "result": []})) + b"\n")
    with pytest.raises(ValueError, match="native_task_finish_without_start"):
        flow.native_activity(tmp_path, task)


def observer_native_fixture(tmp_path):
    scope = {"experiment_id": "instance-cost", "variant": "trial-baseline-user-001",
        "user_id": "user-001", "session_id": "baseline-user-001"}
    task = {"task_id": "baseline-user-001", "owner": "user-001", "kind": "baseline", "scope": scope}
    raw = tmp_path / "users/user-001/evidence/baseline/raw-events.jsonl"
    trusted = tmp_path / "users/user-001/trusted/baseline/tool-events.jsonl"
    raw.parent.mkdir(parents=True)
    trusted.parent.mkdir(parents=True)
    events = [
        ("checkpoints", {"config": {}, "values": {"private": "must-not-enter-observer-errors"}}),
        ("tasks", {"id": "model-a", "name": "model", "input": {"private": "must-not-enter-observer-errors"}}),
        ("messages", ["must-not-enter-observer-errors", {}]),
        ("tasks", {"id": "model-a", "name": "model", "result": [], "error": None, "interrupts": []}),
        ("tasks", {"id": "tools-a", "name": "tools", "input": {}}),
        ("tasks", {"id": "tools-b", "name": "tools", "input": {}}),
        ("tasks", {"id": "tools-b", "name": "tools", "result": [], "error": None, "interrupts": []}),
        ("tasks", {"id": "tools-a", "name": "tools", "result": [], "error": None, "interrupts": []}),
        ("updates", {"model": {}}),
    ]
    raw_rows = [{"time": f"2026-10-20T00:00:0{index}+00:00", "task_id": task["task_id"],
        "scope": scope, "stream_mode": mode, "data": data} for index, (mode, data) in enumerate(events)]
    raw.write_bytes(b"".join(canonical(row) + b"\n" for row in raw_rows))
    tool_rows = [{"time": "2026-10-20T00:00:05+00:00", "scope": scope, "phase": "start",
        "call_id": "actual-call", "tool": "read_file", "arguments": {"private": "must-not-enter-observer-errors"}},
        {"time": "2026-10-20T00:00:06+00:00", "scope": scope, "phase": "finish", "call_id": "actual-call",
            "tool": "read_file", "status": 200, "result": "must-not-enter-observer-errors"}]
    trusted.write_bytes(b"".join(canonical(row) + b"\n" for row in tool_rows))
    return task, raw, trusted, raw_rows


def test_observer_accepts_actual_mixed_stream_shape_and_parallel_original_pairs(tmp_path):
    task, raw, _, original = observer_native_fixture(tmp_path)
    observed = flow.native_activity(tmp_path, task)
    assert observed["native_prefix_lines"] == len(original)
    assert len(observed["intervals"]) == 3 and observed["live"] == {}
    assert observed["successful_tool_times"] == ["2026-10-20T00:00:06+00:00"]
    assert str(raw) in observed["source_paths"]
    assert "must-not-enter-observer-errors" not in json.dumps(observed)


@pytest.mark.parametrize("change,reason", [("scope", "raw_scope_mismatch"), ("task", "raw_task_identity_mismatch"),
    ("missing-task", "raw_task_identity_mismatch"), ("duplicate-start", "native_task_start_duplicate"),
    ("finish-without-start", "native_task_finish_without_start"), ("tool-scope", "trusted_tool_scope_mismatch")])
def test_observer_keeps_scope_identity_pairing_rejections_with_safe_source_coordinates(tmp_path, change, reason):
    task, raw, trusted, records = observer_native_fixture(tmp_path)
    if change == "scope":
        records[0]["scope"] = {**task["scope"], "user_id": "user-002"}
    elif change == "task":
        records[0]["task_id"] = "baseline-user-002"
    elif change == "missing-task":
        del records[0]["task_id"]
    elif change == "duplicate-start":
        records.insert(2, deepcopy(records[1]))
    elif change == "finish-without-start":
        records[3]["data"]["id"] = "no-original-start"
    else:
        tools = flow.rows(trusted)
        tools[1]["scope"] = {**task["scope"], "session_id": "foreign-session"}
        trusted.write_bytes(b"".join(canonical(row) + b"\n" for row in tools))
    raw.write_bytes(b"".join(canonical(row) + b"\n" for row in records))
    with pytest.raises(flow.ObservationError) as caught:
        flow.native_activity(tmp_path, task)
    diagnostic = flow.observer_error_record(caught.value, phase="baseline", stage="native_activity", task=task)
    assert diagnostic["reason_code"] == reason and diagnostic["line"] >= 1
    assert diagnostic["source"] in {str(raw), str(trusted)}
    assert diagnostic["task_id"] == task["task_id"]
    assert "must-not-enter-observer-errors" not in json.dumps(diagnostic)


def test_live_jsonl_ignores_only_incomplete_suffix_including_unfinished_first_line(tmp_path):
    path = tmp_path / "live.jsonl"
    path.write_bytes(b'{"data":"still-being-written')
    assert flow.rows(path, partial=True) == []
    path.write_bytes(b'{"time":"original"}\n{"data":"still-being-written')
    assert flow.rows(path, partial=True) == [{"time": "original"}]
    with pytest.raises(ValueError, match='证据末行被截断'):
        flow.rows(path)
    path.write_bytes(b'{"time":"original"}\n{"data":"must-not-enter-observer-errors",}\n')
    with pytest.raises(flow.ObservationError) as caught:
        flow.rows(path, partial=True)
    assert caught.value.code == "source_json_invalid" and caught.value.line == 2
    assert "must-not-enter-observer-errors" not in str(caught.value)


def control_file(tmp_path, sequence, *, phase="baseline", kind="baseline", nonce=None):
    path = tmp_path / "protected/observer/controls" / f"control-{sequence:012d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"sequence": sequence, "phase": phase, "kind": kind}
    if nonce is not None:
        value["probe_nonce"] = nonce
    path.write_bytes(canonical(value))
    return path


def test_control_read_failure_is_diagnosed_and_preserved_without_killing_later_observations(tmp_path, monkeypatch):
    protected, target = tmp_path / "protected", tmp_path / "protected/observer"
    target.mkdir(parents=True)
    (target / "replies").mkdir()
    (protected / "tasks.json").write_bytes(canonical([]))
    control_file(tmp_path, 1)
    original_read = flow.observer_control
    phase_reads = 0
    sleeps = 0
    def read_phase(path):
        nonlocal phase_reads
        if Path(path).name == "control-000000000001.json":
            phase_reads += 1
            if phase_reads == 1:
                raise ValueError("private exception body must-not-enter-observer-errors")
        return original_read(path)
    def stop_after_second_iteration(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            (target / "stop.json").write_bytes(canonical({"requested": True}))
    monkeypatch.setattr(flow, "observer_control", read_phase)
    monkeypatch.setattr(flow.time, "sleep", stop_after_second_iteration)
    flow.observe(tmp_path)
    errors, live = flow.rows(target / "errors.jsonl"), flow.rows(target / "live.jsonl")
    assert len(errors) == len(live) == 1
    assert errors[0]["reason_code"] == "observer_value_error" and errors[0]["stage"] == "read_control"
    assert live[0]["phase"] == "baseline" and live[0]["tasks"] == []
    assert "must-not-enter-observer-errors" not in (target / "errors.jsonl").read_text()


def test_immutable_control_reader_uses_one_descriptor_and_rejects_unlinked_file(tmp_path, monkeypatch):
    path = control_file(tmp_path, 1, nonce="a" * 32)
    assert flow.observer_control(path) == {"sequence": 1, "phase": "baseline", "kind": "baseline", "probe_nonce": "a" * 32}
    original_open = flow.os.open
    def unlinked_after_open(name, flags, *args, **kwargs):
        descriptor = original_open(name, flags, *args, **kwargs)
        path.unlink()
        assert flow.os.fstat(descriptor).st_nlink == 0
        return descriptor
    monkeypatch.setattr(flow.os, "open", unlinked_after_open)
    with pytest.raises(flow.ObservationError, match="observer_control_descriptor_invalid"):
        flow.observer_control(path)


@pytest.mark.parametrize("change", ["hardlink", "symlink", "oversized", "invalid-json", "wrong-sequence",
    "boolean-sequence", "invalid-nonce", "invalid-kind", "extra-field", "wrong-name", "wrong-directory", "missing", "directory"])
def test_immutable_control_reader_still_refuses_invalid_material(tmp_path, change):
    path = control_file(tmp_path, 1, nonce="a" * 32)
    if change == "hardlink":
        flow.os.link(path, tmp_path / "alias.json")
    elif change == "symlink":
        path.rename(tmp_path / "original.json")
        path.symlink_to(tmp_path / "original.json")
    elif change in {"oversized", "invalid-json"}:
        path.write_bytes(b"x" * 4097 if change == "oversized" else b"{")
    elif change in {"wrong-name", "wrong-directory"}:
        other = path.with_name("control-1.json") if change == "wrong-name" else tmp_path / path.name
        path.rename(other)
        path = other
    elif change in {"missing", "directory"}:
        path.unlink()
        if change == "directory":
            path.mkdir()
    else:
        value = json.loads(path.read_bytes())
        field, replacement = {"wrong-sequence": ("sequence", 2), "boolean-sequence": ("sequence", True),
            "invalid-nonce": ("probe_nonce", "z" * 32), "invalid-kind": ("kind", "unknown"),
            "extra-field": ("unexpected", "no")}[change]
        value[field] = replacement
        path.write_bytes(canonical(value))
    with pytest.raises((ValueError, OSError)):
        flow.observer_control(path)


def run_empty_observer(tmp_path, monkeypatch, *, iterations):
    target = tmp_path / "protected/observer"
    (target / "replies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "protected/tasks.json").write_bytes(canonical([]))
    sleeps = 0
    def stop_after_iterations(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == iterations:
            (target / "stop.json").write_bytes(canonical({"requested": True}))
    monkeypatch.setattr(flow.time, "sleep", stop_after_iterations)
    flow.observe(tmp_path)
    return target


def test_observer_applies_consecutive_snapshots_and_binds_multiple_probes_across_phase_change(tmp_path, monkeypatch):
    control_file(tmp_path, 1)
    control_file(tmp_path, 2, nonce="a" * 32)
    control_file(tmp_path, 3, nonce="b" * 32)
    control_file(tmp_path, 4, phase="active", kind="active")
    control_file(tmp_path, 5, phase="active", kind="active", nonce="c" * 32)
    target = run_empty_observer(tmp_path, monkeypatch, iterations=5)
    records = flow.rows(target / "live.jsonl")
    assert [row["control_sequence"] for row in records] == [1, 2, 3, 4, 5]
    assert not (target / "errors.jsonl").exists()
    for nonce, sequence, phase in [("a" * 32, 2, "baseline"), ("b" * 32, 3, "baseline"), ("c" * 32, 5, "active")]:
        reply = json.loads((target / "replies" / (nonce + ".json")).read_bytes())
        assert reply["probe_nonce"] == nonce and reply["control_sequence"] == sequence
        assert reply["phase"] == reply["kind"] == phase
        assert reply in records


def test_observer_never_skips_missing_control_sequence_or_answers_later_nonce(tmp_path, monkeypatch):
    control_file(tmp_path, 1)
    control_file(tmp_path, 3, phase="active", kind="active", nonce="a" * 32)
    target = run_empty_observer(tmp_path, monkeypatch, iterations=3)
    assert [row["control_sequence"] for row in flow.rows(target / "live.jsonl")] == [1, 1, 1]
    assert list((target / "replies").iterdir()) == []


def test_observer_rejects_reused_nonce_in_a_new_control_snapshot(tmp_path, monkeypatch):
    control_file(tmp_path, 1, nonce="a" * 32)
    control_file(tmp_path, 2, phase="active", kind="active", nonce="a" * 32)
    target = run_empty_observer(tmp_path, monkeypatch, iterations=2)
    assert flow.rows(target / "errors.jsonl")[0]["reason_code"] == "observer_probe_nonce_reused"
    reply = json.loads((target / "replies" / ("a" * 32 + ".json")).read_bytes())
    assert reply["control_sequence"] == 1 and reply["phase"] == "baseline"


@pytest.mark.parametrize("kind", ["active", None])
@pytest.mark.parametrize("change", [None, "nonce", "sequence", "boolean-sequence", "phase", "kind", "missing-kind"])
def test_host_probe_strictly_matches_current_control_binding(tmp_path, monkeypatch, change, kind):
    lab = CostFlowLab(tmp_path, plan())
    lab.phase_to("active", kind)
    nonce = "a" * 32
    monkeypatch.setattr(full_host, "uuid4", lambda: SimpleNamespace(hex=nonce))
    replies = tmp_path / "protected/observer/replies"
    replies.mkdir()
    reply = {"probe_nonce": nonce, "control_sequence": 2, "phase": "active", "kind": kind}
    if change == "missing-kind":
        del reply["kind"]
    elif change:
        key, value = {"nonce": ("probe_nonce", "b" * 32), "sequence": ("control_sequence", 1),
            "boolean-sequence": ("control_sequence", True), "phase": ("phase", "baseline"), "kind": ("kind", "baseline")}[change]
        reply[key] = value
    (replies / (nonce + ".json")).write_bytes(canonical(reply))
    if change:
        with pytest.raises(ValueError, match="observer_probe_nonce_changed" if change == "nonce" else "observer_probe_binding_changed"):
            lab.probe()
    else:
        assert lab.probe() == reply
    controls = sorted((tmp_path / "protected/observer/controls").iterdir())
    assert [path.name for path in controls] == ["control-000000000001.json", "control-000000000002.json"]
    assert flow.observer_control(controls[1]) == {"sequence": 2, "phase": "active", "kind": kind, "probe_nonce": nonce}


def test_host_control_never_replaces_an_already_published_sequence(tmp_path):
    lab = CostFlowLab(tmp_path, plan())
    lab.phase_to("baseline", "baseline")
    path = tmp_path / "protected/observer/controls/control-000000000001.json"
    original = path.read_bytes()
    lab.control_sequence = 0
    with pytest.raises(ValueError, match="host_observer_control_already_exists"):
        lab.phase_to("active", "active")
    assert path.read_bytes() == original and lab.phase == "baseline"
    assert not path.with_name(path.name + ".pending").exists()


def test_host_probe_keeps_original_eight_second_deadline(tmp_path, monkeypatch):
    lab = CostFlowLab(tmp_path, plan())
    lab.phase_to("active", "active")
    ticks = iter((0., 9.))
    monkeypatch.setattr(full_host.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="observer_probe_timeout"):
        lab.probe()


def test_probe_reply_is_complete_before_published_and_never_replaced(tmp_path):
    path = tmp_path / "nonce.json"
    flow.publish_reply(path, {"probe_nonce": "a" * 32, "tasks": []})
    original = path.read_bytes()
    assert json.loads(original)["probe_nonce"] == "a" * 32
    assert not path.with_name(path.name + ".pending").exists()
    with pytest.raises(flow.ObservationError, match="observer_reply_already_exists"):
        flow.publish_reply(path, {"probe_nonce": "b" * 32})
    assert path.read_bytes() == original


def test_host_requires_explicit_data_root_before_output_or_docker(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_LAB_DATA_ROOT", raising=False)
    output = tmp_path / "not-created"
    with pytest.raises(ValueError, match="^host_data_root_missing$"):
        full_host.validate_data_root(output)
    assert not output.exists()


def test_host_data_root_matches_explicit_host_config_and_fresh_child_output(tmp_path, monkeypatch):
    root = tmp_path / "experiment-data"
    root.mkdir()
    output = root / "runs/new-trial"
    monkeypatch.setenv("HARNESS_LAB_DATA_ROOT", str(root))
    monkeypatch.setattr(full_host, "load_config", lambda _: SimpleNamespace(environment="host", host_data_root=root))
    assert full_host.validate_data_root(output) == root.resolve()
    assert not output.exists()


@pytest.mark.parametrize("change,reason", [("relative", "host_data_root_invalid"), ("archive", "host_data_root_invalid"),
    ("outside", "host_output_root_outside_data_root"), ("equal", "host_output_root_outside_data_root"),
    ("config-root", "host_data_root_config_mismatch"), ("container-config", "host_data_root_config_mismatch")])
def test_host_rejects_implicit_mismatched_or_archive_data_roots(tmp_path, monkeypatch, change, reason):
    root, output = tmp_path / "data", tmp_path / "data/runs/new-trial"
    root.mkdir()
    environment, configured = "host", root
    value = str(root)
    if change == "relative":
        value = "relative-data"
    elif change == "archive":
        value = str(root / "old")
    elif change == "outside":
        output = tmp_path / "different/runs/new-trial"
    elif change == "equal":
        output = root
    elif change == "config-root":
        configured = tmp_path / "different"
    elif change == "container-config":
        environment = "container"
    monkeypatch.setenv("HARNESS_LAB_DATA_ROOT", value)
    monkeypatch.setattr(full_host, "load_config", lambda _: SimpleNamespace(environment=environment, host_data_root=configured))
    with pytest.raises(ValueError, match="^" + reason + "$"):
        full_host.validate_data_root(output)


def test_host_baseline_cli_reports_missing_data_root_before_evidence_or_lab(tmp_path, monkeypatch):
    output = tmp_path / "not-created"
    monkeypatch.delenv("HARNESS_LAB_DATA_ROOT", raising=False)
    monkeypatch.setattr("sys.argv", ["run_instance_cost_full.py", "run-baseline", "--trial-id", "offline-new",
        "--pair-id", "offline-pair", "--strategy", "on-demand", "--users", "2",
        "--candidate-image", "sha256:" + "1" * 64, "--bench-image", "sha256:" + "2" * 64,
        "--services-image", "sha256:" + "3" * 64, "--model-name", "offline-model", "--seed", "101",
        "--output-root", str(output)])
    monkeypatch.setattr(full_host, "CostFlowLab", lambda *_: pytest.fail("不得创建 Docker 实验实例"))
    with pytest.raises(ValueError, match="^host_data_root_missing$"):
        full_host.main()
    assert not output.exists()


def test_host_error_classification_does_not_expose_exception_body_or_command_credentials():
    assert full_host.host_failure_reason(TimeoutError("observer_probe_timeout")) == "observer_probe_timeout"
    error = subprocess.TimeoutExpired(["docker", "private-credential-must-not-enter-diagnostic"], 8)
    assert full_host.host_failure_reason(error) == "host_docker_command_timeout"
    diagnostic = {"error_type": "ValueError", "reason_code": full_host.host_failure_reason(
        ValueError("private exception must-not-enter-diagnostic"))}
    assert diagnostic["reason_code"] == "host_unclassified_failure"
    assert "private" not in json.dumps(diagnostic) and "must-not-enter" not in json.dumps(diagnostic)


def overlap_records(monkeypatch):
    activity = {key: {"intervals": [["2026-10-20T00:00:00+00:00", "2026-10-20T00:00:10+00:00"]],
        "successful_tool_times": ["2026-10-20T00:00:05+00:00"]} for key in ("a", "b")}
    monkeypatch.setattr(flow, "native_activity", lambda root, task: activity[task["task_id"]])
    samples, observer = [], []
    for index in (1, 2):
        before = {"phase": "active", "time": f"2026-10-20T00:00:0{index}+00:00", "probe_nonce": str(index) * 32}
        after = {"phase": "active", "time": f"2026-10-20T00:00:0{index + 1}+00:00", "probe_nonce": str(index + 2) * 32}
        observer.extend([before, after])
        samples.append({"phase": "active", "observer_before": before, "observer_after": after,
            "host_before_observer_monotonic": index * 4, "host_started_monotonic": index * 4 + 1,
            "host_finished_monotonic": index * 4 + 2, "host_after_observer_monotonic": index * 4 + 3})
    return activity, samples, observer


def test_actual_overlap_requires_both_tools_and_two_bracketed_raw_samples(monkeypatch):
    _, samples, observer = overlap_records(monkeypatch)
    result = flow.effective_overlap("unused", [{"task_id": "a"}, {"task_id": "b"}], samples, observer)
    assert result["passed"] is True and result["samples_inside_native_overlap"] == 2


@pytest.mark.parametrize("change", ("serial", "no-b-progress", "one-sample", "unrecorded-probe", "host-order", "outside-native-window", "duplicate-sample"))
def test_container_coexistence_or_synthetic_probe_cannot_replace_real_overlap(monkeypatch, change):
    activity, samples, observer = overlap_records(monkeypatch)
    if change == "serial":
        activity["b"]["intervals"] = [["2026-10-20T00:00:11+00:00", "2026-10-20T00:00:20+00:00"]]
    elif change == "no-b-progress":
        activity["b"]["successful_tool_times"] = []
    elif change == "one-sample":
        samples.pop()
    elif change == "unrecorded-probe":
        observer.clear()
    elif change == "host-order":
        samples[0]["host_after_observer_monotonic"] = 0
    elif change == "outside-native-window":
        samples[0]["observer_after"] = {**samples[0]["observer_after"], "time": "2026-10-20T00:00:30+00:00"}
        observer.append(samples[0]["observer_after"])
    elif change == "duplicate-sample":
        samples[1] = deepcopy(samples[0])
    assert flow.effective_overlap("unused", [{"task_id": "a"}, {"task_id": "b"}], samples, observer)["passed"] is False


def business_fixture(tmp_path, *, strategy="on-demand"):
    home = tmp_path / "users/user-001/evidence/active"
    home.mkdir(parents=True)
    value = {"values": {"messages": [{"content": "actual-synthetic-completed-business-state"}]},
        "next": [], "tasks": [], "interrupts": [],
        "config": {"configurable": {"thread_id": "active-user-001", "checkpoint_id": "original-checkpoint"}}}
    summaries = []
    for index in (1, 2):
        name = f"native-state-synthetic-{index}.json"
        path = home / name
        path.write_bytes(canonical(value))
        summaries.append({"scope": {"user_id": "user-001"}, "checkpoint_id": "original-checkpoint",
            "objects": {"pid": 77}, "source": {"path": name, "sha256": sha(path.read_bytes()),
                "public_api": "CompiledStateGraph.aget_state"}})
    item = {"owner": "user-001", "read_succeeded": True, "mode": "cold_same_store" if strategy == "on-demand" else "same_retained_objects",
        "worker_after_id": "b" * 64 if strategy == "on-demand" else "a" * 64, "original": summaries[0], "restored": summaries[1]}
    timings = [{"owner": "user-001", "kind": "active", "worker_id": "a" * 64}]
    return item, timings, home, value


@pytest.mark.parametrize("strategy", ("resident", "on-demand"))
def test_same_original_naturally_completed_business_state_is_checked(tmp_path, strategy):
    item, timings, _, _ = business_fixture(tmp_path, strategy=strategy)
    assert flow.verify_business_read(tmp_path, plan(strategy=strategy), item, timings) is True


@pytest.mark.parametrize("change", ("checkpoint", "messages", "pending", "path", "resident-object", "same-cold-container"))
def test_recreated_sentinel_or_relabelled_container_cannot_pass_business_history(tmp_path, change):
    strategy = "resident" if change == "resident-object" else "on-demand"
    item, timings, home, value = business_fixture(tmp_path, strategy=strategy)
    if change in {"checkpoint", "messages", "pending"}:
        if change == "checkpoint":
            value["config"]["configurable"]["checkpoint_id"] = "other-checkpoint"
        elif change == "messages":
            value["values"]["messages"] = [{"content": "new-application-sentinel"}]
        else:
            value["next"] = ["model"]
        path = home / item["restored"]["source"]["path"]
        path.write_bytes(canonical(value))
        item["restored"]["source"]["sha256"] = sha(path.read_bytes())
    elif change == "path":
        item["restored"]["source"]["path"] = "../foreign.json"
    elif change == "resident-object":
        item["restored"]["objects"] = {"pid": 88}
    else:
        item["worker_after_id"] = timings[0]["worker_id"]
    assert flow.verify_business_read(tmp_path, plan(strategy=strategy), item, timings) is False


@pytest.mark.parametrize("change", ("none", "token", "cap"))
def test_original_proxy_token_and_cap_are_not_replaceable(tmp_path, change):
    home = tmp_path / "users/user-001/budgets/fault"
    home.mkdir(parents=True)
    (home / "run.token").write_text("original-local-token\n")
    token_hash = sha(b"original-local-token")
    with sqlite3.connect(home / "calls.sqlite") as db:
        db.execute("CREATE TABLE budget (id INTEGER, token_hash TEXT, cap INTEGER)")
        db.execute("CREATE TABLE calls (id INTEGER, started_at TEXT, path TEXT, outcome TEXT, upstream_status INTEGER)")
        db.execute("INSERT INTO budget VALUES (1,?,?)", (token_hash, 21 if change == "cap" else 20))
        db.execute("INSERT INTO calls VALUES (1,'2026-10-20T00:00:00+00:00','/chat/completions','forwarded',200)")
    if change == "token":
        (home / "run.token").write_text("new-budget-token\n")
    task = {"owner": "user-001", "kind": "fault", "model_token_sha256": token_hash}
    assert flow.budget(tmp_path, task)["binding_valid"] is (change == "none")


def test_next_bench_is_refused_when_two_workers_already_use_active_total(tmp_path):
    lab = CostFlowLab(tmp_path, plan())
    rows = [{"memory_limit": int(5.5 * 1024**3), "cpu_nano": CPU, "state": {"Running": True}}]
    lab.observations = lambda: rows
    with pytest.raises(RuntimeError, match='以免超过'):
        lab.reserve("bench")


def bench_exit_record(*, code=0):
    return {"id": "b" * 64, "name": "/hl-ic-cost-calibration-bench-1", "role": "bench",
        "image_id": "sha256:" + "2" * 64, "memory_limit": CAPS["bench"][0], "cpu_nano": CAPS["bench"][1],
        "state": {"Status": "exited", "Running": False, "Pid": 0, "ExitCode": code,
            "OOMKilled": False, "Dead": False, "Paused": False, "Restarting": False, "Error": ""}}


def fake_grade_bench(tmp_path, monkeypatch, *, passed=True, cli_exit=0, observed=None, verdict_body=None):
    """使用显式 Docker 替身测试真实 check=False 宿主路径。"""
    (tmp_path / "protected").mkdir()
    lab = CostFlowLab(tmp_path, plan())
    record = deepcopy(observed if observed is not None else bench_exit_record(code=cli_exit))
    created, commands = [], []
    monkeypatch.setattr(lab, "observations", lambda: [record] if created else [])
    monkeypatch.setattr(lab, "reserve", lambda role: None)
    monkeypatch.setattr(lab, "pin", lambda identity, role, image: lab.registered.update({identity: {"role": role}}))
    body = canonical({"cost_subset_passed": passed}) + b"\n" if verdict_body is None else verdict_body
    def call(command, **kwargs):
        commands.append(command)
        if command[1] == "create":
            created.append(True)
            return subprocess.CompletedProcess(command, 0, "b" * 64 + "\n", "")
        if command[1] == "start":
            (tmp_path / "protected/flow-verdict.json").write_bytes(body)
            return subprocess.CompletedProcess(command, cli_exit, "synthetic grade output\n", "")
        if command[1] == "rm":
            return subprocess.CompletedProcess(command, 0, "b" * 64 + "\n", "")
        raise AssertionError("非预期合成 Docker 命令")
    monkeypatch.setattr(lab, "call", call)
    return lab, commands, body


@pytest.mark.parametrize("passed,code", ((True, 0), (False, 1)))
def test_grade_allows_only_healthy_exit_matching_the_original_boolean(tmp_path, monkeypatch, passed, code):
    lab, commands, body = fake_grade_bench(tmp_path, monkeypatch, passed=passed, cli_exit=code)
    assert lab.bench("grade", check=False) == code
    assert (tmp_path / "protected/flow-verdict.json").read_bytes() == body
    integrity = json.loads((tmp_path / "protected/grade-flow-process-integrity.json").read_text())
    assert integrity["passed"] is True and integrity["expected_exit"] == code
    assert integrity["verdict_source"]["sha256"] == sha(body)
    assert commands[-1] == ["docker", "rm", "b" * 64]


@pytest.mark.parametrize("change", (
    "written-pass-then-exit1", "written-fail-then-exit0", "cli-docker-disagree", "oom", "dead", "paused",
    "restarting", "runtime-error", "still-running", "pid-remains", "image", "id", "name", "memory", "cpu",
))
def test_written_verdict_does_not_bypass_grade_process_integrity(tmp_path, monkeypatch, change):
    record = bench_exit_record()
    passed, cli_exit = True, 0
    if change == "written-pass-then-exit1":
        cli_exit, record["state"]["ExitCode"] = 1, 1
    elif change == "written-fail-then-exit0":
        passed = False
    elif change == "cli-docker-disagree":
        record["state"]["ExitCode"] = 1
    elif change in {"oom", "dead", "paused", "restarting"}:
        field = {"oom": "OOMKilled", "dead": "Dead", "paused": "Paused", "restarting": "Restarting"}[change]
        record["state"][field] = True
    elif change == "runtime-error":
        record["state"]["Error"] = "synthetic daemon error"
    elif change == "still-running":
        record["state"].update(Status="running", Running=True)
    elif change == "pid-remains":
        record["state"]["Pid"] = 42
    elif change == "image":
        record["image_id"] = "sha256:" + "9" * 64
    elif change == "id":
        record["id"] = "c" * 64
    elif change == "name":
        record["name"] = "/other-grader"
    elif change == "memory":
        record["memory_limit"] += 1
    else:
        record["cpu_nano"] += 1
    lab, commands, body = fake_grade_bench(tmp_path, monkeypatch, passed=passed, cli_exit=cli_exit, observed=record)
    with pytest.raises(RuntimeError, match='进程完整性失败'):
        lab.bench("grade", check=False)
    assert (tmp_path / "protected/flow-verdict.json").read_bytes() == body
    integrity = json.loads((tmp_path / "protected/grade-flow-process-integrity.json").read_text())
    assert integrity["passed"] is False
    assert not any(command[1] == "rm" for command in commands)


@pytest.mark.parametrize("body", (b'{"cost_subset_passed":1}\n', b'{"passed":true}\n', b'not-json\n'))
def test_grade_exit1_cannot_replace_missing_or_malformed_boolean_verdict(tmp_path, monkeypatch, body):
    lab, _, original = fake_grade_bench(tmp_path, monkeypatch, cli_exit=1, verdict_body=body)
    with pytest.raises(RuntimeError, match='进程完整性失败'):
        lab.bench("grade", check=False)
    assert (tmp_path / "protected/flow-verdict.json").read_bytes() == original


def fault_exit_fixture():
    observed = bench_exit_record(code=137)
    observed["state"].update(StartedAt="2026-10-20T00:00:01.000000000Z", FinishedAt="2026-10-20T00:00:10.000000000Z")
    return {"injected": True, "victim_id": observed["id"], "after_kill": observed,
        "after_observer": {"time": "2026-10-20T00:00:30+00:00"}}


def test_B_progress_between_actual_exit_and_later_probe_counts_as_after_kill():
    fault = fault_exit_fixture()
    activity = {"intervals": [["2026-10-20T00:00:09+00:00", "2026-10-20T00:00:11+00:00"]]}
    value = flow.progress_after_worker_exit(fault, activity)
    assert value["passed"] is True
    assert value["source"] == "after_kill.state.FinishedAt" and value["later_probe_time_used_as_exit"] is False


@pytest.mark.parametrize("change", ("before-exit", "same-instant", "missing-finish", "unqualified-clock", "zero-time", "wrong-id", "not-exited"))
def test_B_progress_requires_the_real_same_clock_worker_exit_boundary(change):
    fault = fault_exit_fixture()
    activity = {"intervals": [["2026-10-20T00:00:09+00:00", "2026-10-20T00:00:11+00:00"]]}
    if change == "before-exit":
        activity["intervals"][0][1] = "2026-10-20T00:00:09.5+00:00"
    elif change == "same-instant":
        activity["intervals"][0][1] = "2026-10-20T00:00:10+00:00"
    elif change == "missing-finish":
        fault["after_kill"]["state"].pop("FinishedAt")
    elif change == "unqualified-clock":
        fault["after_kill"]["state"]["FinishedAt"] = "2026-10-20T00:00:10"
    elif change == "zero-time":
        fault["after_kill"]["state"]["FinishedAt"] = "0001-01-01T00:00:00Z"
    elif change == "wrong-id":
        fault["victim_id"] = "f" * 64
    else:
        fault["after_kill"]["state"].update(Status="running", Running=True)
    assert flow.progress_after_worker_exit(fault, activity)["passed"] is False
