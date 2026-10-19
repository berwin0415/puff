"""Run a non-approval Deep Agents pause/resume cycle and record evidence.

The graph pauses after a model turn at a LangGraph safe point
(``interrupt_after=['model']``).  No human approval is involved.  A new process
then resumes the same thread from the SQLite checkpoint and runs to completion.

Credentials are read from ``.env`` and never written to evidence.
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
PROMPT = "Call fixture_lookup with key alpha, then reply with only the returned value."


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


def _event_types(result: Any) -> list[str]:
    return [str(event["event_type"]) for event in result.events]


def run_pause_resume(
    *,
    artifact_root: str | Path,
    env: dict[str, str],
    timeout: float = 120,
) -> dict[str, Any]:
    missing = [key for key in MODEL_KEYS if not env.get(key)]
    if missing:
        raise ValueError(f"missing model environment values: {', '.join(missing)}")

    run_id = f"deepagents-pause-{uuid.uuid4()}"
    root = Path(artifact_root).expanduser() / "2026-10-19" / "deepagents_harness" / "pause-resume" / run_id
    root.mkdir(parents=True, exist_ok=True)
    checkpoint_db = root / "checkpoints.sqlite"

    first = DeepAgentsLangGraphAdapter(checkpoint_db=checkpoint_db).run_paused(
        PROMPT,
        run_id=run_id,
        env=env,
        timeout=timeout,
    )
    paused = [event for event in first.events if event["event_type"] == "pause_requested"]
    if first.returncode != 0 or not paused:
        raise RuntimeError(f"candidate did not pause as expected: {first.stderr[-1000:]}")

    checkpoint_id = DeepAgentsLangGraphAdapter(checkpoint_db=checkpoint_db).snapshot(run_id)

    # New adapter/process; only the SQLite checkpoint and thread_id are shared.
    second = DeepAgentsLangGraphAdapter(checkpoint_db=checkpoint_db).resume_paused(
        run_id,
        env=env,
        timeout=timeout,
    )
    completed = [event for event in second.events if event["event_type"] == "graph_completed"]
    if second.returncode != 0 or not completed:
        raise RuntimeError(f"candidate did not resume as expected: {second.stderr[-1000:]}")
    answer = str(completed[-1]["payload"].get("answer", ""))

    summary = {
        "run_id": run_id,
        "prompt": PROMPT,
        "model": env.get("MODEL_NAME"),
        "pause_kind": "after_model_safe_point",
        "approval_required": False,
        "first_process": {
            "returncode": first.returncode,
            "event_types": _event_types(first),
            "pause_count": len(paused),
            "checkpoint_id": checkpoint_id,
        },
        "resume_process": {
            "returncode": second.returncode,
            "event_types": _event_types(second),
            "answer": answer,
        },
        "native_pause": len(paused) > 0,
        "cross_process_resume": bool(completed),
        "checkpoint_db": str(checkpoint_db),
    }
    summary_path = root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", default=os.environ.get("HARNESS_LAB_ARTIFACT_ROOT", "~/harness-lab-data/runs"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    result = run_pause_resume(
        artifact_root=args.artifact_root,
        env=load_model_env(args.env_file),
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
