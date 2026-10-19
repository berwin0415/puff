"""Run T3-T5 lifecycle scenarios against the unified Runner.

This script exercises the framework-neutral control plane: task input,
approval, pause/checkpoint, Runner recreation, resume, retry and idempotent
side effects.  It produces the recovery metrics required by the validation
plan.  Candidate-native lifecycle support is recorded separately; a successful
run here does not prove that a candidate adapter can pause its own process.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.tools.basic import http_request  # noqa: E402
from runner import Runner, SQLiteEventStore  # noqa: E402
from runner.scenarios import load_scenario, load_task_config  # noqa: E402


SUPPORTED_TASKS = ("t3-approval", "t4-pause-resume", "t5-fault-recovery")


def _task_spec(task_id: str) -> dict[str, Any]:
    config = load_task_config(task_id)
    scenario = load_scenario(task_id)
    prompt = next(
        (step["prompt"] for step in scenario["steps"] if step.get("prompt")),
        config["description"],
    )
    return {
        "task_id": config["task_id"],
        "tenant_scope": "local-tenant",
        "session_scope": f"lifecycle-{config['task_id']}",
        "prompt": prompt,
        "timeout_seconds": config["timeout_seconds"],
        "budget": {"usd": config["budget_usd"]},
        "checkpoint_policy": config.get("checkpoint", {"enabled": True}),
        "metadata": {"scenario_task_id": scenario["task_id"]},
    }


def _event_types(runner: Runner, run_id: str) -> list[str]:
    _cursor, events = runner.stream_events(run_id)
    return [event["event_type"] for event in events]


def _write_metrics(runner: Runner, run_id: str, metrics: dict[str, Any]) -> Path:
    path = runner.storage.run_dir(run_id) / "metrics.json"
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run_t3(runner: Runner, run_id: str) -> dict[str, Any]:
    scenario = load_scenario("t3-approval")
    instruction = next(step["message"] for step in scenario["steps"] if step["action"] == "send_input")
    approval = next(step for step in scenario["steps"] if step["action"] == "approve")
    runner.send_input(run_id, instruction)
    runner.approve(run_id, approval["approval_id"], approval["decision"])
    runner.complete(run_id, {"answer": "approved"})
    return {
        "pause_point": None,
        "checkpoint_id": None,
        "resume_after_restart": False,
        "duplicate_side_effects": 0,
        "recovery_latency_seconds": None,
        "recovery_failure_reason": None,
        "approval_id": approval["approval_id"],
        "input_recorded": "input_received" in _event_types(runner, run_id),
    }


def _run_t4(runner: Runner, run_id: str, fixture_url: str | None) -> dict[str, Any]:
    checkpoint_id = runner.pause(run_id)
    paused_at = time.monotonic()

    # Simulate a Runner process restart against the same persistence roots.
    restarted = Runner(SQLiteEventStore(runner.storage.db_path, runner.storage.artifact_root))
    recovered = restarted.status(run_id)["state"] == "paused"
    restarted.resume(run_id, checkpoint_id)
    recovery_latency = time.monotonic() - paused_at

    if fixture_url:
        http_request(
            f"{fixture_url.rstrip('/')}/api/side-effects",
            method="POST",
            payload={"operation": "resume", "payload": {"run_id": run_id}},
            idempotency_key=f"{run_id}-resume",
        )
    restarted.complete(run_id, {"answer": "resumed"})

    duplicate_side_effects = 0
    if fixture_url:
        result = http_request(f"{fixture_url.rstrip('/')}/api/side-effects")
        effects = result["body"]["effects"]
        duplicate_side_effects = len(effects) - len({effect["idempotency_key"] for effect in effects})

    return {
        "pause_point": "after_inflight_call",
        "checkpoint_id": checkpoint_id,
        "resume_after_restart": recovered,
        "duplicate_side_effects": duplicate_side_effects,
        "recovery_latency_seconds": round(recovery_latency, 4),
        "recovery_failure_reason": None if recovered else "runner did not observe the paused state",
    }


def _run_t5(runner: Runner, run_id: str, fixture_url: str | None) -> dict[str, Any]:
    if not fixture_url:
        raise ValueError("t5-fault-recovery requires --fixture-url")
    base = fixture_url.rstrip("/")

    first = http_request(f"{base}/api/fail-once", method="POST", payload={"key": run_id})
    second = http_request(f"{base}/api/fail-once", method="POST", payload={"key": run_id})

    checkpoint_id = runner.pause(run_id)
    paused_at = time.monotonic()
    restarted = Runner(SQLiteEventStore(runner.storage.db_path, runner.storage.artifact_root))
    recovered = restarted.status(run_id)["state"] == "paused"
    restarted.resume(run_id, checkpoint_id)
    recovery_latency = time.monotonic() - paused_at

    key = f"{run_id}-side-effect"
    payload = {"operation": "charge", "payload": {"run_id": run_id}}
    created = http_request(f"{base}/api/side-effects", method="POST", payload=payload, idempotency_key=key)
    replayed = http_request(f"{base}/api/side-effects", method="POST", payload=payload, idempotency_key=key)
    restarted.complete(run_id, {"recovered": True, "retry_succeeded": second["status_code"] == 200})

    effects = http_request(f"{base}/api/side-effects")["body"]["effects"]
    duplicate_side_effects = len(effects) - len({effect["idempotency_key"] for effect in effects})

    return {
        "pause_point": "after_inflight_call",
        "checkpoint_id": checkpoint_id,
        "resume_after_restart": recovered,
        "duplicate_side_effects": duplicate_side_effects,
        "recovery_latency_seconds": round(recovery_latency, 4),
        "recovery_failure_reason": None if recovered else "runner did not observe the paused state",
        "fail_once_statuses": [first["status_code"], second["status_code"]],
        "side_effect_statuses": [created["status_code"], replayed["status_code"]],
        "side_effect_replayed": replayed["body"].get("status") == "replayed",
    }


def run_scenario(
    task_id: str,
    *,
    fixture_url: str | None = None,
    db_path: str | Path = "var/state/lifecycle.sqlite3",
    artifact_root: str | Path = "var/artifacts",
) -> dict[str, Any]:
    if task_id not in SUPPORTED_TASKS:
        raise ValueError(f"unsupported lifecycle task: {task_id}; choose from {SUPPORTED_TASKS}")

    runner = Runner(SQLiteEventStore(db_path, artifact_root))
    run_id = runner.submit(_task_spec(task_id))

    if task_id == "t3-approval":
        details = _run_t3(runner, run_id)
    elif task_id == "t4-pause-resume":
        details = _run_t4(runner, run_id, fixture_url)
    else:
        details = _run_t5(runner, run_id, fixture_url)

    final = runner.status(run_id)
    metrics = {
        "task_id": task_id,
        "run_id": run_id,
        "state": final["state"],
        "event_count": len(_event_types(runner, run_id)),
        "events_by_type": _event_types(runner, run_id),
        **details,
    }
    metrics["metrics_path"] = str(_write_metrics(runner, run_id, metrics))
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=SUPPORTED_TASKS)
    parser.add_argument("--fixture-url")
    parser.add_argument("--db", default="var/state/lifecycle.sqlite3")
    parser.add_argument("--artifact-root", default="var/artifacts")
    args = parser.parse_args()
    print(
        json.dumps(
            run_scenario(args.task, fixture_url=args.fixture_url, db_path=args.db, artifact_root=args.artifact_root),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
