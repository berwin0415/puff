"""Unrun boundary tests: fake Worker HTTP, real files/product HTTP, no candidate.

The fake upstream deliberately proves no native capability or resource result.
Read-only mount enforcement is replaced only in these unit calibrations.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

from execution import control_backend as control
from runner.shared_supervisor import BoundaryError


class Journal:
    def __init__(self, path):
        self.path, self.rows = path, []

    def __call__(self, row):
        self.rows.append(row)


class Reply:
    def __init__(self, value, status):
        self.value, self.status = value, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, bound):
        return json.dumps(self.value).encode()[:bound]


class FakeTransport:
    def __init__(self, expected):
        self.expected, self.calls, self.offline = expected, [], set()
        self.fail_start, self.wrong_task = False, False
        self.exited = set()

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        action = url.path.rsplit("/", 1)[1]
        self.calls.append({"url": request.full_url, "method": request.method, "body": request.data})
        if url.hostname in self.offline or action == "start" and self.fail_start:
            raise URLError("synthetic transport loss, no native claim")
        value = dict(self.expected[url.hostname])
        if self.wrong_task:
            value["task_id"] = "other-task"
        if action in {"start", "state"}:
            exited = url.hostname in self.exited
            value.update(process_status="exited" if exited else "running", exit_code=0 if exited else None,
                process_identity={"pid": 777, "start_ticks": 123, "source": "fake_transport_calibration"})
        if request.data:
            value.update(request_id=json.loads(request.data)["request_id"])
            if action == "start":
                value.update(status="child_started", native_resume_completed=False)
            else:
                value.update(status="signal_dispatch_attempted", signal_delivery_confirmed=False,
                    native_pause_confirmed=False, native_cancel_confirmed=False,
                    semantics="cooperative_drain_at_superstep_boundary", immediate_cancel=False)
        return Reply(value, 202 if request.data else 200)


@pytest.fixture
def container_lab(tmp_path, monkeypatch):
    # Actual runtime uses immutable_bytes/statvfs. This fake-transport fixture
    # uses temporary local files and never claims a Docker/mount verification.
    monkeypatch.setattr(control, "immutable_bytes", lambda path, **kwargs: Path(path).read_bytes())
    monkeypatch.setattr(control, "read_secret", lambda path: Path(path).read_text().strip())
    tasks, routes, expected = [], [], {}
    for letter in ("a", "b"):
        task_id, owner = "task-" + letter, "user-" + letter
        root = tmp_path / task_id
        (root / "state").mkdir(parents=True)
        (root / "workspace").mkdir()
        scope = {"experiment_id": "offline", "variant": task_id, "user_id": owner, "session_id": "main"}
        attempts = []
        for attempt in ("worker-a", "worker-b"):
            config, evidence = root / "config" / attempt, root / "evidence" / attempt
            config.mkdir(parents=True); evidence.mkdir(parents=True)
            hostname = "worker-" + task_id + "-" + attempt
            worker_id = task_id + "-" + attempt
            policy = {"schema": "harness-lab-worker-control/1", "worker_id": worker_id,
                "mode": "new" if attempt == "worker-a" else "resume",
                "task": {"task_id": task_id, "owner": owner, "scope": scope, "attempts": [{"attempt_id": attempt}]}}
            raw = json.dumps(policy).encode()
            (config / "worker-policy.json").write_bytes(raw)
            (config / "worker.token").write_text("fabricated-private-" + worker_id)
            attempts.append({"attempt_id": attempt, "candidate": "deepagents_langgraph",
                "argv": ["/fixed-image/python", "/fixed-image/entrypoint.py"], "environment": {},
                "config_dir": str(config), "evidence_dir": str(evidence), "events_file": "raw-events.jsonl",
                "readiness_file": "control-events.jsonl", "ready_match": {"phase": "pause_handler_installed", "signal": "SIGUSR2"},
                "native_ids": {"session_id": "main-not-a-native-proof"}})
            routes.append({"task_id": task_id, "attempt_id": attempt, "worker_id": worker_id,
                "url": "http://" + hostname + ":8092", "policy_file": str(config / "worker-policy.json"),
                "token_file": str(config / "worker.token")})
            expected[hostname] = {"schema": "harness-lab-worker-description/1", "worker_id": worker_id,
                "policy_sha256": hashlib.sha256(raw).hexdigest(), "task_id": task_id, "scope": scope,
                "attempt_id": attempt, "mode": policy["mode"], "candidate": "deepagents_langgraph"}
        tasks.append({"task_id": task_id, "owner": owner, "scope": scope, "state_dir": str(root / "state"),
            "workspace": str(root / "workspace"), "artifacts": ["result.txt"], "attempts": attempts})
    journal = Journal(tmp_path / "private.jsonl")
    transport = FakeTransport(expected)
    registry = control.ContainerWorkerRegistry(tasks, routes, journal, opener=transport)
    credentials = [{"credential_id": owner + "-credential", "principal_id": owner,
        "token_sha256": hashlib.sha256(("fabricated-product-" + owner).encode()).hexdigest()} for owner in ("user-a", "user-b")]
    backend = control.ConnectedProductBackend(registry, credentials, journal)

    def call(task, action, *, owner="user-a", **kwargs):
        method = kwargs.pop("method", "GET" if action in {"state", "events", "artifacts"} else "POST")
        return backend.dispatch("Bearer fabricated-product-" + owner, task, action, method=method, **kwargs)

    return {"registry": registry, "backend": backend, "transport": transport, "journal": journal,
            "tasks": tasks, "routes": routes, "call": call}


@pytest.mark.parametrize("action", ["state", "events", "artifacts", "pause", "resume", "cancel"])
def test_foreign_request_never_reaches_worker_or_durable_material(container_lab, action, monkeypatch):
    lab = container_lab
    monkeypatch.setattr(control, "read_bound_file", lambda *args, **kwargs: pytest.fail("foreign durable read"))
    status, value = lab["call"]("task-b", action, method="POST", body={"worker": "task-a", "secret": "body-secret"}, query={"after": ["bad"]})
    assert (status, value) == (403, {"error": {"code": "scope_denied"}})
    assert lab["transport"].calls == []
    assert "body-secret" not in json.dumps(lab["journal"].rows)


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
@pytest.mark.parametrize("field", ["worker_url", "owner", "attempt_id", "path", "token", "prompt"])
def test_public_owner_cannot_replace_control_route_or_resume_input(container_lab, action, field):
    lab = container_lab
    assert lab["call"]("task-a", action, body={field: "untrusted"})[0] == 400
    assert lab["transport"].calls == []


def test_fixed_owner_routes_and_signal_ack_have_no_native_success_claim(container_lab):
    lab = container_lab
    for task in ("task-a", "task-b"):
        lab["registry"].start_task(task)
    lab["transport"].calls.clear()
    status, value = lab["call"]("task-a", "cancel")
    assert status == 202 and value["signal_delivery_confirmed"] is False
    assert value["native_cancel_confirmed"] is False and value["immediate_cancel"] is False
    assert value["semantics"] == "cooperative_drain_at_superstep_boundary"
    assert [row["url"] for row in lab["transport"].calls] == [
        "http://worker-task-a-worker-a:8092/control/describe", "http://worker-task-a-worker-a:8092/control/cancel"]
    assert lab["backend"].dispatch("Bearer fabricated-product-user-a", "task-a", "start", method="POST")[0] == 404


@pytest.mark.parametrize("registered,declared,accepted", [
    ("deepagents_langgraph", "omitted", True), ("deepagents_langgraph", "deepagents_langgraph", True),
    ("deepagents_langgraph", "deepseek_harness", False), ("deepagents_langgraph", "unknown-framework", False),
    ("deepagents_langgraph", None, False), ("deepseek_harness", "omitted", False),
    ("deepseek_harness", "deepseek_harness", True), ("deepseek_harness", "deepagents_langgraph", False),
    ("deepseek_harness", "unknown-framework", False), ("deepseek_harness", None, False),
])
def test_policy_candidate_legacy_da_default_never_selects_dsh(container_lab, registered, declared, accepted):
    lab = container_lab
    registry = lab["registry"]
    task = registry.tasks["task-a"]
    for attempt, spec in task.attempts.items():
        task.attempts[attempt] = replace(spec, candidate=registered)
    policy_path = Path(registry.routes[("task-a", "worker-a")]["policy_file"])
    policy = json.loads(policy_path.read_bytes())
    if declared != "omitted":
        policy["task"]["attempts"][0]["candidate"] = declared
    policy_path.write_text(json.dumps(policy))
    if accepted:
        _, expected, _ = registry._materials("task-a", "worker-a")
        assert expected["candidate"] == registered
    else:
        with pytest.raises(BoundaryError, match="worker_policy_candidate_mismatch"):
            registry._materials("task-a", "worker-a")
    assert lab["transport"].calls == []


def test_route_scope_and_worker_handshake_are_checked_before_start(container_lab):
    lab = container_lab
    lab["transport"].wrong_task = True
    with pytest.raises(BoundaryError, match="worker_response_identity_mismatch"):
        lab["registry"].start_task("task-a")
    assert all(not row["url"].endswith("/start") for row in lab["transport"].calls)
    assert lab["registry"].current == {}


def test_lost_start_ack_is_preserved_and_not_retried_automatically(container_lab):
    lab = container_lab
    lab["transport"].fail_start = True
    with pytest.raises(BoundaryError, match="worker_endpoint_unavailable"):
        lab["registry"].start_task("task-a")
    lab["transport"].fail_start = False
    with pytest.raises(BoundaryError, match="worker_start_already_attempted"):
        lab["registry"].start_task("task-a")
    assert len([row for row in lab["transport"].calls if row["url"].endswith("/start")]) == 1
    assert any(row["kind"] == "worker_start_result_unknown" for row in lab["journal"].rows)


def test_deleted_endpoint_reads_actual_persistent_material_without_cached_running_state(container_lab):
    lab = container_lab
    registry = lab["registry"]
    registry.start_task("task-a")
    task = registry.tasks["task-a"]
    spec = task.attempts["worker-a"]
    (task.state_dir / "pause-index.json").write_text(json.dumps({"actual_offline_sentinel": "belongs-to-a"}))
    (task.workspace / "result.txt").write_text("actual-persisted-a")
    (spec.evidence_dir / "raw-events.jsonl").write_text('{"actual_event":"a"}\n')
    lab["transport"].offline.add("worker-task-a-worker-a")
    status, state = lab["call"]("task-a", "state")
    assert status == 200
    assert state["process_observation"]["availability"] == "endpoint_unavailable"
    assert "process_status" not in state["process_observation"]
    row = state["durable_native_materials"][0]
    assert row["record"] == {"actual_offline_sentinel": "belongs-to-a"}
    assert row["reference"]["sha256"] == hashlib.sha256((task.state_dir / "pause-index.json").read_bytes()).hexdigest()
    assert lab["call"]("task-a", "events")[1]["records"][0]["record"] == {"actual_event": "a"}
    assert lab["call"]("task-a", "artifacts")[1]["items"][0]["status"] == "observed"


def test_attempt_event_cursor_can_still_read_original_after_replacement(container_lab):
    lab = container_lab
    registry = lab["registry"]
    registry.start_task("task-a")
    task = registry.tasks["task-a"]
    for attempt, text in (("worker-a", "original"), ("worker-b", "replacement")):
        (task.attempts[attempt].evidence_dir / "raw-events.jsonl").write_text(json.dumps({"event": text}) + "\n")
    # This selection calibrates only persistent cursor routing, never a resume.
    registry.current["task-a"] = "worker-b"
    status, events = lab["call"]("task-a", "events", query={"attempt": ["worker-a"], "after": ["0"]})
    assert status == 200 and events["cursor_domain"]["attempt_id"] == "worker-a"
    assert events["records"][0]["record"] == {"event": "original"}
    assert lab["call"]("task-a", "events")[1]["records"][0]["record"] == {"event": "replacement"}
    assert lab["call"]("task-a", "events", query={"attempt": ["../../other"]})[0] == 400


def test_resume_requires_actual_original_exit_and_rejects_prompt_before_replacement_start(container_lab):
    lab = container_lab
    registry = lab["registry"]
    registry.start_task("task-a")
    assert lab["call"]("task-a", "resume")[1]["error"]["code"] == "original_worker_clean_exit_not_observed"
    lab["transport"].exited.add("worker-task-a-worker-a")
    lab["call"]("task-a", "state")
    config = registry.tasks["task-a"].attempts["worker-b"].config_dir
    (config / "request.json").write_text(json.dumps({"prompt": "fake continuation"}))
    (config / "lifecycle.json").write_text("{}")
    assert lab["call"]("task-a", "resume")[1]["error"]["code"] == "original_prompt_free_resume_binding_required"
    assert not any("worker-b:8092/control/start" in row["url"] for row in lab["transport"].calls)


def test_actual_product_socket_eof_then_new_authenticated_connection_preserves_original_task(container_lab):
    lab = container_lab
    lab["registry"].start_task("task-a")
    server = control.ControlBackendServer(("127.0.0.1", 0), lab["backend"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    second = None
    try:
        headers = {"Authorization": "Bearer fabricated-product-user-a"}
        connection.request("GET", "/tasks/task-a/state", headers=headers)
        response = connection.getresponse()
        before = json.loads(response.read())
        old_id = response.getheader("X-Connection-ID")
        connection.close()
        deadline = time.monotonic() + 2
        while not any(row["kind"] == "connection_peer_eof" and row["connection_id"] == old_id for row in lab["journal"].rows):
            assert time.monotonic() < deadline, "Actual peer EOF was not observed"
            time.sleep(0.01)
        second = http.client.HTTPConnection(*server.server_address, timeout=2)
        second.request("GET", "/tasks/task-a/state", headers=headers)
        response = second.getresponse()
        after = json.loads(response.read())
        assert response.getheader("X-Connection-ID") != old_id
        assert before["task_id"] == after["task_id"] == "task-a"
        assert before["attempt_id"] == after["attempt_id"] == "worker-a"
        calls_before_foreign = len(lab["transport"].calls)
        second.request("POST", "/tasks/task-b/cancel", body=b"malformed", headers=headers)
        response = second.getresponse()
        assert response.status == 403 and json.loads(response.read()) == {"error": {"code": "scope_denied"}}
        assert len(lab["transport"].calls) == calls_before_foreign
    finally:
        connection.close()
        if second is not None:
            second.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)
