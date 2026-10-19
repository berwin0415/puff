from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.adapter import Event, RunState, TaskSpec
from runner import LifecycleError, Runner, SQLiteEventStore


TASK = {
    "task_id": "contract-t1",
    "tenant_scope": "local-tenant",
    "session_scope": "local-session",
    "prompt": "Return the fixture value",
    "tools": [{"name": "fixture.lookup"}],
    "checkpoint_policy": {"enabled": True},
}


def make_runner(tmp_path: Path) -> Runner:
    return Runner(SQLiteEventStore(tmp_path / "state.db", tmp_path / "artifacts"))


def test_task_and_event_round_trip() -> None:
    task = TaskSpec.from_mapping(TASK)
    assert task.to_mapping()["task_id"] == TASK["task_id"]
    event = Event(run_id="run-1", event_seq=1, event_type="run_started", state="running")
    assert Event.from_mapping(event.to_mapping()) == event


def test_runner_lifecycle_and_reconnect(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)
    run_id = runner.submit(TASK)
    assert runner.status(run_id)["state"] == RunState.RUNNING.value

    checkpoint_id = runner.pause(run_id)
    assert runner.status(run_id)["state"] == RunState.PAUSED.value
    assert runner.status(run_id)["checkpoint_id"] == checkpoint_id

    cursor, events = runner.stream_events(run_id)
    assert cursor == len(events)
    assert [event["event_seq"] for event in events] == list(range(1, cursor + 1))
    resumed_cursor, resumed_events = runner.stream_events(run_id, after_seq=2)
    assert resumed_cursor == cursor
    assert [event["event_seq"] for event in resumed_events] == list(range(3, cursor + 1))

    runner.resume(run_id)
    runner.complete(run_id, {"answer": "fixture"})
    result = runner.result(run_id)
    assert result["state"] == RunState.COMPLETED.value
    assert result["result"] == {"answer": "fixture"}

    run_dir = tmp_path / "artifacts" / "runs" / run_id
    assert json.loads((run_dir / "manifest.json").read_text())["run_id"] == run_id
    assert len((run_dir / "events.jsonl").read_text().splitlines()) == cursor + 2


def test_invalid_terminal_operation_is_rejected(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)
    run_id = runner.submit(TASK)
    runner.cancel(run_id)
    with pytest.raises(LifecycleError):
        runner.resume(run_id)


def test_candidate_run_id_is_reused_after_runner_recreation(tmp_path: Path) -> None:
    class FakeAdapter:
        name = "fake"
        version = "test"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def submit(self, _task):
            return "candidate-run-1"

        def pause(self, run_id):
            self.calls.append(("pause", run_id))
            return "candidate-checkpoint-1"

        def resume(self, run_id, _checkpoint_id):
            self.calls.append(("resume", run_id))

    adapter = FakeAdapter()
    db = tmp_path / "state.db"
    artifacts = tmp_path / "artifacts"
    runner = Runner(SQLiteEventStore(db, artifacts), adapter=adapter)
    run_id = runner.submit(TASK)
    checkpoint_id = runner.pause(run_id)

    # A new Runner instance must recover the candidate identity from events.
    resumed = Runner(SQLiteEventStore(db, artifacts), adapter=adapter)
    resumed.resume(run_id, checkpoint_id)
    assert adapter.calls == [("pause", "candidate-run-1"), ("resume", "candidate-run-1")]
