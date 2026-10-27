"""I25 已登记生命周期校准源码，只在固定 Docker 中运行。

合成记录与传输替身不能证明真实 Agent、Docker、资源或业务能力。
本模块不调用 Docker 或 SDK。
"""
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from runner.instance_cost import CAPS, canonical, sha
from runner.instance_cost_lifecycle import (SCHEMA, VARIANT, exact_absence,
    resource_complete, rotation_phase_metrics, terminal_ok, validate_registry, validate_sample)
from scripts.instance_cost_lifecycle import RotationLifecycle
from scripts import instance_cost_lifecycle as host


IDENT = "a" * 64
OTHER = "b" * 64
NAME = "hl-ic-lifecycle-calibration-probe-user-001"


def observation(status="created"):
    memory, cpu = CAPS["worker"]
    return {"id": IDENT, "name": "/" + NAME, "image_id": "sha256:" + "1" * 64,
        "role": "worker", "memory_limit": memory, "cpu_nano": cpu,
        "network_mode": "none", "environment_names": ["PATH"],
        "mounts": [{"Source": "/synthetic/owner/state", "Destination": "/state", "RW": True}],
        "state": {"Status": status, "Running": status == "running", "Pid": 42 if status == "running" else 0,
            "ExitCode": 0, "OOMKilled": False, "Dead": False, "Paused": False,
            "Restarting": False, "Error": ""}}


def absence(target, observed):
    return {"target": target, "return_code": 1, "stdout": "[]", "stderr": "Error: No such object: " + target,
        "observed_monotonic": observed}


@pytest.fixture
def material(tmp_path):
    plan = {"trial_id": "lifecycle-calibration", "candidate_image_id": "sha256:" + "1" * 64,
        "bench_image_id": "sha256:" + "2" * 64, "services_image_id": "sha256:" + "3" * 64}
    acknowledgement = {"command_id": "c" * 32, "status": "closed"}
    registry = {"schema": SCHEMA, "sampling_variant": VARIANT, "trial_id": plan["trial_id"],
        "registrations": {IDENT: {"trial_id": plan["trial_id"], "owner": "user-001",
            "registered_monotonic": 1.0, "created": observation()}},
        "starts": {IDENT: {"id": IDENT, "requested_monotonic": 1.1}},
        "requests": {IDENT: {"id": IDENT, "kind": "native_close", "owner": "user-001",
            "before": observation("running"), "command_id": "c" * 32, "requested_monotonic": 2.0}},
        "terminals": {IDENT: {"observation": observation("exited"), "observed_monotonic": 2.5,
            "acknowledgement": acknowledgement, "attach_exit": 0,
            "stdout_source": "protected/native-stdout.jsonl"}},
        "deletions": {IDENT: {"id": IDENT, "before_rm": observation("exited"),
            "requested_monotonic": 3.6, "rm_return_code": 0, "rm_stdout": IDENT,
            "id_absence": absence(IDENT, 3.7), "name_absence": absence(NAME, 3.8)}}}
    (tmp_path / "protected").mkdir()
    (tmp_path / "protected/native-stdout.jsonl").write_bytes(canonical({"phase": "ready"}) + b"\n"
        + canonical(acknowledgement) + b"\n")
    return SimpleNamespace(plan=plan, registry=registry, root=tmp_path)


def command(kind, *, rc=0, stdout="", stderr=""):
    argv = (["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}", IDENT]
        if kind == "stats" else ["docker", "top", IDENT, "-eo", "pid,ppid,args"])
    return {"target": IDENT, "argv": argv, "started_monotonic": 2.1 if kind == "stats" else 2.3,
        "finished_monotonic": 2.2 if kind == "stats" else 2.4,
        "return_code": rc, "stdout": stdout, "stderr": stderr}


