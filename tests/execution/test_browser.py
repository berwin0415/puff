from __future__ import annotations

import threading

import pytest

from fixtures.service import create_server
from execution.browser.smoke import run_smoke


playwright = pytest.importorskip("playwright.sync_api")


def test_playwright_smoke_against_fixture(tmp_path):
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        value = run_smoke(f"http://127.0.0.1:{server.server_port}", tmp_path)
        assert value["title"] == "Harness Fixture"
        assert value["result"] == "accepted:harness-lab"
        assert (tmp_path / "t2-browser.png").exists()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
