"""不运行候选的边界校准；所有子进程均为保留对象替身。

不能证明原生暂停、取消、双真实任务、资源节省、进程或容器复用、
Docker 隔离或生产鉴权。HTTP 子测试通过实际本地请求访问同一处理器。
"""
from copy import deepcopy
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import threading

import pytest

from runner.shared_backend import ProductBackend, ProductHTTPServer, validate_journal_location
from runner.shared_supervisor import BoundaryError, TaskSupervisor


class FakeChild:
    def __init__(self, pid):
        self.pid, self.returncode, self.signals = pid, None, []

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        if self.returncode is not None:
            raise ProcessLookupError()
        self.signals.append(value)


@pytest.fixture
def lab(tmp_path):
    root = tmp_path.resolve()
    tasks = []
    for number in range(3):
        owner = "user-b" if number == 1 else "user-a"
        task_id = ["task-a", "task-b", "task-a-extra"][number]
        task_root = root / task_id
        for part in ["state", "workspace"]:
            (task_root / part).mkdir(parents=True)
        attempts = []
        for attempt_id in ["worker-a", "worker-b"]:
            config, evidence = task_root / "config" / attempt_id, task_root / "evidence" / attempt_id
            config.mkdir(parents=True); evidence.mkdir(parents=True)
            (config / "scope.token").write_text("fabricated-tool-token-" + task_id)
            attempts.append({"attempt_id": attempt_id, "candidate": "deepseek_harness" if number != 1 else "deepagents_langgraph",
                "argv": ["/fixed-image/node", "/fixed-image/goal-entry.mjs", "--request", str(config / "request.json")],
                "environment": {"MODEL_RUN_TOKEN_FILE": str(config / "model.token"), "TASK_TEST_MARKER": task_id},
                "config_dir": str(config), "evidence_dir": str(evidence),
                "events_file": "raw-events.jsonl", "readiness_file": "control-events.jsonl",
                "ready_match": {"phase": "pause_handler_installed", "signal": "SIGUSR2"},
                "native_ids": {"session_id": "main"}})
        tasks.append({"task_id": task_id, "owner": owner,
            "scope": {"experiment_id": "offline-scope", "variant": task_id, "user_id": owner, "session_id": "main"},
            "state_dir": str(task_root / "state"), "workspace": str(task_root / "workspace"),
            "artifacts": ["result.txt"], "attempts": attempts})
    audit, calls, children = [], [], []
    identities = {}

    def spawn(argv, **kwargs):
        child = FakeChild(3000 + len(children))
        identities[child.pid] = {"pid": child.pid, "start_ticks": 100 + child.pid, "source": "synthetic_adapter_calibration"}
        children.append(child); calls.append((argv, kwargs))
        return child

    supervisor = TaskSupervisor(tasks, audit.append, popen_factory=spawn, identity_reader=lambda pid: dict(identities[pid]))
    tokens = {"user-a": "fabricated-api-user-a", "user-b": "fabricated-api-user-b"}
    credentials = [{"principal_id": owner, "credential_id": owner + "-credential", "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for owner, token in tokens.items()]
    backend = ProductBackend(supervisor, credentials, audit.append)

    def start(task_id, attempt="worker-a", ready=True):
        supervisor.start(task_id, attempt)
        task = supervisor.tasks[task_id]
        owned = supervisor.owned[(task_id, attempt)]
        marker = {"phase": "pause_handler_installed", "signal": "SIGUSR2", "scope": task.scope,
                  "chain_id": task.scope["variant"], "attempt_id": attempt}
        if ready:
            (owned.spec.evidence_dir / owned.spec.readiness_file).write_text(json.dumps(marker) + "\n")
        native = {"method": "session.event", "params": {"sessionId": "main", "event": {"seq": 0, "type": "user/message", "data": {"offline_sentinel": task_id}}}}
        (owned.spec.evidence_dir / owned.spec.events_file).write_text(json.dumps(native) + "\n")
        (task.workspace / "result.txt").write_text("actual-offline-file-" + task_id)
        return owned

    def request(task_id, operation, *, owner="user-a", **kwargs):
        method = kwargs.pop("method", "GET" if operation in {"state", "events", "artifacts"} else "POST")
        return backend.dispatch("Bearer " + tokens[owner], task_id, operation, method=method, **kwargs)

    return {"root": root, "tasks": tasks, "supervisor": supervisor, "backend": backend, "audit": audit,
            "calls": calls, "children": children, "identities": identities, "tokens": tokens,
            "start": start, "request": request, "credentials": credentials}


@pytest.mark.parametrize("operation", ["state", "events", "artifacts", "pause", "resume", "cancel"])
def test_foreign_user_is_rejected_before_any_state_io_or_control(lab, operation, monkeypatch):
    b = lab["start"]("task-b")
    original_files = {path: path.read_bytes() for path in lab["root"].rglob("*") if path.is_file()}
    forbidden = lambda *a, **k: pytest.fail("他人请求到达 supervisor 操作")
    for name in ["state", "events", "artifacts", "cancel"]:
        monkeypatch.setattr(lab["supervisor"], name, forbidden)
    # 外部作用域应先于无效请求体、游标、错误方法以及
    # 合法任务运行、暂停或控制不适用状态而被拒绝。
    status, payload = lab["request"]("task-b", operation, method="POST", body={"owner": "user-b", "pid": b.process.pid, "path": "/other", "secret": "do-not-audit"}, query={"after": ["bad"]})
    assert (status, payload) == (403, {"error": {"code": "scope_denied"}})
    assert b.process.signals == []
    assert original_files == {path: path.read_bytes() for path in lab["root"].rglob("*") if path.is_file()}
    assert "do-not-audit" not in json.dumps(lab["audit"])


@pytest.mark.parametrize("owner,task", [("user-a", "task-a"), ("user-b", "task-b")])
def test_both_owners_can_query_actual_process_stream_and_file(lab, owner, task):
    owned = lab["start"](task)
    status, state = lab["request"](task, "state", owner=owner)
    assert status == 200 and state["process_status"] == "running"
    assert state["process_identity"]["pid"] == owned.process.pid
    assert state["native_state_complete"] is False
    assert state["native_ids_source"].endswith("requires_separate_native_confirmation")
    status, events = lab["request"](task, "events", owner=owner)
    assert status == 200 and len(events["records"]) == 1
    assert events["records"][0]["record"]["params"]["event"]["data"]["offline_sentinel"] == task
    assert "scope" not in events["records"][0]["record"]  # 原生响应封装保持不变。
    status, files = lab["request"](task, "artifacts", owner=owner)
    data = base64.b64decode(files["items"][0]["content_base64"])
    assert status == 200 and data == ("actual-offline-file-" + task).encode()
    assert files["items"][0]["sha256"] == hashlib.sha256(data).hexdigest()


def test_cancel_hits_exact_retained_object_and_does_not_claim_native_success(lab):
    a, b = lab["start"]("task-a"), lab["start"]("task-b")
    status, payload = lab["request"]("task-a", "cancel", request_id="trusted-correlation-a")
    assert status == 202 and a.process.signals == [signal.SIGUSR1] and b.process.signals == []
    assert payload["native_cancel_confirmed"] is False
    assert payload["status"] == "signal_dispatch_attempted"
    assert payload["signal_delivery_confirmed"] is False
    assert [row for row in lab["audit"] if row["kind"] == "signal_dispatch_attempted"][0]["request_id"] == "trusted-correlation-a"
    assert lab["request"]("task-a", "cancel")[0] == 409
    assert a.process.signals == [signal.SIGUSR1]


def test_popen_style_silent_exit_during_send_is_never_logged_as_delivery(lab):
    a = lab["start"]("task-a")

    def exited_during_send(value):
        # POSIX Popen.send_signal 可能在内部轮询中发现子进程已退出，
        # 直接返回，不执行 os.kill，也不抛出错误。
        a.process.returncode = 0
        return None

    a.process.send_signal = exited_during_send
    status, payload = lab["request"]("task-a", "cancel")
    assert status == 202 and a.process.signals == []
    assert payload["status"] == "signal_dispatch_attempted"
    assert payload["signal_delivery_confirmed"] is False
    assert payload["native_cancel_confirmed"] is False
    assert not any(row["kind"] == "control_delivered" for row in lab["audit"])
    records = [row for row in lab["audit"] if row["kind"] == "signal_dispatch_attempted"]
    assert len(records) == 1 and records[0]["signal_delivery_confirmed"] is False


@pytest.mark.parametrize("bad", ["missing", "wrong scope", "wrong attempt", "wrong chain", "wrong phase"])
def test_live_child_cannot_receive_native_signal_until_its_handler_marker_is_observed(lab, bad):
    a = lab["start"]("task-a", ready=False)
    if bad != "missing":
        marker = {"phase": "pause_handler_installed", "signal": "SIGUSR2", "scope": dict(lab["supervisor"].tasks["task-a"].scope), "chain_id": "task-a", "attempt_id": "worker-a"}
        if bad == "wrong scope": marker["scope"]["user_id"] = "user-b"
        if bad == "wrong attempt": marker["attempt_id"] = "worker-b"
        if bad == "wrong chain": marker["chain_id"] = "task-b"
        if bad == "wrong phase": marker["phase"] = "requested"
        (a.spec.evidence_dir / a.spec.readiness_file).write_text(json.dumps(marker) + "\n")
    assert lab["request"]("task-a", "cancel") == (409, {"error": {"code": "native_handlers_not_observed"}})
    assert a.process.signals == []


def test_changed_process_identity_and_exited_process_refuse_cancel(lab):
    a = lab["start"]("task-a")
    lab["identities"][a.process.pid]["start_ticks"] += 1
    assert lab["request"]("task-a", "cancel")[1]["error"]["code"] == "process_identity_changed"
    assert a.process.signals == []
    a.process.returncode = 137
    assert lab["request"]("task-a", "cancel")[1]["error"]["code"] == "attempt_not_running"
    assert lab["request"]("task-a", "state")[1]["exit_code"] == 137


def test_attempt_reuse_keeps_old_object_and_rejects_stale_control(lab):
    a1 = lab["start"]("task-a")
    with pytest.raises(BoundaryError, match="task_already_active"):
        lab["supervisor"].start("task-a", "worker-b")
    a1.process.returncode = 0
    a2 = lab["start"]("task-a", "worker-b")
    assert a1.process is not a2.process and a1.identity != a2.identity
    with pytest.raises(BoundaryError, match="stale_attempt"):
        lab["supervisor"].cancel("task-a", "stale-control", attempt_id="worker-a")
    assert a1.process.signals == a2.process.signals == []
    assert lab["request"]("task-a", "cancel")[0] == 202
    assert a2.process.signals == [signal.SIGUSR1]
    with pytest.raises(BoundaryError, match="attempt_already_used"):
        lab["supervisor"].start("task-a", "worker-a")


def test_no_queue_third_active_task_is_rejected_and_env_is_task_local(lab, monkeypatch):
    monkeypatch.setenv("OTHER_USER_SECRET", "ambient-token-must-not-reach-child")
    lab["start"]("task-a"); lab["start"]("task-b")
    with pytest.raises(BoundaryError, match="active_task_limit") as failure:
        lab["supervisor"].start("task-a-extra", "worker-a")
    assert failure.value.status == 429 and len(lab["children"]) == 2
    for index, (argv, kwargs) in enumerate(lab["calls"]):
        assert kwargs["shell"] is False and kwargs["close_fds"] is True
        assert kwargs["env"]["TASK_TEST_MARKER"] == ["task-a", "task-b"][index]
        assert "OTHER_USER_SECRET" not in kwargs["env"]
        assert "MODEL_API_KEY" not in kwargs["env"]
        assert kwargs["cwd"] == str(lab["supervisor"].tasks[["task-a", "task-b"][index]].workspace)
    assert "ambient-token-must-not-reach-child" not in json.dumps(lab["audit"])


def test_default_supervisor_has_no_implicit_same_uid_process_launcher(lab):
    supervisor = TaskSupervisor(lab["tasks"], lab["audit"].append)
    with pytest.raises(BoundaryError, match="process_launcher_not_connected") as failure:
        supervisor.start("task-a", "worker-a")
    assert failure.value.status == 501
    assert supervisor.owned == {} and supervisor.current == {}
    assert all(not any(attempt.evidence_dir.iterdir())
               for task in supervisor.tasks.values() for attempt in task.attempts.values())


@pytest.mark.parametrize("field", ["owner", "pid", "path", "state_dir", "worker_id", "attempt_id", "tools_url", "token"])
def test_owner_cannot_replace_trusted_routing_fields_in_request(lab, field):
    a = lab["start"]("task-a")
    assert lab["request"]("task-a", "cancel", body={field: "foreign"}) == (400, {"error": {"code": "request_fields_forbidden"}})
    assert a.process.signals == []


def test_authentication_not_found_and_scope_denial_are_distinct_and_no_credentials_are_logged(lab):
    backend = lab["backend"]
    assert backend.dispatch("Bearer wrong", "task-b", "state")[0] == 401
    assert backend.dispatch(None, "task-a", "state")[0] == 401
    assert lab["request"]("missing-task", "state")[0] == 404
    assert lab["request"]("task-b", "state")[0] == 403
    encoded = json.dumps(lab["audit"])
    assert all(token not in encoded for token in lab["tokens"].values())


@pytest.mark.parametrize("operation", ["pause", "resume"])
def test_unconnected_positive_controls_never_manufacture_success(lab, operation):
    lab["start"]("task-a")
    assert lab["request"]("task-a", operation) == (501, {"error": {"code": "control_not_connected"}})


def test_log_cursor_excludes_partial_tail_preserves_original_and_rejects_bad_cursor(lab):
    a = lab["start"]("task-a")
    path = a.spec.evidence_dir / a.spec.events_file
    original = path.read_bytes()
    with path.open("ab") as stream: stream.write(b'{"partial":')
    status, payload = lab["request"]("task-a", "events", query={"after": ["0"], "limit": ["1"]})
    assert status == 200 and payload["observed_cursor"] == 1 and payload["sample"]["sampled_bytes"] == len(original)
    assert payload["sample"]["sampled_sha256"] == hashlib.sha256(original).hexdigest()
    assert lab["request"]("task-a", "events", query={"after": ["2"]})[0] == 400
    assert lab["request"]("task-a", "events", query={"path": ["/foreign"]})[0] == 400
    assert lab["request"]("task-a", "events", query={"after": ["0", "1"]})[0] == 400


@pytest.mark.parametrize("kind", ["symlink file", "symlink parent", "hardlink", "oversized", "directory"])
def test_actual_artifact_reader_refuses_escape_and_nonregular_or_unbounded_data(lab, kind):
    lab["start"]("task-a"); lab["start"]("task-b")
    a, b = lab["supervisor"].tasks["task-a"], lab["supervisor"].tasks["task-b"]
    path = a.workspace / "result.txt"
    path.unlink()
    if kind == "symlink file": path.symlink_to(b.workspace / "result.txt")
    if kind == "hardlink": os.link(b.workspace / "result.txt", path)
    if kind == "oversized": path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    if kind == "directory": path.mkdir()
    if kind == "symlink parent":
        a.workspace.rename(a.workspace.with_name("renamed-workspace"))
        a.workspace.symlink_to(b.workspace, target_is_directory=True)
    status, payload = lab["request"]("task-a", "artifacts")
    assert status == 409
    assert "actual-offline-file-task-b" not in json.dumps(payload)


def test_config_workspace_evidence_overlap_and_stale_readiness_fail_fixture_or_start(lab):
    bad = deepcopy(lab["tasks"])
    bad[1]["workspace"] = bad[0]["workspace"]
    with pytest.raises(BoundaryError): TaskSupervisor(bad, lambda row: None)
    bad = deepcopy(lab["tasks"])
    bad[0]["attempts"][0]["config_dir"] = bad[1]["attempts"][0]["evidence_dir"]
    with pytest.raises(BoundaryError): TaskSupervisor(bad, lambda row: None)
    spec = lab["supervisor"].tasks["task-a"].attempts["worker-a"]
    (spec.evidence_dir / spec.readiness_file).write_text("stale-marker\n")
    with pytest.raises(BoundaryError, match="attempt_evidence_not_empty"):
        lab["supervisor"].start("task-a", "worker-a")
    assert lab["children"] == []


def test_global_journal_is_rejected_inside_every_attempt_config(lab):
    for task in lab["supervisor"].tasks.values():
        for attempt in task.attempts.values():
            for root in [task.workspace, task.state_dir, attempt.evidence_dir, attempt.config_dir]:
                with pytest.raises(BoundaryError, match="audit_inside_candidate_tree"):
                    validate_journal_location(lab["supervisor"], root / "backend.jsonl")
    validate_journal_location(lab["supervisor"], lab["root"] / "private" / "backend.jsonl")


@pytest.fixture
def http_backend(lab):
    server = ProductHTTPServer(("127.0.0.1", 0), lab["backend"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_real_http_denies_foreign_before_reading_malformed_body_and_closes_connection(lab, http_backend):
    lab["start"]("task-b")
    connection = http.client.HTTPConnection(*http_backend, timeout=2)
    try:
        connection.request("POST", "/tasks/task-b/cancel", body=b"not-json", headers={"Authorization": "Bearer " + lab["tokens"]["user-a"], "X-Request-ID": "client-cannot-set-correlation"})
        response = connection.getresponse()
        assert response.status == 403 and json.loads(response.read()) == {"error": {"code": "scope_denied"}}
        assert response.getheader("X-Request-ID") != "client-cannot-set-correlation"
        assert response.getheader("Connection") == "close"
    finally:
        connection.close()
    assert lab["children"][0].signals == []


def test_real_http_owner_cancel_is_attempt_and_delivery_and_native_terminal_are_unverified(lab, http_backend):
    a = lab["start"]("task-a")
    connection = http.client.HTTPConnection(*http_backend, timeout=2)
    try:
        connection.request("POST", "/tasks/task-a/cancel", body=b"{}", headers={"Authorization": "Bearer " + lab["tokens"]["user-a"]})
        response = connection.getresponse(); payload = json.loads(response.read())
        assert response.status == 202 and payload["native_cancel_confirmed"] is False
        assert payload["status"] == "signal_dispatch_attempted" and payload["signal_delivery_confirmed"] is False
        assert response.getheader("X-Request-ID") == payload["request_id"]
    finally:
        connection.close()
    assert a.process.signals == [signal.SIGUSR1]