def transition_sample(registry, *, removed=False):
    replies = {kind: command(kind, rc=1, stderr="Error response from daemon: Container " + IDENT + " is not running")
        for kind in ("stats", "top")}
    return {"sampling_variant": VARIANT, "resource_measurement_complete": False,
        "phase": "closing", "host_before_observer_monotonic": 1.7,
        "host_started_monotonic": 1.8, "before_inventory_monotonic": 1.9,
        "after_inventory_monotonic": 4.0 if removed else 3.0,
        "host_finished_monotonic": 4.1 if removed else 3.1,
        "host_after_observer_monotonic": 4.2 if removed else 3.2,
        "docker_observations": [observation("running")],
        "docker_after_observations": [] if removed else [observation("exited")],
        "planned_inventory": [{"id": IDENT, "role": "worker", "image_id": "sha256:" + "1" * 64,
            "memory_limit": CAPS["worker"][0], "cpu_nano": CAPS["worker"][1], "running": True}],
        "resource_commands": {IDENT: replies}, "containers": [], "process_observations": [],
        "lifecycle_transitions": [{"id": IDENT,
            "registration_sha256": sha(canonical(registry["registrations"][IDENT])),
            "request_sha256": sha(canonical(registry["requests"][IDENT])),
            "terminal_observation": observation("exited"), "commands": deepcopy(replies),
            "native_task_health_claimed": False}]}


def rebind_commands(sample):
    sample["lifecycle_transitions"][0]["commands"] = deepcopy(sample["resource_commands"][IDENT])


def assert_registry_rejected(material):
    with pytest.raises(ValueError):
        validate_registry(material.registry, material.plan, root=material.root)


def test_complete_native_exit0_and_exact_absences_are_a_valid_evidence_chain(material):
    assert validate_registry(material.registry, material.plan, root=material.root) is True
    assert validate_sample(transition_sample(material.registry), material.registry, material.plan) is True
    assert validate_sample(transition_sample(material.registry, removed=True), material.registry, material.plan) is True


@pytest.mark.parametrize("change", ("registration", "start", "request", "terminal", "wrong-request-id", "wrong-terminal-id"))
def test_removed_container_cannot_borrow_a_missing_or_different_lifecycle(material, change):
    if change in {"registration", "start", "request", "terminal"}:
        key = {"registration": "registrations", "start": "starts", "request": "requests", "terminal": "terminals"}[change]
        material.registry[key].pop(IDENT)
    elif change == "wrong-request-id":
        material.registry["requests"][IDENT]["id"] = OTHER
    else:
        material.registry["terminals"][IDENT]["observation"]["id"] = OTHER
    assert_registry_rejected(material)


@pytest.mark.parametrize("exit_code,oom", ((137, False), (137, True), (1, False), (0, True)))
def test_close_ack_never_authorizes_exit137_exit1_or_oom(material, exit_code, oom):
    for value in (material.registry["terminals"][IDENT]["observation"], material.registry["deletions"][IDENT]["before_rm"]):
        value["state"].update(ExitCode=exit_code, OOMKilled=oom)
    assert terminal_ok(material.registry["terminals"][IDENT]["observation"], material.registry["requests"][IDENT]) is False
    assert_registry_rejected(material)


@pytest.mark.parametrize("field,value", (("Status", "created"), ("Running", False), ("Pid", 0),
    ("Pid", True), ("ExitCode", 137), ("ExitCode", True), ("OOMKilled", True), ("Dead", True),
    ("Paused", True), ("Restarting", True), ("Error", "runtime failure")))
def test_close_request_requires_the_exact_actually_healthy_running_worker(material, field, value):
    material.registry["requests"][IDENT]["before"]["state"][field] = value
    assert_registry_rejected(material)


@pytest.mark.parametrize("change", ("rm-failed", "wrong-rm-id", "wrong-absence-id", "absence-rc-zero", "wrong-name",
    "absence-unknown-error", "missing-id-absence", "missing-original-terminal"))
