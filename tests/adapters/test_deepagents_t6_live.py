"""Opt-in live test for the Deep Agents T6 extension measurement."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.run_deepagents_t6 import load_model_env, run_t6


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


def test_live_t6_extension(tmp_path: Path) -> None:
    summary = run_t6(artifact_root=tmp_path, env=load_model_env(Path(".env")), timeout=180)
    assert summary["ok"] is True
    assert summary["approval_interrupted"] is True
    assert summary["state_node_value"] == "started"
    assert summary["answer"] == "3"
    assert summary["event_count"] >= 6
    assert Path(summary["summary_path"]).exists()
