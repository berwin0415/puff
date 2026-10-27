"""I25 服务 init 校准源码，不是 Docker 执行证据。

只使用传输替身和合成原状态记录。不能导入早期校准模块的
测试数据，也不能放宽原生退出要求。
"""
from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest

from runner.instance_cost import CAPS
from runner.instance_cost_lifecycle import (SCHEMA, VARIANT, same_identity,
    terminal_ok, validate_registry)
from scripts.instance_cost_lifecycle import RotationLifecycle, RotationNativeWorker
from scripts import instance_cost_lifecycle as host


IDENT = "a" * 64
OTHER = "b" * 64
MISSING = object()
BAD_INIT = (MISSING, False, None, 1, "true")
INIT_IDS = ("missing", "false", "null", "integer-one", "string-true")


def state(status, *, exit_code=0):
    return {"Status": status, "Running": status == "running", "Pid": 42 if status == "running" else 0,
        "ExitCode": exit_code, "OOMKilled": False, "Dead": False,
        "Paused": False, "Restarting": False, "Error": ""}


def projection(role="fixtures", *, status="created", init=MISSING, exit_code=0):
    memory, cpu = CAPS["backend" if role == "observer" else role]
    image = "sha256:" + ("1" if role == "worker" else "2" if role == "observer" else "3") * 64
    value = {"id": IDENT, "name": "/hl-ic-service-init-calibration-fixture", "image_id": image,
        "role": role, "memory_limit": memory, "cpu_nano": cpu, "network_mode": "none" if role == "worker" else "fake-private",
        "environment_names": ["PATH"], "mounts": [], "state": state(status, exit_code=exit_code)}
    if init is not MISSING:
        value["host_config_init"] = init
    return value


def complete_registry(role="fixtures", *, init=True, exit_code=0):
    created = projection(role, init=init)
    before = projection(role, status="running")  # 原全局投影刻意不包含 Init。
    exited = projection(role, status="exited", exit_code=exit_code)
    plan = {"trial_id": "service-init-calibration", "candidate_image_id": "sha256:" + "1" * 64,
        "bench_image_id": "sha256:" + "2" * 64, "services_image_id": "sha256:" + "3" * 64}
    request = {"id": IDENT, "kind": "native_close" if role == "worker" else "service_stop",
        "owner": "user-001", "before": before, "requested_monotonic": 2.0}
    terminal = {"observation": exited, "observed_monotonic": 2.5}
    if role == "worker":
        request["command_id"] = "c" * 32
        terminal.update(acknowledgement={"command_id": "c" * 32, "status": "closed"}, attach_exit=0,
            stdout_source="protected/native-stdout.jsonl")
    registry = {"schema": SCHEMA, "sampling_variant": VARIANT, "trial_id": plan["trial_id"],
        "registrations": {IDENT: {"trial_id": plan["trial_id"], "owner": "user-001",
            "registered_monotonic": 1.0, "created": created}},
        "starts": {IDENT: {"id": IDENT, "requested_monotonic": 1.1}},
        "requests": {IDENT: request}, "terminals": {IDENT: terminal},
        "deletions": {IDENT: {"id": IDENT, "before_rm": deepcopy(exited), "requested_monotonic": 3.0,
            "rm_return_code": 0, "rm_stdout": IDENT,
            "id_absence": {"target": IDENT, "return_code": 1, "stdout": "[]",
                "stderr": "Error: No such object: " + IDENT, "observed_monotonic": 3.1},
            "name_absence": {"target": created["name"].removeprefix("/"), "return_code": 1, "stdout": "[]",
                "stderr": "Error: No such object: " + created["name"].removeprefix("/"), "observed_monotonic": 3.2}}}}
    return plan, registry