def test_deletion_requires_original_healthy_terminal_successful_rm_and_two_strict_absences(material, change):
    deletion = material.registry["deletions"][IDENT]
    if change == "rm-failed":
        deletion["rm_return_code"] = 1
    elif change == "wrong-rm-id":
        deletion["rm_stdout"] = OTHER
    elif change == "wrong-absence-id":
        deletion["id_absence"] = absence(OTHER, 3.7)
    elif change == "absence-rc-zero":
        deletion["id_absence"]["return_code"] = 0
    elif change == "wrong-name":
        deletion["name_absence"] = absence(NAME + "-other", 3.8)
    elif change == "absence-unknown-error":
        deletion["id_absence"]["stderr"] = "Cannot connect to Docker daemon"
    elif change == "missing-id-absence":
        deletion["id_absence"] = None
    else:
        material.registry["terminals"].pop(IDENT)
    assert_registry_rejected(material)


@pytest.mark.parametrize("change", ("start-before-registration", "request-before-start", "terminal-before-request",
    "rm-before-terminal", "id-absence-before-rm", "name-absence-before-id"))
def test_registration_start_request_terminal_and_removal_clocks_cannot_run_backwards(material, change):
    registry = material.registry
    if change == "start-before-registration":
        registry["starts"][IDENT]["requested_monotonic"] = .9
    elif change == "request-before-start":
        registry["requests"][IDENT]["requested_monotonic"] = 1.05
    elif change == "terminal-before-request":
        registry["terminals"][IDENT]["observed_monotonic"] = 1.9
    elif change == "rm-before-terminal":
        registry["deletions"][IDENT]["requested_monotonic"] = 2.4
    elif change == "id-absence-before-rm":
        registry["deletions"][IDENT]["id_absence"]["observed_monotonic"] = 3.5
    else:
        registry["deletions"][IDENT]["name_absence"]["observed_monotonic"] = 3.65
    assert_registry_rejected(material)


@pytest.mark.parametrize("change", ("missing-ack", "wrong-command", "wrong-status", "attach-one", "source-mismatch", "source-duplicate"))
def test_close_ack_is_bound_to_actual_original_stdout_and_real_attach_exit(material, change):
    terminal = material.registry["terminals"][IDENT]
    if change == "missing-ack":
        terminal["acknowledgement"] = {}
    elif change == "wrong-command":
        terminal["acknowledgement"]["command_id"] = "another-close"
    elif change == "wrong-status":
        terminal["acknowledgement"]["status"] = "requested"
    elif change == "attach-one":
        terminal["attach_exit"] = 1
    elif change == "source-mismatch":
        (material.root / terminal["stdout_source"]).write_bytes(canonical({"command_id": "different", "status": "closed"}) + b"\n")
    else:
        data = canonical(terminal["acknowledgement"]) + b"\n"
        (material.root / terminal["stdout_source"]).write_bytes(data + data)
    assert_registry_rejected(material)


def test_ack_object_is_not_a_real_terminal_or_permission_to_hide_missing_samples(material):
    request = material.registry["requests"][IDENT]
    ack = material.registry["terminals"][IDENT]["acknowledgement"]
    assert terminal_ok(ack, request) is False
    sample = transition_sample(material.registry, removed=True)
    material.registry["terminals"] = {}
    sample["lifecycle_transitions"][0]["terminal_observation"] = ack
    with pytest.raises(ValueError):
        validate_sample(sample, material.registry, material.plan)


@pytest.mark.parametrize("change", ("wrong-target", "wrong-argv-id", "wrong-subcommand", "argv-missing", "target-missing",
    "stats-end-after-top-start", "command-before-sample", "command-after-sample", "backwards-command"))
@pytest.mark.parametrize("kind", ("stats", "top"))
def test_resource_commands_need_exact_target_argv_and_real_ordered_intervals(material, change, kind):
    sample = transition_sample(material.registry)
    reply = sample["resource_commands"][IDENT][kind]
    if change == "wrong-target":
        reply["target"] = OTHER
    elif change == "wrong-argv-id":
        reply["argv"] = [OTHER if part == IDENT else part for part in reply["argv"]]
    elif change == "wrong-subcommand":
        reply["argv"][1] = "inspect"
    elif change == "argv-missing":
        reply.pop("argv")
    elif change == "target-missing":
        reply.pop("target")
    elif change == "stats-end-after-top-start":
        sample["resource_commands"][IDENT]["stats"]["finished_monotonic"] = 2.35
    elif change == "command-before-sample":
        reply["started_monotonic"] = 1.5
    elif change == "command-after-sample":
        reply["finished_monotonic"] = 3.5
    else:
        reply["finished_monotonic"] = reply["started_monotonic"] - .1
    rebind_commands(sample)
    with pytest.raises(ValueError):
        validate_sample(sample, material.registry, material.plan)


