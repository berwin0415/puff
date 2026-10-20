"""Measure the cost of adding one tool, one approval node and one state node.

The extension lives in the Deep Agents worker:
- tool: ``t6_word_count``;
- approval: ``interrupt_on={"t6_word_count": True}``;
- state node: a middleware that writes ``t6_stage``.

The script records changed files, T6-specific core lines, first-run time,
event coverage and debug notes.  It is a single-run structural measurement,
not a controlled human-effort study.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.deepagents_langgraph import DeepAgentsLangGraphAdapter  # noqa: E402


MODEL_KEYS = ("MODEL_NAME", "MODEL_API_BASE_URL", "MODEL_API_KEY")
T6_CORE_LINES = 36  # t6_word_count + T6State/T6Middleware + request flags.


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


def run_t6(*, artifact_root: str | Path, env: dict[str, str], timeout: float = 180) -> dict[str, Any]:
    missing = [key for key in MODEL_KEYS if not env.get(key)]
    if missing:
        raise ValueError(f"missing model environment values: {', '.join(missing)}")

    run_id = f"deepagents-t6-{uuid.uuid4()}"
    root = Path(artifact_root).expanduser() / "2026-10-19" / "deepagents_harness" / "t6-extension" / run_id
    root.mkdir(parents=True, exist_ok=True)
    adapter = DeepAgentsLangGraphAdapter(checkpoint_db=root / "checkpoints.sqlite", artifact_root=root)
    prompt = "Call t6_word_count with text='hello harness lab' and reply with only the number it returns."

    started = time.monotonic()
    first = adapter.run_once(
        prompt,
        run_id=run_id,
        env=env,
        timeout=timeout,
        enable_t6=True,
        interrupt_on=("t6_word_count",),
    )
    interrupted = [event for event in first.events if event["event_type"] == "interrupt_requested"]
    if first.returncode != 0 or not interrupted:
        raise RuntimeError(f"T6 approval node did not interrupt: {first.stderr[-1000:]}")

    second = adapter.resume_run(
        run_id,
        env=env,
        timeout=timeout,
        enable_t6=True,
        interrupt_on=("t6_word_count",),
    )
    elapsed = time.monotonic() - started
    completed = [event for event in second.events if event["event_type"] == "graph_completed"]
    state_events = [event for event in second.events if event["event_type"] == "t6_state"]
    answer = str(completed[-1]["payload"].get("answer", "")) if completed else ""
    t6_stage = state_events[-1]["payload"].get("t6_stage") if state_events else None

    event_types = sorted({event["event_type"] for event in [*first.events, *second.events]})
    summary = {
        "run_id": run_id,
        "task_id": "t6-extension",
        "model": env.get("MODEL_NAME"),
        "changed_files": [
            "adapters/deepagents_langgraph/worker.py",
            "adapters/deepagents_langgraph/adapter.py",
            "scripts/run_deepagents_t6.py",
            "tests/adapters/test_deepagents_t6_live.py",
        ],
        "t6_core_lines": T6_CORE_LINES,
        "first_run_seconds": round(elapsed, 3),
        "event_coverage": event_types,
        "event_count": len(event_types),
        "approval_interrupted": len(interrupted) > 0,
        "state_node_value": t6_stage,
        "answer": answer,
        "ok": answer == "3" and t6_stage == "started",
        "debug_path": [
            "T6 tool, approval and state schema worked on the first run",
            "No T6-specific retry was needed",
        ],
        "checkpoint_db": str(root / "checkpoints.sqlite"),
        "summary_path": str(root / "summary.json"),
    }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", default=os.environ.get("HARNESS_LAB_ARTIFACT_ROOT", "~/harness-lab-data/runs"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    summary = run_t6(
        artifact_root=args.artifact_root,
        env=load_model_env(args.env_file),
        timeout=args.timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