class FakeLab:
    """全部子进程响应都在内存中，不存在真实 Docker 调用。"""
    def __init__(self, root, *, role="fixtures", actual_init=True):
        self.root, self.role, self.actual_init = root, role, actual_init
        self.plan = {"trial_id": "service-init-calibration", "candidate_image_id": "sha256:" + "1" * 64,
            "bench_image_id": "sha256:" + "2" * 64, "services_image_id": "sha256:" + "3" * 64}
        self.owned, self.registered, self.attached_workers, self.calls = [], {}, [], []
        self.created, self.running, self.mounts = False, False, []
        self.name = "hl-ic-service-init-calibration-fixture"
        self.network_mode = "none" if role == "worker" else "fake-private"
        (root / "protected").mkdir(parents=True, exist_ok=True)

    def caps(self, role):
        return CAPS["backend" if role == "observer" else role]

    def labels(self, role):
        return ["--label", "org.harness-lab.instance-cost-trial=" + self.plan["trial_id"],
            "--label", "org.harness-lab.role=" + role]

    @staticmethod
    def mount(source, destination, readonly=False):
        return ["--mount", "type=bind,src=" + str(source) + ",dst=" + destination + (",readonly" if readonly else "")]

    def observations(self):
        if not self.created:
            return []
        value = self.safe_projection()
        value.pop("host_config_init", None)  # 保留冻结的早期观察结构。
        return [value]

    def safe_projection(self):
        value = projection(self.role, status="running" if self.running else "created", init=self.actual_init)
        value.update(name="/" + self.name, network_mode=self.network_mode, mounts=deepcopy(self.mounts))
        return value

    def pin(self, identity, role, image, *, running=False):
        assert identity == IDENT and role == self.role and image == self.safe_projection()["image_id"]
        memory, cpu = self.caps(role)
        self.registered[identity] = {"id": identity, "role": role, "image_id": image,
            "memory_limit": memory, "cpu_nano": cpu, "running": running}

    def call(self, argv, *, timeout=15, check=True):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "create"]:
            self.created = True
            if "--name" in argv:
                self.name = argv[argv.index("--name") + 1]
            if "--network" in argv:
                self.network_mode = argv[argv.index("--network") + 1]
            for index, part in enumerate(argv[:-1]):
                if part == "--mount":
                    pieces = argv[index + 1].split(",")
                    fields = dict(piece.split("=", 1) for piece in pieces if "=" in piece)
                    self.mounts.append({"Source": fields["src"], "Destination": fields["dst"], "RW": "readonly" not in pieces})
            return SimpleNamespace(returncode=0, stdout=IDENT + "\n", stderr="")
        if argv[:2] == ["docker", "inspect"]:
            value = self.safe_projection()
            raw = {"Id": value["id"], "Name": value["name"], "Image": value["image_id"], "State": value["state"],
                "Config": {"Labels": {"org.harness-lab.instance-cost-trial": self.plan["trial_id"],
                    "org.harness-lab.role": self.role}, "Env": ["PATH=/synthetic/bin"]},
                "HostConfig": {"Memory": value["memory_limit"], "NanoCpus": value["cpu_nano"],
                    "NetworkMode": value["network_mode"]}, "Mounts": value["mounts"]}
            if self.actual_init is not MISSING:
                raw["HostConfig"]["Init"] = self.actual_init
            return SimpleNamespace(returncode=0, stdout=json.dumps([raw]), stderr="")
        if argv[:2] == ["docker", "start"]:
            self.running = True
            return SimpleNamespace(returncode=0, stdout=IDENT + "\n", stderr="")
        raise AssertionError("非预期模拟 Docker 命令：" + repr(argv))


def service(lifecycle, *, role="fixtures"):
    image = lifecycle.lab.plan["bench_image_id"] if role == "observer" else lifecycle.lab.plan["services_image_id"]
    return lifecycle.service(role, "fixture", "fake-private", "fixture", [],
        ["-m", "fixtures.service.server", "--db", "/tmp/synthetic.sqlite"], image=image)


@pytest.mark.parametrize("role", ("fixtures", "observer", "tools", "model-proxy"))
def test_service_uses_init_and_only_registers_after_actual_docker_true(tmp_path, role):
    lab = FakeLab(tmp_path, role=role, actual_init=True)
    lifecycle = RotationLifecycle(lab)
    assert service(lifecycle, role=role) == IDENT
    argv = next(command for command in lab.calls if command[:2] == ["docker", "create"])
    assert argv.count("--init") == 1
    assert lifecycle.registrations[IDENT]["created"]["host_config_init"] is True
    assert IDENT in lifecycle.starts and lab.running is True


@pytest.mark.parametrize("actual", BAD_INIT, ids=INIT_IDS)
def test_init_flag_is_insufficient_if_actual_docker_init_is_missing_false_null_or_coerced(tmp_path, actual):
    lab = FakeLab(tmp_path, actual_init=actual)
    lifecycle = RotationLifecycle(lab)
    with pytest.raises(ValueError, match="lifecycle_service_init_not_enabled"):
        service(lifecycle)
    argv = next(command for command in lab.calls if command[:2] == ["docker", "create"])
    assert "--init" in argv  # 标志只表示请求，不能替代实际 inspect 字段。
    assert lifecycle.registrations == {} and lifecycle.starts == {}
    assert not any(command[:2] == ["docker", "start"] for command in lab.calls)


