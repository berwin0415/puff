"""Unrun fake-child/unit-file calibration; no Docker, SDK, model or native proof."""
from copy import deepcopy
import json
from pathlib import Path
import signal

import pytest

from execution import worker_control as worker_module
from runner.shared_supervisor import BoundaryError, TaskSupervisor, sha256


class Child:
    def __init__(self):
        self.pid, self.returncode, self.signals = 6001, None, []

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)


@pytest.fixture
def fixed_worker(tmp_path, monkeypatch):
    # Test-only object assembly permits temporary paths and a fake Popen.
    # Production construction separately enforces /config,/state,/workspace,
    # /evidence, immutable mounts and the original DA argv.
    root = tmp_path
    for name in ("state", "workspace", "config", "evidence", "private"):
        (root / name).mkdir()
    spec = {"attempt_id": "worker-a", "candidate": "deepagents_langgraph", "argv": list(worker_module.DA_ARGV),
        "environment": {"MODEL_API_BASE_URL": "http://model-proxy:8082/v1", "MODEL_NAME": "offline-only",
                        "MODEL_RUN_TOKEN_FILE": "/config/model-run.token"},
        "config_dir": str(root / "config"), "evidence_dir": str(root / "evidence"),
        "events_file": "raw-events.jsonl", "readiness_file": "control-events.jsonl",
        "ready_match": {"phase": "pause_handler_installed", "signal": "SIGUSR2"},
        "native_ids": {"session_id": "main-not-native-proof"}}
    task = {"task_id": "task-a", "owner": "user-a", "scope": {"experiment_id": "offline", "variant": "chain-a",
        "user_id": "user-a", "session_id": "main"}, "state_dir": str(root / "state"),
        "workspace": str(root / "workspace"), "artifacts": ["result.txt"], "attempts": [spec]}
    child, calls, audit = Child(), [], []
    identities = {child.pid: {"pid": child.pid, "start_ticks": 100, "source": "fake_child_only"}}

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return child

    worker = worker_module.SingleAttemptWorker.__new__(worker_module.SingleAttemptWorker)
    TaskSupervisor.__init__(worker, [task], audit.append, max_active=1, popen_factory=spawn,
                            identity_reader=lambda pid: dict(identities[pid]))
    worker.task = worker.tasks["task-a"]
    worker.spec = worker.task.attempts["worker-a"]
    worker.worker_id, worker.policy_sha256, worker.mode = "offline-worker-a", "0" * 64, "new"
    worker.control_token, worker.pending_control = "fabricated-control-secret", None
    worker.private_root = root / "private"
    monkeypatch.setattr(worker, "_bound_request", lambda: {"request_sha256": "1" * 64,
        "binding_sha256": "2" * 64, "original_pause": None})

    def ready():
        marker = {**worker.spec.ready_match, "scope": worker.task.scope,
                  "chain_id": "chain-a", "attempt_id": "worker-a"}
        (worker.spec.evidence_dir / worker.spec.readiness_file).write_text(json.dumps(marker) + "\n")

    return {"worker": worker, "child": child, "calls": calls, "audit": audit,
            "identities": identities, "ready": ready}


def test_fixed_start_uses_exact_retained_child_clean_environment_and_private_stdio(fixed_worker, monkeypatch):
    lab, worker = fixed_worker, fixed_worker["worker"]
    monkeypatch.setenv("MODEL_API_KEY", "remote-key-must-not-inherit")
    monkeypatch.setenv("OTHER_USER_SECRET", "ambient-must-not-inherit")
    status, result = worker.handle("Bearer fabricated-control-secret", "start", "POST", {"request_id": "request-one"})
    assert status == 202 and result["status"] == "child_started"
    assert result["native_resume_completed"] is False
    argv, kwargs = lab["calls"][0]
    assert tuple(argv) == worker_module.DA_ARGV
    assert kwargs["cwd"] == "/workspace" and kwargs["shell"] is False and kwargs["close_fds"] is True
    assert "MODEL_API_KEY" not in kwargs["env"] and "OTHER_USER_SECRET" not in kwargs["env"]
    assert not any(worker.spec.evidence_dir.iterdir())
    assert {path.name for path in worker.private_root.iterdir()} == {"candidate.stdout", "candidate.stderr"}
    assert worker.owned[("task-a", "worker-a")].process is lab["child"]
    with pytest.raises(BoundaryError, match="fixed_attempt_already_used"):
        worker.start_fixed("request-two")
    assert len(lab["calls"]) == 1


@pytest.mark.parametrize("action, expected", [("pause", signal.SIGUSR2), ("cancel", signal.SIGUSR1)])
def test_exact_child_signal_is_attempt_only_with_native_drain_semantics(fixed_worker, action, expected):
    lab, worker = fixed_worker, fixed_worker["worker"]
    worker.start_fixed("start")
    lab["ready"]()
    status, result = worker.handle("Bearer fabricated-control-secret", action, "POST", {"request_id": "control-one"})
    assert status == 202 and lab["child"].signals == [expected]
    assert result["status"] == "signal_dispatch_attempted" and result["signal_delivery_confirmed"] is False
    assert result["immediate_cancel"] is False
    assert result["semantics"] == "cooperative_drain_at_superstep_boundary"
    assert result["native_pause_confirmed" if action == "pause" else "native_cancel_confirmed"] is False
    with pytest.raises(BoundaryError, match="control_already_pending"):
        worker.signal_control(action, "control-two")
    assert lab["child"].signals == [expected]


