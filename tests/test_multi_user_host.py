"""I24 无模型替身测试，仅在复核后的 Docker 验收镜像运行。

只校准编排与证据拒绝，不证明原生能力。
不启动 Docker、候选 SDK 或远程模型。
"""
from copy import deepcopy
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from runner import multi_user as verifier
from scripts import run_multi_user as host


def container(role="acceptance", *, status="running", code=0):
    return {"id": "a" * 64, "name": "/harness-lab-offline-bench-1", "image_id": "sha256:" + "b" * 64,
        "role": role, "trial_id": "offline", "memory_limit": 512 * 1024**2, "cpu_nano": 500000000,
        "init_enabled": role != "acceptance",
        "state": {"Status": status, "Running": status == "running", "ExitCode": code,
            "Pid": 123 if status == "running" else 0, "Paused": False, "Restarting": False,
            "OOMKilled": False, "Dead": False, "Error": "", "FinishedAt": "2026-10-20T00:00:01Z"}}


def transition_fixture(role="acceptance", *, absent=False):
    before = container(role)
    registered = container(role, status="created")
    terminal = container(role, status="exited", code=0 if role == "acceptance" else 143)
    record = {"after_stop": terminal, "removal_requested_monotonic": 10.5,
        "deleted": {"id": before["id"], "id_absent": True, "observed_at": "2026-10-20T00:00:02Z"}}
    if role != "acceptance":
        record.update({"before_stop": before, "requested_monotonic": 10.0})
    replies = {"stats": subprocess.CompletedProcess([], 0, "", ""),
        "top": subprocess.CompletedProcess([], 1, "", "Error response from daemon: Container " + before["id"] + " is not running")}
    retirement, completion = (None, record) if role == "acceptance" else (record, None)
    return before, replies, None if absent else terminal, registered, retirement, completion


def evidence(values):
    before, _, _, registered, retirement, completion = values
    item = host.explained_sampling_transition(*values)
    if item["absence_observed"]:
        item["absence_observation"] = {"id": before["id"], "id_absent": True, "observed_at": "2026-10-20T00:00:02Z"}
    registry = {"registrations": {before["id"]: registered}, "retirements": {}, "bench_completions": {}}
    registry["bench_completions" if completion else "retirements"][before["id"]] = completion or retirement
    return item, registry


def removing_fixture(role="tools"):
    values = list(transition_fixture(role))
    terminal = (values[5] if role == "acceptance" else values[4])["after_stop"]
    terminal["state"]["StartedAt"] = "2026-10-20T00:00:00Z"
    removing = deepcopy(terminal)
    removing["state"].update({"Status": "removing", "Dead": True})
    values[2] = removing
    proof = {"id": values[0]["id"], "id_absent": True, "returncode": 1, "stdout": "[]\n",
        "stderr": "Error: No such object: " + values[0]["id"] + "\n",
        "observed_monotonic": 11.5, "observed_at": "2026-10-20T00:00:03Z"}
    return values, {"removing_observed_monotonic": 11.0, "removal_absence": proof}


def removing_evidence():
    values, kwargs = removing_fixture()
    item = host.explained_sampling_transition(*values, **kwargs)
    registry = {"registrations": {values[0]["id"]: values[3]},
        "retirements": {values[0]["id"]: values[4]}, "bench_completions": {}}
    return values, deepcopy(item), deepcopy(registry)


@pytest.mark.parametrize("role", ["acceptance", "worker", "tools"])
def test_removing_snapshot_requires_independent_exact_absence_and_keeps_healthy_terminal_separate(role):
    values, kwargs = removing_fixture(role)
    original = deepcopy((values[0], values[2:], kwargs))
    item = host.explained_sampling_transition(*values, **kwargs)
    registry = {"registrations": {values[0]["id"]: values[3]}, "retirements": {}, "bench_completions": {}}
    registry["bench_completions" if role == "acceptance" else "retirements"][values[0]["id"]] = values[5] or values[4]
    assert verifier.validate_sampling_transition(item, [values[0]], registry,
        {"started_monotonic": 9.0, "finished_monotonic": 12.0}) == values[0]["id"]
    assert item["removing_observation"]["state"]["Status"] == "removing"
    assert item["removing_observation"]["state"]["Dead"] is True
    assert item["terminal_observation"]["state"]["Status"] == "exited"
    assert item["terminal_observation"]["state"]["Dead"] is False
    assert item["absence_observation"] == kwargs["removal_absence"]
    assert (values[0], values[2:], kwargs) == original


@pytest.mark.parametrize("change", ["no-proof", "exists", "wrong-id", "unknown-error", "wrong-error-id", "code-type",
    "output", "missing-delete", "wrong-delete-id", "late-delete", "future-rm", "future-stop", "future-snapshot",
    "snapshot-type", "oom", "kill", "pid", "started", "image", "mount", "terminal-oom"])
def test_host_never_turns_other_removing_errors_into_healthy_terminal(change):
    values, kwargs = removing_fixture()
    proof, record = kwargs["removal_absence"], values[4]
    if change == "no-proof":
        del kwargs["removal_absence"]
    elif change == "exists":
        proof.update(returncode=0, stdout=None, stderr="", id_absent=False)
    elif change == "wrong-id":
        proof["id"] = "c" * 64
    elif change in {"unknown-error", "wrong-error-id"}:
        proof["stderr"] = "Cannot connect to Docker" if change == "unknown-error" else "Error: No such object: " + "c" * 64
    elif change == "code-type":
        proof["returncode"] = True
    elif change == "output":
        proof["stdout"] = "[] extra"
    elif change == "missing-delete":
        del record["deleted"]
    elif change == "wrong-delete-id":
        record["deleted"]["id"] = "c" * 64
    elif change == "late-delete":
        record["deleted"]["observed_at"] = "2026-10-20T00:00:04Z"
    elif change == "future-rm":
        record["removal_requested_monotonic"] = 11.25
    elif change == "future-stop":
        record["requested_monotonic"] = 10.75
    elif change == "future-snapshot":
        kwargs["removing_observed_monotonic"] = 11.75
    elif change == "snapshot-type":
        kwargs["removing_observed_monotonic"] = True
    elif change == "terminal-oom":
        record["after_stop"]["state"]["OOMKilled"] = True
    elif change == "image":
        values[2]["image_id"] = "sha256:" + "c" * 64
    elif change == "mount":
        values[2]["mounts"] = []
    else:
        values[2]["state"][{"oom": "OOMKilled", "kill": "ExitCode", "pid": "Pid", "started": "StartedAt"}[change]] = {
            "oom": True, "kill": 137, "pid": 1, "started": "2026-10-20T00:00:00.1Z"}[change]
    with pytest.raises(RuntimeError):
        host.explained_sampling_transition(*values, **kwargs)


