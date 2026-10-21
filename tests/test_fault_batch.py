"""Host batch boundaries using synthetic files and subprocess/Docker doubles.

No candidate, model or Docker operation is performed by these tests. They do
not establish recovery capability; real I23 calibration and chains remain due.
"""
from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import subprocess

import pytest

from runner.config import load_config
from scripts import run_fault_batch as host

WORKER = "sha256:" + "a" * 64
BENCH = "sha256:" + "b" * 64
SERVICES = "sha256:" + "c" * 64
IMAGES = {"candidate_image_id": WORKER, "bench_image_id": BENCH, "services_image_id": SERVICES}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def file_row(root, relative, *, mode=False):
    path = root / relative
    row = {"path": relative, "type": "file", "size": path.stat().st_size, "sha256": host.digest(path)}
    if mode:
        row["mode"] = path.stat().st_mode & 0o777
    return row


def export(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


class DockerDouble:
    def __init__(self, config):
        self.config, self.env = config, {"SYNTHETIC_SECRET": "never-public-key"}
        self.remaining, self.drift = [], False
        self.commands = []

    def call(self, command, **kwargs):
        self.commands.append(command)
        assert command[:3] == ["docker", "image", "inspect"]
        value = {"candidate:mutable": WORKER, "bench:mutable": BENCH, "services:mutable": SERVICES}[command[-1]]
        if self.drift and command[-1] == "candidate:mutable":
            value = "sha256:" + "d" * 64
        return subprocess.CompletedProcess(command, 0, value + "\n", "")

    def containers(self):
        return self.remaining


@pytest.fixture
def batch(tmp_path, monkeypatch):
    config = replace(load_config(), host_data_root=tmp_path / "data")
    root = tmp_path / "repository"
    monkeypatch.setattr(host, "ROOT", root)
    for relative in host.HOST_MATERIALS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("frozen host material " + relative)
    context = root / "external/build-context/fault"
    source_path, adapter_path = "sources/framework/runtime.py", "harness-lab/fault.py"
    for relative in (source_path, adapter_path):
        path = context / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixed bytes")
    source_rows = [file_row(context, source_path, mode=True)]
    source = {"schema": "harness-lab-source-manifest/1", "candidate": "deepagents_langgraph",
              "files": source_rows, "export_sha256": export(source_rows)}
    write(context / "SOURCE_MANIFEST.json", source)
    adapter_rows = [file_row(context, adapter_path)]
    adapter = {"schema": "harness-lab-adapter-manifest/1", "candidate": "deepagents_langgraph",
        "variant": "fault-prewrite-bridge", "source_manifest_sha256": host.digest(context / "SOURCE_MANIFEST.json"),
        "files": adapter_rows, "export_sha256": export(adapter_rows)}
    write(context / "LAB_ADAPTER_MANIFEST.json", adapter)
    origin = root / "adapters/deepagents_langgraph/fault/bridge.py"
    origin.parent.mkdir(parents=True, exist_ok=True)
    origin.write_bytes((context / adapter_path).read_bytes())
    overlay = {"schema": "harness-lab-fault-bridge-manifest/1", "candidate": "deepagents_langgraph",
        "variant": "fault-prewrite-bridge", "adapter_export_sha256": adapter["export_sha256"],
        "sources": [{"repository_path": origin.relative_to(root).as_posix(), "image_material_path": adapter_path,
                     "size": origin.stat().st_size, "sha256": host.digest(origin)}]}
    write(context / "LAB_FAULT_BRIDGE_MANIFEST.json", overlay)
    (context / "Dockerfile").write_text("FROM pinned")
    args = Namespace(candidate="deepagents_langgraph", prefix="boundary", batch_id="boundary-batch",
        phase="baseline-101", image_tag="candidate:mutable", bench_image="bench:mutable",
        services_image="services:mutable", materials_context=context)
    value = host.FaultBatch(args, lab=DockerDouble(config))
    value.record["image_ids"] = value.image_ids()
    value.record["materials"] = value.material_hashes()
    return value


def state(code):
    return {"Status": "exited", "Pid": 0, "ExitCode": code, "Running": False, "OOMKilled": False,
            "Dead": False, "Paused": False, "Restarting": False, "Error": ""}


def evidence(batch, item, *, passed=True, gap=False):
    """Synthesize the host reader's input, without running any grader code."""
    paths = batch.paths(item)
    directory = paths.run
    scope = {"experiment_id": "fault-recovery", "variant": item["chain_id"], "user_id": "alice", "session_id": "main"}
    identity = {"candidate": batch.args.candidate, **item}
    request = paths.fixture / "candidate-request.json"
    write(request, {"original": "request, never resubmitted by batch"})
    input_ = {"candidate": batch.args.candidate, "chain_id": item["chain_id"], "scope": scope,
        "seed": item["seed"], "base_task": "tools", "expected_answer_delivered": False,
        "task_sha256": batch.record["materials"]["host_material_hashes"]["configs/tasks/tools.yaml"]}
    write(directory / "input.json", input_)
    write(directory / "chain.json", {"schema": "harness-lab-fault-chain/1", **identity, "scope": scope,
        "task_sha256": input_["task_sha256"], "input_sha256": host.digest(directory / "input.json"),
        "request_sha256": host.digest(request)})
    pins = {"schema": "harness-lab-fault-pins/1", **identity, "scope": scope, "date": batch.day,
        **IMAGES, "active_limit_seconds": 300, "model_call_cap": 20, "model_name": "selected-model",
        **{key: batch.record["materials"][key] for key in (
            "source_export_sha256", "adapter_export_sha256", "fault_overlay_sha256", "adapter_helper_sha256")}}
    write(directory / "trusted/pins.json", pins)
    outcome = {"schema": "harness-lab-fault-orchestration/1", "chain_id": item["chain_id"], "scope": scope,
        "errors": [], "timed_out": False, "used_active_seconds": 45, "native_start_available": True}
    for name in ("orchestration.json", "trusted/orchestration-final.json"):
        write(directory / name, outcome)
    write(directory / "cleanup.json", {"remaining": [], "finished_at": "complete"})
    write(directory / "model-calls.json", {"cap": 20, "count": 4, "in_flight": []})
    (directory / "resource-samples.jsonl").write_text('{"synthetic":true}\n')
    write(directory / "trusted/resource-summary.json", {"passed": True, "sample_count": 2,
        "samples_sha256": host.digest(directory / "resource-samples.jsonl")})
    write(directory / "business-verdict.json", {"passed": passed})
    write(directory / "operation-analysis.json", {"operations": []})
    worker = {"chain_id": item["chain_id"], "scope": scope, "attempt_id": "worker-a", "errors": [], "timed_out": False,
        "before_start": {"id": "d" * 64, "image_id": WORKER},
        "after_exit": {"id": "d" * 64, "image_id": WORKER, "state": state(0)}}
    actions = [("prepare", 0)]
    checks = set(host.REQUIRED_CHECKS)
    if item["fault"] == "kill_before_write":
        checks.add("original_budget_and_tool_prefix")
        write(directory / "trusted/gate-hit.json", {"synthetic": "bound hit"})
        write(directory / "trusted/gate-signal.json", {"chain_id": item["chain_id"], "scope": scope,
            "attempt_id": "worker-a", "observed": True, "snapshot_sha256": host.digest(directory / "trusted/gate-hit.json")})
        write(directory / "trusted/gate-after-kill.json", {"gate": {"state": "abandoned"}, "unsettled_dispatches": []})
        boundary = {"chain_id": item["chain_id"], "scope": scope, "worker_id": "d" * 64,
            "pause_index_used": False, "native_state_explicitly_flushed": False,
            "gate_snapshot_sha256": host.digest(directory / "trusted/gate-after-kill.json")}
        write(directory / "trusted/crash-boundary.json", boundary)
        worker.update({"process_exit": 137, "attach_exit": 137,
            "kill": {"signal": "SIGKILL", "exit_code": 0, "container_id": "d" * 64,
                     "gate_signal_sha256": host.digest(directory / "trusted/gate-signal.json")},
            "deleted": {"id_absent": True, "name_absent": True}})
        worker["after_exit"]["state"] = state(137)
        if gap:
            write(directory / "trusted/native-recovery-gap.json", {"schema": "harness-lab-native-fault-gap/1",
                "status": "not_implemented", "candidate": batch.args.candidate, "scope": scope,
                "chain_id": item["chain_id"], "candidate_image_id": WORKER,
                "crash_boundary_sha256": host.digest(directory / "trusted/crash-boundary.json"),
                "new_prompt_submitted": False, "business_actions_performed_by_bench": False})
            actions.append(("bind-crash-resume", 3))
        else:
            checks.add("original_native_crash_recovery")
            other = deepcopy(worker)
            other["attempt_id"] = "worker-b"
            other["before_start"]["id"] = other["after_exit"]["id"] = "e" * 64
            other["after_exit"]["state"] = state(0)
            write(directory / "trusted/worker-b.json", other)
            actions.append(("bind-crash-resume", 0))
    write(directory / "trusted/worker-a.json", worker)
    write(directory / "trusted/service-host.json", {name: {"image_id": SERVICES} for name in ("fixtures", "tools", "model-proxy")})
    actions.append(("grade", 0 if passed else 1))
    for sequence, (action, code) in enumerate(actions):
        name = f"harness-lab-fault-bench-{item['chain_id']}-{sequence}"
        after = {"id": format(sequence + 1, "064x"), "name": "/" + name,
            "image_id": BENCH, "memory_limit": 512 * 1024**2, "cpu_nano": 500000000, "state": state(code)}
        write(directory / f"trusted/bench-process-{sequence:04d}.json", {"name": name,
            "action": action, "exit_code": code, "after_exit": after})
        if gap and action == "bind-crash-resume":
            created = deepcopy(after)
            created["state"] = {**state(0), "Status": "created"}
            write(directory / f"trusted/bench-registration-{sequence:04d}.json", {"name": name,
                "id": created["id"], "action": action, "image_id": BENCH, "scope": scope,
                "chain_id": item["chain_id"], "registered_at": "2026-10-20T00:00:00+00:00",
                "before_start": created, "crash_boundary_sha256": host.digest(directory / "trusted/crash-boundary.json")})
    axes = {"task_result": {"passed": passed, "source": "business-verdict.json"}, "injection_valid": True,
        "budget_and_process": {"passed": True, "model_calls": 4, "active_limit_seconds": 300, "model_call_cap": 20},
        "replay_behavior": {"operations_file": "operation-analysis.json", "server_idempotency_is_candidate_no_replay": False,
                            "reissued_after_commit": 0}, "handling_outcome": "automatic_completed" if passed else "incomplete"}
    verdict = {"schema": "harness-lab-fault-verdict/1", **identity, "axes": axes, "passed": passed,
        "checks": [{"name": name, "passed": passed if name == "native_terminal" else True} for name in sorted(checks)]}
    write(directory / "verdict.json", verdict)
    return directory, verdict


@pytest.mark.parametrize(("phase", "count"), [("baseline-101", 2), ("smoke", 4), ("formal", 24), ("baseline-rest", 4), ("final", 8)])
def test_matrix_counts_and_unique_independent_chains(phase, count):
    rows = host.plan(phase, "fixed")
    assert len(rows) == len({row["chain_id"] for row in rows}) == count
    assert all(host.identifier(row["chain_id"]) == row["chain_id"] for row in rows)


def test_phases_never_reuse_chain_ids_even_when_inputs_overlap():
    rows = [row for phase in host.PHASES for row in host.plan(phase, "fixed")]
    assert len(rows) == len({row["chain_id"] for row in rows}) == 42
    assert {row["write_mode"] for row in host.plan("smoke", "fixed")} == {"non_idempotent"}
    assert {row["fault"] for row in host.plan("final", "fixed")} == {"kill_before_write", "drop_receipt_once"}


def test_child_receives_exact_ids_not_mutable_tags(batch):
    command = batch.command(batch.plan[0])
    for flag, value in (("--image-tag", WORKER), ("--bench-image", BENCH), ("--services-image", SERVICES)):
        assert command[command.index(flag) + 1] == value
    assert "candidate:mutable" not in command
    assert "run_fault_recovery.py" in command[1]


@pytest.mark.parametrize("drift", ["image", "source", "adapter", "helper", "task", "host", "failure_decoder"])
def test_frozen_material_drift_stops_before_another_chain(batch, drift):
    if drift == "image":
        batch.lab.drift = True
    else:
        path = {"source": batch.context / "sources/framework/runtime.py", "adapter": batch.context / "harness-lab/fault.py",
            "helper": host.ROOT / "adapters/deepagents_langgraph/pause_recovery.py", "task": host.ROOT / "configs/tasks/tools.yaml",
            "host": host.ROOT / "scripts/run_fault_recovery.py", "failure_decoder": host.ROOT / "runner/fault_outcome.py"}[drift]
        path.write_text("changed bytes")
    with pytest.raises(host.BatchStopped):
        batch.assert_frozen()


def test_valid_complete_outcome_retained(batch):
    evidence(batch, batch.plan[0])
    row = batch.completed_evidence(batch.plan[0], 0)
    assert row["passed"] is True and row["native_recovery_gap"] is False
    assert set(row["axes"]) == host.AXES


@pytest.mark.parametrize('business_passed', [False, True])
@pytest.mark.parametrize('change', [None, 'sources_changed', 'no_native_audit', 'bad_blob_hash',
    'helper_rejects', 'success_exit1', 'attach_exit', 'boolean_attach', 'oom'])
def test_exit1_requires_independent_native_and_resource_bindings_and_never_implies_success(batch, monkeypatch, business_passed, change):
    item = batch.plan[0]
    directory, verdict = evidence(batch, item, passed=False)
    worker_path = directory / 'trusted/worker-a.json'
    worker = host.read_object(worker_path)
    worker.update(attach_exit=1, process_exit=1)
    worker['after_exit']['state'] = state(1)
    if change == 'attach_exit': worker['attach_exit'] = 0
    elif change == 'boolean_attach': worker['attach_exit'] = True
    elif change == 'oom': worker['after_exit']['state']['OOMKilled'] = True
    write(worker_path, worker)
    physical = {'checkpoint_id': 'head', 'checkpoint_blob_sha256': 'd' * 64, 'metadata_sha256': 'e' * 64}
    facts = {'kind': 'controlled_failure', 'result_sha256': 'f' * 64, 'native_checkpoint': physical}
    resources_path = directory / 'trusted/resource-summary.json'
    resources = host.read_object(resources_path)
    resources['controlled_failures'] = {'worker-a': {'evidence': facts,
        'native_verification': {'physical_native_failure_verified': True, 'original_files_unchanged': True,
            'reader': 'independent_bench_safe_msgpack_projection', **physical}}}
    if change == 'no_native_audit': del resources['controlled_failures']
    elif change == 'bad_blob_hash': resources['controlled_failures']['worker-a']['native_verification']['checkpoint_blob_sha256'] = '0' * 64
    write(resources_path, resources)
    write(directory / 'business-verdict.json', {'passed': business_passed})
    verdict['axes']['task_result']['passed'] = business_passed
    if change == 'success_exit1':
        verdict['passed'] = True
        verdict['axes']['handling_outcome'] = 'automatic_completed'
        for row in verdict['checks']:
            row['passed'] = True
        grade_path = directory / 'trusted/bench-process-0001.json'
        grade = host.read_object(grade_path)
        grade['exit_code'] = grade['after_exit']['state']['ExitCode'] = 0
        write(grade_path, grade)
    write(directory / 'verdict.json', verdict)
    calls = []
    def recheck(run, fixture, scope, pins, candidate, attempt, actual, *, state):
        calls.append((run, candidate, attempt, actual, state))
        if change == 'helper_rejects' or actual['state']['OOMKilled']:
            raise ValueError('invalid bound native failure')
        return {**facts, 'result_sha256': '0' * 64} if change == 'sources_changed' else facts
    monkeypatch.setattr(host, 'controlled_failure', recheck)
    if change is not None:
        with pytest.raises(host.BatchStopped):
            batch.completed_evidence(item, 0 if change == 'success_exit1' else 1)
        return
    row = batch.completed_evidence(item, 1)
    assert row['passed'] is False and row['failed_checks'] == ['native_terminal']
    assert row['axes']['task_result']['passed'] is business_passed
    assert row['axes']['handling_outcome'] == 'incomplete'
    assert len(calls) == 1 and calls[0][0] == directory and calls[0][2] == 'worker-a'


@pytest.mark.parametrize("corruption", ["missing_verdict", "identity", "image", "missing_axis", "missing_check", "duplicate_check",
    "injection", "resource", "orchestration", "cleanup", "resource_file", "child_exit", "control_exit", "model"])
def test_incomplete_or_invalid_evidence_is_never_capability_failure(batch, corruption):
    directory, verdict = evidence(batch, batch.plan[0])
    code = 0
    if corruption == "missing_verdict":
        (directory / "verdict.json").unlink()
    elif corruption in {"identity", "missing_axis", "missing_check", "duplicate_check", "injection", "resource"}:
        if corruption == "identity":
            verdict["chain_id"] = "another-chain"
        elif corruption == "missing_axis":
            del verdict["axes"]["replay_behavior"]
        elif corruption == "missing_check":
            verdict["checks"].pop()
        elif corruption == "duplicate_check":
            verdict["checks"].append(deepcopy(verdict["checks"][0]))
        elif corruption == "injection":
            verdict["axes"]["injection_valid"] = False
        else:
            verdict["axes"]["budget_and_process"]["passed"] = False
        write(directory / "verdict.json", verdict)
    elif corruption == "image":
        path = directory / "trusted/pins.json"
        value = host.read_object(path)
        value["candidate_image_id"] = SERVICES
        write(path, value)
    elif corruption == "orchestration":
        path = directory / "orchestration.json"
        value = host.read_object(path)
        value["errors"] = ["control_error"]
        write(path, value)
    elif corruption == "cleanup":
        write(directory / "cleanup.json", {"remaining": [{"id": "residue"}], "finished_at": "complete"})
    elif corruption == "resource_file":
        (directory / "resource-samples.jsonl").unlink()
    elif corruption == "child_exit":
        code = 3
    elif corruption == "control_exit":
        path = directory / "trusted/bench-process-0000.json"
        value = host.read_object(path)
        value["exit_code"] = value["after_exit"]["state"]["ExitCode"] = 3
        write(path, value)
    elif corruption == "model":
        batch.record["model_name"] = "previously-frozen-model"
    with pytest.raises(host.BatchStopped):
        batch.completed_evidence(batch.plan[0], code)


def test_natural_recovery_gap_with_valid_injection_is_an_observed_capability_gap(batch):
    item = host.plan("smoke", "gap")[-1]
    evidence(batch, item, passed=False, gap=True)
    row = batch.completed_evidence(item, 1)
    assert row["passed"] is False and row["native_recovery_gap"] is True
    assert row["axes"]["budget_and_process"]["passed"] is True


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "foreign_id", "foreign_scope", "foreign_chain",
    "wrong_action", "wrong_name", "wrong_process_name", "wrong_image", "changed_boundary", "missing_before",
    "wrong_created_id", "wrong_created_name", "wrong_created_image", "created_running", "created_status",
    "created_memory", "created_cpu", "linked", "invalid_actual_id"])