def test_pause_requires_actual_task_marker_and_original_process_identity(fixed_worker):
    lab, worker = fixed_worker, fixed_worker["worker"]
    worker.start_fixed("start")
    with pytest.raises(BoundaryError, match="native_handlers_not_observed"):
        worker.signal_control("pause", "pause-one")
    lab["ready"]()
    lab["identities"][lab["child"].pid]["start_ticks"] += 1
    with pytest.raises(BoundaryError, match="process_identity_changed"):
        worker.signal_control("pause", "pause-two")
    assert lab["child"].signals == []


def test_credential_is_checked_before_body_fields_or_child_start(fixed_worker):
    lab, worker = fixed_worker, fixed_worker["worker"]
    with pytest.raises(BoundaryError, match="unauthenticated"):
        worker.handle("Bearer wrong", "start", "POST", {"pid": "untrusted"})
    with pytest.raises(BoundaryError, match="invalid_fixture"):
        worker.handle("Bearer fabricated-control-secret", "start", "POST", {"request_id": "one", "pid": 123})
    assert lab["calls"] == []


@pytest.fixture
def closed_materials(fixed_worker):
    worker = fixed_worker["worker"]
    task = worker.task
    # Deliberately not a valid SQLite DB: this calibrates integrity checks only.
    # The real I22 child independently opens SQLite and reads the graph state.
    (task.state_dir / "checkpoints.sqlite").write_bytes(b"offline-calibration-bytes")
    (task.workspace / "result.txt").write_text("offline-write-sentinel")
    snapshot = {"config": {"configurable": {"thread_id": "main", "checkpoint_id": "checkpoint-one"}},
                "next": ["model"], "tasks": []}
    saved = {"schema": "harness-lab-pause-index/1", "candidate": "deepagents_langgraph",
        "attempt_id": "worker-a", "chain_id": "chain-a", "scope": task.scope, "session_id": "main",
        "checkpoint_ns": "", "saver_closed": True, "tools_closed": True, "safe_pause": True,
        "observed_active_graph_tasks": 0, "observed_active_http_calls": 0,
        "observed_http_errors_or_unknown_results": 0, "source_export_sha256": "1" * 64,
        "adapter_export_sha256": "2" * 64, "runtime_sha256": "3" * 64,
        "checkpoint_id": "checkpoint-one", "exact_snapshot": snapshot, "head_snapshot": snapshot,
        "exact_snapshot_sha256": sha256(worker_module.canonical(snapshot)),
        "head_snapshot_sha256": sha256(worker_module.canonical(snapshot)), "total_active_seconds": 10}
    for field, root, native in (("state_files", task.state_dir, True), ("workspace_files", task.workspace, False)):
        saved[field] = worker_module.inventory(root, state=native)
        saved[field + "_sha256"] = sha256(worker_module.canonical(saved[field]))
    raw = worker_module.canonical(saved) + b"\n"
    (task.state_dir / "pause-index.json").write_bytes(raw)
    binding = {key: saved[key] for key in ("source_export_sha256", "adapter_export_sha256", "runtime_sha256")}
    binding["budget"] = {"active_limit_seconds": 300, "used_active_seconds": 10, "remaining_seconds": 290}
    binding["recovery"] = {key: saved[key] for key in ("attempt_id", "checkpoint_id", "exact_snapshot_sha256",
        "head_snapshot_sha256", "state_files_sha256", "workspace_files_sha256")}
    binding["recovery"]["pause_index_sha256"] = sha256(raw)
    return {"task": task, "request": {"timeout_seconds": 290}, "binding": binding, "saved": saved}


def test_resume_guard_uses_protected_original_index_and_actual_closed_files(closed_materials):
    lab = closed_materials
    result = worker_module.verify_resume_materials(lab["task"], lab["request"], lab["binding"])
    assert result["checkpoint_id"] == "checkpoint-one"
    assert result["native_resume_completed"] is False
    (lab["task"].workspace / "result.txt").write_text("changed-after-safe-pause")
    with pytest.raises(BoundaryError, match="closed_native_material_changed"):
        worker_module.verify_resume_materials(lab["task"], lab["request"], lab["binding"])


@pytest.mark.parametrize("change", ["scope", "active_http", "saver_open", "budget", "index"])
def test_resume_guard_fails_closed_on_wrong_scope_active_work_or_new_budget(closed_materials, change):
    lab = closed_materials
    binding, request = deepcopy(lab["binding"]), dict(lab["request"])
    saved = deepcopy(lab["saved"])
    if change == "scope": saved["scope"]["user_id"] = "user-b"
    if change == "active_http": saved["observed_active_http_calls"] = 1
    if change == "saver_open": saved["saver_closed"] = False
    if change == "budget": binding["budget"]["used_active_seconds"] = 0
    if change in {"scope", "active_http", "saver_open", "index"}:
        raw = worker_module.canonical(saved) + b"\n "
        (lab["task"].state_dir / "pause-index.json").write_bytes(raw)
        if change != "index": binding["recovery"]["pause_index_sha256"] = sha256(raw)
    with pytest.raises(BoundaryError):
        worker_module.verify_resume_materials(lab["task"], request, binding)