@pytest.mark.parametrize("change", ["missing-snapshot", "dead-type", "exit", "finished", "image", "mount", "proof-id",
    "proof-error", "proof-output", "proof-code", "proof-time", "snapshot-time", "sample-time", "rm-time", "stop-time",
    "no-captured-delete", "late-delete", "absence-false"])
def test_verifier_independently_rechecks_removing_snapshot_absence_and_order(change):
    values, item, registry = removing_evidence()
    sample = {"started_monotonic": 9.0, "finished_monotonic": 12.0}
    if change == "missing-snapshot":
        del item["removing_observation"]
    elif change == "dead-type":
        item["removing_observation"]["state"]["Dead"] = 1
    elif change in {"exit", "finished"}:
        item["removing_observation"]["state"]["ExitCode" if change == "exit" else "FinishedAt"] = (
            137 if change == "exit" else "2026-10-20T00:00:01.1Z")
    elif change == "image":
        item["removing_observation"]["image_id"] = "sha256:" + "c" * 64
    elif change == "mount":
        item["removing_observation"]["mounts"] = []
    elif change == "proof-id":
        item["absence_observation"]["id"] = "c" * 64
    elif change == "proof-error":
        item["absence_observation"]["stderr"] = "Error: No such object: " + "c" * 64
    elif change == "proof-output":
        item["absence_observation"]["stdout"] = "[] extra"
    elif change == "proof-code":
        item["absence_observation"]["returncode"] = True
    elif change == "proof-time":
        item["absence_observation"]["observed_monotonic"] = 10.9
    elif change == "snapshot-time":
        item["removing_observed_monotonic"] = 8.9
    elif change == "sample-time":
        sample["finished_monotonic"] = 11.4
    elif change == "rm-time":
        item["retirement"]["removal_requested_monotonic"] = 11.1
        registry["retirements"][values[0]["id"]]["removal_requested_monotonic"] = 11.1
    elif change == "stop-time":
        item["retirement"]["requested_monotonic"] = 10.75
        registry["retirements"][values[0]["id"]]["requested_monotonic"] = 10.75
    elif change == "no-captured-delete":
        del item["retirement"]["deleted"]
    elif change == "late-delete":
        for record in (item["retirement"], registry["retirements"][values[0]["id"]]):
            record["deleted"]["observed_at"] = "2026-10-20T00:00:04Z"
    else:
        item["absence_observed"] = False
    with pytest.raises(ValueError):
        verifier.validate_sampling_transition(item, [values[0]], registry, sample)


@pytest.mark.parametrize("role", ["acceptance", "worker", "tools"])
@pytest.mark.parametrize("absent", [False, True])
def test_only_registered_actual_terminal_objects_explain_sampling_race(role, absent):
    values = transition_fixture(role, absent=absent)
    item, registry = evidence(values)
    assert verifier.validate_sampling_transition(item, [values[0]], registry, {"finished_monotonic": 12.0}) == values[0]["id"]
    assert item["native_child_health_claimed"] is False
    assert item["commands"]["top"]["returncode"] == 1


@pytest.mark.parametrize("role", ["acceptance", "worker", "model-proxy"])
@pytest.mark.parametrize("absent", [False, True])
def test_stats_eof_requires_recorded_healthy_exit_and_exact_top_confirmation(role, absent):
    values = list(transition_fixture(role, absent=absent))
    values[1]["stats"] = subprocess.CompletedProcess([], 1, "", "EOF\n")
    item, registry = evidence(values)
    assert item["commands"]["stats"]["stderr"] == "EOF\n"
    assert verifier.validate_sampling_transition(item, [values[0]], registry,
        {"finished_monotonic": 12.0}) == values[0]["id"]


@pytest.mark.parametrize("change", ["peer-id", "peer-error", "peer-success", "stats-output", "stats-code", "stats-suffix", "kill", "oom", "no-record"])
def test_stats_eof_is_not_a_general_docker_error_exception(change):
    values = list(transition_fixture("worker", absent=True))
    values[1]["stats"] = subprocess.CompletedProcess([], 1, "", "EOF\n")
    if change.startswith("peer"):
        values[1]["top"] = subprocess.CompletedProcess([], 0 if change == "peer-success" else 1, "",
            "" if change == "peer-success" else "Cannot connect" if change == "peer-error" else
            "Error response from daemon: Container " + "c" * 64 + " is not running")
    elif change in {"stats-output", "stats-code", "stats-suffix"}:
        values[1]["stats"] = subprocess.CompletedProcess([], 2 if change == "stats-code" else 1,
            "unexpected" if change == "stats-output" else "", "EOF extra" if change == "stats-suffix" else "EOF")
    elif change == "no-record":
        values[4] = None
    else:
        values[4]["after_stop"]["state"]["ExitCode" if change == "kill" else "OOMKilled"] = 137 if change == "kill" else True
    with pytest.raises(RuntimeError):
        host.explained_sampling_transition(*values)