def test_gap_requires_one_matching_original_control_registration(batch, corruption):
    item = host.plan("smoke", "registration")[-1]
    directory, _ = evidence(batch, item, passed=False, gap=True)
    path = directory / "trusted/bench-registration-0001.json"
    registration = host.read_object(path)
    if corruption == "missing":
        path.unlink()
    elif corruption == "duplicate":
        write(directory / "trusted/bench-registration-0099.json", registration)
    elif corruption == "linked":
        retained = path.with_name("retained-original.json")
        path.rename(retained)
        path.symlink_to(retained)
    elif corruption == "wrong_process_name":
        process_path = directory / "trusted/bench-process-0001.json"
        process = host.read_object(process_path)
        process["name"] = "a-different-process"
        write(process_path, process)
    else:
        if corruption == "foreign_id": registration["id"] = "f" * 64
        elif corruption == "foreign_scope": registration["scope"]["user_id"] = "foreign"
        elif corruption == "foreign_chain": registration["chain_id"] = "foreign"
        elif corruption == "wrong_action": registration["action"] = "after-kill"
        elif corruption == "wrong_name": registration["name"] = "foreign"
        elif corruption == "wrong_image": registration["image_id"] = SERVICES
        elif corruption == "changed_boundary": registration["crash_boundary_sha256"] = "f" * 64
        elif corruption == "missing_before": del registration["before_start"]
        elif corruption == "wrong_created_id": registration["before_start"]["id"] = "f" * 64
        elif corruption == "wrong_created_name": registration["before_start"]["name"] = "/foreign"
        elif corruption == "wrong_created_image": registration["before_start"]["image_id"] = SERVICES
        elif corruption == "created_running": registration["before_start"]["state"]["Running"] = True
        elif corruption == "created_status": registration["before_start"]["state"]["Status"] = "exited"
        elif corruption == "created_memory": registration["before_start"]["memory_limit"] = 256 * 1024**2
        elif corruption == "created_cpu": registration["before_start"]["cpu_nano"] = 250000000
        elif corruption == "invalid_actual_id":
            process_path = directory / "trusted/bench-process-0001.json"
            process = host.read_object(process_path)
            process["after_exit"]["id"] = registration["id"] = registration["before_start"]["id"] = "short-id"
            write(process_path, process)
        write(path, registration)
    with pytest.raises(host.BatchStopped):
        batch.completed_evidence(item, 1)


