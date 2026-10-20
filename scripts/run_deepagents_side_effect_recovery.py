"""Pause after a tool side effect, resume in a new process, verify no duplicate.

The graph pauses after the ``tools`` node (a non-approval safe point).  The
worker process exits with a SQLite checkpoint.  A new adapter process resumes
the same thread; the fixture must still contain exactly one side effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.deepagents_langgraph import DeepAgentsLangGraphAdapter  # noqa: E402
from execution.tools.basic import http_request  # noqa: E402
from fixtures.service import create_server  # noqa: E402


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


def run_recovery(
    *,
    artifact_root: str | Path,
    env: dict[str, str],
    timeout: float = 180,
) -> dict[str, Any]:
    missing = [key for key in MODEL_KEYS if not env.get(key)]
    if missing:
        raise ValueError(f"missing model environment values: {', '.join(missing)}")

    run_id = f"deepagents-recovery-{uuid.uuid4()}"
    root = Path(artifact_root).expanduser() / "2026-10-19" / "deepagents_harness" / "side-effect-recovery" / run_id
    root.mkdir(parents=True, exist_ok=True)
    checkpoint_db = root / "checkpoints.sqlite"
    fixture_state = root / "fixture-state.json"

    server = create_server(state_file=fixture_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        prompt = (
            f"Call fixture_effect with base_url='{base_url}', "
            f"idempotency_key='{run_id}-step-1', amount=10. "
            "Then call fixture_lookup with key alpha. "
            "Reply with only the fixture_lookup value."
        )
        first = DeepAgentsLangGraphAdapter(checkpoint_db=checkpoint_db).run_paused(
            prompt,
            run_id=run_id,
            pause_after="tools",
            enable_fixture_effect=True,
            env=env,
            timeout=timeout,
        )
        paused = [event for event in first.events if event["event_type"] == "pause_requested"]
        if first.returncode != 0 or not paused:
            raise RuntimeError(
                "candidate did not pause after tools: "
                f"events={[event['event_type'] for event in first.events]} "
                f"answer={[event['payload'] for event in first.events if event['event_type'] == 'graph_completed'][-1:]} "
                f"stderr={first.stderr[-1000:]}"
            )
        checkpoint_id = DeepAgentsLangGraphAdapter(checkpoint_db=checkpoint_db).snapshot(run_id)

        # The first worker process has exited.  A new adapter instance below is
        # a new process; only the SQLite checkpoint and fixture state persist.
        second = DeepAgentsLangGraphAdapter(checkpoint_db=checkpoint_db).resume_paused(
            run_id,
            enable_fixture_effect=True,
            env=env,
            timeout=timeout,
        )
        completed = [event for event in second.events if event["event_type"] == "graph_completed"]
        if second.returncode != 0 or not completed:
            raise RuntimeError(f"candidate did not resume after tools: {second.stderr[-1000:]}")
        answer = str(completed[-1]["payload"].get("answer", ""))

        effects = http_request(f"{base_url}/api/side-effects")["body"]["effects"]
        keys = [str(effect.get("idempotency_key")) for effect in effects]
        duplicate_side_effects = len(keys) - len(set(keys))

        summary = {
            "run_id": run_id,
            "task_id": "t4-t5-side-effect-recovery",
            "pause_kind": "after_tools",
            "approval_required": False,
            "first_process": {
                "returncode": first.returncode,
                "event_types": [event["event_type"] for event in first.events],
                "checkpoint_id": checkpoint_id,
            },
            "resume_process": {
                "returncode": second.returncode,
                "event_types": [event["event_type"] for event in second.events],
                "answer": answer,
            },
            "fixture_effect_count": len(effects),
            "duplicate_side_effects": duplicate_side_effects,
            "ok": answer == "fixture-alpha" and duplicate_side_effects == 0 and len(effects) == 1,
            "checkpoint_db": str(checkpoint_db),
            "fixture_state": str(fixture_state),
            "summary_path": str(root / "summary.json"),
        }
        (root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return summary
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", default=os.environ.get("HARNESS_LAB_ARTIFACT_ROOT", "~/harness-lab-data/runs"))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    summary = run_recovery(
        artifact_root=args.artifact_root,
        env=load_model_env(args.env_file),
        timeout=args.timeout,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
