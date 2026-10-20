"""Run a managed-browser smoke through the OpenClaw adapter.

This does not use the Chrome extension relay.  OpenClaw's built-in ``openclaw``
profile launches a managed headless Chromium and the agent opens the fixture
page with the browser tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.openclaw import OpenClawAdapter  # noqa: E402


def load_model_env(path: str | Path = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    source = Path(path)
    if not source.exists():
        return env
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"MODEL_NAME", "MODEL_API_BASE_URL", "MODEL_API_KEY"}:
            env[key] = value.strip().strip('"').strip("'")
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--browser-executable")
    parser.add_argument("--artifact-root", default="~/harness-lab-data/runs/2026-10-19/openclaw/browser-smoke")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()

    prompt = (
        f"Use only the browser tool (not curl, exec, web_fetch or any other fallback) to open "
        f"{args.base_url.rstrip('/')}/browser/ and reply with only the page title."
    )
    adapter = OpenClawAdapter(artifact_root=Path(args.artifact_root).expanduser())
    result = adapter.run_once(
        {"task_id": "openclaw-browser-title", "prompt": prompt},
        timeout=args.timeout,
        env=load_model_env(args.env_file),
        enable_browser=True,
        browser_executable=args.browser_executable,
    )
    summary = {
        "run_id": result.run_id,
        "ok": result.ok,
        "timed_out": result.timed_out,
        "cleanup_timed_out": result.cleanup_timed_out,
        "event_types": [event["event_type"] for event in result.events],
        "result": adapter.result(result.run_id),
        "manifest": str(adapter.artifact_root / result.run_id / "manifest.json"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not result.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
