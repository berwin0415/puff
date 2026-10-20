"""Opt-in live test: pause after tools, resume, and verify idempotency."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.run_deepagents_side_effect_recovery import load_model_env, run_recovery


EXTERNAL_PYTHON = Path(
    os.environ.get(
        "DEEPAGENTS_PYTHON",
        "~/harness-lab-sources/deepagents/libs/deepagents/.venv/bin/python",
    )
).expanduser()


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_MODEL_TESTS") != "1" or not EXTERNAL_PYTHON.exists(),
    reason="set RUN_LIVE_MODEL_TESTS=1 and provide the external Deep Agents environment",
)


def test_live_pause_after_tools_and_resume_without_duplicate(tmp_path: Path) -> None:
    summary = run_recovery(artifact_root=tmp_path, env=load_model_env(Path(".env")), timeout=180)
    assert summary["ok"] is True
    assert summary["pause_kind"] == "after_tools"
    assert summary["approval_required"] is False
    assert summary["resume_process"]["answer"] == "fixture-alpha"
    assert summary["fixture_effect_count"] == 1
    assert summary["duplicate_side_effects"] == 0
    assert Path(summary["summary_path"]).exists()
