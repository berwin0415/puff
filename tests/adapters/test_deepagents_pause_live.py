"""Opt-in live test for non-approval Deep Agents pause/resume."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.run_deepagents_pause import load_model_env, run_pause_resume


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


def test_live_non_approval_pause_and_resume(tmp_path: Path) -> None:
    summary = run_pause_resume(artifact_root=tmp_path, env=load_model_env(Path(".env")), timeout=180)
    assert summary["pause_kind"] == "after_model_safe_point"
    assert summary["approval_required"] is False
    assert summary["native_pause"] is True
    assert summary["cross_process_resume"] is True
    assert summary["first_process"]["checkpoint_id"]
    assert summary["resume_process"]["answer"] == "fixture-alpha"
