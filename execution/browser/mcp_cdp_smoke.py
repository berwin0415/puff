"""Validate @playwright/mcp against a standalone Chromium CDP endpoint.

The browser is launched directly, not by Electron.  A stdio MCP server connects
to it with ``--cdp-endpoint``; this script speaks the MCP JSON-RPC protocol to
exercise navigation, form input, a second tab, a screenshot and profile
persistence across a Chromium restart.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
from typing import Any

from execution.browser.cdp_smoke import _close_chromium, _start_chromium, _stop_chromium


PROFILE_FLUSH_SECONDS = float(os.environ.get("HARNESS_LAB_PROFILE_FLUSH_SECONDS", "6"))

DEFAULT_MCP_CLI = Path(
    os.environ.get(
        "PLAYWRIGHT_MCP_CLI",
        "~/workspace/baidu/ai/hogee-hsi/packages/hsi-desktop/node_modules/@playwright/mcp/cli.js",
    )
).expanduser()


class MCPError(RuntimeError):
    pass


class MCPClient:
    """Minimal MCP stdio client for requests and tool calls."""

    def __init__(self, command: list[str], *, cwd: str | Path | None = None, startup_timeout: float = 30) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise MCPError("failed to open MCP stdio pipes")
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 1
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self.startup_timeout = startup_timeout

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self._queue.put(value)
        self._queue.put({"__eof__": True})

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self._stderr.append(line)
            del self._stderr[:-200]

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 60) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self._queue.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if value.get("__eof__"):
                raise MCPError(f"MCP server exited; stderr={''.join(self._stderr)[-1000:]}")
            if value.get("id") == request_id:
                if "error" in value:
                    raise MCPError(f"MCP error for {method}: {value['error']}")
                return value
            # Ignore notifications and unrelated responses.
        raise MCPError(f"timed out waiting for MCP response to {method}; stderr={''.join(self._stderr)[-1000:]}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise MCPError(f"MCP server is not running; stderr={''.join(self._stderr)[-1000:]}")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def initialize(self) -> dict[str, Any]:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "harness-lab", "version": "0.1"},
            },
            timeout=self.startup_timeout,
        )
        self.notify("notifications/initialized")
        return response

    def list_tools(self) -> list[str]:
        response = self.request("tools/list")
        tools = response.get("result", {}).get("tools", [])
        return [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = 60) -> str:
        response = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        result = response.get("result", {})
        if result.get("isError"):
            raise MCPError(f"MCP tool {name} failed: {result}")
        parts: list[str] = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


def _start_mcp(port: int, output_dir: Path, *, mcp_cli: Path = DEFAULT_MCP_CLI, timeout: float = 60) -> MCPClient:
    if not mcp_cli.exists():
        raise RuntimeError(f"Playwright MCP CLI does not exist: {mcp_cli}")
    client = MCPClient(
        [
            "node",
            str(mcp_cli),
            "--cdp-endpoint",
            f"http://127.0.0.1:{port}",
            "--headless",
            "--output-dir",
            str(output_dir),
        ],
        startup_timeout=timeout,
    )
    client.initialize()
    return client


def _stop_chromium_gracefully(port: int, process: subprocess.Popen[str]) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _stop_chromium(process)
        return
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            _close_chromium(browser, page, process)
    except Exception:
        _stop_chromium(process)


def run_mcp_cdp_smoke(
    base_url: str,
    output_dir: str | Path | None = None,
    *,
    mcp_cli: str | Path = DEFAULT_MCP_CLI,
) -> dict[str, Any]:
    """Run the cloud-shaped Chromium + Playwright MCP smoke."""

    from playwright.sync_api import sync_playwright

    base_url = base_url.rstrip("/")
    output = Path(output_dir) if output_dir is not None else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)

    mcp_cli = Path(mcp_cli).expanduser()
    with tempfile.TemporaryDirectory(prefix="harness-lab-mcp-") as temporary:
        root = Path(temporary)
        process: subprocess.Popen[str] | None = None
        client: MCPClient | None = None
        try:
            with sync_playwright() as playwright:
                executable = playwright.chromium.executable_path
                process, port, _log_path = _start_chromium(executable, root)
                client = _start_mcp(port, output or root, mcp_cli=mcp_cli)
                tools = client.list_tools()
                required = {"browser_navigate", "browser_type", "browser_click", "browser_snapshot", "browser_tabs"}
                missing = sorted(required - set(tools))
                if missing:
                    raise MCPError(f"MCP server is missing tools: {missing}")

                client.call_tool("browser_navigate", {"url": f"{base_url}/browser/"})
                client.call_tool(
                    "browser_evaluate",
                    {"function": "() => { localStorage.setItem('mcp-state', 'persisted'); return 'ok'; }"},
                )
                client.call_tool("browser_type", {"target": "#name", "text": "harness-lab"})
                client.call_tool("browser_click", {"target": "#submit"})
                client.call_tool("browser_wait_for", {"text": "accepted:harness-lab"})
                snapshot = client.call_tool("browser_snapshot", {})
                if "accepted:harness-lab" not in snapshot:
                    raise MCPError(f"snapshot did not contain the submitted result: {snapshot[:500]}")

                client.call_tool("browser_tabs", {"action": "new", "url": f"{base_url}/browser/?tab=2"})
                client.call_tool(
                    "browser_evaluate",
                    {"function": f"() => {{ window.open('{base_url}/browser/?popup=1', '_blank'); return 'opened'; }}"},
                )
                time.sleep(0.5)
                tab_list = client.call_tool("browser_tabs", {"action": "list"})
                tab_count = tab_list.count("http")
                if tab_count < 3:
                    raise MCPError(f"expected at least three tabs (new + window.open), got: {tab_list[:500]}")

                screenshot: str | None = None
                if output is not None:
                    image = output / "mcp-browser.png"
                    client.call_tool("browser_take_screenshot", {"filename": str(image), "fullPage": True})
                    screenshot = str(image)
                    if not image.exists():
                        raise MCPError(f"MCP screenshot was not written: {image}")

                # Chromium commits localStorage on a short timer.  The MCP
                # shutdown path does not guarantee a storage flush, so allow the
                # periodic commit before closing the browser.
                time.sleep(PROFILE_FLUSH_SECONDS)
                client.close()
                client = None
                _stop_chromium_gracefully(port, process)
                process = None

                # Restart Chromium with the same profile and verify localStorage.
                process, port, _log_path = _start_chromium(executable, root)
                client = _start_mcp(port, output or root, mcp_cli=mcp_cli)
                client.call_tool("browser_navigate", {"url": f"{base_url}/browser/"})
                restored = client.call_tool(
                    "browser_evaluate",
                    {"function": "() => localStorage.getItem('mcp-state')"},
                )
                persisted_after_restart = "persisted" in restored

                return {
                    "transport": "playwright-mcp-cdp",
                    "electron": False,
                    "mcp_cli": str(mcp_cli),
                    "tools": tools,
                    "tab_count": tab_count,
                    "snapshot_contains_result": True,
                    "screenshot": screenshot,
                    "profile_flush_seconds": PROFILE_FLUSH_SECONDS,
                    "persisted_after_restart": persisted_after_restart,
                }
        finally:
            if client is not None:
                client.close()
            _stop_chromium(process)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--mcp-cli", default=str(DEFAULT_MCP_CLI))
    args = parser.parse_args()
    print(json.dumps(run_mcp_cdp_smoke(args.base_url, args.output_dir, mcp_cli=args.mcp_cli), ensure_ascii=False))


if __name__ == "__main__":
    main()