@pytest.mark.parametrize("change", ("unknown-stderr", "wrong-id-stderr", "eof-without-stopped-top", "stats-wrong-id",
    "source-map-missing", "transition-source-edited", "resource-identity-added"))
def test_known_transition_is_narrow_and_cannot_hide_unknown_or_different_commands(material, change):
    sample = transition_sample(material.registry)
    replies = sample["resource_commands"][IDENT]
    if change == "unknown-stderr":
        replies["stats"]["stderr"] = "Docker daemon unavailable"
    elif change == "wrong-id-stderr":
        replies["top"]["stderr"] = "Error: No such container: " + OTHER
    elif change == "eof-without-stopped-top":
        replies["stats"]["stderr"] = "EOF"
        replies["top"].update(return_code=0, stdout="PID PPID COMMAND\n42 1 python resident\n", stderr="")
    elif change == "stats-wrong-id":
        replies["stats"].update(return_code=0, stdout=json.dumps({"Container": OTHER}), stderr="")
    elif change == "source-map-missing":
        sample["resource_commands"] = {}
    elif change == "transition-source-edited":
        sample["lifecycle_transitions"][0]["commands"]["stats"]["stderr"] = "EOF"
    else:
        sample["resource_commands"][OTHER] = deepcopy(replies)
    if change not in {"source-map-missing", "transition-source-edited"}:
        rebind_commands(sample)
    with pytest.raises(ValueError):
        validate_sample(sample, material.registry, material.plan)


def test_exact_stopped_top_allows_only_same_id_eof_with_real_healthy_terminal(material):
    sample = transition_sample(material.registry)
    sample["resource_commands"][IDENT]["stats"]["stderr"] = "EOF"
    rebind_commands(sample)
    assert validate_sample(sample, material.registry, material.plan) is True


def test_synchronously_omitting_every_inventory_and_resource_row_is_still_missing_an_original_container(material):
    sample = transition_sample(material.registry)
    for key in ("docker_observations", "docker_after_observations", "planned_inventory", "containers",
            "process_observations", "lifecycle_transitions"):
        sample[key] = []
    sample["resource_commands"] = {}
    with pytest.raises(ValueError, match="inventory_registration_set_changed"):
        validate_sample(sample, material.registry, material.plan)


@pytest.mark.parametrize("kind", ("stats", "top"))
def test_a_later_close_cannot_explain_an_earlier_failed_resource_command(material, kind):
    sample = transition_sample(material.registry)
    material.registry["requests"][IDENT]["requested_monotonic"] = 2.45
    sample["lifecycle_transitions"][0]["request_sha256"] = sha(canonical(material.registry["requests"][IDENT]))
    rebind_commands(sample)
    with pytest.raises(ValueError, match="future_request_cannot_explain_command"):
        validate_sample(sample, material.registry, material.plan)


@pytest.mark.parametrize("kind", ("stats", "top"))
def test_a_later_removal_cannot_explain_an_earlier_no_such_container_reply(material, kind):
    sample = transition_sample(material.registry, removed=True)
    sample["resource_commands"][IDENT][kind].update(stdout="", stderr="Error: No such container: " + IDENT)
    rebind_commands(sample)
    with pytest.raises(ValueError, match="future_removal_cannot_explain_command"):
        validate_sample(sample, material.registry, material.plan)


@pytest.mark.parametrize("field,value", (("memory_limit", 1), ("cpu_nano", 0), ("cpu_nano", True),
    ("environment_names", ["OPENAI_API_KEY"]), ("role", "unplanned-service")))