@pytest.mark.parametrize("corruption", ["boundary_hash", "b_created", "b_binding", "b_link", "gap_identity", "missing_gate", "fake_kill", "diagnostic_exit"])
def test_recovery_gap_still_requires_original_kill_and_complete_control_evidence(batch, corruption):
    item = host.plan("smoke", "gap")[-1]
    directory, _ = evidence(batch, item, passed=False, gap=True)
    if corruption in {"boundary_hash", "gap_identity"}:
        path = directory / "trusted/native-recovery-gap.json"
        value = host.read_object(path)
        value["crash_boundary_sha256" if corruption == "boundary_hash" else "chain_id"] = "wrong"
        write(path, value)
    elif corruption in {"b_created", "b_binding"}:
        name = "worker-b-created.json" if corruption == "b_created" else "worker-b-binding.json"
        write(directory / "trusted" / name, {"unexpected": True})
    elif corruption == "b_link":
        (directory / "trusted/worker-b-created.json").symlink_to("nonexistent")
    elif corruption == "missing_gate":
        (directory / "trusted/gate-hit.json").unlink()
    elif corruption == "fake_kill":
        path = directory / "trusted/worker-a.json"
        value = host.read_object(path)
        value["kill"]["signal"] = "SIGTERM"
        write(path, value)
    else:
        path = directory / "trusted/bench-process-0001.json"
        value = host.read_object(path)
        value["exit_code"] = value["after_exit"]["state"]["ExitCode"] = 2
        write(path, value)
    with pytest.raises(host.BatchStopped):
        batch.completed_evidence(item, 1)