@pytest.mark.parametrize("role", ["acceptance", "worker"])
@pytest.mark.parametrize("change", ["id", "image", "caps", "init", "unregistered", "oom", "dead", "error", "kill", "nonzero", "unknown-docker-error", "wrong-docker-id"])
def test_host_rejects_unexplained_or_unhealthy_sampling_transition(role, change):
    values = list(transition_fixture(role))
    before, replies, terminal, registered, retirement, completion = values
    if change == "id":
        registered["id"] = "c" * 64
    elif change == "image":
        registered["image_id"] = "sha256:" + "c" * 64
    elif change == "caps":
        registered["memory_limit"] += 1
    elif change == "init":
        registered["init_enabled"] = not registered["init_enabled"]
    elif change == "unregistered":
        values[3] = None
    elif change in {"oom", "dead", "error"}:
        terminal["state"][{"oom": "OOMKilled", "dead": "Dead", "error": "Error"}[change]] = True
    elif change in {"kill", "nonzero"}:
        terminal["state"]["ExitCode"] = 137 if change == "kill" else 3
    elif change == "unknown-docker-error":
        replies["top"] = subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon")
    else:
        replies["top"] = subprocess.CompletedProcess([], 1, "", "Error response from daemon: Container " + "d" * 64 + " is not running")
    with pytest.raises(RuntimeError):
        host.explained_sampling_transition(*values)


def test_unrequested_wrapper_exit_and_unregistered_removal_are_rejected():
    values = list(transition_fixture("worker"))
    values[4] = None
    with pytest.raises(RuntimeError, match='未针对该确切对象请求采样过渡'):
        host.explained_sampling_transition(*values)
    values = list(transition_fixture(absent=True))
    del values[5]["removal_requested_monotonic"]
    with pytest.raises(RuntimeError, match='此前未观察到确切删除'):
        host.explained_sampling_transition(*values)


@pytest.mark.parametrize("change", ["registration", "init", "terminal", "kind", "native", "missing-final", "future-stop", "absence-id", "unknown-error", "wrong-stats-id"])
def test_verifier_recomputes_transition_identity_and_actual_terminal(change):
    values = transition_fixture("worker", absent=True)
    item, registry = deepcopy(evidence(values))
    if change == "registration":
        item["registration"]["image_id"] = "sha256:" + "d" * 64
    elif change == "init":
        item["registration"]["init_enabled"] = False
    elif change == "terminal":
        item["terminal_observation"]["state"]["ExitCode"] = 137
    elif change == "kind":
        item["kind"] = "registered_bench_healthy_exit"
    elif change == "native":
        item["native_child_health_claimed"] = True
    elif change == "missing-final":
        registry["retirements"].clear()
    elif change == "future-stop":
        item["retirement"]["requested_monotonic"] = 13
    elif change == "absence-id":
        item["absence_observation"]["id"] = "d" * 64
    elif change == "unknown-error":
        item["commands"]["top"]["stderr"] = "Docker timeout"
    else:
        item["commands"]["stats"]["stdout"] = json.dumps({"ID": "d" * 64})
    with pytest.raises(ValueError):
        verifier.validate_sampling_transition(item, [values[0]], registry, {"finished_monotonic": 12.0})


def mounted_transition_evidence():
    values = transition_fixture("tools")
    mounts = [{"Source": "/safe/b", "Destination": "/b", "RW": True},
        {"Source": "/safe/a", "Destination": "/a", "RW": False}]
    for observed in (values[0], values[2], values[3]):
        observed["mounts"] = deepcopy(mounts)
    item, registry = evidence(values)
    # 实际采样器与退出登记持有独立 inspect 对象；
    # 避免共享测试对象掩盖单侧修改。
    item = deepcopy(item)
    item["terminal_observation"] = deepcopy(item["terminal_observation"])
    return values, item, deepcopy(registry)


@pytest.mark.parametrize("duplicate", [False, True])
def test_verifier_ignores_only_mount_order_in_complete_independent_observations(duplicate):
    values, item, registry = mounted_transition_evidence()
    original = deepcopy((item, registry))
    if duplicate:
        for observed in (item["registration"], item["terminal_observation"],
                item["retirement"]["before_stop"], item["retirement"]["after_stop"],
                registry["registrations"][values[0]["id"]],
                registry["retirements"][values[0]["id"]]["before_stop"],
                registry["retirements"][values[0]["id"]]["after_stop"]):
            observed["mounts"].append(deepcopy(observed["mounts"][0]))
    for observed in (item["registration"], item["terminal_observation"],
            item["retirement"]["before_stop"], item["retirement"]["after_stop"]):
        observed["mounts"].reverse()
    before_validation = deepcopy((item, registry))
    assert verifier.validate_sampling_transition(item, [values[0]], registry,
        {"finished_monotonic": 12.0}) == values[0]["id"]
    assert (item, registry) == before_validation
    assert original[0]["terminal_observation"]["mounts"] == values[2]["mounts"]


@pytest.mark.parametrize("change", ["Source", "Destination", "RW", "RW-type", "duplicate", "missing", "extra-field"])
def test_verifier_rejects_any_mount_content_or_multiplicity_change(change):
    values, item, registry = mounted_transition_evidence()
    mounts = item["terminal_observation"]["mounts"]
    if change in {"Source", "Destination"}:
        mounts[0][change] += "-changed"
    elif change == "RW":
        mounts[0]["RW"] = False
    elif change == "RW-type":
        mounts[0]["RW"] = 1
    elif change == "duplicate":
        mounts.append(deepcopy(mounts[0]))
    elif change == "missing":
        mounts.pop()
    else:
        mounts[0]["unexpected"] = "changed"
    with pytest.raises(ValueError, match="sampling_transition_actual_terminal_changed"):
        verifier.validate_sampling_transition(item, [values[0]], registry, {"finished_monotonic": 12.0})


@pytest.mark.parametrize("field", ["id", "name", "image_id", "trial_id", "role", "init_enabled", "memory_limit", "cpu_nano"])
def test_mount_order_normalization_preserves_every_identity_and_quota_field(field):
    values, item, registry = mounted_transition_evidence()
    terminal = item["terminal_observation"]
    value = terminal[field]
    terminal[field] = not value if type(value) is bool else value + 1 if type(value) is int else value + "-changed"
    with pytest.raises(ValueError, match="sampling_transition_actual_terminal_changed"):
        verifier.validate_sampling_transition(item, [values[0]], registry, {"finished_monotonic": 12.0})


