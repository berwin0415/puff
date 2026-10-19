"""SQLite and JSONL persistence used by the local validation runner.

SQLite is the queryable source of truth for the MVP.  Each event is also
appended to the run's JSONL file so that a run can be copied and inspected
without the database.  The implementation deliberately has no framework or
adapter dependencies.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any, Iterator, Mapping, Sequence
import uuid

from contracts.adapter import Event, RunState, TaskSpec


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteEventStore:
    """Persist task state, checkpoints, normalized events and run manifests."""

    def __init__(self, db_path: str | Path = "var/state/runner.sqlite3", artifact_root: str | Path = "var/artifacts") -> None:
        self.db_path = Path(db_path)
        self.artifact_root = Path(artifact_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if str(self.db_path) == ":memory:":
            # Keep one connection alive: a new sqlite3 connection would create
            # a fresh in-memory database for every operation.
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
            self._memory_connection.row_factory = sqlite3.Row
            self._memory_connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            yield self._memory_connection
            return
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    candidate TEXT,
                    candidate_version TEXT,
                    state TEXT NOT NULL,
                    checkpoint_id TEXT,
                    result_json TEXT,
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    event_seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, event_seq)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    checkpoint_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_time ON events(run_id, occurred_at);
                """
            )

    def run_dir(self, run_id: str) -> Path:
        path = self.artifact_root / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "state").mkdir(exist_ok=True)
        (path / "artifacts").mkdir(exist_ok=True)
        return path

    def create_run(
        self,
        run_id: str,
        task: TaskSpec | Mapping[str, Any],
        *,
        candidate: str = "unassigned",
        candidate_version: str = "unknown",
    ) -> dict[str, Any]:
        task_spec = task if isinstance(task, TaskSpec) else TaskSpec.from_mapping(task)
        task_json = task_spec.to_mapping()
        timestamp = utc_now()
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """INSERT INTO runs
                    (run_id, task_id, task_json, candidate, candidate_version, state,
                     checkpoint_id, result_json, artifacts_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '[]', ?, ?)""",
                    (
                        run_id,
                        task_spec.task_id,
                        _json(task_json),
                        candidate,
                        candidate_version,
                        RunState.PLANNED.value,
                        timestamp,
                        timestamp,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        self.run_dir(run_id)
        self.write_manifest(run_id)
        self.append_event(
            run_id,
            event_type="run_submitted",
            state=RunState.PLANNED.value,
            payload={"task_id": task_spec.task_id},
        )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        result = dict(row)
        result["task"] = json.loads(result.pop("task_json"))
        result["result"] = json.loads(result.pop("result_json")) if result["result_json"] else None
        result["artifacts"] = json.loads(result.pop("artifacts_json"))
        return result

    def update_state(self, run_id: str, state: str, *, expected: Sequence[str] | None = None) -> dict[str, Any]:
        timestamp = utc_now()
        with self._lock, self._connection() as db:
            row = db.execute("SELECT state FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown run_id: {run_id}")
            if expected and row["state"] not in expected:
                raise ValueError(f"run {run_id} is {row['state']}, expected one of {list(expected)}")
            db.execute("UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?", (state, timestamp, run_id))
        return self.get_run(run_id)

    def update_checkpoint(self, run_id: str, checkpoint_id: str | None) -> None:
        with self._lock, self._connection() as db:
            db.execute("UPDATE runs SET checkpoint_id = ?, updated_at = ? WHERE run_id = ?", (checkpoint_id, utc_now(), run_id))

    def set_result(self, run_id: str, result: Mapping[str, Any] | None) -> None:
        with self._lock, self._connection() as db:
            db.execute("UPDATE runs SET result_json = ?, updated_at = ? WHERE run_id = ?", (_json(result) if result is not None else None, utc_now(), run_id))
        self.write_manifest(run_id)

    def set_artifacts(self, run_id: str, artifacts: Sequence[Mapping[str, Any]]) -> None:
        with self._lock, self._connection() as db:
            db.execute("UPDATE runs SET artifacts_json = ?, updated_at = ? WHERE run_id = ?", (_json(list(artifacts)), utc_now(), run_id))
        self.write_manifest(run_id)

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        state: str,
        previous_state: str | None = None,
        payload: Mapping[str, Any] | None = None,
        candidate: str | None = None,
        candidate_version: str | None = None,
        parent_event_id: str | None = None,
        candidate_event_id: str | None = None,
        tool_call: Mapping[str, Any] | None = None,
        model_call: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> Event:
        with self._lock, self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT candidate, candidate_version FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown run_id: {run_id}")
                latest = db.execute("SELECT COALESCE(MAX(event_seq), 0) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]
                event = Event(
                    run_id=run_id,
                    event_seq=int(latest) + 1,
                    event_type=event_type,
                    state=state,
                    previous_state=previous_state,
                    candidate=candidate if candidate is not None else row["candidate"],
                    candidate_version=candidate_version if candidate_version is not None else row["candidate_version"],
                    parent_event_id=parent_event_id,
                    candidate_event_id=candidate_event_id,
                    payload=dict(payload or {}),
                    tool_call=tool_call,
                    model_call=model_call,
                    error=error,
                )
                encoded = _json(event.to_mapping())
                db.execute(
                    "INSERT INTO events(run_id, event_seq, event_id, event_json, occurred_at) VALUES (?, ?, ?, ?, ?)",
                    (run_id, event.event_seq, event.event_id, encoded, event.occurred_at),
                )
                db.execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", (event.occurred_at, run_id))
                db.commit()
            except Exception:
                db.rollback()
                raise
        path = self.run_dir(run_id) / "events.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        self.write_manifest(run_id)
        return event

    def stream_events(self, run_id: str, after_seq: int | None = 0) -> tuple[int, list[Event]]:
        after_seq = 0 if after_seq is None else after_seq
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT event_json FROM events WHERE run_id = ? AND event_seq > ? ORDER BY event_seq ASC",
                (run_id, after_seq),
            ).fetchall()
        if not rows:
            self.get_run(run_id)  # preserve unknown-run error semantics
            return after_seq, []
        events = [Event.from_mapping(json.loads(row["event_json"])) for row in rows]
        return events[-1].event_seq, events

    def write_checkpoint(self, run_id: str, checkpoint: Mapping[str, Any]) -> str:
        checkpoint_id = str(uuid.uuid4())
        created_at = utc_now()
        data = {"checkpoint_id": checkpoint_id, "run_id": run_id, "created_at": created_at, **dict(checkpoint)}
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO checkpoints(checkpoint_id, run_id, checkpoint_json, created_at) VALUES (?, ?, ?, ?)",
                (checkpoint_id, run_id, _json(data), created_at),
            )
        run_dir = self.run_dir(run_id)
        self._atomic_json(run_dir / "state" / f"{checkpoint_id}.json", data)
        self.update_checkpoint(run_id, checkpoint_id)
        self.write_manifest(run_id)
        return checkpoint_id

    def read_checkpoint(self, run_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        if checkpoint_id is None:
            checkpoint_id = self.get_run(run_id).get("checkpoint_id")
        if not checkpoint_id:
            raise KeyError(f"run {run_id} has no checkpoint")
        with self._lock, self._connection() as db:
            row = db.execute("SELECT checkpoint_json FROM checkpoints WHERE checkpoint_id = ? AND run_id = ?", (checkpoint_id, run_id)).fetchone()
        if row is None:
            raise KeyError(f"unknown checkpoint_id: {checkpoint_id}")
        return json.loads(row["checkpoint_json"])

    def write_manifest(self, run_id: str) -> Path:
        run = self.get_run(run_id)
        manifest = {
            "run_id": run["run_id"],
            "task_id": run["task_id"],
            "candidate": run["candidate"],
            "candidate_version": run["candidate_version"],
            "state": run["state"],
            "checkpoint_id": run["checkpoint_id"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "task": run["task"],
            "result": run["result"],
            "artifacts": run["artifacts"],
            "evidence": {"events": "events.jsonl", "state": "state/", "artifacts": "artifacts/"},
        }
        path = self.run_dir(run_id) / "manifest.json"
        self._atomic_json(path, manifest)
        return path

    @staticmethod
    def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        temporary.replace(path)


Storage = SQLiteEventStore
