"""I22 宿主传输测试初稿，最初仅在 /tmp 编写。

下方 Docker、进程和时钟操作全部为测试替身，
只验证编排边界，不证明框架行为或候选能力。
"""
from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess

import pytest

from runner.config import load_config
from scripts import run_pause_recovery as host


CANDIDATE = "sha256:" + "a" * 64
BENCH = "sha256:" + "b" * 64
SERVICES = "sha256:" + "c" * 64
WORKER_ID = "d" * 64


class Clock:
    value = 0.0

    def monotonic(self):
        return self.value


class ThreadDouble:
    """不运行线程，只记录各控制事件是否停止并完成 join。"""
    created = []

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args, self.joined, self.started = target, args, False, False
        self.created.append(self)

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.joined = True

    def is_alive(self):
        return False


class DockerDoubleLab(host.PauseLab):
    def __init__(self, config, clock):
        self.config, self.clock = config, clock
        self.env = {"HOST_ONLY_SECRET": "must-not-leak"}
        self.prefix = ["docker", "compose", "--project-name", "harness-lab", "-f", "compose.yml"]
        self.commands, self.raw, self.bench_actions, self.wait_calls = [], {}, [], []
        self.counter = 0
        self.create_cost, self.execution_cost = 4.2, 8.3
        self.fail_create = False
        self.corruption = None
        self.attach_timeouts = []

    def call(self, args, **kwargs):
        args = list(args)
        self.commands.append(tuple(args))
        if args[:3] == ["docker", "image", "inspect"]:
            image = args[-1]
            value = CANDIDATE if image == "candidate:mutable" else BENCH if image == "harness-lab-bench:local" else SERVICES
            return subprocess.CompletedProcess(args, 0, value + "\n", "")
        if args[:2] == ["docker", "compose"]:
            if "config" in args:
                payload = {"services": {"model-proxy": {"environment": {
                    "MODEL_NAME": "selected-model", "MODEL_API_KEY": "must-not-leak"}}}}
                return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["docker", "create"]:
            self.counter += 1
            identity = format(self.counter, "064x")
            name = args[args.index("--name") + 1]
            mounts = []
            for index, word in enumerate(args):
                if word == "--mount":
                    parts = dict(piece.split("=", 1) for piece in args[index + 1].split(",") if "=" in piece)
                    mounts.append({"Source": parts["src"], "Destination": parts["dst"],
                                   "RW": "readonly" not in args[index + 1].split(",")})
            image = next(word for word in args if word.startswith("sha256:"))
            self.raw[identity] = {"Id": identity, "Name": "/" + name, "Image": image,
                "Config": {"Image": image, "Env": ["SYNTHETIC_PRIVATE=must-not-leak"]},
                "HostConfig": {"Memory": 2 * 1024**3, "NanoCpus": 10**9}, "Mounts": mounts,
                "State": {"Status": "created", "Running": False}}
            self.clock.value += self.create_cost
            if self.fail_create:
                # CLI 超时前，Docker 可能已经创建指定名称的容器。
                raise subprocess.TimeoutExpired(args, 30)
            return subprocess.CompletedProcess(args, 0, identity + "\n", "")
        if args[:2] == ["docker", "inspect"]:
            identity = args[2]
            matches = [record for record in self.raw.values() if record["Id"] == identity or record["Name"] == "/" + identity]
            if not matches:
                return subprocess.CompletedProcess(args, 1, "[]", "Error: No such object: " + identity)
            output = deepcopy(matches)
            if self.corruption == "image":
                output[0]["Image"] = SERVICES
            elif self.corruption == "writable_control":
                next(row for row in output[0]["Mounts"] if row["Destination"] == "/config/lifecycle.json")["RW"] = True
            elif self.corruption == "extra_mount":
                output[0]["Mounts"].append({"Source": "/host-private", "Destination": "/extra", "RW": True})
            return subprocess.CompletedProcess(args, 0, json.dumps(output), "")
        if args[:2] in (["docker", "kill"], ["docker", "rm"]):
            name = args[-1]
            matches = [key for key, record in self.raw.items() if key == name or record["Name"] == "/" + name]
            for key in matches:
                if args[1] == "rm":
                    del self.raw[key]
                else:
                    self.raw[key]["State"] = self.exited()
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(args, 0, '{"passed":true}\n', "")
        raise AssertionError("非预期 Docker 传输命令：" + repr(args))

    @staticmethod
    def exited():
        return {"Status": "exited", "Pid": 0, "ExitCode": 0, "OOMKilled": False,
                "Dead": False, "Running": False, "Paused": False, "Restarting": False, "Error": ""}

    def attach(self, args, **kwargs):
        assert args[:3] == ["docker", "start", "--attach"]
        self.commands.append(tuple(args))
        self.attach_timeouts.append(kwargs["timeout"])
        self.clock.value += self.execution_cost
        self.raw[args[-1]]["State"] = self.exited()
        return subprocess.CompletedProcess(args, 0)

    def containers(self):
        return [{"id": row["Id"], "name": row["Name"], "image_id": row["Image"],
                 "state": row["State"]} for row in self.raw.values()]

    def wait_health(self, service, port):
        self.commands.append(("health", service, str(port)))

    def sample(self, event, samples, evidence):
        raise AssertionError("线程替身不得实际执行采样")

    def bench(self, action, **kwargs):
        self.bench_actions.append((action, kwargs))
        if action == "prepare":
            for directory in (self.paths.run, self.paths.state, self.paths.workspace, self.paths.budget):
                directory.mkdir(parents=True)
            (self.paths.run / "trusted").mkdir()
            for attempt in ("worker-a", "worker-b"):
                (self.paths.run / "attempts" / attempt / "candidate").mkdir(parents=True)
                controls = self.paths.fixture / "attempts" / attempt
                controls.mkdir(parents=True)
                for name in ("request.json", "lifecycle.json", "lifecycle-state.json", "lifecycle-pause.json"):
                    (controls / name).write_text("{}")
            for name in ("scope.json", "scope.token"):
                (self.paths.fixture / name).write_text("{}")
            (self.paths.budget / "run.token").write_text("synthetic-private-token")
        return subprocess.CompletedProcess([action], 0, "", "")

    def wait_after_delete(self, a):
        self.wait_calls.append(a)
        assert a["deleted"]["id_absent"] and a["deleted"]["name_absent"]


