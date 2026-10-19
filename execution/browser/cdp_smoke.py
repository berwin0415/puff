"""Validate the cloud-shaped browser boundary without Electron.

The smoke starts a standalone Chromium process with a CDP endpoint, then uses
Playwright's CDP client to exercise the same browser primitives used by the
hsi-desktop bridge: navigation, form submission, a second tab, a screenshot,
and profile state after a browser process restart.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


_DEVTOOLS_RE = re.compile(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/")


def _start_chromium(executable: str, profile_dir: Path) -> tuple[subprocess.Popen[str], int, Path]:
    log_path = profile_dir / "chromium-stderr.log"
    log_path.touch()
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile_dir / 'profile'}",
            "about:blank",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=log,
        text=True,
    )
    log.close()
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Chromium exited before CDP was ready (code={process.returncode})")
            match = _DEVTOOLS_RE.search(log_path.read_text(encoding="utf-8", errors="replace"))
            if match:
                return process, int(match.group(1)), log_path
            time.sleep(0.05)
    except BaseException:
        _stop_chromium(process)
        raise
    _stop_chromium(process)
    raise TimeoutError("Chromium did not expose a CDP endpoint within 15 seconds")


def _stop_chromium(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _close_chromium(browser: Any, page: Any, process: subprocess.Popen[str] | None) -> None:
    """Shut the browser down gracefully so profile storage is flushed.

    ``Browser.close()`` on a CDP-connected browser only disconnects from the
    remote endpoint.  Terminating the process right afterwards loses profile
    writes (localStorage is committed on a delay or during a clean shutdown),
    so the restart check would see an empty profile.  The CDP ``Browser.close``
    command performs a clean shutdown instead.
    """

    try:
        session = page.context.new_cdp_session(page)
        session.send("Browser.close")
    except Exception:  # noqa: BLE001 - fall back to termination below
        browser.close()
    if process is not None:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _stop_chromium(process)


def run_cdp_smoke(base_url: str, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Run the standalone Chromium/CDP smoke against a fixture URL."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("Playwright is required for the CDP browser smoke") from exc

    base_url = base_url.rstrip("/")
    output = Path(output_dir) if output_dir is not None else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="harness-lab-cdp-") as temporary:
        root = Path(temporary)
        process: subprocess.Popen[str] | None = None
        screenshot: str | None = None
        first_title = ""
        submit_result = ""
        tab_count = 0
        persisted_before_restart = False
        persisted_after_restart = False
        try:
            with sync_playwright() as playwright:
                executable = playwright.chromium.executable_path
                process, port, _log_path = _start_chromium(executable, root)
                endpoint = f"http://127.0.0.1:{port}"
                browser = playwright.chromium.connect_over_cdp(endpoint)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"{base_url}/browser/", wait_until="networkidle")
                first_title = page.title()
                page.evaluate("window.localStorage.setItem('harness-lab-state', 'persisted')")
                page.locator("#name").fill("harness-lab")
                page.locator("#submit").click()
                page.wait_for_function(
                    "() => document.querySelector('#result')?.textContent === 'accepted:harness-lab'"
                )
                submit_result = page.locator("#result").inner_text()
                persisted_before_restart = page.evaluate(
                    "window.localStorage.getItem('harness-lab-state') === 'persisted'"
                )
                second = context.new_page()
                second.goto(f"{base_url}/browser/?tab=2", wait_until="networkidle")
                tab_count = len(context.pages)
                if output is not None:
                    image = output / "cdp-browser-smoke.png"
                    page.screenshot(path=str(image), full_page=True)
                    screenshot = str(image)
                # Graceful CDP shutdown is the restart boundary; it guarantees
                # the profile is flushed before the process is restarted.
                _close_chromium(browser, page, process)
                process = None

                process, port, _log_path = _start_chromium(executable, root)
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.contexts[0]
                restored = context.new_page()
                restored.goto(f"{base_url}/browser/", wait_until="networkidle")
                persisted_after_restart = restored.evaluate(
                    "window.localStorage.getItem('harness-lab-state') === 'persisted'"
                )
                _close_chromium(browser, restored, process)
                process = None
        finally:
            _stop_chromium(process)

    result = {
        "transport": "chromium-cdp",
        "electron": False,
        "title": first_title,
        "submit_result": submit_result,
        "tab_count": tab_count,
        "persisted_before_restart": persisted_before_restart,
        "persisted_after_restart": persisted_after_restart,
        "screenshot": screenshot,
    }
    if submit_result != "accepted:harness-lab":
        raise RuntimeError(f"CDP browser form assertion failed: {result}")
    if tab_count < 2:
        raise RuntimeError(f"CDP browser multi-tab assertion failed: {result}")
    if not persisted_before_restart or not persisted_after_restart:
        raise RuntimeError(f"CDP browser profile persistence assertion failed: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    print(json.dumps(run_cdp_smoke(args.base_url, args.output_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