@pytest.mark.parametrize("field", ["Status", "Running", "ExitCode", "Pid", "Paused", "Restarting", "OOMKilled", "Dead", "Error", "StartedAt", "FinishedAt"])
def test_mount_order_normalization_preserves_every_native_state_field(field):
    values, item, registry = mounted_transition_evidence()
    state = item["terminal_observation"]["state"]
    value = state.get(field, "2026-10-20T00:00:00Z")
    state[field] = not value if type(value) is bool else value + 1 if type(value) is int else value + "-changed"
    with pytest.raises(ValueError, match="sampling_transition_actual_terminal_changed"):
        verifier.validate_sampling_transition(item, [values[0]], registry, {"finished_monotonic": 12.0})


def test_host_captures_stable_mount_order_without_dropping_duplicate_items():
    observed = container("tools")
    first = {"Source": "/safe/b", "Destination": "/b", "RW": True}
    second = {"Source": "/safe/a", "Destination": "/a", "RW": False}
    raw = {"Id": observed["id"], "Name": observed["name"], "Image": observed["image_id"],
        "Config": {"Image": observed["image_id"], "Labels": {
            "org.harness-lab.multi-user-trial": observed["trial_id"], "org.harness-lab.role": "tools"}},
        "State": observed["state"], "HostConfig": {"Memory": observed["memory_limit"],
            "NanoCpus": observed["cpu_nano"], "Init": True}, "NetworkSettings": {"Networks": {}},
        "Mounts": [first, second, deepcopy(first)]}
    unchanged = deepcopy(raw)
    assert host.container_observation(raw)["mounts"] == [second, first, first]
    assert raw == unchanged


def test_sampler_retains_real_commands_and_terminal_transition_without_fabricated_metrics():
    values = transition_fixture(absent=True)
    before, replies, _, registered, _, completion = values
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args, lab.phase = SimpleNamespace(trial_id="offline"), "main-prepare"
    lab.registry_lock = threading.RLock()
    lab.registrations, lab.retirements, lab.bench_completions = {before["id"]: registered}, {}, {before["id"]: completion}
    lab.overages = []
    inventories = iter([[before], []])
    lab.inventory = lambda: next(inventories)
    lab.observation = lambda _: None
    lab.call = lambda argv, **kwargs: replies[argv[1]]
    sample = lab.sample_once()
    assert sample["containers"] == sample["process_observations"] == []
    assert len(sample["transition_observations"]) == 1
    assert sample["allocated_memory_bytes"] == before["memory_limit"]
    assert sample["transition_observations"][0]["absence_observation"]["id_absent"] is True


@pytest.mark.parametrize("exists", [False, True])
def test_sampler_removing_path_performs_new_exact_id_inspect_and_keeps_original_snapshot(exists):
    values, _ = removing_fixture()
    before, replies, removing, registered, retirement, _ = values
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args, lab.phase = SimpleNamespace(trial_id="offline"), "main-resume"
    lab.registry_lock = threading.RLock()
    lab.registrations, lab.retirements, lab.bench_completions = {before["id"]: registered}, {before["id"]: retirement}, {}
    lab.overages = []
    inventories = iter([[before], [removing]])
    lab.inventory = lambda: next(inventories)
    inspected = []

    def call(argv, **kwargs):
        if argv[1] != "inspect":
            return replies[argv[1]]
        inspected.append(argv)
        return subprocess.CompletedProcess(argv, 0 if exists else 1,
            '[{"Config":{"Env":["MODEL_API_KEY=must-not-enter-evidence"]}}]' if exists else "[]\n",
            "" if exists else "Error: No such object: " + before["id"] + "\n")

    lab.call = call
    if exists:
        with pytest.raises(host.SamplingFailure) as caught:
            lab.sample_once()
        assert caught.value.code == "transition_removing_unproven"
        assert caught.value.observations["docker_after_observations"] == [removing]
        proof = caught.value.observations["removal_absence_observations"][before["id"]]
        assert proof["returncode"] == 0 and proof["stdout"] is None and proof["id_absent"] is False
        assert "must-not-enter-evidence" not in json.dumps(caught.value.observations)
    else:
        sample = lab.sample_once()
        item = sample["transition_observations"][0]
        assert sample["containers"] == sample["process_observations"] == []
        assert item["removing_observation"] == removing
        assert item["terminal_observation"] == retirement["after_stop"]
        assert item["absence_observation"]["stdout"] == "[]\n"
        registry = {"registrations": lab.registrations, "retirements": lab.retirements, "bench_completions": {}}
        assert verifier.validate_sampling_transition(item, [before], registry, sample) == before["id"]
    assert inspected == [["docker", "inspect", before["id"]]]


@pytest.mark.parametrize("registration_during_inspect", ["deleted", "missing", "changed-terminal"])
def test_sampler_refreshes_only_unchanged_registry_fields_after_exact_absence_inspect(registration_during_inspect):
    values, _ = removing_fixture()
    before, replies, removing, registered, retirement, _ = values
    del retirement["deleted"]
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args, lab.phase = SimpleNamespace(trial_id="offline"), "main-resume"
    lab.registry_lock = threading.RLock()
    lab.registrations, lab.retirements, lab.bench_completions = {before["id"]: registered}, {before["id"]: retirement}, {}
    lab.overages = []
    inventories = iter([[before], [removing]])
    lab.inventory = lambda: next(inventories)
    inspected = []

    def call(argv, **kwargs):
        assert not lab.registry_lock._is_owned()
        if argv[1] != "inspect":
            return replies[argv[1]]
        inspected.append(argv)
        if registration_during_inspect != "missing":
            with lab.registry_lock:
                retirement["deleted"] = {"id": before["id"], "id_absent": True, "observed_at": host.now()}
                if registration_during_inspect == "changed-terminal":
                    retirement["after_stop"]["state"]["ExitCode"] = 137
        return subprocess.CompletedProcess(argv, 1, "[]\n", "Error: No such object: " + before["id"] + "\n")

    lab.call = call
    if registration_during_inspect == "deleted":
        sample = lab.sample_once()
        item = sample["transition_observations"][0]
        assert item["retirement"]["deleted"] == retirement["deleted"]
        registry = {"registrations": lab.registrations, "retirements": lab.retirements, "bench_completions": {}}
        assert verifier.validate_sampling_transition(item, [before], registry, sample) == before["id"]
    else:
        with pytest.raises(host.SamplingFailure) as caught:
            lab.sample_once()
        assert caught.value.code == ("transition_removing_unproven" if registration_during_inspect == "missing"
            else "transition_registry_changed_during_confirmation")
    assert inspected == [["docker", "inspect", before["id"]]]