def test_unsampled_original_registrations_keep_fixed_caps_roles_and_secret_boundary(material, field, value):
    for row in (material.registry["registrations"][IDENT]["created"],
            material.registry["requests"][IDENT]["before"],
            material.registry["terminals"][IDENT]["observation"],
            material.registry["deletions"][IDENT]["before_rm"]):
        row[field] = value
    assert_registry_rejected(material)


def complete_resource_sample():
    value = {"docker_observations": [observation("running")],
        "docker_after_observations": [observation("running")], "lifecycle_transitions": [],
        "containers": [{"Container": IDENT, "MemUsage": "10MiB / 2GiB", "CPUPerc": "2%", "PIDs": "3"}],
        "process_observations": [{"container_id": IDENT, "process_count": 2}]}
    return value


def test_transition_only_resources_do_not_become_zero_usage_or_zero_process_totals(material):
    sample = transition_sample(material.registry)
    assert resource_complete(sample) is False
    metric = rotation_phase_metrics([sample])
    assert metric["resource_measurement_complete"] is False
    assert metric["resource_incomplete_samples"] == 1
    assert metric["resources"] == {}
    assert metric["actual_process_count"] is None


def test_one_partial_sample_disables_whole_phase_means_and_peaks(material):
    metric = rotation_phase_metrics([complete_resource_sample(), transition_sample(material.registry)])
    assert metric["resource_complete_samples"] == 1 and metric["resource_incomplete_samples"] == 1
    assert metric["resources"] == {} and metric["actual_process_count"] is None


def test_complete_idle_resources_retain_actual_totals_and_reject_a_transition():
    sample = complete_resource_sample()
    assert resource_complete(sample) is True
    metric = rotation_phase_metrics([sample])
    assert metric["resource_measurement_complete"] is True
    assert metric["resources"]["memory_bytes"]["peak"] == 10 * 1024**2
    assert metric["actual_process_count"] == {"mean": 2, "peak": 2}
    sample["lifecycle_transitions"] = [{"id": IDENT}]
    assert resource_complete(sample) is False


@pytest.mark.parametrize("change", ("target", "rc", "stdout", "stderr"))
def test_exact_absence_rejects_generic_failure_or_another_container(change):
    proof = absence(IDENT, 3.7)
    if change == "target":
        proof["target"] = OTHER
    elif change == "rc":
        proof["return_code"] = True
    elif change == "stdout":
        proof["stdout"] = ""
    else:
        proof["stderr"] = "Error: No such object: " + OTHER
    assert exact_absence(proof, IDENT) is False


def test_observer_wait_releases_all_lifecycle_locks_without_forgiving_its_timeout(tmp_path, monkeypatch):
    """用事件控制探针等待，不实际休眠、调用 Docker 或伪造采样。"""
    waiting, reply_ready = threading.Event(), threading.Event()
    state, errors, values = {}, [], []

    class Lab:
        root = tmp_path
        plan = {"trial_id": "lifecycle-calibration"}
        phase, kind = "starting", None

        def publish_control(self, phase, kind, *, nonce=None):
            value = {"sequence": 1, "phase": phase, "kind": kind, "probe_nonce": nonce}
            state.update(value)
            return value

    lifecycle = RotationLifecycle(Lab())
    (tmp_path / "protected/observer/replies").mkdir(parents=True)

    def blocked_wait(seconds):
        assert seconds == .05
        waiting.set()
        assert reply_ready.wait(2), "测试未能发布原始观察进程响应"

    monkeypatch.setattr(host.time, "sleep", blocked_wait)

    def worker():
        try:
            values.append(lifecycle.probe())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert waiting.wait(2)
        for lock in (lifecycle.control_lock, lifecycle.registry_lock, lifecycle.inventory_lock):
            assert lock.acquire(blocking=False), "长时间等待观察进程时仍持有生命周期锁"
            lock.release()
        path = tmp_path / "protected/observer/replies" / (state["probe_nonce"] + ".json")
        path.write_bytes(canonical({"control_sequence": state["sequence"], "phase": state["phase"],
            "kind": state["kind"], "probe_nonce": state["probe_nonce"]}) + b"\n")
    finally:
        reply_ready.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == []
    assert len(values) == 1 and values[0]["phase"] == "starting"