@pytest.fixture
def lab_fixture(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    materials = repository / "external/build-context/lifecycle"
    materials.mkdir(parents=True)
    (materials / "SOURCE_MANIFEST.json").write_text(json.dumps({"export_sha256": "e" * 64}))
    (materials / "LAB_ADAPTER_MANIFEST.json").write_text(json.dumps({"export_sha256": "f" * 64}))
    helper = repository / "adapters/deepagents_langgraph/pause_recovery.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# inert test material; never imported\n")
    monkeypatch.setattr(host, "ROOT", repository)
    clock = Clock()
    config = replace(load_config(), host_data_root=tmp_path / "data")
    lab = DockerDoubleLab(config, clock)
    args = Namespace(candidate="deepagents_langgraph", chain_id="host-unit", seed=101,
                     variant="uninterrupted", restart_proxy=False, image_tag="candidate:mutable", materials_context=materials)
    ThreadDouble.created = []
    monkeypatch.setattr(host.threading, "Thread", ThreadDouble)
    monkeypatch.setattr(host.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(host.subprocess, "run", lab.attach)
    return lab, args, clock


def test_worker_uses_immutable_image_exact_mounts_and_caps(lab_fixture):
    lab, args, _ = lab_fixture
    assert lab.run_chain(args) == 0
    command = next(command for command in lab.commands if command[:2] == ("docker", "create"))
    assert command[command.index("--memory") + 1] == "2g"
    assert command[command.index("--cpus") + 1] == "1"
    assert CANDIDATE in command and "candidate:mutable" not in command
    assert "--read-only" in command
    mounts = [command[index + 1] for index, word in enumerate(command) if word == "--mount"]
    destinations = {next(piece[4:] for piece in mount.split(",") if piece.startswith("dst=")): mount for mount in mounts}
    assert set(destinations) == {"/state", "/workspace", "/evidence", "/config/request.json", "/config/lifecycle.json",
                                 "/config/scope.json", "/config/scope.token", "/config/model-run.token"}
    assert all("readonly" in value.split(",") for key, value in destinations.items() if key.startswith("/config/"))
    assert all("readonly" not in destinations[key].split(",") for key in ("/state", "/workspace", "/evidence"))
    assert not any("must-not-leak" in value for command in lab.commands for value in command)
    assert all("must-not-leak" not in file.read_text() for file in lab.paths.run.rglob("*") if file.is_file())
    override = json.loads((lab.paths.fixture / "services.compose.json").read_text())
    assert all(row["image"] == SERVICES for row in override["services"].values())


@pytest.mark.parametrize("corruption", ["image", "writable_control", "extra_mount"])
def test_created_worker_mismatch_never_starts_and_still_cleans(lab_fixture, corruption):
    lab, args, _ = lab_fixture
    lab.corruption = corruption
    with pytest.raises(RuntimeError, match='创建的 Worker'):
        lab.run_chain(args)
    assert not any(command[:2] == ("docker", "start") for command in lab.commands)
    assert not lab.raw
    assert any(command[:3] == ("docker", "rm", "-f") for command in lab.commands)


def test_replacement_uses_floored_original_remaining_budget_and_charges_creation(lab_fixture):
    lab, args, _ = lab_fixture
    args.variant = "requested-pause"
    assert lab.run_chain(args) == 0
    a = json.loads((lab.paths.run / "trusted/worker-a.json").read_text())
    b = json.loads((lab.paths.run / "trusted/worker-b.json").read_text())
    assert a["after_exit"]["id"] != b["after_exit"]["id"]
    assert a["active_seconds"] == pytest.approx(lab.create_cost + lab.execution_cost)
    remaining = math.floor(300 - a["active_seconds"])
    assert lab.attach_timeouts[0] == pytest.approx(300 - lab.create_cost)
    assert lab.attach_timeouts[1] == pytest.approx(remaining - lab.create_cost)
    assert [action for action, _ in lab.bench_actions] == ["prepare", "bind-start", "check-paused", "bind-resume", "grade"]
    assert len(lab.wait_calls) == 1
    assert not lab.raw


def test_dsh_a_b_mount_native_bindings_readonly_with_only_original_budget_token(lab_fixture, monkeypatch):
    lab, args, _ = lab_fixture
    args.candidate, args.variant = "deepseek_harness", "requested-pause"
    # 校准有效性由独立测试负责；此处不运行候选。
    monkeypatch.setattr(lab, "calibrate_surface", lambda: None)
    assert lab.run_chain(args) == 0
    commands = [command for command in lab.commands if command[:2] == ("docker", "create")]
    assert len(commands) == 2
    for index, command in enumerate(commands):
        assert command[command.index("--entrypoint") + 1] == "node"
        assert "MODEL_RUN_TOKEN_FILE=/budget/run.token" in command
        assert "HARNESS_LIFECYCLE_IMAGE_ID=" + CANDIDATE in command
        mounts = [command[i + 1] for i, word in enumerate(command) if word == "--mount"]
        destinations = {next(piece[4:] for piece in mount.split(",") if piece.startswith("dst=")): mount for mount in mounts}
        assert "/config/model-run.token" not in destinations
        assert "readonly" in destinations["/budget/run.token"].split(",")
        assert "readonly" in destinations["/config/lifecycle-state.json"].split(",")
        assert ("/config/lifecycle-pause.json" in destinations) is (index == 1)
        if index == 1:
            assert "readonly" in destinations["/config/lifecycle-pause.json"].split(",")


def test_create_timeout_remembers_name_and_cleans_possible_created_container(lab_fixture):
    lab, args, _ = lab_fixture
    lab.fail_create = True
    with pytest.raises(subprocess.TimeoutExpired):
        lab.run_chain(args)
    name = "harness-lab-pause-host-unit-worker-a"
    assert name in lab.names
    assert ("docker", "kill", name) in lab.commands
    assert ("docker", "rm", "-f", name) in lab.commands
    assert not lab.raw
    assert not any(command[:2] == ("docker", "start") for command in lab.commands)


def test_creation_can_exhaust_budget_without_starting_or_allocating_more_time(lab_fixture):
    lab, args, _ = lab_fixture
    lab.create_cost = 301
    with pytest.raises(TimeoutError, match='累计活跃预算已用尽'):
        lab.run_chain(args)
    assert not any(command[:2] == ("docker", "start") for command in lab.commands)
    assert not lab.raw


@pytest.mark.parametrize("filename", ["worker-a-created.json", "worker-a.json", "orchestration.json"])
def test_evidence_write_failure_still_halts_paid_execution_then_removes_all(lab_fixture, monkeypatch, filename):
    lab, args, _ = lab_fixture
    original = host.save
    def fail(path, value):
        if path.name == filename:
            lab.commands.append(("injected_evidence_failure", filename))
            raise OSError("模拟证据磁盘故障")
        return original(path, value)
    monkeypatch.setattr(host, "save", fail)
    with pytest.raises(OSError, match="模拟证据磁盘故障"):
        lab.run_chain(args)
    failure = lab.commands.index(("injected_evidence_failure", filename))
    assert any(command[:3] == ("docker", "rm", "-f") for command in lab.commands[failure:])
    assert ("docker", "kill", "harness-lab-pause-host-unit-worker-a") in lab.commands
    assert any("stop" in command and "model-proxy" in command for command in lab.commands)
    assert not lab.raw


def test_candidate_log_open_failure_closes_every_control_thread(lab_fixture, monkeypatch):
    lab, args, _ = lab_fixture
    args.variant = "requested-pause"
    original = Path.open
    def fail(path, *positional, **kwargs):
        if path.name == "candidate.log":
            raise OSError("模拟候选日志故障")
        return original(path, *positional, **kwargs)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(OSError, match="模拟候选日志故障"):
        lab.run_chain(args)
    controls = [thread for thread in ThreadDouble.created if thread.target.__name__ == "pause_trigger"]
    assert all(thread.args[-1].is_set() and thread.joined for thread in controls)
    assert not lab.raw


def test_attach_transport_failure_after_handler_start_still_joins_and_cleans(lab_fixture, monkeypatch):
    lab, args, _ = lab_fixture
    args.variant = "requested-pause"
    def broken_attach(*_args, **_kwargs):
        raise OSError("模拟 attach 传输失败")
    monkeypatch.setattr(host.subprocess, "run", broken_attach)
    with pytest.raises(OSError, match="模拟 attach 传输失败"):
        lab.run_chain(args)
    controls = [thread for thread in ThreadDouble.created if thread.target.__name__ == "pause_trigger"]
    assert len(controls) == 1 and controls[0].args[-1].is_set() and controls[0].joined
    assert not lab.raw


@pytest.mark.parametrize("stderr", ["Cannot connect to the Docker daemon", "permission denied", "context deadline exceeded", ""])
def test_unknown_docker_inspect_failure_is_not_absence(lab_fixture, stderr):
    lab, _, _ = lab_fixture
    lab.call = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", stderr)
    with pytest.raises(RuntimeError, match='尚未证明对象不存在'):
        lab.snapshot(WORKER_ID)


def test_only_explicit_missing_inspect_error_is_accepted_as_absence(lab_fixture):
    lab, _, _ = lab_fixture
    lab.call = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "[]", "Error: No such object: " + WORKER_ID)
    assert lab.snapshot(WORKER_ID) is None


