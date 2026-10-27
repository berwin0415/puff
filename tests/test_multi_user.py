"""仅校准合成验收器，测试数据不能代替原生 I24 通过。

在复核后的 Docker 验收镜像运行，不启动 SDK 或调用模型。
真实 I24 判定还需候选原件、产品 HTTP、原生 SQLite checkpoint、
完整 Docker 身份与工具副作用证据。
"""
from datetime import datetime, timedelta, timezone
import base64
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner import multi_user as m
from runner import pause_recovery as p
from runner.config import load_config
from scripts.run_multi_user import paths_for
from adapters.deepagents_langgraph import pause_recovery as lifecycle_identity


def encoded_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(p.canonical(row) + b"\n" for row in rows))


def stamp(seconds):
    return (datetime(2026, 10, 20, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def sample_reference(path, relative, through, *, selected=None):
    lines = path.read_bytes().splitlines(keepends=True)
    result = {"path": relative, "observed_through_line": through,
        "observed_prefix_sha256": p.sha(b"".join(lines[:through]))}
    if selected:
        result.update({"line": selected, "line_sha256": p.sha(lines[selected - 1])})
    return result


def fake_overlap(tmp_path, *, second_offset=.1):
    tasks, samples = [], {"before": {}, "after": {}}
    for letter, offset in (("a", 0), ("b", second_offset)):
        scope = {"experiment_id": "calibration", "variant": "fake-" + letter,
            "user_id": "user-" + letter, "session_id": "session-" + letter}
        run = tmp_path / letter
        task = m.Task("task-" + letter, "user-" + letter, "main", letter, scope, SimpleNamespace(run=run))
        tasks.append(task)
        raw_path = run / "attempts/worker-a/candidate/raw-events.jsonl"
        raw = []
        for seconds, identity, name, phase in ((0, "m1", "model", "start"), (1, "m1", "model", "finish"),
                (1, "t1", "tools", "start"), (2, "t1", "tools", "finish"),
                (2, "m2", "model", "start"), (4, "m2", "model", "finish")):
            event = {"id": identity, "name": name, "input" if phase == "start" else "result": {}}
            raw.append({"scope": scope, "time": stamp(seconds + offset), "stream_mode": "tasks", "data": event})
        encoded_lines(raw_path, raw)
        tool_path = run / "tool-events.jsonl"
        encoded_lines(tool_path, [{"scope": scope, "time": stamp(seconds + offset), "phase": "finish", "status": 200}
            for seconds in (1.1, 1.2)])
        for point, through, selected, tool_through in (("before", 1, 1, 0), ("after", 5, 5, 2)):
            samples[point][task.task_id] = {
                "activity_source": sample_reference(raw_path, "attempts/worker-a/candidate/raw-events.jsonl", through, selected=selected),
                "tools_source": sample_reference(tool_path, "tool-events.jsonl", tool_through),
                "native_prefix_lines": through, "successful_tools": tool_through}
    return tasks, {point: {"tasks": value} for point, value in samples.items()}


def test_overlap_requires_actual_intersecting_native_activity_and_tool_progress(tmp_path):
    tasks, record = fake_overlap(tmp_path)
    result = m.validate_effective_overlap(tasks, record)
    assert result["both_tasks_have_actual_tool_success_within_overlap"] is True
    assert result["overlapping_native_intervals"] > 0


def test_two_completed_successful_serial_tasks_do_not_pass_overlap(tmp_path):
    tasks, record = fake_overlap(tmp_path, second_offset=10)
    with pytest.raises(ValueError, match="tool_progress_not_within_actual_native_overlap"):
        m.validate_effective_overlap(tasks, record)


def test_prefix_that_already_finished_cannot_claim_active(tmp_path):
    tasks, record = fake_overlap(tmp_path)
    target = record["before"]["tasks"]["task-a"]
    raw = tasks[0].paths.run / "attempts/worker-a/candidate/raw-events.jsonl"
    target["activity_source"] = sample_reference(raw, "attempts/worker-a/candidate/raw-events.jsonl", 2, selected=1)
    target["native_prefix_lines"] = 2
    with pytest.raises(ValueError, match="overlap_prefix_has_no_live_native_task"):
        m.validate_effective_overlap(tasks, record)


def test_native_token_stream_without_new_successful_tools_is_insufficient(tmp_path):
    tasks, record = fake_overlap(tmp_path)
    sample = record["after"]["tasks"]["task-b"]
    sample["tools_source"] = sample_reference(tasks[1].paths.run / "tool-events.jsonl", "tool-events.jsonl", 0)
    sample["successful_tools"] = 0
    with pytest.raises(ValueError, match="overlap_missing_effective_progress"):
        m.validate_effective_overlap(tasks, record)


def test_rehashed_foreign_scope_native_stream_still_fails(tmp_path):
    tasks, record = fake_overlap(tmp_path)
    path = tasks[1].paths.run / "attempts/worker-a/candidate/raw-events.jsonl"
    rows = [row for _, _, row in p.jsonl(path)]
    rows[0]["scope"]["user_id"] = "user-a"
    encoded_lines(path, rows)
    for point, through, selected in (("before", 1, 1), ("after", 5, 5)):
        record[point]["tasks"]["task-b"]["activity_source"] = sample_reference(
            path, "attempts/worker-a/candidate/raw-events.jsonl", through, selected=selected)
    with pytest.raises(ValueError, match="overlap_foreign_native_scope"):
        m.validate_effective_overlap(tasks, record)


def test_denied_request_natural_state_changes_are_retained_not_relabelled(tmp_path, monkeypatch):
    task = SimpleNamespace(task_id="task-b")
    sequence = iter({"actual_file_hash": str(index)} for index in range(6))
    monkeypatch.setattr(m, "observed_files", lambda _: next(sequence))
    monkeypatch.setattr(m, "ledger", lambda *_: {"writes": []})
    client = SimpleNamespace(request=lambda *_: {"status": 403, "body": {"error": {"code": "scope_denied"}}})
    result = tmp_path / "foreign.json"
    m.foreign_probe(None, client, task, "pause", result)
    observed = p.read(result)
    assert observed["passed"] is False and len(observed["attempts"]) == 3
    assert all(attempt["before"] != attempt["after"] for attempt in observed["attempts"])


def test_real_stable_window_does_not_discard_previous_changed_snapshot(tmp_path, monkeypatch):
    task = SimpleNamespace(task_id="task-b")
    sequence = iter({"actual_file_hash": value} for value in ("one", "two", "three", "three"))
    monkeypatch.setattr(m, "observed_files", lambda _: next(sequence))
    monkeypatch.setattr(m, "ledger", lambda *_: {"writes": []})
    client = SimpleNamespace(request=lambda *_: {"status": 403, "body": {"error": {"code": "scope_denied"}}})
    result = tmp_path / "foreign.json"
    m.foreign_probe(None, client, task, "resume", result)
    observed = p.read(result)
    assert observed["passed"] is True and len(observed["attempts"]) == 2
    assert observed["attempts"][0]["before"] != observed["attempts"][0]["after"]


def test_missing_authentication_and_missing_task_do_not_count_as_scope_rejection(tmp_path, monkeypatch):
    task = SimpleNamespace(task_id="task-b")
    monkeypatch.setattr(m, "observed_files", lambda _: {"actual_file_hash": "stable"})
    monkeypatch.setattr(m, "ledger", lambda *_: {"writes": []})
    for status in (401, 404):
        client = SimpleNamespace(request=lambda *_, status=status: {"status": status, "body": {"error": {"code": "scope_denied"}}})
        result = tmp_path / (str(status) + ".json")
        m.foreign_probe(None, client, task, "cancel", result)
        assert p.read(result)["passed"] is False


@pytest.mark.parametrize("kind", ["main", "cancel"])
def test_active_controls_and_sequential_exit_reads_refresh_idle_sockets_without_replaying_posts(tmp_path, monkeypatch, kind):
    """通过15秒空闲限制的后端替身测试真实 ProductClient。

    每个用户的活动选择耗时20秒；随后轮询 A 超过15秒时，
    B 的控制确认连接保持空闲。校准不进行真实等待，不运行候选、
    模型或原生能力断言。
    """
    clock, sequence, posts, first_a_poll = [100.0], [0], [], [None]
    owner_state_reads, delayed_b_eof = {"user-a": 0, "user-b": 0}, [False]
    paths = SimpleNamespace(fixture=tmp_path / "fixture", run=tmp_path / "run")
    for directory in (paths.fixture, paths.run / "clients", paths.run / "trusted", paths.run / ("backend-" + kind)):
        directory.mkdir(parents=True)
    tasks = []
    for letter in ("a", "b"):
        task_id = "task-" + letter + ("-cancel" if kind == "cancel" else "")
        task_run = tmp_path / task_id
        (task_run / "trusted").mkdir(parents=True)
        tasks.append(SimpleNamespace(task_id=task_id, kind=kind, letter=letter,
            owner="user-" + letter, paths=SimpleNamespace(run=task_run)))
        (paths.fixture / ("user-" + letter + ".token")).write_text("user-" + letter + "\n")
    task_owner = {task.task_id: task.owner for task in tasks}
    control = "pause" if kind == "main" else "cancel"
    journal = paths.run / ("backend-" + kind) / "control.jsonl"
    encoded_lines(journal, [{"kind": "worker_start_attempt", "task_id": task.task_id,
        "attempt_id": "worker-a", "observation_clock": "Docker_VM_monotonic/1", "controller_monotonic": 0.0}
        for task in tasks])
    p.create(paths.run / "trusted" / (kind + "-worker-a.json"),
        {task.task_id: {"id": "container-" + task.letter} for task in tasks})

    class Reply:
        def __init__(self, status, body, connection_id, request_id):
            self.status, self.body = status, body
            self.headers = {"X-Connection-ID": connection_id, "X-Request-ID": request_id}

        def read(self, _limit):
            return p.canonical(self.body)

        def getheader(self, name):
            return self.headers.get(name)

    class IdleBackendConnection:
        def __init__(self, *_args, **_kwargs):
            sequence[0] += 1
            self.identity = "connection-" + str(sequence[0])
            self.last_response, self.principal, self.closed = None, None, False

        def request(self, method, route, *, body, headers):
            assert not self.closed, "复用了已关闭的 HTTPConnection"
            if self.last_response is not None and clock[0] - self.last_response > 15:
                raise ConnectionError("backend_closed_idle_socket")
            principal = headers["Authorization"].removeprefix("Bearer ")
            assert self.principal in (None, principal), "socket 改变了已鉴权所有者"
            self.principal = principal
            _, _, task_id, operation = route.split("/", 3)
            operation = operation.split("?", 1)[0]
            status, response = 200, {}
            if principal != task_owner[task_id]:
                status, response = 403, {"error": {"code": "scope_denied"}}
            elif method == "POST":
                assert operation == control
                posts.append((principal, task_id, operation, self.identity))
                status = 202
            elif operation == "state":
                owner_state_reads[principal] += 1
                self.last_owner_state_count = owner_state_reads[principal]
                exited = len(posts) == 2
                if exited and task_id == tasks[0].task_id:
                    if first_a_poll[0] is None:
                        first_a_poll[0] = clock[0]
                    exited = clock[0] - first_a_poll[0] > 16
                response = {"process_observation": {"availability": "observed",
                    "process_status": "exited" if exited else "running", "exit_code": 0}}
            self.reply = Reply(status, response, self.identity, "request-" + str(sequence[0]))

        def getresponse(self):
            self.last_response = clock[0]
            return self.reply

        def close(self):
            if not self.closed:
                if (kind == "main" and self.principal == "user-b"
                        and getattr(self, "last_owner_state_count", 0) == 3 and not delayed_b_eof[0]):
                    # B 的专用重连或 EOF 处理可能超过空闲上限，
                    # A 的拒绝请求必须先使用新连接。
                    clock[0] += 20
                    delayed_b_eof[0] = True
                m.append(journal, {"kind": "connection_peer_eof", "connection_id": self.identity})
                self.closed = True

    counters = {task.task_id: 0 for task in tasks}

    def activity(task, **_kwargs):
        counters[task.task_id] += 1
        return {"native_prefix_lines": counters[task.task_id], "successful_tools": counters[task.task_id]}

    def wait_activity(_task, _deadline):
        clock[0] += 20
        return {}

    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(m.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(m.http.client, "HTTPConnection", IdleBackendConnection)
    monkeypatch.setattr(m, "activity", activity)
    monkeypatch.setattr(m, "wait_activity", wait_activity)
    monkeypatch.setattr(m, "observed_files", lambda _task: {"unchanged": True})
    monkeypatch.setattr(m, "ledger", lambda *_args: {"unchanged": True})
    m.exercise(SimpleNamespace(kind=kind, phase="active", endpoint="http://127.0.0.1:8091"), paths, tasks)
    assert [(owner, task_id, operation) for owner, task_id, operation, _ in posts] == [
        (task.owner, task.task_id, control) for task in tasks]
    assert first_a_poll[0] is not None and clock[0] - first_a_poll[0] > 16
    assert p.read(paths.run / ("clients/" + kind + "-active/finished.json"))["phase"] == "active"
    if kind == "main":
        assert delayed_b_eof[0] is True
        output = paths.run / "clients/main-active"
        reconnected = p.read(output / "reconnect-a.json")["second"]["connection_id"]
        denial = p.read(output / "foreign-reconnect-state.json")
        assert denial["passed"] is True
        assert denial["attempts"][0]["response"]["connection_id"] == reconnected


def test_main_cancel_and_user_paths_have_no_shared_state_workspace_or_budget():
    config = load_config()
    args = SimpleNamespace(trial_id="path-calibration", date="2026-10-20")
    values = [paths_for(config, args, kind, letter) for kind in ("main", "cancel") for letter in ("a", "b")]
    for name in ("state", "workspace", "fixture", "budget", "run"):
        paths = [getattr(value, name) for value in values]
        assert len(set(paths)) == 4
        assert all(left != right and left not in right.parents and right not in left.parents
            for index, left in enumerate(paths) for right in paths[index + 1:])
    assert all("alice" not in str(path.state) for path in values)


def test_product_202_alone_cannot_pass_dispatch_verifier(tmp_path):
    global_paths = SimpleNamespace(run=tmp_path)
    (tmp_path / "backend-main").mkdir()
    (tmp_path / "backend-main/control.jsonl").write_text("")
    task = SimpleNamespace(owner="user-a", task_id="task-a", kind="main")
    trigger = {"product_http": {"status": 202, "principal": "user-a", "task_id": "task-a", "operation": "pause",
        "request_id": "fake", "body": {"native_pause_confirmed": True}}}
    with pytest.raises(ValueError, match="actual_owner_authorization_missing"):
        m.validate_product_dispatch(global_paths, task, trigger)


def test_idempotent_reissued_write_cannot_hide_behind_unchanged_business_ledger(tmp_path):
    scope = {"experiment_id": "calibration", "variant": "fake-a", "user_id": "user-a", "session_id": "main"}
    paths = SimpleNamespace(run=tmp_path)
    filename = tmp_path / "tool-events.jsonl"
    original = [{"scope": scope, "tool": "write", "phase": "start", "call_id": "first",
        "arguments": {"operation_id": "confirmed-operation"}},
        {"scope": scope, "tool": "write", "phase": "finish", "call_id": "first", "status": 200}]
    encoded_lines(filename, original)
    data = filename.read_bytes()
    paused = {"audit": {"bytes": len(data), "sha256": p.sha(data), "completed_write_ids": ["confirmed-operation"]}}
    # 模拟业务幂等机制可能保留完全相同的原业务记录，
    # 但重复派发已确认操作仍须独立判为失败。
    encoded_lines(filename, original + [{"scope": scope, "tool": "write", "phase": "start", "call_id": "second",
        "arguments": {"operation_id": "confirmed-operation"}}])
    with pytest.raises(ValueError, match="completed_business_write_reissued_after_resume"):
        m.validate_no_reissued_writes(paths, scope, paused)


def test_changed_original_tool_prefix_cannot_be_accepted_by_a_new_suffix(tmp_path):
    paths = SimpleNamespace(run=tmp_path)
    filename = tmp_path / "tool-events.jsonl"
    encoded_lines(filename, [{"original": "actual"}])
    original = filename.read_bytes()
    paused = {"audit": {"bytes": len(original), "sha256": p.sha(original), "completed_write_ids": ["operation"]}}
    encoded_lines(filename, [{"original": "changed"}])
    with pytest.raises(ValueError, match="trusted_tool_prefix_changed"):
        m.validate_no_reissued_writes(paths, {}, paused)


def cancel_identity_fixture():
    scope = {"experiment_id": "calibration", "variant": "fake-cancel-a", "user_id": "user-a", "session_id": "main"}
    task = SimpleNamespace(scope=scope)
    common = {"scope": scope, "run_id": scope["variant"], "session_id": "main", "chain_id": scope["variant"],
        "attempt_id": "worker-a", "signal": "SIGUSR1", "action": "cancel", "native_api": "RunControl.request_drain",
        "reason": "experiment_cancel", "semantics": "cooperative_drain_at_superstep_boundary", "immediate_cancel": False,
        "request_id": "original-drain-request"}
    request = {**common, "api_executed": False}
    api = {**common, "api_executed": True, "drain_requested": True, "drain_reason": "experiment_cancel"}
    observation = {**common, "api_executed": True, "native_exception_reason": "experiment_cancel",
        "native_status_after": "drained", "inflight_operations_may_finish_after_request": True, "task_continuation_verified": False}
    native_api = {**common, "returned": None, "drain_requested": True, "drain_reason": "experiment_cancel"}
    native = {"scope": scope, "run_id": scope["variant"], "session_id": "main",
        "drain_requested": True, "drain_reason": "experiment_cancel"}
    return task, [request, api, observation, native_api, native]


def test_cancel_identity_metadata_is_only_one_guard_not_native_confirmation():
    task, chain = cancel_identity_fixture()
    m.validate_cancel_identity(task, *chain)
    # 此辅助函数只接纳一致的元数据；cancel_native 仍需实际原始
    # 文件引用、活动流、SQLite 和审计证据。


@pytest.mark.parametrize("index,field,value", [(3, "request_id", "different-request"),
    (0, "chain_id", "different-chain"), (1, "session_id", "other-session"),
    (2, "attempt_id", "worker-b"), (3, "run_id", "different-run"),
    (1, "api_executed", False), (3, "drain_requested", False), (4, "drain_requested", False),
    (4, "drain_reason", "experiment_pause")])
def test_cancel_rehashed_wrong_request_identity_or_explicit_false_drain_cannot_pass(index, field, value):
    task, chain = cancel_identity_fixture()
    chain[index][field] = value
    with pytest.raises(ValueError, match="native_cancel_"):
        m.validate_cancel_identity(task, *chain)


def control_integrity_fixture(tmp_path):
    paths = SimpleNamespace(run=tmp_path / "run", fixture=tmp_path / "fixture", budget=tmp_path / "budget")
    for directory in (paths.run, paths.fixture, paths.budget):
        directory.mkdir()
    (paths.fixture / "scope.json").write_text("{}\n")
    (paths.fixture / "scope.token").write_text("local-scope-token\n")
    (paths.budget / "run.token").write_text("original-budget-token\n")
    token_hash = p.sha(b"original-budget-token")
    for attempt in ("worker-a", "worker-b"):
        target = paths.fixture / "attempts" / attempt
        target.mkdir(parents=True)
        p.create(target / "request.json", {"lifecycle": {"mode": "new" if attempt == "worker-a" else "resume"}})
        p.create(target / "lifecycle.json", {"attempt_id": attempt})
        p.create(paths.run / ("start-binding.json" if attempt == "worker-a" else "resume-binding.json"), {
            "attempt_id": attempt, "request_sha256": p.sha(p.regular(target / "request.json")),
            "binding_sha256": p.sha(p.regular(target / "lifecycle.json")),
            "scope_sha256": p.sha(p.regular(paths.fixture / "scope.json")),
            "scope_token_sha256": p.sha(b"local-scope-token"), "model_token_sha256": token_hash})
    paused = {"budget": {"token_sha256": token_hash, "cap": 20, "count": 1, "rows": [[1, "original-call"]]}}
    final = {"token_sha256": token_hash, "cap": 20, "rows": [[1, "original-call"], [2, "next-call"]]}
    return paths, paused, final


def test_original_model_token_cap_and_reservation_prefix_survive_replacement(tmp_path):
    paths, paused, final = control_integrity_fixture(tmp_path)
    m.validate_cumulative_budget(paths, paused, final)


@pytest.mark.parametrize("change", ["token", "cap", "rows", "replacement-request"])
def test_budget_reset_or_mutated_replacement_controls_fail(tmp_path, change):
    paths, paused, final = control_integrity_fixture(tmp_path)
    if change == "token":
        (paths.budget / "run.token").write_text("replacement-budget-token\n")
        final["token_sha256"] = p.sha(b"replacement-budget-token")
    elif change == "cap":
        final["cap"] = 21
    elif change == "rows":
        final["rows"][0] = [1, "new-reservation"]
    else:
        (paths.fixture / "attempts/worker-b/request.json").write_text('{"prompt":"do it again"}\n')
    with pytest.raises(ValueError, match="original_model_budget|protected_original_controls"):
        m.validate_cumulative_budget(paths, paused, final)


def reconnect_denial_fixture(tmp_path, *, connection="new-authenticated-a"):
    global_paths = SimpleNamespace(run=tmp_path)
    body = {"error": {"code": "scope_denied"}}
    raw = p.canonical(body)
    response = {"principal": "user-a", "task_id": "task-b", "operation": "state", "status": 403,
        "request_id": "actual-request", "connection_id": connection, "body": body,
        "raw_body_base64": base64.b64encode(raw).decode(), "response_bytes": len(raw), "body_sha256": p.sha(raw)}
    encoded_lines(tmp_path / "backend-main/control.jsonl", [
        {"kind": "request", "request_id": "actual-request", "principal_id": "user-a", "task_id": "task-b", "operation": "state"},
        {"kind": "authorization", "request_id": "actual-request", "decision": "deny", "reason": "scope_mismatch"},
        {"kind": "response", "request_id": "actual-request", "connection_id": connection, "status": 403, "body": body}])
    return global_paths, {"attempts": [{"before": {"actual_files": "stable"}, "after": {"actual_files": "stable"}, "response": response}]}


def test_reconnected_socket_itself_must_have_actual_foreign_denial(tmp_path):
    paths, probe = reconnect_denial_fixture(tmp_path)
    m.validate_foreign_probe(paths, "main", probe, "state", connection_id="new-authenticated-a")


def test_foreign_denial_from_before_reconnect_cannot_pass_new_connection_check(tmp_path):
    paths, probe = reconnect_denial_fixture(tmp_path, connection="old-disconnected-a")
    with pytest.raises(ValueError, match="foreign_denial_not_on_reauthenticated_connection"):
        m.validate_foreign_probe(paths, "main", probe, "state", connection_id="new-authenticated-a")


def test_renaming_response_connection_without_actual_server_journal_cannot_pass(tmp_path):
    paths, probe = reconnect_denial_fixture(tmp_path, connection="old-disconnected-a")
    probe["attempts"][0]["response"]["connection_id"] = "new-authenticated-a"
    with pytest.raises(ValueError, match="actual_product_response_source_missing"):
        m.validate_foreign_probe(paths, "main", probe, "state", connection_id="new-authenticated-a")


def test_baseline_task_state_and_budget_do_not_alias_full_I24_trial():
    config = load_config()
    baseline = SimpleNamespace(trial_id="same-id", date="2026-10-20", baseline=True)
    full = SimpleNamespace(trial_id="same-id", date="2026-10-20", baseline=False)
    left, right = paths_for(config, baseline, "main", "a"), paths_for(config, full, "main", "a")
    assert left.state != right.state and left.budget != right.budget and left.run != right.run
    _, global_paths, tasks = m.layout(baseline)
    assert global_paths.run != m.layout(full)[1].run
    assert all(task.scope["experiment_id"] == "multi-user-baseline" for task in tasks)


def baseline_control_fixture(tmp_path):
    scope = {"experiment_id": "multi-user-baseline", "variant": "baseline-main-a", "user_id": "user-a", "session_id": "session-a"}
    task = SimpleNamespace(task_id="task-a", owner="user-a", scope=scope, paths=SimpleNamespace(run=tmp_path / "task"))
    identity = {"pid": 123, "start_ticks": 456}
    rows = [{"kind": "worker_start_attempt", "task_id": "task-a", "attempt_id": "worker-a", "request_id": "fixed-start"},
        {"kind": "worker_start_acknowledged", "task_id": "task-a", "attempt_id": "worker-a", "request_id": "fixed-start", "process_identity": identity},
        {"kind": "request", "task_id": "task-a", "principal_id": "user-a", "operation": "state"}]
    private = [{"kind": "process_started", "task_id": "task-a", "attempt_id": "worker-a", "request_id": "fixed-start", "scope": scope, "identity": identity}]
    encoded_lines(tmp_path / "backend-main/control.jsonl", rows)
    encoded_lines(task.paths.run / "attempts/worker-a/private/worker-control.jsonl", private)
    return SimpleNamespace(run=tmp_path), task, rows, private


def test_baseline_requires_exact_A_start_and_only_read_requests(tmp_path):
    paths, task, _, _ = baseline_control_fixture(tmp_path)
    m.validate_baseline_control(paths, task)


@pytest.mark.parametrize("change", ["start-B", "cancel", "resume", "read-B", "pause-request", "wrong-child", "private-signal"])
def test_baseline_rejects_extra_task_or_native_control_activity(tmp_path, change):
    paths, task, rows, private = baseline_control_fixture(tmp_path)
    if change == "start-B":
        rows.append({"kind": "worker_start_attempt", "task_id": "task-b", "attempt_id": "worker-a"})
    elif change == "cancel":
        rows.append({"kind": "worker_control_dispatch_attempt", "task_id": "task-a", "action": "cancel"})
    elif change == "resume":
        rows.append({"kind": "replacement_resume_material_verified", "task_id": "task-a"})
    elif change == "read-B":
        rows[2]["task_id"] = "task-b"
    elif change == "pause-request":
        rows[2]["operation"] = "pause"
    elif change == "wrong-child":
        private[0]["identity"] = {"pid": 999, "start_ticks": 456}
    else:
        private.append({"kind": "cancel_dispatch_attempt"})
    encoded_lines(paths.run / "backend-main/control.jsonl", rows)
    encoded_lines(task.paths.run / "attempts/worker-a/private/worker-control.jsonl", private)
    with pytest.raises(ValueError):
        m.validate_baseline_control(paths, task)


def test_baseline_material_cannot_be_graded_as_full_I24(tmp_path):
    p.create(tmp_path / "plan.json", {"baseline_only": True})
    with pytest.raises(ValueError, match="single_task_baseline_cannot_be_graded_as_I24"):
        m.grade(SimpleNamespace(), SimpleNamespace(run=tmp_path), [])
    assert not (tmp_path / "scope-verdict.json").exists()


@pytest.mark.parametrize("arguments", [
    ["exercise", "--baseline"], ["bind-replacements", "--baseline"], ["grade", "--baseline"],
    ["bind-start", "--baseline", "--letter", "b"], ["bind-start", "--baseline", "--letter", "a", "--kind", "cancel"],
    ["baseline-exercise"], ["baseline-grade"]])
def test_baseline_CLI_rejects_B_control_and_mixed_verdict_modes(monkeypatch, arguments):
    monkeypatch.setattr(m, "environment", lambda: {})
    monkeypatch.setattr("sys.argv", ["runner.multi_user", *arguments, "--trial-id", "offline", "--date", "2026-10-20"])
    with pytest.raises(ValueError, match="baseline_"):
        m.main()


def baseline_allocation_fixture(tmp_path):
    config = load_config()
    paths = SimpleNamespace(state=Path("/data/state/baseline-a"), workspace=Path("/data/workspaces/baseline-a"),
        fixture=Path("/data/fixtures/baseline-a"), budget=Path("/data/budgets/baseline-a"), run=Path("/data/runs/baseline-a"))
    task = SimpleNamespace(task_id="task-a", scope={"experiment_id": "multi-user-baseline"}, paths=paths)
    def host_path(path):
        return str(config.host_data_root / path.relative_to("/data"))
    mappings = [("/state", paths.state, True), ("/workspace", paths.workspace, True),
        ("/evidence", paths.run / "attempts/worker-a/candidate", True),
        ("/worker-private", paths.run / "attempts/worker-a/private", True),
        ("/control/policy.json", paths.fixture / "attempts/worker-a/worker-policy.json", False),
        ("/control/worker.token", paths.fixture / "attempts/worker-a/worker.token", False),
        ("/config/request.json", paths.fixture / "attempts/worker-a/request.json", False),
        ("/config/lifecycle.json", paths.fixture / "attempts/worker-a/lifecycle.json", False),
        ("/config/scope.json", paths.fixture / "scope.json", False),
        ("/config/scope.token", paths.fixture / "scope.token", False),
        ("/config/model-run.token", paths.budget / "run.token", False)]
    worker = {"id": "a" * 64, "role": "worker", "trial_id": "offline", "image_id": "sha256:" + "b" * 64, "init_enabled": True,
        "memory_limit": 2 * 1024**3, "cpu_nano": 750000000, "state": {"Running": True},
        "networks": {"user-a-network": {}}, "environment_names": [],
        "mounts": [{"Destination": destination, "Source": host_path(path), "RW": rw} for destination, path, rw in mappings]}
    active = [worker]
    for digit, role in enumerate(("tools", "model-proxy", "fixtures", "backend"), 1):
        row = {"id": str(digit) * 64, "role": role, "trial_id": "offline", "image_id": "sha256:" + "c" * 64, "init_enabled": True,
            "state": {"Running": True}, "memory_limit": 256 * 1024**2, "cpu_nano": 250000000,
            "networks": {"user-a-network": {}}, "environment_names": [], "mounts": []}
        if role in {"tools", "model-proxy"}:
            row["mounts"] = [{"Destination": "/workspace" if role == "tools" else "/budget",
                "Source": host_path(paths.workspace if role == "tools" else paths.budget), "RW": True}]
        active.append(row)
    registered = deepcopy(worker)
    registered["state"] = {"Running": False, "Status": "created"}
    registry = {"registrations": {worker["id"]: registered}}
    terminal = deepcopy(worker)
    terminal["state"] = {"Status": "exited", "Running": False, "ExitCode": 143, "Pid": 0,
        "OOMKilled": False, "Dead": False, "Paused": False, "Restarting": False, "Error": ""}
    lifecycle = [{"before_stop": deepcopy(worker), "after_stop": terminal,
        "deleted": {"id": worker["id"], "id_absent": True}}]
    (tmp_path / "trusted").mkdir()
    p.create(tmp_path / "trusted/main-worker-a.json", {"task-a": worker})
    p.create(tmp_path / "main-active-containers.json", active)
    plan = {"baseline_only": True, "executed_tasks": ["task-a"], "pins": {"worker_image_id": worker["image_id"]}}
    return SimpleNamespace(run=tmp_path), task, plan, registry, lifecycle, active


def test_baseline_allocation_uses_same_exact_I24_entry_mounts_at_075CPU(tmp_path):
    paths, task, plan, registry, lifecycle, _ = baseline_allocation_fixture(tmp_path)
    m.validate_baseline_allocation(paths, task, plan, registry, lifecycle)


@pytest.mark.parametrize("change", ["old-1CPU", "B-model-budget", "extra-worker", "wrong-image", "kill", "missing-retirement", "no-init", "registered-init-changed", "terminal-init-changed"])
def test_baseline_old_entry_wrong_binding_or_unhealthy_wrapper_cannot_pass(tmp_path, change):
    paths, task, plan, registry, lifecycle, active = baseline_allocation_fixture(tmp_path)
    if change == "old-1CPU":
        active[0]["cpu_nano"] = 1000000000
        (paths.run / "main-active-containers.json").write_bytes(p.canonical(active))
    elif change == "B-model-budget":
        next(item for item in registry["registrations"]["a" * 64]["mounts"]
            if item["Destination"] == "/config/model-run.token")["Source"] = "/different-user/run.token"
    elif change == "extra-worker":
        registry["registrations"]["e" * 64] = deepcopy(registry["registrations"]["a" * 64])
    elif change == "wrong-image":
        plan["pins"]["worker_image_id"] = "sha256:" + "e" * 64
    elif change == "kill":
        lifecycle[0]["after_stop"]["state"]["ExitCode"] = 137
    elif change == "no-init":
        active[1]["init_enabled"] = False
        (paths.run / "main-active-containers.json").write_bytes(p.canonical(active))
    elif change == "registered-init-changed":
        registry["registrations"]["a" * 64]["init_enabled"] = False
    elif change == "terminal-init-changed":
        lifecycle[0]["after_stop"]["init_enabled"] = False
    else:
        lifecycle.clear()
    with pytest.raises(ValueError):
        m.validate_baseline_allocation(paths, task, plan, registry, lifecycle)


def baseline_native_binding_fixture(tmp_path, monkeypatch):
    """匹配真实产物布局，来源信息存于 entrypoint.json。"""
    paths = SimpleNamespace(run=tmp_path / "run", fixture=tmp_path / "fixture", budget=tmp_path / "budget")
    scope = {"experiment_id": "multi-user-baseline", "variant": "baseline-main-a",
        "user_id": "user-a", "session_id": "session-a"}
    task = SimpleNamespace(task_id="task-a", scope=scope, paths=paths)
    helper = Path(__file__).resolve().parents[1] / "adapters/deepagents_langgraph/pause_recovery.py"
    monkeypatch.setattr(p, "HELPER", helper)
    pins = {"candidate": m.CANDIDATE, "chain_id": scope["variant"], "model_name": "offline-model",
        "source_export_sha256": "a" * 64, "adapter_export_sha256": "b" * 64,
        "adapter_helper_sha256": p.sha(helper.read_bytes())}
    original = {"run_id": scope["variant"], "session_id": scope["session_id"], "prompt": "offline calibration input",
        "timeout_seconds": 300, "max_output_tokens": 4096, "max_retries": 2,
        "candidate_options": {"case": "tools", "trusted_scope_file": "/config/scope.json"}}
    tools = [{"name": "read", "description": "offline schema", "input_schema": {"type": "object"}}]
    target, root = paths.fixture / "attempts/worker-a", paths.run / "attempts/worker-a/candidate"
    for directory in (target, root, paths.budget, paths.run / "trusted"):
        directory.mkdir(parents=True, exist_ok=True)
    p.create(paths.fixture / "candidate-request.json", original)
    p.create(paths.fixture / "scope.json", scope)
    (paths.fixture / "scope.token").write_text("offline-scope-token\n")
    (paths.budget / "run.token").write_text("offline-budget-token\n")
    p.create(paths.run / "chain.json", {"original_request_sha256": p.sha(p.regular(paths.fixture / "candidate-request.json"))})
    p.create(paths.run / "trusted/http-tool-surface.json", {"tools": tools})
    p.create(paths.run / "start-binding.json", p.bind(paths, scope, pins, "worker-a", used=0))
    runtime = lifecycle_identity.runtime_identity(model_name=pins["model_name"],
        max_output_tokens=4096, max_retries=2, candidate_options=original["candidate_options"], tool_definitions=tools)
    p.create(root / "lifecycle-runtime.json", runtime)
    p.create(root / "entrypoint.json", {"run_id": scope["variant"], "session_id": scope["session_id"], "scope": scope,
        "provenance": {key: pins[key] for key in ("source_export_sha256", "adapter_export_sha256")},
        "lifecycle": p.read(target / "request.json")["lifecycle"], "factory": "deepagents.create_deep_agent",
        "checkpointer": "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver", "durability": "sync", "model": pins["model_name"]})
    result = {"run_id": scope["variant"], "session_id": scope["session_id"], "scope": scope, "elapsed_seconds": 32.176}
    return task, pins, result, target, root


def test_baseline_uses_actual_entrypoint_bytes_and_protected_binding_without_invented_result_provenance(tmp_path, monkeypatch):
    task, pins, result, target, root = baseline_native_binding_fixture(tmp_path, monkeypatch)
    assert "provenance" not in result
    observed = m.validate_baseline_native_binding(task, pins, result)
    assert observed["entrypoint_source"] == {"path": "entrypoint.json", "sha256": p.sha((root / "entrypoint.json").read_bytes())}
    assert observed["binding_sha256"] == p.sha((target / "lifecycle.json").read_bytes())


@pytest.mark.parametrize("change", ["result-scope", "result-run", "result-session", "active-budget", "entrypoint-materials",
    "entrypoint-scope", "entrypoint-mode", "entrypoint-model", "runtime", "trusted-tools", "protected-binding", "request", "original-request"])
def test_baseline_rejects_wrong_actual_source_or_original_controls_even_with_claimed_result_provenance(tmp_path, monkeypatch, change):
    task, pins, result, target, root = baseline_native_binding_fixture(tmp_path, monkeypatch)
    result["provenance"] = {key: pins[key] for key in ("source_export_sha256", "adapter_export_sha256")}
    def rewrite(path, action):
        value = p.read(path)
        action(value)
        path.write_bytes(p.canonical(value) + b"\n")
    if change == "result-scope":
        result["scope"] = {**task.scope, "user_id": "user-b"}
    elif change == "result-run":
        result["run_id"] = "other-task"
    elif change == "result-session":
        result["session_id"] = "session-b"
    elif change == "active-budget":
        result["elapsed_seconds"] = 301
    elif change == "entrypoint-materials":
        rewrite(root / "entrypoint.json", lambda value: value["provenance"].update(adapter_export_sha256="c" * 64))
    elif change == "entrypoint-scope":
        rewrite(root / "entrypoint.json", lambda value: value.update(scope={**task.scope, "user_id": "user-b"}))
    elif change == "entrypoint-mode":
        rewrite(root / "entrypoint.json", lambda value: value["lifecycle"].update(mode="resume"))
    elif change == "entrypoint-model":
        rewrite(root / "entrypoint.json", lambda value: value.update(model="other-model"))
    elif change == "runtime":
        rewrite(root / "lifecycle-runtime.json", lambda value: value["model"].update(name="other-model"))
    elif change == "trusted-tools":
        rewrite(task.paths.run / "trusted/http-tool-surface.json", lambda value: value["tools"][0].update(description="changed"))
    elif change == "protected-binding":
        rewrite(target / "lifecycle.json", lambda value: value.update(mode="resume"))
    elif change == "request":
        rewrite(target / "request.json", lambda value: value.update(prompt="replacement input"))
    else:
        rewrite(task.paths.fixture / "candidate-request.json", lambda value: value.update(prompt="replacement input"))
    with pytest.raises(ValueError):
        m.validate_baseline_native_binding(task, pins, result)


@pytest.mark.parametrize("change", ["mode", "budget", "recovery"])
def test_baseline_rejects_consistently_hashed_but_wrong_new_attempt_binding(tmp_path, monkeypatch, change):
    task, pins, result, target, _ = baseline_native_binding_fixture(tmp_path, monkeypatch)
    binding = p.read(target / "lifecycle.json")
    if change == "mode":
        binding["mode"] = "resume"
    elif change == "budget":
        binding["budget"]["used_active_seconds"] = 1
    else:
        binding["recovery"] = {"attempt_id": "worker-a"}
    (target / "lifecycle.json").write_bytes(p.canonical(binding) + b"\n")
    integrity = p.read(task.paths.run / "start-binding.json")
    integrity["binding_sha256"] = p.sha((target / "lifecycle.json").read_bytes())
    (task.paths.run / "start-binding.json").write_bytes(p.canonical(integrity) + b"\n")
    with pytest.raises(ValueError, match="baseline_original_native_"):
        m.validate_baseline_native_binding(task, pins, result)
