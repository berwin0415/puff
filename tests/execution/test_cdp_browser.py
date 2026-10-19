from __future__ import annotations

import threading

import pytest

from execution.browser.cdp_smoke import run_cdp_smoke
from fixtures.service import create_server


pytest.importorskip("playwright.sync_api")


def test_standalone_chromium_cdp_smoke(tmp_path):
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        value = run_cdp_smoke(f"http://127.0.0.1:{server.server_port}", tmp_path)
        assert value["electron"] is False
        assert value["transport"] == "chromium-cdp"
        assert value["submit_result"] == "accepted:harness-lab"
        assert value["tab_count"] >= 2
        assert value["persisted_after_restart"] is True
        assert (tmp_path / "cdp-browser-smoke.png").exists()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