def test_bench_cli_uses_frozen_image_and_exact_container_host_record_path(lab_fixture):
    lab, args, _ = lab_fixture
    assert lab.run_chain(args) == 0
    # 在 run_chain 后测试真实 CLI 构造器；传输仍使用替身。
    host.PauseLab.bench(lab, "bind-start", host_record=lab.paths.run / "trusted/pins.json")
    command = next(command for command in reversed(lab.commands) if command[:2] == ("docker", "run"))
    assert BENCH in command and "harness-lab-bench:local" not in command
    assert command[command.index("-m") + 1:command.index("-m") + 3] == ("runner.pause_recovery", "bind-start")
    assert command[command.index("--candidate") + 1] == args.candidate
    assert command[command.index("--chain-id") + 1] == args.chain_id
    assert command[command.index("--host-record") + 1] == str(lab.paths.run / "trusted/pins.json").replace(str(lab.config.host_data_root), "/data", 1)
    assert not any("MODEL_API_KEY" in value or "MODEL_NAME=" in value for value in command)


class OneSample:
    stopped = False
    def is_set(self):
        return self.stopped
    def wait(self, _seconds):
        self.stopped = True


def sampling_lab(tmp_path, *, final_state=None, absent=False, inspect_error=False, reappeared=False):
    import threading
    lab = host.PauseLab.__new__(host.PauseLab)
    lab.sampling_lock = threading.Lock()
    lab.sampling_phase = 'candidate_active'
    lab.candidate_image_ids = {CANDIDATE}
    lab.pins = {'worker_names': {'worker-a': 'worker-a'}, 'bench_image_id': BENCH}
    worker = {'id': WORKER_ID, 'name': '/worker-a', 'image_id': CANDIDATE,
              'state': {'Running': True}, 'memory_limit': 2 * 1024**3, 'cpu_nano': 10**9}
    service = {'id': 'e' * 64, 'name': '/tools', 'image_id': SERVICES,
               'state': {'Running': True}, 'memory_limit': 512 * 1024**2, 'cpu_nano': 500000000}
    raw = json.dumps({'Name': 'tools', 'ID': service['id'][:12],
                      'MemUsage': '17MiB / 512MiB', 'CPUPerc': '1.25%', 'PIDs': '3'}) + '\n'
    lab.containers = lambda: deepcopy([worker, service])
    inspected = []
    def snapshot(identity):
        inspected.append(identity)
        assert identity == WORKER_ID
        if inspect_error:
            raise RuntimeError('模拟 Docker inspect 不可用')
        return None if absent else {**deepcopy(worker), 'state': deepcopy(final_state or DockerDoubleLab.exited())}
    lab.snapshot = snapshot
    def call(command, **_kwargs):
        if command[:2] == ['docker', 'stats']:
            assert command[-2:] == [WORKER_ID, service['id']]
            return subprocess.CompletedProcess(command, 0, raw, '')
        assert command[:3] == ['docker', 'ps', '-q']
        return subprocess.CompletedProcess(command, 0, WORKER_ID if reappeared else '', '')
    lab.call = call
    samples = []
    host.PauseLab.sample(lab, OneSample(), samples, tmp_path)
    assert inspected == [WORKER_ID]
    assert len(samples) == 1
    assert json.loads((tmp_path / 'resource-samples.jsonl').read_text()) == samples[0]
    return samples[0], raw


