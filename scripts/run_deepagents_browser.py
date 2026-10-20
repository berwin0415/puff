"""Run the unified Execution Playwright T2 task through Deep Agents.

The candidate graph calls a `browser_fixture` tool, which invokes the common
Execution Playwright smoke.  This is the main comparison track: the browser
runs in the shared execution environment, not inside the candidate process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.deepagents_langgraph import DeepAgentsLangGraphAdapter  # noqa: E402


MODEL_KEYS = ("MODEL_NAME", "MODEL_API_BASE_URL", "MODEL_API_KEY")


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
        if key in MODEL_KEYS:
            env[key] = value.strip().strip('"').strip("'")
    return env


def run_browser_task(
    *,
    base_url: str,
    artifact_root: str | Path,
    env: dict[str, str],
    timeout: float = 240,
) -> dict[str, Any]:
    missing = [key for key in MODEL_KEYS if not env.get(key)]
    if missing:
        raise ValueError(f"missing model environment values: {', '.join(missing)}")

    run_id = f"deepagents-browser-{uuid.uuid4()}"
    root = Path(artifact_root).expanduser() / "2026-10-19" / "deepagents_harness" / "browser" / run_id
    root.mkdir(parents=True, exist_ok=True)
    adapter = DeepAgentsLangGraphAdapter(
        checkpoint_db=root / "checkpoints.sqlite",
        artifact_root=root,
    )
    prompt = (
        f"Call browser_fixture with base_url='{base_url.rstrip('/')}' "
        "and reply with only the result field from its JSON output."
    )
    result = adapter.run_once(
        prompt,
        run_id=run_id,
        env=env,
        timeout=timeout,
        enable_browser_tool=True,
    )
    completed = [event for event in result.events if event["event_type"] == "graph_completed"]
    answer = str(completed[-1]["payload"].get("answer", "")) if completed else ""
    screenshot = root / "browser" / run_id / "t2-browser.png"

    summary = {
        "run_id": run_id,
        "task_id": "t2-browser",
        "transport": "execution-playwright-via-tool",
        "returncode": result.returncode,
        "event_types": [event["event_type"] for event in result.events],
        "answer": answer,
        "ok": result.ok and answer == "accepted:harness-lab",
        "screenshot": str(screenshot),
        "screenshot_exists": screenshot.exists(),
        "prompt": prompt,
        "model": env.get("MODEL_NAME"),
        "summary_path": str(root / "summary.json"),
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--artifact-root", default=os.environ.get("HARNESS_LAB_ARTIFACT_ROOT", "~/harness-lab-data/runs"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    summary = run_browser_task(
        base_url=args.base_url,
        artifact_root=args.artifact_root,
        env=load_model_env(args.env_file),
        timeout=args.timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
