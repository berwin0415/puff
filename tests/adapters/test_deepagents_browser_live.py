"""Opt-in live test for Deep Agents T2 on the unified Playwright track."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.run_deepagents_browser import load_model_env, run_browser_task


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


def test_live_deepagents_t2_browser(tmp_path: Path) -> None:
    from fixtures.service import create_server
    import threading

    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        summary = run_browser_task(
            base_url=f"http://127.0.0.1:{server.server_port}",
            artifact_root=tmp_path,
            env=load_model_env(Path(".env")),
            timeout=240,
        )
        assert summary["ok"] is True
        assert summary["answer"] == "accepted:harness-lab"
        assert summary["screenshot_exists"] is True
        assert "graph_completed" in summary["event_types"]
        assert Path(summary["summary_path"]).exists()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