def test_sampling_records_exact_healthy_exit_without_fabricating_docker_measurements(tmp_path):
    sample, raw = sampling_lab(tmp_path)
    assert sample['error'] is None and sample['worker_ids'] == []
    assert sample['docker_stats_raw'] == raw
    assert [row['id'] for row in sample['containers']] == ['e' * 64]
    assert sample['containers'][0]['MemUsage'] == '17MiB / 512MiB'
    assert sample['containers'][0]['CPUPerc'] == '1.25%'
    assert len(sample['lifecycle']) == 1
    lifecycle = sample['lifecycle'][0]
    assert lifecycle['event'] == 'exited_during_sample' and lifecycle['id'] == WORKER_ID
    assert lifecycle['before']['state']['Running'] is True
    assert lifecycle['after']['state'] == DockerDoubleLab.exited()
    assert 'MemUsage' not in lifecycle and 'CPUPerc' not in lifecycle


@pytest.mark.parametrize('change', [
    {'Status': 'running', 'Running': True, 'Pid': 111},
    {'OOMKilled': True}, {'ExitCode': 137}, {'Pid': 111}, {'Dead': True},
    {'Paused': True}, {'Restarting': True}, {'Error': 'runtime failure'},
])
def test_missing_stats_with_unhealthy_or_running_exact_id_remains_error(tmp_path, change):
    sample, _ = sampling_lab(tmp_path, final_state={**DockerDoubleLab.exited(), **change})
    assert sample['error'] and sample['worker_ids'] is None
    assert not sample['lifecycle']


