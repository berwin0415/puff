from __future__ import annotations

import shutil
import threading

import pytest

from execution.browser.mcp_cdp_smoke import DEFAULT_MCP_CLI, run_mcp_cdp_smoke
from fixtures.service import create_server


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not DEFAULT_MCP_CLI.exists(),
    reason="Node or the Playwright MCP CLI is not available",
)


def test_playwright_mcp_connects_to_standalone_chromium(tmp_path):
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        value = run_mcp_cdp_smoke(f"http://127.0.0.1:{server.server_port}", tmp_path)
        assert value["transport"] == "playwright-mcp-cdp"
        assert value["electron"] is False
        assert value["tab_count"] >= 3
        assert value["snapshot_contains_result"] is True
        assert value["persisted_after_restart"] is True
        assert (tmp_path / "mcp-browser.png").exists()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
