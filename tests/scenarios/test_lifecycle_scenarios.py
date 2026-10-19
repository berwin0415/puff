"""T3-T6 scenario definitions and runner-level lifecycle checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.adapter import RunState
from runner import LifecycleError, Runner, SQLiteEventStore
from runner.scenarios import load_scenario, load_task_config, task_ids


EXPECTED_TASKS = [
    "t1-tools",
    "t2-browser",
    "t3-approval",
    "t4-pause-resume",
    "t5-fault-recovery",
    "t6-extension",
]


def _spec(task_id: str, prompt: str | None = None) -> dict:
    config = load_task_config(task_id)
    scenario = load_scenario(task_id)
    if prompt is None:
        prompt = next(
            (step["prompt"] for step in scenario["steps"] if step.get("prompt")),
            config["description"],
        )
    return {
        "task_id": config["task_id"],
        "tenant_scope": "local-tenant",
        "session_scope": f"scenario-{config['task_id']}",
        "prompt": prompt,
        "timeout_seconds": config["timeout_seconds"],
        "budget": {"usd": config["budget_usd"]},
        "checkpoint_policy": config.get("checkpoint", {"enabled": True}),
        "metadata": {"scenario_task_id": scenario["task_id"]},
    }


def _runner(tmp_path: Path) -> Runner:
    return Runner(SQLiteEventStore(tmp_path / "state.db", tmp_path / "artifacts"))


def _types(runner: Runner, run_id: str) -> list[str]:
    _cursor, events = runner.stream_events(run_id)
    return [event["event_type"] for event in events]


def test_t1_to_t6_configs_and_scenarios_are_complete() -> None:
    assert task_ids() == EXPECTED_TASKS
    for task_id in EXPECTED_TASKS:
        config = load_task_config(task_id)
        scenario = load_scenario(task_id)
        assert config["task_id"] == task_id
        assert config["timeout_seconds"] > 0
        assert config["budget_usd"] > 0
        assert scenario["config_task_id"] == task_id
        assert scenario["steps"]
        assert scenario["assertions"]


def test_t3_live_input_and_approval_share_the_run(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    run_id = runner.submit(_spec("t3-approval"))

    scenario = load_scenario("t3-approval")
    instruction = next(step["message"] for step in scenario["steps"] if step["action"] == "send_input")
    approval = next(step for step in scenario["steps"] if step["action"] == "approve")

    runner.send_input(run_id, instruction)
    runner.approve(run_id, approval["approval_id"], approval["decision"])

    _cursor, events = runner.stream_events(run_id)
    assert [event["run_id"] for event in events] == [run_id] * len(events)
    assert "input_received" in _types(runner, run_id)
    assert "approval_decided" in _types(runner, run_id)
    assert runner.status(run_id)["state"] == RunState.RUNNING.value
    assert [event["event_seq"] for event in events] == list(range(1, len(events) + 1))


def test_t4_pause_survives_runner_recreation_and_resumes(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    run_id = runner.submit(_spec("t4-pause-resume"))
    checkpoint_id = runner.pause(run_id)

    checkpoint = runner.storage.read_checkpoint(run_id, checkpoint_id)
    assert checkpoint["task"]["task_id"] == "t4-pause-resume"
    assert checkpoint["safe_point"] == "after_inflight_call"
    assert checkpoint["run_id"] == run_id

    # Simulate a Runner process restart over the same SQLite/artifact roots.
    restarted = _runner(tmp_path)
    assert restarted.status(run_id)["state"] == RunState.PAUSED.value
    restarted.resume(run_id, checkpoint_id)
    assert restarted.status(run_id)["state"] == RunState.RUNNING.value

    cursor, events = restarted.stream_events(run_id)
    assert cursor == len(events)
    assert [event["event_seq"] for event in events] == list(range(1, cursor + 1))
    assert len({event["event_id"] for event in events}) == cursor

    restarted.complete(run_id, {"answer": "resumed"})
    assert restarted.result(run_id)["state"] == RunState.COMPLETED.value


def test_t4_resume_requires_a_checkpoint(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    run_id = runner.submit(_spec("t4-pause-resume"))
    with pytest.raises(LifecycleError):
        runner.resume(run_id)


def test_t6_extension_config_is_measurable() -> None:
    config = load_task_config("t6-extension")
    scenario = load_scenario("t6-extension")
    assert set(config["measure"]) == {"changed_files", "first_run_seconds", "event_coverage", "debug_path"}
    actions = {step["action"] for step in scenario["steps"]}
    assert {"register_tool", "add_approval_node", "add_state_node", "record_metrics"} <= actions


def test_run_manifest_lists_lifecycle_evidence(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    run_id = runner.submit(_spec("t5-fault-recovery"))
    runner.pause(run_id)
    runner.resume(run_id)
    runner.complete(run_id, {"recovered": True})

    run_dir = tmp_path / "artifacts" / "runs" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == RunState.COMPLETED.value
    assert manifest["checkpoint_id"]
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "state" / f"{manifest['checkpoint_id']}.json").exists()