@pytest.mark.parametrize("actual", (True, False, None, 1, "true", MISSING),
    ids=("true", "false", "null", "integer-one", "string-true", "missing"))
def test_inspect_retains_actual_init_value_without_truthiness_conversion(tmp_path, actual):
    lab = FakeLab(tmp_path, actual_init=actual)
    lifecycle = RotationLifecycle(lab)
    value, _ = lifecycle.inspect(IDENT)
    expected = None if actual is MISSING else actual
    assert "host_config_init" in value
    assert type(value["host_config_init"]) is type(expected)
    assert value["host_config_init"] == expected


@pytest.mark.parametrize("role", ("fixtures", "observer", "tools", "model-proxy"))
@pytest.mark.parametrize("actual", BAD_INIT, ids=INIT_IDS)
def test_independent_registry_rejects_nonworker_missing_false_or_coerced_init(role, actual):
    plan, registry = complete_registry(role, init=actual)
    with pytest.raises(ValueError, match="lifecycle_service_init_not_enabled"):
        validate_registry(registry, plan)


@pytest.mark.parametrize("role", ("fixtures", "observer", "tools", "model-proxy"))
@pytest.mark.parametrize("exit_code", (0, 143))
def test_service_init_does_not_change_the_existing_zero_or_143_exit_policy(role, exit_code):
    plan, registry = complete_registry(role, init=True, exit_code=exit_code)
    assert validate_registry(registry, plan) is True


@pytest.mark.parametrize("exit_code", (1, 137))
def test_enabled_init_does_not_authorize_unknown_service_exit_or_kill(exit_code):
    plan, registry = complete_registry(init=True, exit_code=exit_code)
    request = registry["requests"][IDENT]
    assert terminal_ok(registry["terminals"][IDENT]["observation"], request) is False
    with pytest.raises(ValueError):
        validate_registry(registry, plan)


@pytest.mark.parametrize("actual", (MISSING, False, None, 1, "true"), ids=INIT_IDS)
def test_worker_registry_keeps_its_original_contract_without_a_new_init_requirement(actual):
    plan, registry = complete_registry("worker", init=actual)
    assert validate_registry(registry, plan) is True


@pytest.mark.parametrize("exit_code", (1, 137, 143))
def test_native_worker_close_still_requires_actual_exit_zero(exit_code):
    plan, registry = complete_registry("worker", init=MISSING, exit_code=exit_code)
    assert terminal_ok(registry["terminals"][IDENT]["observation"], registry["requests"][IDENT]) is False
    with pytest.raises(ValueError):
        validate_registry(registry, plan)


def test_created_init_is_bound_to_the_same_exact_container_without_expanding_old_projection():
    created = projection(init=True)
    old_running = projection(status="running")
    assert "host_config_init" not in old_running
    assert same_identity(created, old_running) is True
    old_running["id"] = OTHER
    assert same_identity(created, old_running) is False


def test_worker_create_argv_remains_without_init_with_fake_attach_only(tmp_path, monkeypatch):
    lab = FakeLab(tmp_path, role="worker", actual_init=MISSING)
    lifecycle = RotationLifecycle(lab)

    def attach(argv, **kwargs):
        assert argv == ["docker", "start", "--attach", "--interactive", IDENT]
        lab.running = True
        return SimpleNamespace(args=argv, stdout=iter([json.dumps({"phase": "ready", "owner": "user-001"}) + "\n"]),
            stdin=io.StringIO(), returncode=None)

    monkeypatch.setattr(host.subprocess, "Popen", attach)
    worker = RotationNativeWorker(lifecycle, {"owner": "user-001"}, "synthetic-prepare", network="none")
    try:
        argv = next(command for command in lab.calls if command[:2] == ["docker", "create"])
        assert "--init" not in argv
        assert IDENT in lifecycle.registrations
        assert lifecycle.registrations[IDENT]["created"]["host_config_init"] is None
    finally:
        worker.reader.join(timeout=2)
        worker.console.close()
        worker.stderr.close()
        worker.process.stdin.close()
    assert not worker.reader.is_alive()