@pytest.mark.parametrize('failure', [{'absent': True}, {'inspect_error': True}])
def test_missing_stats_without_retained_exit_fact_is_not_healthy(tmp_path, failure):
    sample, _ = sampling_lab(tmp_path, **failure)
    assert sample['error'] and sample['worker_ids'] is None
    assert not sample['lifecycle']


def test_worker_reappearing_after_healthy_exit_inspect_is_not_hidden(tmp_path):
    sample, _ = sampling_lab(tmp_path, reappeared=True)
    assert sample['error'] and sample['worker_ids'] is None
    assert sample['lifecycle'][0]['id'] == WORKER_ID


def test_wait_monitor_is_retained_for_exit_inspection_until_generic_cleanup(tmp_path, monkeypatch):
    import threading
    lab = host.PauseLab.__new__(host.PauseLab)
    lab.args = Namespace(chain_id='monitor-unit', candidate='deepagents_langgraph', seed=101, restart_proxy=False)
    lab.names, lab.env, lab.day = [], {}, '2026-10-20'
    lab.config = Namespace(host_data_root=tmp_path)
    lab.paths = Namespace(run=tmp_path / 'runs' / 'chain', budget=tmp_path / 'budgets' / 'chain')
    (lab.paths.run / 'trusted').mkdir(parents=True)
    lab.paths.budget.mkdir(parents=True)
    lab.pins = {'bench_image_id': BENCH, 'worker_names': {'worker-a': 'worker-a'}}
    lab.sampling_lock = threading.Lock()
    lab.samples = [{'phase': 'paused_wait', 'unix_time': 0, 'at': 'fixed', 'worker_ids': [],
                    'containers': [], 'error': None}]
    lab.containers = lambda: [{'id': 'proxy-id', 'name': '/harness-lab-model-proxy-1'}]
    lab.snapshot = lambda _identity: None
    commands = []
    class FinishedMonitor:
        returncode = 0
        def poll(self):
            return 0
    def popen(command, **_kwargs):
        commands.append(command)
        return FinishedMonitor()
    ticks = iter([0, 604])
    monkeypatch.setattr(host.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(host.subprocess, 'Popen', popen)
    host.PauseLab.wait_after_delete(lab, {'after_exit': {'id': WORKER_ID}})
    assert len(commands) == 1 and '--rm' not in commands[0]
    assert 'harness-lab-wait-monitor-monitor-unit' in lab.names
    # 普通短时验收命令刻意保留原策略。
    assert '--rm' in lab.bench_command()


@pytest.mark.parametrize('message', [
    'error: no such object: ', 'Error: No such object: ',
    'Error response from daemon: No such container: ',
])
def test_absence_accepts_docker_case_variants_only_for_exact_identity(message):
    lab = host.PauseLab.__new__(host.PauseLab)
    lab.call = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, '[]\n', message + WORKER_ID + '\n')
    assert lab.snapshot(WORKER_ID) is None