def test_capability_failure_continues_with_private_logs_and_no_public_raw_output(batch, monkeypatch, capsys):
    calls = []
    def child(command, *, stdout, **kwargs):
        item = batch.plan[len(calls)]
        passed = bool(calls)
        evidence(batch, item, passed=passed)
        stdout.write("private reasoning never-public-key\n")
        calls.append(item)
        return subprocess.CompletedProcess(command, 0 if passed else 1)
    monkeypatch.setattr(host.subprocess, "run", child)
    assert batch.run() == 1
    assert len(calls) == 2
    index = host.read_object(batch.index / "batch.json")
    assert index["status"] == "completed"
    assert [row["status"] for row in index["runs"]] == ["capability_failed", "passed"]
    assert stat.S_IMODE(batch.index.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in batch.index.iterdir())
    public = capsys.readouterr().out
    assert "reasoning" not in public and "never-public-key" not in public
    assert "private reasoning" in Path(index["runs"][0]["log"]).read_text()


@pytest.mark.parametrize("reason", ["invalid_injection", "missing_verdict", "residue"])
def test_integrity_failure_stops_without_automatically_selecting_a_new_chain(batch, monkeypatch, reason):
    calls = []
    def child(command, **kwargs):
        item = batch.plan[len(calls)]
        directory, verdict = evidence(batch, item)
        if reason == "invalid_injection":
            verdict["axes"]["injection_valid"] = False
            write(directory / "verdict.json", verdict)
        elif reason == "missing_verdict":
            (directory / "verdict.json").unlink()
        else:
            batch.lab.remaining = [{"id": "not-cleaned"}]
        calls.append(item)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(host.subprocess, "run", child)
    assert batch.run() == 1 and len(calls) == 1
    index = host.read_object(batch.index / "batch.json")
    assert index["status"] == "stopped" and index["runs"][0]["status"] == "integrity_stopped"