def test_bench_registers_created_exact_identity_before_start_and_keeps_terminal(tmp_path):
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args = SimpleNamespace(trial_id="offline", date="2026-10-20", seed=101)
    lab.config = SimpleNamespace(host_data_root=tmp_path)
    lab.global_paths = SimpleNamespace(run=tmp_path)
    lab.sequence, lab.owned, lab.registered = 0, [], {}
    lab.registry_lock = threading.RLock()
    lab.registrations, lab.retirements, lab.bench_completions = {}, {}, {}
    lab.image = {"bench": "sha256:" + "b" * 64}
    lab.inventory = lambda: []
    observations = iter([container(status="created"), container(status="exited"), None])
    lab.observation = lambda _: next(observations)
    commands = []

    def call(argv, **kwargs):
        commands.append(argv)
        if argv[1] == "create":
            return subprocess.CompletedProcess(argv, 0, "a" * 64 + "\n", "")
        if argv[1] == "start":
            assert "a" * 64 in lab.registrations
            return subprocess.CompletedProcess(argv, 0, "offline output\n", "")
        assert argv[1] == "rm"
        return subprocess.CompletedProcess(argv, 0, "", "")

    lab.call = call
    assert lab.bench("bind-start") == 0
    assert [argv[1] for argv in commands] == ["create", "start", "rm"]
    assert "--init" not in commands[0]
    assert lab.bench_completions["a" * 64]["after_stop"]["state"]["ExitCode"] == 0
    assert lab.bench_completions["a" * 64]["deleted"]["id_absent"] is True


@pytest.mark.parametrize("role", ["worker", "tools", "model-proxy", "fixtures", "backend"])
def test_long_lived_create_enables_init_before_registration_and_start(role):
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args = SimpleNamespace(trial_id="offline")
    lab.owned, lab.registered, commands = [], {}, []
    lab.image = {"worker": "sha256:" + "b" * 64, "services": "sha256:" + "c" * 64}
    lab.register_container = lambda identity: commands.append(("registered", identity))

    def call(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "a" * 64 + "\n", "")

    lab.call = call
    assert lab.create(role, role, "private-network", role, [], ["-m", "offline"], worker=role == "worker") == "a" * 64
    assert commands[0][0:3] == ["docker", "create", "--init"]
    assert commands[1] == ("registered", "a" * 64)
    assert commands[2] == ["docker", "start", "a" * 64]


@pytest.mark.parametrize("role", ["worker", "tools", "model-proxy", "fixtures", "backend"])
@pytest.mark.parametrize("init_value", [True, False, None, 1, "true", "missing"])
def test_registration_requires_actual_boolean_init_for_every_long_lived_role(role, init_value):
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    observed = container(role, status="created")
    if init_value == "missing":
        del observed["init_enabled"]
    else:
        observed["init_enabled"] = init_value
    observed["memory_limit"], observed["cpu_nano"] = ((2 * 1024**3, 750000000)
        if role == "worker" else (256 * 1024**2, 250000000))
    lab.args = SimpleNamespace(trial_id="offline")
    lab.owned, lab.registered = [observed["name"].lstrip("/")], {observed["id"]: role}
    lab.image = {"worker": observed["image_id"], "services": observed["image_id"]}
    lab.registry_lock, lab.registrations = threading.RLock(), {}
    lab.observation = lambda _: observed
    if init_value is True:
        lab.register_container(observed["id"])
        assert lab.registrations[observed["id"]]["init_enabled"] is True
    else:
        with pytest.raises(RuntimeError, match='创建容器的确切身份'):
            lab.register_container(observed["id"])


def failing_sampler_lab():
    values = transition_fixture("worker")
    before, replies, terminal, registered, retirement, _ = values
    terminal["state"]["ExitCode"] = 137
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args, lab.phase = SimpleNamespace(trial_id="offline"), "main-baseline"
    lab.registry_lock = threading.RLock()
    lab.registrations, lab.retirements, lab.bench_completions = {before["id"]: registered}, {before["id"]: retirement}, {}
    lab.overages = []
    inventories = iter([[before], [terminal]])
    lab.inventory = lambda: next(inventories)
    lab.call = lambda argv, **kwargs: replies[argv[1]]
    return lab, before, terminal, replies


def test_sampler_keeps_original_commands_and_safe_before_after_for_unhealthy_terminal():
    lab, before, terminal, replies = failing_sampler_lab()
    with pytest.raises(host.SamplingFailure) as caught:
        lab.sample_once()
    failure = caught.value
    assert failure.code == "transition_terminal_not_healthy"
    assert failure.observations["docker_observations"] == [before]
    assert failure.observations["docker_after_observations"] == [terminal]
    assert failure.observations["container_id"] == before["id"]
    assert failure.observations["command_observations"][before["id"]] == {
        kind: {"returncode": reply.returncode, "stdout": reply.stdout, "stderr": reply.stderr}
        for kind, reply in replies.items()}
    assert "containers" not in failure.observations  # 不伪造指标样本。


def test_sampler_retains_partial_timeout_output_without_exception_message_or_fake_returncode():
    lab, before, _, replies = failing_sampler_lab()

    def call(argv, **kwargs):
        if argv[1] == "stats":
            raise subprocess.TimeoutExpired("exception-command-must-not-enter-evidence", 10,
                output=b"actual-partial-stats", stderr=b"actual-partial-stderr")
        return replies["top"]

    lab.call = call
    with pytest.raises(host.SamplingFailure) as caught:
        lab.sample_once()
    assert caught.value.code == "sampling_command_timeout"
    commands = caught.value.observations["command_observations"][before["id"]]
    assert commands["stats"]["returncode"] is None
    assert commands["stats"]["stdout"] == "actual-partial-stats"
    assert commands["stats"]["stderr"] == "actual-partial-stderr"
    assert commands["top"]["stderr"] == replies["top"].stderr
    assert "exception-command-must-not-enter-evidence" not in json.dumps(caught.value.observations)


