"""Fault-injection checks for runner restart and idempotent side effects."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib import error, request

from fixtures.service import create_server
from runner import Runner, SQLiteEventStore


TASK = {
    "task_id": "t5-fault-recovery",
    "tenant_scope": "local-tenant",
    "session_scope": "fault-session",
    "prompt": "Recover the fixture task after an injected fault.",
    "timeout_seconds": 60,
    "budget": {"usd": 0.1},
    "checkpoint_policy": {"enabled": True},
}


def _call(url: str, method: str = "GET", body: dict | None = None, key: str | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    if key:
        headers["Idempotency-Key"] = key
    req = request.Request(url, method=method, data=data, headers=headers)
    try:
        with request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _server(tmp_path: Path):
    server = create_server(state_file=tmp_path / "fixture-state.json")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread) -> None:
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def _runner(tmp_path: Path) -> Runner:
    return Runner(SQLiteEventStore(tmp_path / "state.db", tmp_path / "artifacts"))


def test_fail_once_retry_is_observable(tmp_path: Path) -> None:
    server, thread = _server(tmp_path)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        status, body = _call(base + "/api/fail-once", "POST", {"key": "t5-attempt"})
        assert status == 503 and body["error"] == "injected_failure"
        assert body["attempt"] == 1
        status, body = _call(base + "/api/fail-once", "POST", {"key": "t5-attempt"})
        assert status == 200 and body == {"status": "ok", "attempt": 2}
    finally:
        _stop(server, thread)


def test_side_effect_replay_survives_fixture_restart(tmp_path: Path) -> None:
    first, thread = _server(tmp_path)
    base = f"http://127.0.0.1:{first.server_port}"
    payload = {"operation": "charge", "payload": {"amount": 10}}
    status, created = _call(base + "/api/side-effects", "POST", payload, "t5-step-1")
    assert status == 201 and created["status"] == "created"
    _stop(first, thread)

    second, thread = _server(tmp_path)
    try:
        base = f"http://127.0.0.1:{second.server_port}"
        status, replayed = _call(base + "/api/side-effects", "POST", payload, "t5-step-1")
        assert status == 200 and replayed["status"] == "replayed"
        assert replayed["record"]["effect_id"] == created["record"]["effect_id"]
        status, effects = _call(base + "/api/side-effects")
        assert status == 200 and effects["count"] == 1
    finally:
        _stop(second, thread)


def test_runner_recreation_preserves_events_and_checkpoint(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    run_id = runner.submit(TASK)
    runner.send_input(run_id, "Keep the same run after the restart.")
    checkpoint_id = runner.pause(run_id)
    cursor_before, events_before = runner.stream_events(run_id)

    restarted = _runner(tmp_path)
    assert restarted.status(run_id)["state"] == "paused"
    restarted.resume(run_id, checkpoint_id)
    cursor, events = restarted.stream_events(run_id, after_seq=cursor_before)
    assert cursor > cursor_before
    assert [event["event_seq"] for event in events] == list(range(cursor_before + 1, cursor + 1))
    assert not ({event["event_id"] for event in events_before} & {event["event_id"] for event in events})

    restarted.complete(run_id, {"recovered": True})
    result = restarted.result(run_id)
    assert result["state"] == "completed"
    assert result["result"] == {"recovered": True}


def test_repeated_submissions_create_independent_runs(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    first = runner.submit(TASK)
    second = runner.submit(TASK)
    assert first != second
    runner.pause(first)
    assert runner.status(first)["state"] == "paused"
    assert runner.status(second)["state"] == "running"
