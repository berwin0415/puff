"""Execute the lifecycle scenario script against a real fixture server."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from fixtures.service import create_server
from scripts.run_lifecycle_scenario import run_scenario


@pytest.fixture
def fixture_server(tmp_path: Path):
    server = create_server(state_file=tmp_path / "fixture-state.json")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _run(tmp_path: Path, task_id: str, fixture_url: str | None = None) -> dict:
    return run_scenario(
        task_id,
        fixture_url=fixture_url,
        db_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
    )


def test_t3_script_records_input_and_approval(tmp_path: Path) -> None:
    metrics = _run(tmp_path, "t3-approval")
    assert metrics["state"] == "completed"
    assert metrics["input_recorded"] is True
    assert metrics["approval_id"] == "t3-step-1"
    assert "approval_decided" in metrics["events_by_type"]
    assert Path(metrics["metrics_path"]).exists()


def test_t4_script_records_restart_recovery_metrics(tmp_path: Path, fixture_server: str) -> None:
    metrics = _run(tmp_path, "t4-pause-resume", fixture_server)
    assert metrics["state"] == "completed"
    assert metrics["pause_point"] == "after_inflight_call"
    assert metrics["checkpoint_id"]
    assert metrics["resume_after_restart"] is True
    assert metrics["duplicate_side_effects"] == 0
    assert metrics["recovery_latency_seconds"] >= 0
    assert metrics["recovery_failure_reason"] is None
    saved = json.loads(Path(metrics["metrics_path"]).read_text(encoding="utf-8"))
    assert saved["run_id"] == metrics["run_id"]


def test_t5_script_records_retry_and_idempotent_replay(tmp_path: Path, fixture_server: str) -> None:
    metrics = _run(tmp_path, "t5-fault-recovery", fixture_server)
    assert metrics["state"] == "completed"
    assert metrics["fail_once_statuses"] == [503, 200]
    assert metrics["side_effect_statuses"] == [201, 200]
    assert metrics["side_effect_replayed"] is True
    assert metrics["duplicate_side_effects"] == 0
    assert metrics["resume_after_restart"] is True
    assert metrics["recovery_failure_reason"] is None


def test_t5_requires_fixture_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _run(tmp_path, "t5-fault-recovery")