@pytest.mark.parametrize('stdout,stderr', [
    ('[]', 'error: no such object: unrelated'),
    ('[]', 'daemon unavailable; no such object: ' + WORKER_ID),
    ('', 'error: no such object: ' + WORKER_ID),
    ('[]', 'error: no such object: ' + WORKER_ID + '\nadditional failure'),
])
def test_absence_never_accepts_other_identity_or_mixed_runtime_failures(stdout, stderr):
    lab = host.PauseLab.__new__(host.PauseLab)
    lab.call = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout, stderr)
    with pytest.raises(RuntimeError, match='尚未证明对象不存在'):
        lab.snapshot(WORKER_ID)


def test_sampled_bench_exit_remains_inspectable_until_cleanup(tmp_path):
    lab = host.PauseLab.__new__(host.PauseLab)
    lab.args = Namespace(candidate='deepagents_langgraph', chain_id='retained-bench', seed=101)
    lab.names, lab.day = [], '2026-10-20'
    lab.paths = Namespace(run=tmp_path, fixture=tmp_path)
    lab.bench_command = lambda: ['docker', 'run', '--rm', '--entrypoint', 'python', BENCH]
    commands = []
    def call(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, 'verified', '')
    lab.call = call
    lab.bench('check-paused')
    assert '--rm' not in commands[0]
    assert commands[0][commands[0].index('--name') + 1] in lab.names
    assert (tmp_path / 'bench-check-paused.log').read_text() == 'verified'
