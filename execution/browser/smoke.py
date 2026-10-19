"""Run the deterministic Playwright browser smoke task (T2)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def run_smoke(base_url: str, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Visit the fixture page, submit a value, and optionally save a screenshot."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised in environments without Playwright
        raise RuntimeError("Playwright is required for the browser smoke") from exc

    base_url = base_url.rstrip("/")
    screenshot: str | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        response = page.goto(f"{base_url}/browser/", wait_until="networkidle")
        if response is None or not response.ok:
            raise RuntimeError(f"fixture page failed to load: {response.status if response else 'no response'}")
        title = page.title()
        page.locator("#name").fill("harness-lab")
        page.locator("#submit").click()
        # The form submits asynchronously through fetch.  Waiting only for
        # the element itself would race with the initial ``ready`` text.
        page.wait_for_function(
            "() => document.querySelector('#result')?.textContent === 'accepted:harness-lab'"
        )
        result = page.locator("#result").inner_text()
        if output_dir is not None:
            target = Path(output_dir)
            target.mkdir(parents=True, exist_ok=True)
            image = target / "t2-browser.png"
            page.screenshot(path=str(image), full_page=True)
            screenshot = str(image)
        browser.close()
    return {"title": title, "result": result, "screenshot": screenshot}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    print(json.dumps(run_smoke(args.base_url, args.output_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