def test_sampler_retains_completed_commands_when_after_inventory_fails():
    lab, before, _, replies = failing_sampler_lab()
    calls = 0

    def inventory():
        nonlocal calls
        calls += 1
        if calls == 1:
            return [before]
        raise host.SamplingFailure("inventory_identity_set_changed", {"inventory_docker_observations": []})

    lab.inventory = inventory
    with pytest.raises(host.SamplingFailure) as caught:
        lab.sample_once()
    assert caught.value.code == "inventory_identity_set_changed"
    assert caught.value.observations["docker_observations"] == [before]
    assert caught.value.observations["docker_after_observations"] == []
    assert caught.value.observations["command_observations"][before["id"]]["top"]["stderr"] == replies["top"].stderr


def test_sampler_loop_never_serializes_unclassified_exception_message(tmp_path):
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.global_paths, lab.phase = SimpleNamespace(run=tmp_path), "main-prepare"
    lab.stop_sampling, lab.sampling_errors = threading.Event(), []

    def fail():
        lab.stop_sampling.set()
        raise RuntimeError("private-message-must-not-enter-evidence")

    lab.sample_once = fail
    lab.sample_loop()
    raw = (tmp_path / "resource-samples.jsonl").read_text()
    assert "private-message-must-not-enter-evidence" not in raw
    assert json.loads(raw)["reason_code"] == "sampling_unclassified_failure"
    assert len(lab.sampling_errors) == 1


def inventory_lab(*, error=None):
    values = transition_fixture(absent=True)
    missing, _, _, registered, _, completion = values
    existing_id = "d" * 64
    raw = {"Id": existing_id, "Name": "/retained-proxy", "Image": "sha256:" + "c" * 64,
        "Config": {"Image": "sha256:" + "c" * 64, "Env": ["MODEL_API_KEY=must-not-enter-evidence"],
            "Labels": {"org.harness-lab.multi-user-trial": "offline", "org.harness-lab.role": "model-proxy"}},
        "State": container()["state"], "HostConfig": {"Memory": 256 * 1024**2, "NanoCpus": 250000000, "Init": True},
        "NetworkSettings": {"Networks": {}}, "Mounts": []}
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.registry_lock = threading.RLock()
    lab.registrations, lab.retirements = {missing["id"]: registered}, {}
    lab.bench_completions, lab.inventory_absences = {missing["id"]: completion}, []

    def call(argv, **kwargs):
        if argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 0, missing["id"] + "\n" + existing_id + "\n", "")
        assert argv[1] == "inspect" and kwargs["check"] is False
        return subprocess.CompletedProcess(argv, 1, json.dumps([raw]),
            error or "Error: No such object: " + missing["id"] + "\n")

    lab.call = call
    return lab


def test_inventory_ps_to_inspect_exact_registered_removal_keeps_other_objects_and_redacts_secrets():
    lab = inventory_lab()
    observed = lab.inventory()
    assert len(observed) == 1 and observed[0]["id"] == "d" * 64
    assert observed[0]["init_enabled"] is True
    assert "must-not-enter-evidence" not in json.dumps(observed)
    assert len(lab.inventory_absences) == 1
    registry = {"registrations": lab.registrations, "retirements": lab.retirements,
        "bench_completions": lab.bench_completions}
    verifier.validate_inventory_absence(lab.inventory_absences[0], registry)
    assert "must-not-enter-evidence" not in json.dumps(lab.inventory_absences)


def test_failed_inventory_diagnostic_retains_safe_projection_without_raw_inspect_env():
    lab = inventory_lab(error="Cannot connect to the Docker daemon")
    with pytest.raises(host.SamplingFailure) as caught:
        lab.inventory()
    assert caught.value.code == "inventory_inspect_error_unexplained"
    observations = caught.value.observations["inventory_docker_observations"]
    assert observations[0]["init_enabled"] is True
    assert observations[0]["environment_names"] == ["MODEL_API_KEY"]
    assert "must-not-enter-evidence" not in json.dumps(caught.value.observations)
    assert "inventory_inspect_stdout" not in caught.value.observations


@pytest.mark.parametrize("change", ["unknown-error", "wrong-id", "unregistered", "no-removal", "oom", "kill"])
def test_inventory_never_swallows_unregistered_disappearance_or_other_inspect_errors(change):
    error = "Cannot connect to the Docker daemon" if change == "unknown-error" else (
        "Error: No such object: " + "e" * 64 if change == "wrong-id" else None)
    lab = inventory_lab(error=error)
    if change == "unregistered":
        lab.registrations.clear()
    elif change == "no-removal":
        del lab.bench_completions["a" * 64]["removal_requested_monotonic"]
    elif change == "oom":
        lab.bench_completions["a" * 64]["after_stop"]["state"]["OOMKilled"] = True
    elif change == "kill":
        lab.bench_completions["a" * 64]["after_stop"]["state"]["ExitCode"] = 137
    with pytest.raises(RuntimeError):
        lab.inventory()


@pytest.mark.parametrize("change", ["unknown-error", "wrong-id", "unregistered", "no-removal", "oom", "kill", "source-replaced"])
def test_inventory_absence_verifier_rechecks_source_and_registry(change):
    lab = inventory_lab()
    lab.inventory()
    item, registry = deepcopy((lab.inventory_absences[0], {"registrations": lab.registrations,
        "retirements": lab.retirements, "bench_completions": lab.bench_completions}))
    if change == "unknown-error":
        item["inspect_stderr"] = "Docker timeout"
    elif change == "wrong-id":
        item["container_id"] = "e" * 64
    elif change == "unregistered":
        registry["registrations"].clear()
    elif change == "no-removal":
        del registry["bench_completions"]["a" * 64]["removal_requested_monotonic"]
    elif change in {"oom", "kill"}:
        terminal = registry["bench_completions"]["a" * 64]["after_stop"]["state"]
        terminal["OOMKilled" if change == "oom" else "ExitCode"] = True if change == "oom" else 137
    else:
        item["registration"]["image_id"] = "sha256:" + "e" * 64
    with pytest.raises(ValueError):
        verifier.validate_inventory_absence(item, registry)


