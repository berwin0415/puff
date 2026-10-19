"""Run an OpenClaw ``agent exec`` smoke through the unified adapter.

Examples::

    .venv/bin/python scripts/run_openclaw_p0.py --task api-smoke
    .venv/bin/python scripts/run_openclaw_p0.py --task t1-tools --fixture-url http://127.0.0.1:8080

The script reads model credentials from ``.env`` and never writes them to the
manifest, events or console.
"""

from __future__ import annotations

import argparse
import json
import os
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


def prompt_for(task: str, fixture_url: str | None) -> str:
    if task == "api-smoke":
        return "Reply with exactly OPENCLAW_API_OK and no other text."
    if task == "t1-tools":
        if not fixture_url:
            raise ValueError("t1-tools requires --fixture-url")
        return (
            "Use the shell to run: curl -s '" + fixture_url.rstrip("/") + "/api/items?limit=3'. "
            "Then reply with only the sum of the value fields as a number."
        )
    raise ValueError(f"unknown task: {task}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="api-smoke", choices=("api-smoke", "t1-tools"))
    parser.add_argument("--fixture-url")
    parser.add_argument("--artifact-root", default="~/harness-lab-data/runs/2026-10-19/openclaw/agent-smoke")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    env = load_model_env(args.env_file)
    prompt = prompt_for(args.task, args.fixture_url)
    adapter = OpenClawAdapter(artifact_root=Path(args.artifact_root).expanduser())
    result = adapter.run_once(
        {"task_id": args.task, "prompt": prompt},
        timeout=args.timeout,
        env=env,
    )
    summary = {
        "run_id": result.run_id,
        "task_id": args.task,
        "returncode": result.returncode,
        "ok": result.ok,
        "events": [event["event_type"] for event in result.events],
        "result": adapter.result(result.run_id),
        "manifest": str(adapter.artifact_root / result.run_id / "manifest.json"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not result.ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