def test_used_chain_state_is_not_overwritten(batch):
    batch.paths(batch.plan[0]).fixture.mkdir(parents=True)
    with pytest.raises(host.BatchStopped):
        batch.assert_fresh_chain(batch.plan[0])


def test_worker_actual_image_is_checked_independently_of_pins(batch):
    directory, _ = evidence(batch, batch.plan[0])
    path = directory / "trusted/worker-a.json"
    value = host.read_object(path)
    value["after_exit"]["image_id"] = SERVICES
    write(path, value)
    with pytest.raises(host.BatchStopped):
        batch.completed_evidence(batch.plan[0], 0)


def test_batch_uses_a_distinct_lock_from_its_children(batch, monkeypatch):
    def child(command, **kwargs):
        assert (batch.data_root / "runs/.fault-batch.lock").is_file()
        assert not (batch.data_root / "runs/.detailed-batch.lock").exists()
        item = batch.plan[len(batch.record["runs"]) - 1]
        evidence(batch, item)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(host.subprocess, "run", child)
    assert batch.run() == 0
    assert stat.S_IMODE((batch.data_root / "runs/.fault-batch.lock").stat().st_mode) == 0o600


def test_gap_capability_failure_continues_without_replacing_the_failed_chain(batch, monkeypatch):
    batch.plan = host.plan("smoke", "gap-batch")[-1:] + host.plan("baseline-101", "after-gap")[:1]
    calls = []
    def child(command, **kwargs):
        item = batch.plan[len(calls)]
        gap = item["fault"] == "kill_before_write"
        evidence(batch, item, passed=not gap, gap=gap)
        calls.append(item["chain_id"])
        return subprocess.CompletedProcess(command, 1 if gap else 0)
    monkeypatch.setattr(host.subprocess, "run", child)
    assert batch.run() == 1
    assert calls == [row["chain_id"] for row in batch.plan]
    index = host.read_object(batch.index / "batch.json")
    assert index["status"] == "completed" and index["runs"][0]["native_recovery_gap"] is True


def test_material_or_evidence_symlinks_are_rejected(batch):
    path = batch.context / "harness-lab/fault.py"
    original = path.read_bytes()
    replacement = path.with_name("alternate.py")
    replacement.write_bytes(original)
    path.unlink()
    path.symlink_to(replacement)
    with pytest.raises(host.BatchStopped):
        batch.material_hashes()


def test_public_summary_projects_only_known_axis_fields(batch):
    directory, verdict = evidence(batch, batch.plan[0])
    verdict["axes"]["task_result"]["untrusted_extra"] = "private reasoning never-public-key"
    write(directory / "verdict.json", verdict)
    row = batch.completed_evidence(batch.plan[0], 0)
    assert "never-public-key" not in json.dumps(host.summary(row))