def cleanup_lab(tmp_path, *, mode=None):
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args = SimpleNamespace(trial_id="offline")
    lab.global_paths = SimpleNamespace(run=tmp_path)
    snapshot = container("fixtures")
    snapshot["name"] = "/harness-lab-offline-fixtures"
    registered = deepcopy(snapshot)
    registered["state"].update(Status="created", Running=False, Pid=0)
    lab.owned, lab.registered = [snapshot["name"].lstrip("/")], {snapshot["id"]: "fixtures"}
    lab.registrations, lab.retirements, lab.bench_completions = {snapshot["id"]: registered}, {}, {}
    lab.image = {"services": snapshot["image_id"]}
    network_id, network_name = "c" * 64, "harness-lab-offline-a"
    lab.networks = [network_name]
    lab.network_registrations = {network_id: {"id": network_id, "name": network_name, "trial_id": "offline"}}
    present = {"container": mode not in {"prior-absent-proven", "prior-absent-unproven"}, "network": True}
    if mode == "prior-absent-proven":
        terminal = deepcopy(snapshot)
        terminal["state"].update(Status="exited", Running=False, Pid=0, ExitCode=143)
        lab.retirements[snapshot["id"]] = {"after_stop": terminal,
            "deleted": {"id": snapshot["id"], "id_absent": True, "observed_at": "2026-10-20T00:00:02Z"}}
    calls = []

    def call(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            assert argv == ["docker", "inspect", snapshot["id"]]
            if mode == "inspect-failure":
                return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to Docker")
            if not present["container"]:
                return subprocess.CompletedProcess(argv, 1, "[]\n", "Error: No such object: " + snapshot["id"])
            raw = {"Id": "d" * 64 if mode == "wrong-inspect-id" else snapshot["id"], "Name": snapshot["name"],
                "Image": snapshot["image_id"], "Config": {"Image": snapshot["image_id"],
                    "Env": ["MODEL_API_KEY=must-not-enter-evidence"], "Labels": {
                        "org.harness-lab.multi-user-trial": "foreign" if mode == "foreign-container" else "offline",
                        "org.harness-lab.role": "fixtures"}},
                "State": deepcopy(snapshot["state"]), "HostConfig": {"Memory": snapshot["memory_limit"],
                    "NanoCpus": snapshot["cpu_nano"], "Init": True}, "NetworkSettings": {"Networks": {}}, "Mounts": []}
            return subprocess.CompletedProcess(argv, 0, json.dumps([raw]), "")
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 0, "actual fixture log\n", "")
        if argv[1] == "stop":
            assert argv[-1] == snapshot["id"]
            snapshot["state"].update(Status="exited", Running=False, Pid=0, ExitCode=143)
            return subprocess.CompletedProcess(argv, 0, snapshot["id"] + "\n", "")
        if argv[1] == "rm":
            assert argv == ["docker", "rm", "-f", snapshot["id"]]
            present["container"] = mode == "remove-still-present"
            return subprocess.CompletedProcess(argv, 0, snapshot["id"] + "\n", "")
        if argv[1] == "ps":
            assert argv[-2:] == ["--filter", "label=org.harness-lab.multi-user-trial=offline"]
            if mode == "final-list-failure":
                return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to Docker")
            remaining = snapshot["id"] if present["container"] and mode != "foreign-container" else ""
            return subprocess.CompletedProcess(argv, 0, remaining, "")
        assert argv[1] == "network"
        if argv[2] == "inspect":
            assert argv[-1] == network_id
            if not present["network"]:
                return subprocess.CompletedProcess(argv, 1, "[]\n", "Error response from daemon: network " + network_id + " not found")
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"Id": network_id, "Name": network_name,
                "Labels": {"org.harness-lab.multi-user-trial": "foreign" if mode == "foreign-network" else "offline"},
                "Containers": {}}]), "")
        if argv[2] == "rm":
            assert argv == ["docker", "network", "rm", network_id]
            present["network"] = mode == "network-still-present"
            return subprocess.CompletedProcess(argv, 0, network_id + "\n", "")
        assert argv[2] == "ls"
        assert argv[-2:] == ["--filter", "label=org.harness-lab.multi-user-trial=offline"]
        return subprocess.CompletedProcess(argv, 0, network_id if present["network"] and mode != "foreign-network" else "", "")

    lab.call = call
    return lab, calls


def test_cleanup_records_exact_fixture_terminal_deletion_networks_and_empty_trial_inventory(tmp_path):
    lab, calls = cleanup_lab(tmp_path)
    assert lab.cleanup() == []
    result = json.loads((tmp_path / "cleanup-result.json").read_text())
    assert result["passed"] is True and result["native_child_health_claimed"] is False
    container_result = result["containers"][0]
    assert container_result["before_cleanup"]["observation"]["state"]["Running"] is True
    assert container_result["terminal_observation"]["observation"]["state"]["Status"] == "exited"
    assert container_result["terminal_observation"]["observation"]["state"]["ExitCode"] == 143
    assert container_result["after_remove"]["id_absent"] is True
    assert result["networks"][0]["after_remove"]["id_absent"] is True
    assert all(item["confirmed"] is True and item["ids"] == [] for item in result["final_inventory"])
    assert "must-not-enter-evidence" not in json.dumps(result)
    assert ["docker", "rm", "-f", "a" * 64] in calls
    assert ["docker", "network", "rm", "c" * 64] in calls


@pytest.mark.parametrize("mode", ["foreign-container", "wrong-inspect-id", "inspect-failure"])
def test_cleanup_never_mutates_container_with_foreign_or_unproven_identity_and_always_records_failure(tmp_path, mode):
    lab, calls = cleanup_lab(tmp_path, mode=mode)
    assert lab.cleanup()
    result = json.loads((tmp_path / "cleanup-result.json").read_text())
    assert result["passed"] is False
    assert not any(argv[1] in {"stop", "rm", "logs"} for argv in calls)
    assert "must-not-enter-evidence" not in json.dumps(result)


