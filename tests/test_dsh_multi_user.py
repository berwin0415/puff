"""离线边界测试初稿，不能作为原生 I24 或取消证据。

仅在经过复核的 Docker 验收镜像中执行。不启动 SDK、Docker、
模型或子进程；假子进程断言只测试本仓库包装层。
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import threading

import pytest

from execution import dsh_materials as d
from execution import worker_control as common_worker
from execution import worker_control_dsh as dsh_worker
from execution.model_proxy import CallBudget
from execution.worker_control_dsh import DSHAttemptWorker
from execution.control_backend import ContainerWorkerRegistry
from runner import multi_user as m
from runner import multi_user_dsh as s
from runner import pause_recovery as p
from runner.shared_supervisor import BoundaryError, TaskSupervisor
from scripts import run_multi_user as host


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(p.canonical(value) + b"\n")


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(p.canonical(row) + b"\n" for row in rows))


def fixed_binding(tmp_path):
    scope = {"experiment_id": "offline", "variant": "chain-a", "user_id": "user-a", "session_id": "session-a"}
    paths = SimpleNamespace(run=tmp_path / "run", fixture=tmp_path / "fixture",
        state=tmp_path / "state", workspace=tmp_path / "workspace")
    for directory in vars(paths).values():
        directory.mkdir()
    task = m.Task("task-a", "user-a", "main", "a", scope, paths, d.CANDIDATE)
    # 执行检查使用监督器实际任务根目录。
    guard_task = SimpleNamespace(scope=scope, state_dir=paths.state, workspace=paths.workspace)
    request = {"run_id": "chain-a", "session_id": "session-a", "prompt": "original objective",
        "workspace": "/workspace", "state_dir": "/state", "output_dir": "/evidence", "tools_url": "http://tools:8081",
        "tool_token_file": "/config/scope.token", "max_output_tokens": 4096, "max_retries": 2, "timeout_seconds": 300,
        "candidate_options": {"case": "tools", "trusted_scope_file": "/config/scope.json"},
        "lifecycle": {"mode": "new", "chain_id": "chain-a", "attempt_id": "worker-a", "binding_file": "/config/lifecycle.json"}}
    env = s.environment("offline-only", "sha256:" + "1" * 64)
    binding = {"schema": "harness-lab-lifecycle/1", "candidate": d.CANDIDATE, "variant": "goal-runtime", "mode": "new",
        "chain_id": "chain-a", "attempt_id": "worker-a", "scope": scope, "session_id": "session-a",
        "materials": {"source_export_sha256": "2" * 64, "adapter_export_sha256": "3" * 64},
        "image": {"original_image_id": env["HARNESS_LIFECYCLE_IMAGE_ID"]},
        "model": {"config_sha256": d.digest({"provider": "deepseek-official", "base_url": env["MODEL_API_BASE_URL"],
            "model": env["MODEL_NAME"], "max_output_tokens": 4096, "max_retries": 2, "thinking": "disabled", "reasoning_effort": "off"}),
            "run_token_sha256": "4" * 64, "budget_id": "chain-a", "max_model_calls": 20},
        "tools": {"config_sha256": "5" * 64, "contract_sha256": "6" * 64},
        "state": {"index_file": "/config/lifecycle-state.json", "index_sha256": "7" * 64},
        "goal": {"max_goal_rounds": 20}, "active_budget": {"limit_seconds": 300, "used_seconds": 0, "remaining_seconds": 300}}
    return task, guard_task, request, binding, env


def test_dsh_binding_accepts_only_explicit_goal_shape_without_running_it(tmp_path):
    task, guard, request, binding, env = fixed_binding(tmp_path)
    result = d.verify_binding(guard, request, binding, config_dir=task.paths.fixture, environment=env, verify_files=False)
    assert result["mode"] == "new" and result["materials"] == binding["materials"]


@pytest.mark.parametrize("section,key,value", [
    ("binding", "candidate", "deepagents_langgraph"), ("binding", "variant", "fault-round-order"),
    ("binding", "session_id", "session-b"), ("binding", "chain_id", "other-chain"),
    ("binding", "attempt_id", "worker-b"), ("request", "tools_url", "http://other-tools:8081"),
    ("request", "timeout_seconds", 301), ("request", "max_retries", True),
    ("request", "lifecycle", {"mode": "new"}),
    ("environment", "MODEL_RUN_TOKEN_FILE", "/config/model-run.token"),
    ("environment", "MODEL_API_BASE_URL", "https://remote-provider.example/v1"),
    ("environment", "HARNESS_LIFECYCLE_IMAGE_ID", "sha256:" + "9" * 64),
])
def test_dsh_binding_rejects_scope_material_route_and_budget_changes(tmp_path, section, key, value):
    task, guard, request, binding, env = fixed_binding(tmp_path)
    {"binding": binding, "request": request, "environment": env}[section][key] = value
    with pytest.raises(BoundaryError):
        d.verify_binding(guard, request, binding, config_dir=task.paths.fixture, environment=env, verify_files=False)


@pytest.mark.parametrize("mutation", ["prompt", "reset-budget", "wrong-goal-cap", "followup", "boolean-call-cap"])
def test_resume_rejects_new_prompt_and_budget_resets(tmp_path, mutation):
    task, guard, request, binding, env = fixed_binding(tmp_path)
    request.pop("prompt")
    request["lifecycle"].update(mode="resume", attempt_id="worker-b")
    request["timeout_seconds"] = 260
    binding.update(mode="resume", attempt_id="worker-b", resume={"pause_index_file": "/config/lifecycle-pause.json", "pause_index_sha256": "8" * 64})
    binding["active_budget"].update(used_seconds=40, remaining_seconds=260)
    if mutation == "prompt":
        request["prompt"] = "continue"
    elif mutation == "reset-budget":
        binding["active_budget"]["remaining_seconds"] = request["timeout_seconds"] = 300
    elif mutation == "wrong-goal-cap":
        binding["goal"]["max_goal_rounds"] = 200
    elif mutation == "followup":
        request["candidate_options"]["followup"] = "continue"
    else:
        binding["model"]["max_model_calls"] = True
    with pytest.raises(BoundaryError):
        d.verify_binding(guard, request, binding, config_dir=task.paths.fixture, environment=env, verify_files=False)


def test_complete_inventory_includes_empty_directories_and_rejects_links(tmp_path):
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    (state / "empty").mkdir()
    (state / "session.jsonl").write_bytes(b"native bytes\n")
    result = d.inventory(state, workspace)
    assert {"root": "state", "path": "empty"} in result["directories"]
    assert result["files"][0]["sha256"] == p.sha(b"native bytes\n")
    (workspace / "foreign").symlink_to(state / "session.jsonl")
    with pytest.raises(BoundaryError, match="inventory_symlink"):
        d.inventory(state, workspace)


@pytest.fixture
def real_bound_request_files(tmp_path, monkeypatch):
    """使用真实文件大小与读取检查，只映射绝对路径和只读标志。

    不替换 _bound_request、verify_binding、read_bound_file，
    不启动 SDK 或子进程。statvfs 标志为合成，不能证明真实挂载安全。
    """
    task, guard, request, binding, env = fixed_binding(tmp_path)
    config = task.paths.fixture
    run_token = tmp_path / "run.token"
    run_token.write_text("offline-model-token\n")
    (config / "scope.token").write_text("offline-scope-token\n")
    write_json(config / "scope.json", task.scope)
    write_json(config / "lifecycle-state.json", d.inventory(guard.state_dir, guard.workspace))
    binding["state"]["index_sha256"] = p.sha((config / "lifecycle-state.json").read_bytes())
    binding["model"]["run_token_sha256"] = p.sha(b"offline-model-token")
    binding["tools"]["config_sha256"] = d.digest({"url": request["tools_url"], "scope": task.scope,
        "token_sha256": p.sha(b"offline-scope-token")})
    # 使用与 prepare_candidate_source.inventory 相同的结构及逐文件形状。
    entries = [{"path": "sources/main/packages/offline-fixture/src/module-" + str(number) + ".ts",
        "type": "file", "size": 0, "mode": 420, "sha256": p.sha(b"")} for number in range(10000)]
    source = {"schema": "harness-lab-source-manifest/1", "candidate": d.CANDIDATE,
        "created_at": "2026-10-21T00:00:00+00:00", "config_sha256": "1" * 64,
        "sources": [{"id": "main", "kind": "git", "commit": "dd6322d604e00eec1ba5e0c8541159906a21094a",
            "observed_commit": "dd6322d604e00eec1ba5e0c8541159906a21094a", "dirty": False}],
        "export_sha256": p.sha(json.dumps(entries, sort_keys=True).encode()), "files": entries}
    materials = tmp_path / "materials"
    materials.mkdir()
    source_path = materials / "SOURCE_MANIFEST.json"
    source_path.write_text(json.dumps(source, indent=2) + "\n")
    assert 1024 * 1024 < source_path.stat().st_size < 8 * 1024 * 1024
    binding["materials"]["source_export_sha256"] = source["export_sha256"]
    write_json(materials / "LAB_ADAPTER_MANIFEST.json", {"schema": "harness-lab-adapter-manifest/1",
        "candidate": d.CANDIDATE, "export_sha256": binding["materials"]["adapter_export_sha256"]})
    write_json(config / "request.json", request)
    write_json(config / "lifecycle.json", binding)
    routed = {"/config/request.json": config / "request.json", "/config/lifecycle.json": config / "lifecycle.json",
        "/budget/run.token": run_token, "/opt/materials/SOURCE_MANIFEST.json": source_path,
        "/opt/materials/LAB_ADAPTER_MANIFEST.json": materials / "LAB_ADAPTER_MANIFEST.json"}
    original_read, reads = common_worker.immutable_bytes, []
    def read_original(path, **kwargs):
        reads.append((str(path), kwargs.get("limit", 1024 * 1024)))
        return original_read(routed.get(str(path), path), **kwargs)
    monkeypatch.setattr(common_worker.os, "statvfs", lambda path: SimpleNamespace(f_flag=os.ST_RDONLY))
    for module in (common_worker, dsh_worker, d):
        monkeypatch.setattr(module, "immutable_bytes", read_original)
    worker = DSHAttemptWorker.__new__(DSHAttemptWorker)
    worker.task, worker.mode = guard, "new"
    worker.spec = SimpleNamespace(config_dir=config, environment=env, attempt_id="worker-a")
    return worker, routed, reads


def test_real_bound_request_reads_source_manifest_larger_than_default_limit(real_bound_request_files):
    worker, paths, reads = real_bound_request_files
    result = worker._bound_request()
    assert result["request_sha256"] == p.sha(paths["/config/request.json"].read_bytes())
    assert result["binding_sha256"] == p.sha(paths["/config/lifecycle.json"].read_bytes())
    assert result["original_pause"] is None
    assert ("/opt/materials/SOURCE_MANIFEST.json", 8 * 1024 * 1024) in reads
    assert ("/opt/materials/LAB_ADAPTER_MANIFEST.json", 1024 * 1024) in reads
    assert ("/config/request.json", 1024 * 1024) in reads
    assert ("/config/lifecycle.json", 1024 * 1024) in reads


@pytest.mark.parametrize("changed", ["source-over-8MiB", "adapter-over-1MiB", "request-over-1MiB", "binding-over-1MiB"])
def test_real_bound_request_retains_source_and_other_file_size_bounds(real_bound_request_files, changed):
    worker, paths, _ = real_bound_request_files
    target, maximum = {"source-over-8MiB": ("/opt/materials/SOURCE_MANIFEST.json", 8 * 1024 * 1024),
        "adapter-over-1MiB": ("/opt/materials/LAB_ADAPTER_MANIFEST.json", 1024 * 1024),
        "request-over-1MiB": ("/config/request.json", 1024 * 1024),
        "binding-over-1MiB": ("/config/lifecycle.json", 1024 * 1024)}[changed]
    # 超过上限但合法的 JSON 用于区分文件拒绝与 JSON 错误。
    paths[target].write_bytes(b'"' + b"x" * maximum + b'"\n')
    with pytest.raises(BoundaryError):
        worker._bound_request()


def test_real_bound_request_still_requires_readonly_control_mount(real_bound_request_files, monkeypatch):
    worker, _, _ = real_bound_request_files
    monkeypatch.setattr(common_worker.os, "statvfs", lambda path: SimpleNamespace(f_flag=0))
    with pytest.raises(BoundaryError, match="readonly_control_material_required"):
        worker._bound_request()


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    task, _, _, _, env = fixed_binding(tmp_path)
    evidence, private = task.paths.run / "candidate", task.paths.run / "private"
    evidence.mkdir()
    private.mkdir()
    spec = {"attempt_id": "worker-a", "candidate": d.CANDIDATE, "argv": list(d.ARGV), "environment": env,
        "config_dir": str(task.paths.fixture), "evidence_dir": str(evidence), "events_file": "raw-events.jsonl",
        "readiness_file": "control-events.jsonl", "ready_match": {"phase": "pause_handler_installed", "signal": "SIGUSR2"},
        "native_ids": {"session_id": task.scope["session_id"]}}
    fixture = {"task_id": task.task_id, "owner": task.owner, "scope": task.scope, "state_dir": str(task.paths.state),
        "workspace": str(task.paths.workspace), "artifacts": ["report.json"], "attempts": [spec]}
    class FakeChild:
        pid = 6001
        returncode = None
        def __init__(self):
            self.signals = []
        def poll(self):
            return self.returncode
        def send_signal(self, value):
            self.signals.append(value)
    child, launches, audit = FakeChild(), [], []
    identity = {"pid": child.pid, "start_ticks": 123, "source": "fake_child_only"}
    def spawn(argv, **kwargs):
        launches.append((argv, kwargs))
        return child
    worker = DSHAttemptWorker.__new__(DSHAttemptWorker)
    TaskSupervisor.__init__(worker, [fixture], audit.append, max_active=1,
        popen_factory=spawn, identity_reader=lambda pid: dict(identity))
    worker.task, worker.spec = worker.tasks[task.task_id], worker.tasks[task.task_id].attempts["worker-a"]
    worker.worker_id, worker.policy_sha256, worker.mode = "fake-dsh-worker", "0" * 64, "new"
    worker.private_root, worker.control_token, worker.pending_control = private, "fake-private-token", None
    monkeypatch.setattr(worker, "_bound_request", lambda: {"request_sha256": "1" * 64, "binding_sha256": "2" * 64})
    def ready():
        write_json(evidence / "control-events.jsonl", {**spec["ready_match"], "scope": task.scope,
            "chain_id": task.scope["variant"], "attempt_id": "worker-a"})
    return worker, child, launches, identity, ready


def test_dsh_worker_uses_fixed_node_package_cwd_and_clean_environment(fake_worker, monkeypatch):
    worker, child, launches, _, _ = fake_worker
    monkeypatch.setenv("MODEL_API_KEY", "remote-secret-must-not-be-inherited")
    worker.start_fixed("start-one")
    argv, kwargs = launches[0]
    assert tuple(argv) == d.ARGV and kwargs["cwd"] == d.CWD
    assert kwargs["env"]["MODEL_RUN_TOKEN_FILE"] == "/budget/run.token"
    assert kwargs["env"]["TSX_TSCONFIG_PATH"] == d.CWD + "/tsconfig.json"
    assert "MODEL_API_KEY" not in kwargs["env"] and kwargs["shell"] is False
    assert worker.describe()["candidate"] == d.CANDIDATE
    with pytest.raises(BoundaryError, match="fixed_attempt_already_used"):
        worker.start_fixed("do-not-replay")
    assert len(launches) == 1 and worker.owned[("task-a", "worker-a")].process is child


@pytest.mark.parametrize("action,signum", [("pause", signal.SIGUSR2), ("cancel", signal.SIGUSR1)])
def test_dsh_ack_never_confirms_immediate_cancellation_or_pause(fake_worker, action, signum):
    worker, child, _, _, ready = fake_worker
    worker.start_fixed("start")
    ready()
    result = worker.signal_control(action, "control-one")
    assert child.signals == [signum] and result["signal_delivery_confirmed"] is False
    assert result["native_pause_confirmed" if action == "pause" else "native_cancel_confirmed"] is False
    assert result["immediate_cancel"] is False
    assert result["immediate_cancel_requested"] is (action == "cancel")
    assert result["semantics"] == d.SEMANTICS[action]
    with pytest.raises(BoundaryError, match="control_already_pending"):
        worker.signal_control(action, "do-not-replay")


def test_dsh_control_rejects_stale_process_identity(fake_worker):
    worker, child, _, identity, ready = fake_worker
    worker.start_fixed("start")
    ready()
    identity["start_ticks"] += 1
    with pytest.raises(BoundaryError, match="process_identity_changed"):
        worker.signal_control("pause", "stale-pid")
    assert child.signals == []


def test_candidate_selection_preserves_da_default_and_rejects_unknown():
    args = SimpleNamespace()
    assert m.candidate_for(args) == host.candidate_for(args) == "deepagents_langgraph"
    assert host.worker_python(args) == "/opt/venv/bin/python"
    args.candidate = d.CANDIDATE
    assert host.worker_python(args) == "/usr/bin/python3"
    assert m.environment_for("offline", d.CANDIDATE, "sha256:" + "1" * 64)["MODEL_RUN_TOKEN_FILE"] == d.TOKEN
    args.candidate = "client-provided-framework"
    with pytest.raises(ValueError):
        host.candidate_for(args)
    with pytest.raises(ValueError):
        m.candidate_for(args)


@pytest.mark.parametrize("mutation", [None, "DA-semantics", "native-confirmed", "capability-claim", "requested-missing"])
def test_backend_dsh_ack_is_api_request_only(mutation):
    registry = ContainerWorkerRegistry.__new__(ContainerWorkerRegistry)
    registry.lock = threading.RLock()
    task = SimpleNamespace(task_id="task-a", attempts={"worker-a": SimpleNamespace(candidate=d.CANDIDATE)})
    registry._selected = lambda task_id: (task, "worker-a", task.attempts["worker-a"])
    registry._record = lambda *args, **kwargs: None
    ack = {"status": "signal_dispatch_attempted", "signal_delivery_confirmed": False,
        "native_cancel_confirmed": False, "immediate_cancel": False, "immediate_cancel_requested": True,
        "semantics": d.SEMANTICS["cancel"]}
    if mutation == "DA-semantics":
        ack["semantics"] = "cooperative_drain_at_superstep_boundary"
    elif mutation == "native-confirmed":
        ack["native_cancel_confirmed"] = True
    elif mutation == "capability-claim":
        ack["immediate_cancel"] = True
    elif mutation == "requested-missing":
        ack.pop("immediate_cancel_requested")
    registry._request = lambda task_id, attempt, action, **kwargs: {} if action == "describe" else ack
    if mutation is None:
        assert registry.cancel("task-a", "request-one") == ack
    else:
        with pytest.raises(BoundaryError, match="worker_signal_ack_mismatch"):
            registry.cancel("task-a", "request-one")


def test_dsh_baseline_uses_actual_entrypoint_shape_without_da_result_fields(tmp_path, monkeypatch):
    task, _, request, binding, env = fixed_binding(tmp_path)
    original = {key: value for key, value in request.items() if key != "lifecycle"}
    target = task.paths.fixture / "attempts/worker-a"
    write_json(task.paths.fixture / "candidate-request.json", original)
    write_json(task.paths.run / "chain.json", {"original_request_sha256": p.sha(p.regular(task.paths.fixture / "candidate-request.json"))})
    write_json(target / "request.json", request)
    write_json(target / "lifecycle.json", binding)
    root = task.paths.run / "attempts/worker-a/candidate"
    entry = {"scope": task.scope, "run_id": task.scope["variant"], "session_id": task.scope["session_id"],
        "variant": "goal-runtime", "session_action": "create", "resume_input": None,
        "runtime_api": "official runProfile + agents.create/agents.resume({setup})",
        "provenance": {"candidate": d.CANDIDATE, "commit": "fixed", **binding["materials"], "source_file_count": 100}}
    write_json(root / "entrypoint.json", entry)
    write_json(root / "session-before-followup.json", {"header": {"id": task.scope["session_id"], "cwd": "/workspace"}})
    integrity = {"remaining_seconds": 300, "request_sha256": "1" * 64, "binding_sha256": "2" * 64, "scope_sha256": "3" * 64}
    monkeypatch.setattr(p, "control_integrity", lambda *args: integrity)
    monkeypatch.setattr(p, "dsh_contract", lambda *args: "independently-tested-elsewhere")
    result = {"session_id": task.scope["session_id"], "elapsed_seconds": 12,
        "lifecycle": {**request["lifecycle"], "image_verification": "trusted_declaration_match_only_host_must_verify_actual_docker_image_id"}}
    pins = {"model_name": env["MODEL_NAME"], "worker_image_id": env["HARNESS_LIFECYCLE_IMAGE_ID"], **binding["materials"]}
    assert s.baseline_binding(task, pins, result)["entrypoint_source"]["sha256"] == p.sha(p.regular(root / "entrypoint.json"))
    entry["provenance"]["adapter_export_sha256"] = "9" * 64
    write_json(root / "entrypoint.json", entry)
    with pytest.raises(ValueError, match="entrypoint_changed"):
        s.baseline_binding(task, pins, result)


def native_row(scope, seq, kind, turn=1, milliseconds=1000, **data):
    return {"method": "session.event", "params": {"sessionId": scope["session_id"],
        "event": {"seq": seq, "time": milliseconds, "type": kind, "data": {"turn": turn, **data}}}}


def test_dsh_native_activity_reads_original_session_vocabulary(tmp_path):
    task, _, _, _, _ = fixed_binding(tmp_path)
    rows = [native_row(task.scope, 2, "turn/start"), native_row(task.scope, 3, "step/start")]
    path = task.paths.run / "events.jsonl"
    write_rows(path, rows)
    live = s.live_turn(task, p.jsonl(path))
    assert live[0] == 1 and live[2]["type"] == "turn/start"
    rows.append(native_row(task.scope, 4, "turn/end", reason={"kind": "blocked"}))
    write_rows(path, rows)
    assert s.live_turn(task, p.jsonl(path)) is None
    rows[0]["params"]["sessionId"] = "session-b"
    write_rows(path, rows)
    with pytest.raises(ValueError, match="foreign_native_event"):
        s.live_turn(task, p.jsonl(path))


@pytest.mark.parametrize("offset,passes", [(100, True), (10000, False)])
def test_dsh_overlap_requires_real_intersecting_turns_and_tool_progress(tmp_path, offset, passes):
    tasks, points = [], {"before": {"tasks": {}}, "after": {"tasks": {}}}
    for letter, shift in (("a", 0), ("b", offset)):
        base = tmp_path / letter
        base.mkdir()
        task, _, _, _, _ = fixed_binding(base)
        scope = {**task.scope, "user_id": "user-" + letter, "session_id": "session-" + letter, "variant": "chain-" + letter}
        task = m.Task("task-" + letter, "user-" + letter, "main", letter, scope, task.paths, d.CANDIDATE)
        tasks.append(task)
        root = task.paths.run / "attempts/worker-a/candidate"
        write_json(root / "entrypoint.json", {"scope": scope, "run_id": scope["variant"], "session_id": scope["session_id"], "variant": "goal-runtime"})
        write_json(root / "session-before-followup.json", {"header": {"id": scope["session_id"], "cwd": "/workspace"}})
        raw = root / "raw-events.jsonl"
        write_rows(raw, [native_row(scope, number, kind, milliseconds=milliseconds + shift)
            for number, kind, milliseconds in ((1, "turn/start", 1000), (2, "step/start", 1500),
                                               (3, "step/end", 2000), (4, "turn/end", 4000))])
        tool_file = task.paths.run / "tool-events.jsonl"
        write_rows(tool_file, [{"scope": scope, "phase": "finish", "status": 200,
            "time": datetime.fromtimestamp((1800 + shift) / 1000, timezone.utc).isoformat()}])
        for point, through, tools_through in (("before", 1, 0), ("after", 3, 1)):
            lines = raw.read_bytes().splitlines(keepends=True)
            tool_lines = tool_file.read_bytes().splitlines(keepends=True)
            points[point]["tasks"][task.task_id] = {"activity_source": {
                "path": "attempts/worker-a/candidate/raw-events.jsonl", "line": 1, "line_sha256": p.sha(lines[0]),
                "observed_through_line": through, "observed_prefix_sha256": p.sha(b"".join(lines[:through]))},
                "tools_source": {"path": "tool-events.jsonl", "observed_through_line": tools_through,
                    "observed_prefix_sha256": p.sha(b"".join(tool_lines[:tools_through]))},
                "native_prefix_lines": through, "successful_tools": tools_through}
    if passes:
        assert m.validate_effective_overlap(tasks, points)["both_tasks_have_actual_tool_success_within_overlap"] is True
    else:
        with pytest.raises(ValueError, match="tool_progress_not_within_actual_native_overlap"):
            m.validate_effective_overlap(tasks, points)


def test_idle_without_matching_native_end_stays_a_cancel_gap(tmp_path, monkeypatch):
    task, _, _, _, _ = fixed_binding(tmp_path)
    task = m.Task(task.task_id, task.owner, "cancel", task.letter, task.scope, task.paths, task.candidate)
    root = task.paths.run / "attempts/worker-a/candidate"
    write_json(root / "entrypoint.json", {"scope": task.scope, "run_id": task.scope["variant"],
        "session_id": task.scope["session_id"], "variant": "goal-runtime"})
    write_json(root / "session-before-followup.json", {"header": {"id": task.scope["session_id"], "cwd": "/workspace"}})
    raw = root / "raw-events.jsonl"
    write_rows(raw, [native_row(task.scope, 1, "turn/start")])
    stamp = datetime.fromtimestamp(2, timezone.utc).isoformat()
    base = {"scope": task.scope, "request_id": "native-cancel-one", "time": stamp, "signal": "SIGUSR1",
        "action": "cancel", "native_api": "Agent.cancel", "native_status_before": "running", "turn": 1,
        "active_input_submitted": True, "cause": {"kind": "hook", "reason": "experiment_cancel"}}
    write_rows(root / "control-events.jsonl", [{**base, "phase": "requested", "api_executed": False},
        {**base, "phase": "native_api_returned", "api_executed": True, "request_source": {"path": "control-events.jsonl", "line": 1}},
        {**base, "phase": "native_idle_observed", "native_status_after": "idle", "native_inbox_pending": False,
         "cancel_confirmed": False}])
    encoded = raw.read_bytes()
    trigger = {"activity_source": {"path": "attempts/worker-a/candidate/raw-events.jsonl", "line": 1,
        "line_sha256": p.sha(encoded), "observed_through_line": 1, "observed_prefix_sha256": p.sha(encoded)},
        "control_source": {"path": "attempts/worker-a/candidate/control-events.jsonl", "observed_through_line": 0,
            "observed_prefix_sha256": p.sha(b"")},
        "observation_started_monotonic": 10, "observation_finished_monotonic": 10.1,
        "signal_sent_monotonic": 10.2, "signal_acknowledged_monotonic": 10.3,
        "product_http": {"request_id": "different-product-UUID"}}
    write_json(task.paths.run / "trusted/closed-observation.json", {"http": {"task_id": task.task_id, "principal": task.owner,
        "status": 200, "body": {"scope": task.scope, "process_observation": {"availability": "observed",
            "process_status": "exited", "exit_code": 1}}}})
    monkeypatch.setattr(m, "validate_http_source", lambda *args: None)  # 仅用于边界测试，不是 HTTP 实验证据。
    tools = [{"scope": task.scope, "call_id": "read-one", "tool": "read_file", "phase": phase,
        "arguments": {"path": "request.json"}, "status": 200, "time": stamp} for phase in ("start", "finish")]
    ledger = {"requests": [], "writes": [], "faults": [], "request_count": 0, "write_count": 0}
    result = s.cancel_native(task, trigger, tools, ledger, seed=101, global_paths=SimpleNamespace(run=tmp_path))
    assert result["passed"] is False and result["native_cancel_confirmed"] is False
    assert result["api_returned"] is True and result["actual_child_exit_code"] == 1
    assert result["native_request_id"] != result["product_request_id"]
    assert result["post_confirmation_effects"] == "unverified_without_native_confirmation"


def resume_materials(tmp_path, monkeypatch):
    # 对合成临时文件只模拟只读挂载标志。
    # chmod 只改变权限，不改变 statvfs(ST_RDONLY)；正式代码仍
    # 调用未修改的 immutable_bytes 与真实有界文件读取器。
    monkeypatch.setattr(common_worker.os, "statvfs", lambda path: SimpleNamespace(f_flag=os.ST_RDONLY))
    task, guard, original, first, env = fixed_binding(tmp_path)
    a, b = task.paths.fixture / "attempts/worker-a", task.paths.fixture / "attempts/worker-b"
    evidence = task.paths.run / "attempts/worker-a/candidate"
    goal = {"id": "original-goal", "revision": 1, "objective": original["prompt"], "phase": "active", "maxGoalRounds": 20}
    create = {"kind": "goal/change", "version": 1, "operation": "create", "goal": goal,
        "roundsStarted": 0, "createdAt": 1000, "updatedAt": 1000}
    pause = {**create, "operation": "pause", "goal": {**goal, "revision": 2, "phase": "paused"},
        "roundsStarted": 1, "updatedAt": 2000}
    events = [{"seq": 0, "type": "goal/change", "data": create},
        {"seq": 1, "type": "user/message", "data": {"source": {"kind": "goal", "goalId": goal["id"], "revision": 1, "round": 1}}},
        {"seq": 2, "type": "turn/start", "data": {"turn": 1}},
        {"seq": 3, "type": "goal/change", "data": pause},
        {"seq": 4, "type": "turn/end", "data": {"turn": 1, "reason": {"kind": "blocked"}}}]
    physical = task.paths.state / "sessions/main.jsonl"
    write_rows(physical, [{"type": "session", "id": task.scope["session_id"], "cwd": "/workspace"}, *events])
    (task.paths.workspace / "empty").mkdir()
    (task.paths.workspace / "receipt.txt").write_bytes(b"already committed\n")
    live_goal = {**pause["goal"], "roundsStarted": 1, "createdAt": 1000, "updatedAt": 2000, "activation": "disarmed"}
    index = {"schema": "harness-lab-goal-pause/1", "candidate": d.CANDIDATE, "chain_id": task.scope["variant"],
        "scope": task.scope, "session_id": task.scope["session_id"], "goal": live_goal,
        "session": {"id": task.scope["session_id"], "cwd": "/workspace", "next_seq": 5,
            "artifact_sha256": p.sha(physical.read_bytes()), "persisted_events_sha256": d.digest(events)},
        "source": {"native_turn_end_seq": 4, "flush": "await_live_sessions_flush_after_native_turn_end_and_whenIdle",
            "close": "handle_dispose_private_scopes_and_runtime_shutdown_all_resolved"}}
    write_json(a / "request.json", original)
    write_json(a / "lifecycle.json", first)
    write_json(evidence / "result.json", {"session_id": task.scope["session_id"], "status": "paused", "pause_confirmed": False,
        "stop_reason": "blocked", "elapsed_seconds": 39.5, "lifecycle": original["lifecycle"]})
    write_json(evidence / "session-artifact-after-flush.json", {"scope": task.scope,
        "native_location": {"path": "/state/sessions/main.jsonl"}, "physical_sha256": p.sha(physical.read_bytes())})
    write_json(evidence / "lifecycle-pause-candidate.json", index)
    write_json(evidence / "native-state-index-after-close.json", d.inventory(guard.state_dir, guard.workspace))
    write_json(b / "lifecycle-pause.json", index)
    write_json(b / "lifecycle-state.json", d.inventory(guard.state_dir, guard.workspace))
    request, binding = deepcopy(original), deepcopy(first)
    request.pop("prompt")
    request["lifecycle"].update(mode="resume", attempt_id="worker-b")
    request["timeout_seconds"] = 260
    binding.update(mode="resume", attempt_id="worker-b", resume={"pause_index_file": "/config/lifecycle-pause.json",
        "pause_index_sha256": p.sha((b / "lifecycle-pause.json").read_bytes())})
    binding["state"]["index_sha256"] = p.sha((b / "lifecycle-state.json").read_bytes())
    binding["active_budget"].update(used_seconds=40, remaining_seconds=260)
    guard.attempts = {"worker-a": SimpleNamespace(config_dir=a, evidence_dir=evidence, environment=env),
        "worker-b": SimpleNamespace(config_dir=b, environment=deepcopy(env))}
    return task, guard, request, binding, physical


def test_resume_materials_retain_original_goal_files_and_budget_without_claiming_resume(tmp_path, monkeypatch):
    _, guard, request, binding, _ = resume_materials(tmp_path, monkeypatch)
    result = d.verify_resume_materials(guard, request, binding)
    assert result["goal_id"] == "original-goal" and result["native_resume_completed"] is False
    # B 的 Worker 只看到自身挂载；原生入口在派发工作前
    # 再次检查完整恢复前缀。
    guard.attempts.pop("worker-a")
    assert d.verify_resume_materials(guard, request, binding)["native_resume_completed"] is False


@pytest.mark.parametrize("mutation", ["extra-file", "empty-directory", "pause-bytes", "budget-reset", "model-token", "image", "source", "old-result", "elapsed"])
def test_resume_rejects_actual_files_original_binding_and_time_mutations(tmp_path, monkeypatch, mutation):
    task, guard, request, binding, _ = resume_materials(tmp_path, monkeypatch)
    original = guard.attempts["worker-a"]
    if mutation == "extra-file":
        (task.paths.workspace / "foreign.txt").write_text("changed")
    elif mutation == "empty-directory":
        (task.paths.workspace / "empty").rmdir()
    elif mutation == "pause-bytes":
        (guard.attempts["worker-b"].config_dir / "lifecycle-pause.json").write_text("{}")
    elif mutation == "budget-reset":
        binding["active_budget"].update(used_seconds=0, remaining_seconds=300)
        request["timeout_seconds"] = 300
    elif mutation == "model-token":
        binding["model"]["run_token_sha256"] = "9" * 64
    elif mutation == "image":
        binding["image"]["original_image_id"] = guard.attempts["worker-b"].environment["HARNESS_LIFECYCLE_IMAGE_ID"] = "sha256:" + "9" * 64
    elif mutation == "source":
        binding["materials"]["source_export_sha256"] = "9" * 64
    else:
        value = p.read(original.evidence_dir / "result.json")
        value["session_id" if mutation == "old-result" else "elapsed_seconds"] = "other-session" if mutation == "old-result" else 41
        write_json(original.evidence_dir / "result.json", value)
    with pytest.raises(BoundaryError):
        d.verify_resume_materials(guard, request, binding)


@pytest.mark.parametrize("mutation,code", [("header", "physical_header_changed"), ("sequence", "physical_sequence_changed"),
    ("partial", "partial_session"), ("event-bytes", "physical_events_changed"), ("goal", "physical_goal_changed"),
    ("end-reason", "physical_blocked_end_missing")])
def test_resume_replays_physical_bytes_even_with_rehashed_candidate_copies(tmp_path, monkeypatch, mutation, code):
    task, guard, request, binding, physical = resume_materials(tmp_path, monkeypatch)
    records = [json.loads(line) for line in physical.read_bytes().splitlines()]
    if mutation == "header":
        records[0]["id"] = "foreign-session"
    elif mutation == "sequence":
        records[-1]["seq"] = 99
    elif mutation == "event-bytes":
        records[3]["data"]["untrusted_extra"] = True
    elif mutation == "goal":
        for row in records:
            if row.get("type") == "goal/change":
                row["data"]["goal"]["objective"] = "changed objective"
    elif mutation == "end-reason":
        records[-1]["data"]["reason"] = {"kind": "completed"}
    write_rows(physical, records)
    if mutation == "partial":
        physical.write_bytes(physical.read_bytes()[:-1])
    # 即使不可信候选重新计算报告哈希，独立的物理文件头、序列、
    # Goal、结束标记及可信实时事件摘要仍必须一致。
    a, b = guard.attempts["worker-a"], guard.attempts["worker-b"]
    artifact = p.read(a.evidence_dir / "session-artifact-after-flush.json")
    artifact["physical_sha256"] = p.sha(physical.read_bytes())
    write_json(a.evidence_dir / "session-artifact-after-flush.json", artifact)
    index = p.read(b.config_dir / "lifecycle-pause.json")
    index["session"]["artifact_sha256"] = artifact["physical_sha256"]
    if mutation != "event-bytes":
        index["session"]["persisted_events_sha256"] = d.digest(records[1:])
    for path in (a.evidence_dir / "lifecycle-pause-candidate.json", b.config_dir / "lifecycle-pause.json"):
        write_json(path, index)
    inventory = d.inventory(guard.state_dir, guard.workspace)
    write_json(a.evidence_dir / "native-state-index-after-close.json", inventory)
    write_json(b.config_dir / "lifecycle-state.json", inventory)
    binding["resume"]["pause_index_sha256"] = p.sha((b.config_dir / "lifecycle-pause.json").read_bytes())
    binding["state"]["index_sha256"] = p.sha((b.config_dir / "lifecycle-state.json").read_bytes())
    with pytest.raises(BoundaryError, match=code):
        d.verify_resume_materials(guard, request, binding)


def test_provenance_expansion_is_lossless_and_rejects_reordered_ranges():
    event = {"seq": 5, "type": "step/end", "sourceEventSeqs": [0, [1, 3]]}
    assert d.expanded_events([event])[0]["sourceEventSeqs"] == [0, 1, 2, 3]
    assert event["sourceEventSeqs"] == [0, [1, 3]]
    with pytest.raises(BoundaryError, match="provenance_range_order"):
        d.expanded_events([{**event, "sourceEventSeqs": [[2, 3], 1]}])


def surface_budget(task, tmp_path):
    task.paths.budget = tmp_path / "budget"
    task.paths.budget.mkdir()
    (task.paths.budget / "run.token").write_text("offline-surface-token\n")
    return CallBudget(task.paths.budget / "calls.sqlite", "offline-surface-token", 20)


def surface_materials(tmp_path, monkeypatch):
    task, _, _, _, env = fixed_binding(tmp_path)
    task = m.Task(task.task_id, task.owner, task.kind, task.letter,
        {**task.scope, "variant": "offline-main-a"}, task.paths, task.candidate)
    surface_budget(task, tmp_path)
    pins = {"candidate_image_id": env["HARNESS_LIFECYCLE_IMAGE_ID"], "source_export_sha256": "2" * 64,
        "adapter_export_sha256": "3" * 64}
    write_json(task.paths.run / "trusted/pins.json", pins)
    surface_root = task.paths.fixture / "surface"
    host = lambda path: str(path)
    mounts = [{"Source": host(source), "Destination": target, "RW": writable} for source, target, writable in (
        (surface_root / "request.json", "/config/surface.json", False),
        (task.paths.fixture / "scope.json", "/config/scope.json", False),
        (task.paths.fixture / "scope.token", "/config/scope.token", False),
        (surface_root / "workspace", "/workspace", True),
        (task.paths.run / "surface-calibration", "/evidence", True))]
    network = {"id": "8" * 64, "name": "harness-lab-offline-a", "trial_id": "offline", "aliases": ["surface-a"]}
    before = {"id": "offline-only-container", "image_id": pins["candidate_image_id"], "role": "worker", "init_enabled": True,
        "memory_limit": 2 * 1024**3, "cpu_nano": 750000000, "mounts": mounts, "environment_names": [],
        "trial_id": "offline", "networks": {network["name"]: {"network_id": "", "aliases": ["surface-a"]}},
        "state": {"Status": "created", "Running": False, "ExitCode": 0, "OOMKilled": False,
            "Dead": False, "Paused": False, "Restarting": False, "Error": ""}}
    after = deepcopy(before)
    after["state"]["Status"] = "exited"
    after["networks"][network["name"]]["network_id"] = network["id"]
    record = {"before_start": before, "after_exit": after, "network_binding": network,
        "deleted": {"id": before["id"], "id_absent": True}}
    write_json(task.paths.run / "trusted/surface-container.json", record)
    schemas = [{"name": "offline-tool-" + str(number), "description": "synthetic boundary fixture", "parameters": {}}
        for number in range(11)]
    surface = {"schema": "harness-lab-goal-tool-surface/1", "scope": task.scope,
        "declared_image_id": pins["candidate_image_id"], "provenance": {key: pins[key] for key in ("source_export_sha256", "adapter_export_sha256")},
        "model_dispatch_attempts": 0, "schemas": schemas, "contract_sha256": d.digest(sorted(schemas, key=lambda item: item["name"]))}
    raw = json.dumps(surface, indent=3).encode() + b"\n"
    output = task.paths.run / "surface-calibration/goal-tool-surface.json"
    output.parent.mkdir()
    output.write_bytes(raw)
    monkeypatch.setattr(s, "load_config", lambda: SimpleNamespace(host_data_root=tmp_path, container_data_root=tmp_path))
    return task, record, raw


def test_surface_preparation_accepts_real_empty_sqlite_budget_without_reset(tmp_path):
    task, _, _, _, _ = fixed_binding(tmp_path)
    surface_budget(task, tmp_path)
    before = p.budget_snapshot(task.paths.budget, allow_empty=True)
    assert before["count"] == 0 and before["cap"] == 20 and before["rows"] == []
    # 普通活跃任务快照仍要求非空历史。
    with pytest.raises(ValueError, match="model_call_reservation_history_invalid"):
        p.budget_snapshot(task.paths.budget)
    token = (task.paths.budget / "run.token").read_bytes()
    s.prepare_surface(task)
    assert p.budget_snapshot(task.paths.budget, allow_empty=True) == before
    assert (task.paths.budget / "run.token").read_bytes() == token
    assert p.read(task.paths.fixture / "surface/request.json")["session_id"] == task.scope["session_id"]


def test_surface_acceptance_preserves_original_bytes_and_pins(tmp_path, monkeypatch):
    task, _, raw = surface_materials(tmp_path, monkeypatch)
    original = (task.paths.run / "trusted/pins.json").read_bytes()
    budget = p.budget_snapshot(task.paths.budget, allow_empty=True)
    s.accept_surface(task)
    assert (task.paths.run / "trusted/tool-surface.json").read_bytes() == raw
    assert (task.paths.run / "trusted/pins.json").read_bytes() == original
    assert s.task_pins(task)["tool_surface_sha256"] == p.sha(raw)
    assert p.budget_snapshot(task.paths.budget, allow_empty=True) == budget


@pytest.mark.parametrize("phase", ["prepare", "accept"])
@pytest.mark.parametrize("mutation", ["wrong-token", "wrong-cap", "existing-call"])
def test_surface_empty_history_exception_keeps_real_budget_identity_and_zero_calls(tmp_path, monkeypatch, phase, mutation):
    if phase == "prepare":
        task, _, _, _, _ = fixed_binding(tmp_path)
        budget = surface_budget(task, tmp_path)
    else:
        task, _, _ = surface_materials(tmp_path, monkeypatch)
        budget = CallBudget(task.paths.budget / "calls.sqlite", "offline-surface-token", 20)
    if mutation == "wrong-token":
        (task.paths.budget / "run.token").write_text("different-surface-token\n")
    elif mutation == "wrong-cap":
        with budget.connect() as db:
            db.execute("UPDATE budget SET cap=21 WHERE id=1")
    else:
        call = budget.reserve("/chat/completions")
        budget.finish(call, "forwarded", 200)
    code = ("original_model_budget_binding_mismatch" if mutation != "existing-call"
        else "dsh_surface_model_calls_" + ("before" if phase == "prepare" else "after") + "_calibration")
    with pytest.raises(ValueError, match=code):
        (s.prepare_surface if phase == "prepare" else s.accept_surface)(task)


@pytest.mark.parametrize("mutation", ["extra-mount", "missing-mount", "duplicate-mount", "writable-scope", "changed-source",
    "model-key", "cpu", "network", "exit", "image", "not-deleted", "model-call"])
def test_surface_rejects_mount_cap_and_zero_model_mutations(tmp_path, monkeypatch, mutation):
    task, record, _ = surface_materials(tmp_path, monkeypatch)
    before, after = record["before_start"], record["after_exit"]
    if mutation == "extra-mount":
        before["mounts"].append({"Source": "/foreign", "Destination": "/budget/run.token", "RW": False})
    elif mutation == "missing-mount":
        before["mounts"].pop()
    elif mutation == "duplicate-mount":
        before["mounts"][-1] = deepcopy(before["mounts"][0])
    elif mutation == "writable-scope":
        before["mounts"][1]["RW"] = True
    elif mutation == "changed-source":
        after["mounts"][0]["Source"] = "/other/source"
    elif mutation == "model-key":
        before["environment_names"].append("MODEL_API_KEY")
    elif mutation == "cpu":
        after["cpu_nano"] = 1000000000
    elif mutation == "network":
        after["networks"] = {"other": {"network_id": "other"}}
    elif mutation == "exit":
        after["state"]["ExitCode"] = 1
    elif mutation == "image":
        after["image_id"] = "sha256:" + "9" * 64
    elif mutation == "not-deleted":
        record["deleted"]["id_absent"] = False
    else:
        budget = CallBudget(task.paths.budget / "calls.sqlite", "offline-surface-token", 20)
        call = budget.reserve("/chat/completions")
        budget.finish(call, "forwarded", 200)
    write_json(task.paths.run / "trusted/surface-container.json", record)
    with pytest.raises(ValueError):
        s.accept_surface(task)


@pytest.mark.parametrize("created_id", ["", "8" * 64])
def test_surface_accepts_created_empty_or_fixed_id_only_with_exact_registered_exit(tmp_path, monkeypatch, created_id):
    task, record, _ = surface_materials(tmp_path, monkeypatch)
    network = record["network_binding"]
    record["before_start"]["networks"][network["name"]]["network_id"] = created_id
    write_json(task.paths.run / "trusted/surface-container.json", record)
    s.accept_surface(task)
    assert p.read(task.paths.run / "trusted/surface-container.json")["after_exit"]["networks"] == {
        network["name"]: {"network_id": network["id"], "aliases": ["surface-a"]}}


@pytest.mark.parametrize("mutation", ["wrong-after-id", "empty-after-id", "wrong-before-id", "multiple-networks",
    "changed-before-alias", "changed-after-alias", "changed-name", "missing-registration", "wrong-registration-id"])
def test_surface_refuses_network_changes_outside_its_exact_created_registration(tmp_path, monkeypatch, mutation):
    task, record, _ = surface_materials(tmp_path, monkeypatch)
    network = record["network_binding"]
    before, after = record["before_start"]["networks"], record["after_exit"]["networks"]
    if mutation == "wrong-after-id":
        after[network["name"]]["network_id"] = "9" * 64
    elif mutation == "empty-after-id":
        after[network["name"]]["network_id"] = ""
    elif mutation == "wrong-before-id":
        before[network["name"]]["network_id"] = "9" * 64
    elif mutation == "multiple-networks":
        after["foreign-network"] = {"network_id": "9" * 64, "aliases": ["surface-a"]}
    elif mutation == "changed-before-alias":
        before[network["name"]]["aliases"] = ["surface-b"]
    elif mutation == "changed-after-alias":
        after[network["name"]]["aliases"] = ["surface-a", "extra-alias"]
    elif mutation == "changed-name":
        after["renamed-network"] = after.pop(network["name"])
    elif mutation == "missing-registration":
        record.pop("network_binding")
    else:
        network["id"] = "9" * 64
    write_json(task.paths.run / "trusted/surface-container.json", record)
    with pytest.raises(ValueError):
        s.accept_surface(task)


def surface_host(tmp_path, monkeypatch):
    """测试真实宿主校准与 JSON 写入器，仅 Docker 输入输出为替身。"""
    paths = SimpleNamespace(fixture=tmp_path / "fixture", run=tmp_path / "run")
    (paths.run / "surface-calibration").mkdir(parents=True)
    (paths.run / "trusted").mkdir()
    lab = host.MultiUserLab.__new__(host.MultiUserLab)
    lab.args = SimpleNamespace(trial_id="offline", candidate=d.CANDIDATE)
    lab.config = None
    lab.pins, lab.image = {"model_name": "offline"}, {"worker": "sha256:" + "1" * 64}
    network_id = "8" * 64
    lab.network_registrations = {network_id: {"id": network_id, "name": "harness-lab-offline-a", "trial_id": "offline"}}
    registration = deepcopy(lab.network_registrations)
    before = {"id": "7" * 64, "state": {"Status": "created", "ExitCode": 0, "OOMKilled": False},
        "networks": {"harness-lab-offline-a": {"network_id": "", "aliases": ["surface-a"]}}}
    after = deepcopy(before)
    after["state"]["Status"] = "exited"
    after["networks"]["harness-lab-offline-a"]["network_id"] = network_id
    lab.registrations, lab.registry_lock = {}, threading.RLock()
    calls = []
    def create(*args, **kwargs):
        calls.append(("create", args, kwargs))
        lab.registrations[before["id"]] = deepcopy(before)
        return before["id"]
    lab.create = create
    lab.bench = lambda *args, **kwargs: calls.append(("bench", args, kwargs))
    lab.observation = lambda identity: deepcopy(after)
    lab.retire = lambda identity: calls.append(("retire", identity))
    lab.call = lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="0\n" if argv[1] == "wait" else "offline Docker boundary\n", stderr="")
    monkeypatch.setattr(host, "paths_for", lambda *args: paths)
    return lab, paths, registration, calls


def test_host_surface_record_binds_exact_create_registration_without_mutating_it(tmp_path, monkeypatch):
    lab, paths, registrations, calls = surface_host(tmp_path, monkeypatch)
    lab.calibrate_surface("main", "a")
    record = p.read(paths.run / "trusted/surface-container.json")
    assert record["network_binding"] == {**registrations["8" * 64], "aliases": ["surface-a"]}
    assert lab.network_registrations == registrations
    created = next(row for row in calls if row[0] == "create")
    assert created[1][2:4] == ("harness-lab-offline-a", "surface-a")
    assert record["before_start"]["networks"]["harness-lab-offline-a"]["network_id"] == ""
    assert record["after_exit"]["networks"]["harness-lab-offline-a"]["network_id"] == "8" * 64


@pytest.mark.parametrize("mutation", ["missing", "wrong-key", "duplicate-name", "wrong-trial"])
def test_host_surface_rejects_unbound_network_before_launch(tmp_path, monkeypatch, mutation):
    lab, _, _, calls = surface_host(tmp_path, monkeypatch)
    if mutation == "missing":
        lab.network_registrations.clear()
    elif mutation == "wrong-key":
        lab.network_registrations["9" * 64] = lab.network_registrations.pop("8" * 64)
    elif mutation == "duplicate-name":
        lab.network_registrations["9" * 64] = {**lab.network_registrations["8" * 64], "id": "9" * 64}
    else:
        lab.network_registrations["8" * 64]["trial_id"] = "other-trial"
    with pytest.raises(RuntimeError, match='已创建网络的确切身份'):
        lab.calibrate_surface("main", "a")
    assert calls == []
