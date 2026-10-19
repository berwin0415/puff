"""Opt-in live test for the Deep Agents native interrupt/resume path.

Set ``RUN_LIVE_MODEL_TESTS=1`` to execute the real remote-model cycle.  The
default suite skips it so normal runs do not consume model quota.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.run_deepagents_interrupt import load_model_env, run_interrupt_resume


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


def test_live_interrupt_and_cross_process_resume(tmp_path: Path) -> None:
    env = load_model_env(Path(".env"))
    summary = run_interrupt_resume(artifact_root=tmp_path, env=env, timeout=180)
    assert summary["native_interrupt"] is True
    assert summary["cross_process_resume"] is True
    assert summary["first_process"]["interrupt_count"] >= 1
    assert summary["first_process"]["checkpoint_id"]
    assert summary["resume_process"]["answer"] == "fixture-alpha"
    assert Path(summary["summary_path"]).exists()