def test_cleanup_never_deletes_foreign_network_even_with_previously_owned_name_and_id(tmp_path):
    lab, calls = cleanup_lab(tmp_path, mode="foreign-network")
    assert lab.cleanup()
    result = json.loads((tmp_path / "cleanup-result.json").read_text())
    assert result["networks"][0]["passed"] is False
    assert ["docker", "network", "rm", "c" * 64] not in calls


@pytest.mark.parametrize("mode", ["remove-still-present", "network-still-present", "final-list-failure", "prior-absent-unproven"])
def test_cleanup_requires_positive_absence_prior_deletion_and_successful_final_queries(tmp_path, mode):
    lab, _ = cleanup_lab(tmp_path, mode=mode)
    assert lab.cleanup()
    result = json.loads((tmp_path / "cleanup-result.json").read_text())
    assert result["passed"] is False
    if mode == "remove-still-present":
        assert result["containers"][0]["remove_returncode"] == 0
        assert result["containers"][0]["after_remove"]["status"] == "present"
    elif mode == "network-still-present":
        assert result["networks"][0]["remove_returncode"] == 0
        assert result["networks"][0]["after_remove"]["status"] == "present"
    elif mode == "final-list-failure":
        assert result["containers"][0]["passed"] is True
        assert result["final_inventory"][0]["confirmed"] is False
    else:
        assert result["containers"][0]["before_cleanup"]["status"] == "absent"
        assert result["containers"][0]["passed"] is False


def test_cleanup_accepts_exact_absence_for_previously_observed_and_registered_deletion(tmp_path):
    lab, calls = cleanup_lab(tmp_path, mode="prior-absent-proven")
    assert lab.cleanup() == []
    result = json.loads((tmp_path / "cleanup-result.json").read_text())
    assert result["containers"][0]["prior_terminal_observation"]["state"]["ExitCode"] == 143
    assert result["containers"][0]["prior_deleted"]["id_absent"] is True
    assert not any(argv[1] in {"stop", "rm", "logs"} for argv in calls)


@pytest.mark.parametrize("change", ["running", "wrong-terminal-id", "missing-delete-time"])
def test_cleanup_absent_owned_id_does_not_hide_unknown_or_changed_prior_terminal(tmp_path, change):
    lab, _ = cleanup_lab(tmp_path, mode="prior-absent-proven")
    record = lab.retirements["a" * 64]
    if change == "running":
        record["after_stop"]["state"].update(Status="running", Running=True, Pid=123)
    elif change == "wrong-terminal-id":
        record["after_stop"]["id"] = "d" * 64
    else:
        del record["deleted"]["observed_at"]
    assert lab.cleanup()
    assert json.loads((tmp_path / "cleanup-result.json").read_text())["containers"][0]["passed"] is False


def test_network_creation_keeps_exact_create_ids_for_later_ownership_checked_cleanup():
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args, lab.networks, lab.network_registrations = SimpleNamespace(trial_id="offline"), [], {}
    identities = iter(["a" * 64, "b" * 64])
    lab.call = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, next(identities) + "\n", "")
    lab.create_networks()
    assert lab.network_registrations == {identity: {"id": identity, "name": "harness-lab-offline-" + letter,
        "trial_id": "offline"} for identity, letter in (("a" * 64, "a"), ("b" * 64, "b"))}


@pytest.mark.parametrize("failure", [None, "sampling", "overage"])
def test_baseline_host_only_starts_A_main_and_never_enters_pause_resume_cancel(tmp_path, monkeypatch, failure):
    class NoThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def join(self, **kwargs):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(host.threading, "Thread", NoThread)
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args = SimpleNamespace(trial_id="offline", date="2026-10-20", seed=101, baseline=True)
    lab.global_paths = SimpleNamespace(run=tmp_path)
    lab.registry_lock, lab.stop_sampling = threading.RLock(), threading.Event()
    lab.registrations, lab.retirements, lab.bench_completions, lab.inventory_absences = {}, {}, {}, []
    lab.registered, lab.records, lab.sampling_errors, lab.overages, lab.sampler = {}, [], [], [], None
    lab.proxy_env_file = tmp_path / "proxy.env"
    lab.proxy_env_file.write_text("offline-only\n")
    actions = []
    lab.preflight = lambda: None
    lab.create_networks = lambda: None
    lab.start_fixture = lambda: None
    lab.bench = lambda action, **kwargs: actions.append(("bench", action)) or 0
    lab.start_services = lambda kind, **kwargs: actions.append(("services", kind, kwargs)) or ["tools-a", "proxy-a"]
    lab.start_workers = lambda kind, attempt, **kwargs: actions.append(("workers", kind, attempt, kwargs)) or ["worker-a"]
    lab.start_backend = lambda kind, **kwargs: actions.append(("backend", kind, kwargs)) or "backend-a"
    def collector(backend, kind, phase):
        actions.append(("collector", kind, phase))
        if failure == "sampling":
            lab.sampling_errors.append({"reason_code": "sampling_observation_failed"})
        elif failure == "overage":
            lab.overages.append({"memory": 7 * 1024**3})

    lab.collector = collector
    lab.retire = lambda identity: actions.append(("retire", identity))
    lab.cleanup = lambda: []
    lab.call = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "offline-log\n", "")
    lab.inventory = lambda: [{"id": role, "role": role, "state": {"Running": True},
        "memory_limit": 2 * 1024**3 if role == "worker" else 256 * 1024**2,
        "cpu_nano": 750000000 if role == "worker" else 250000000}
        for role in ("worker", "tools", "model-proxy", "fixtures", "backend")]
    assert lab.run() == (0 if failure is None else 1)
    assert ("workers", "main", "worker-a", {"letters": ("a",)}) in actions
    assert ("services", "main", {"letters": ("a",)}) in actions
    assert ("backend", "main", {"start_tasks": ("task-a",)}) in actions
    assert ("collector", "main", "baseline") in actions
    assert ("bench", "baseline-grade") in actions
    assert not any("cancel" in str(action) or "resume" in str(action) or "worker-b" in str(action) for action in actions)
    assert json.loads((tmp_path / "host-result.json").read_text())["baseline_only"] is True
    assert json.loads((tmp_path / "host-result.json").read_text())["passed"] is (failure is None)
