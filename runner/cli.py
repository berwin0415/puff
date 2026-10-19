"""Command-line entry point for the minimal local runner.

Examples::

    python -m runner.cli submit --task task.json
    python -m runner.cli pause RUN_ID
    python -m runner.cli stream RUN_ID --after-seq 2
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from contracts.adapter import TaskSpec
from .lifecycle import Runner
from .storage import SQLiteEventStore


def _json_arg(value: str) -> dict[str, Any]:
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def _runner(args: argparse.Namespace) -> Runner:
    return Runner(SQLiteEventStore(args.db, args.artifact_root))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harness Lab local lifecycle runner")
    parser.add_argument("--db", default="var/state/runner.sqlite3", help="SQLite database path")
    parser.add_argument("--artifact-root", default="var/artifacts", help="artifact root directory")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="submit a task specification")
    submit.add_argument("--task", required=True, help="JSON file or JSON object")
    submit.add_argument("--planned", action="store_true", help="leave run in planned state")

    stream = sub.add_parser("stream", help="stream normalized events")
    stream.add_argument("run_id")
    stream.add_argument("--after-seq", type=int, default=0)

    for name in ("pause", "resume", "cancel", "snapshot", "result", "status", "artifacts", "restore"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
        if name in {"resume", "restore"}:
            command.add_argument("--checkpoint-id")

    complete = sub.add_parser("complete", help="complete a run with a result")
    complete.add_argument("run_id")
    complete.add_argument("--result", default="{}", help="JSON object or JSON file")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runner = _runner(args)
    try:
        if args.command == "submit":
            run_id = runner.submit(TaskSpec.from_mapping(_json_arg(args.task)), start=not args.planned)
            output: Any = {"run_id": run_id, "state": runner.status(run_id)["state"]}
        elif args.command == "stream":
            cursor, events = runner.stream_events(args.run_id, args.after_seq)
            output = {"cursor": cursor, "events": events}
        elif args.command == "pause":
            output = {"run_id": args.run_id, "checkpoint_id": runner.pause(args.run_id), "state": runner.status(args.run_id)["state"]}
        elif args.command == "resume":
            output = runner.resume(args.run_id, args.checkpoint_id)
        elif args.command == "restore":
            if not args.checkpoint_id:
                raise ValueError("restore requires --checkpoint-id")
            output = runner.restore(args.run_id, args.checkpoint_id)
        elif args.command == "cancel":
            output = runner.cancel(args.run_id)
        elif args.command == "snapshot":
            output = {"run_id": args.run_id, "checkpoint_id": runner.snapshot(args.run_id)}
        elif args.command == "result":
            output = runner.result(args.run_id)
        elif args.command == "status":
            output = runner.status(args.run_id)
        elif args.command == "artifacts":
            output = {"run_id": args.run_id, "artifacts": runner.artifacts(args.run_id)}
        elif args.command == "complete":
            output = runner.complete(args.run_id, _json_arg(args.result))
        else:  # pragma: no cover - argparse enforces this
            parser.error(f"unknown command: {args.command}")
    except (KeyError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
