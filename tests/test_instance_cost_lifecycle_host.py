"""I25 宿主边界反例；使用合成传输，不证明运行时能力。"""
from copy import deepcopy
import io
import json
import threading
from types import SimpleNamespace

import pytest

from runner.instance_cost import CAPS
from scripts.instance_cost_lifecycle import RotationLifecycle, RotationNativeWorker
from scripts import instance_cost_lifecycle as host
from scripts.run_instance_cost_full import CostFlowLab


IDENT = "a" * 64


def lab(tmp_path):
    (tmp_path / "protected").mkdir()
    return SimpleNamespace(root=tmp_path, plan={"trial_id": "lifecycle-calibration"},
        registered={IDENT: {"running": True}}, sequence=0, normal_closes=[],
        sampling_status={"started_monotonic": 1.0, "samples_written": 0, "completed": False},
        stop_sampling=threading.Event())


def unlocked(lifecycle):
    for lock in (lifecycle.inventory_lock, lifecycle.registry_lock, lifecycle.control_lock):
        assert lock.acquire(blocking=False), "长时间等待原生执行时仍持有生命周期锁"
        lock.release()


def value(status):
    return {"id": IDENT, "name": "/synthetic-worker", "image_id": "sha256:" + "1" * 64,
        "role": "worker", "memory_limit": CAPS["worker"][0], "cpu_nano": CAPS["worker"][1],
        "network_mode": "none", "environment_names": ["PATH"], "mounts": [],
        "state": {"Status": status, "Running": status == "running", "Pid": 10 if status == "running" else 0,
            "ExitCode": 0, "OOMKilled": False, "Dead": False, "Paused": False, "Restarting": False, "Error": ""}}


def test_close_ack_and_actual_process_wait_leave_all_sampling_locks_available(tmp_path, monkeypatch):
    lifecycle = RotationLifecycle(lab(tmp_path))
    lifecycle.registrations[IDENT] = {"trial_id": "lifecycle-calibration", "owner": "user-001",
        "registered_monotonic": .5, "created": value("created")}
    lifecycle.starts[IDENT] = {"id": IDENT, "requested_monotonic": .6}
    worker = RotationNativeWorker.__new__(RotationNativeWorker)
    worker.lifecycle, worker.lab, worker.id, worker.name = lifecycle, lifecycle.lab, IDENT, "synthetic-worker"
    worker.user, worker.closed = {"owner": "user-001"}, False
    worker.console, worker.stderr = io.StringIO(), io.StringIO()
    ack_waiting, ack_allowed, exit_waiting, exit_allowed = [threading.Event() for _ in range(4)]
    state, errors, results = {"exited": False}, [], []
    stdin = io.StringIO()

    def receive(*, timeout):
        assert timeout == 60
        body = json.loads(stdin.getvalue())
        ack_waiting.set()
        assert ack_allowed.wait(2)
        return {"command_id": body["command_id"], "status": "closed"}

    def wait(*, timeout):
        assert timeout == 60
        exit_waiting.set()
        assert exit_allowed.wait(2)
        state["exited"] = True
        return 0

    worker.receive = receive
    worker.process = SimpleNamespace(stdin=stdin, wait=wait)
    worker.reader = SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False)
    monkeypatch.setattr(lifecycle, "inspect", lambda target: (value("exited" if state["exited"] else "running"), {}))
    removed = []
    monkeypatch.setattr(lifecycle, "remove", removed.append)

    def close():
        try:
            results.append(worker.close())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert ack_waiting.wait(2)
        unlocked(lifecycle)
        ack_allowed.set()
        assert exit_waiting.wait(2)
        unlocked(lifecycle)
    finally:
        ack_allowed.set()
        exit_allowed.set()
        thread.join(timeout=2)
    assert not thread.is_alive() and not errors
    assert removed == [IDENT] and results[0]["healthy_closed"] is True


def sample(sequence=1, before=1.1, after=2.0):
    return {"sample_sequence": sequence, "host_before_observer_monotonic": before,
        "host_started_monotonic": before + .1, "host_finished_monotonic": after - .1,
        "host_after_observer_monotonic": after}


@pytest.mark.parametrize("reason,first", (("rotation_sampling_error", {"sample_sequence": 1, "error": "ValueError"}),
    ("rotation_sampling_span_exceeded", sample(after=6.2)),
    ("rotation_sampling_first_edge_exceeded", sample(before=6.1, after=6.4))))
def test_first_failed_original_sample_latches_reason_and_blocks_new_submissions(tmp_path, monkeypatch, reason, first):
    owner = lab(tmp_path)
    lifecycle = RotationLifecycle(owner)
    lifecycle.sample_written(first)
    original = deepcopy(lifecycle.first_sampling_failure)
    lifecycle.sample_written({"sample_sequence": 2, "error": "DifferentLaterError"})
    assert lifecycle.first_sampling_failure == original
    assert original["reason_code"] == reason and original["sample_sequence"] == 1
    assert json.loads((tmp_path / "protected/rotation-sampling-failure.json").read_text()) == original
    target = CostFlowLab.__new__(CostFlowLab)
    target.lifecycle = lifecycle
    calls = []
    worker = SimpleNamespace(command=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="sampling_integrity_failed"):
        target.task_command(worker, "wake")
    with pytest.raises(ValueError, match="sampling_integrity_failed"):
        target.wave("wake", [])
    assert calls == []


def test_online_start_gap_and_inflight_span_keep_original_five_second_limit(tmp_path, monkeypatch):
    lifecycle = RotationLifecycle(lab(tmp_path))
    lifecycle.sample_written(sample())
    lifecycle.sample_written(sample(sequence=2, before=6.2, after=6.5))
    assert lifecycle.first_sampling_failure["reason_code"] == "rotation_sampling_start_gap_exceeded"
    assert lifecycle.first_sampling_failure["sample_sequence"] == 2


def test_late_inflight_sample_blocks_submission_before_its_original_result_arrives(tmp_path, monkeypatch):
    lifecycle = RotationLifecycle(lab(tmp_path))
    lifecycle.sample_started(1.1)
    monkeypatch.setattr(host.time, "monotonic", lambda: 6.2)
    with pytest.raises(ValueError, match="sampling_integrity_failed"):
        lifecycle.assert_sampling_ok()
    assert lifecycle.first_sampling_failure["reason_code"] == "rotation_sampling_span_exceeded"
    assert lifecycle.first_sampling_failure["sample_sequence"] == 1


def test_healthy_samples_do_not_open_or_latch_a_failure_file(tmp_path):
    lifecycle = RotationLifecycle(lab(tmp_path))
    lifecycle.sample_written(sample())
    lifecycle.sample_written(sample(sequence=2, before=3.0, after=4.0))
    lifecycle.lab.stop_sampling.set()
    lifecycle.assert_sampling_ok()
    assert lifecycle.first_sampling_failure is None
    assert not (tmp_path / "protected/rotation-sampling-failure.json").exists()


def test_old_default_cost_flow_does_not_construct_lifecycle_transport(tmp_path):
    # rotation_users 未收到显式启用值时，使用原默认路径。
    target = CostFlowLab(tmp_path, {"trial_id": "legacy-default"})
    assert target.lifecycle is None
