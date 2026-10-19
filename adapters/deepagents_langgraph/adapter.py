"""Thin subprocess adapter for Deep Agents + LangGraph.

The candidate packages stay outside this repository.  The adapter launches a
worker with the candidate virtualenv's Python executable, captures JSONL event
summaries, and exposes the common read-side protocol.  Checkpoint inspection is
supported through the candidate SQLite checkpointer.  Native pause/cancel and
mid-run resume still require a streaming worker transport; the SQLite
checkpoints are nevertheless retained for later resume experiments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence
import uuid


DEFAULT_DEEPAGENTS_SOURCE = "~/harness-lab-sources/deepagents"
DEFAULT_LANGGRAPH_SOURCE = "~/harness-lab-sources/langgraph"
DEFAULT_DEEPAGENTS_COMMIT = "0089e635dce525055272883b53bc222f385beac1"
DEFAULT_LANGGRAPH_COMMIT = "98043c37abc60374030f960a03a6e0d4c0112643"
DEFAULT_DEEPAGENTS_ARCHIVE_SHA256 = "2b0cb8cd05f7faeb8e8745845958056a9e974658fc85f4682643874c99d8a548"
DEFAULT_CANDIDATE = "deepagents_langgraph"


class DeepAgentsLangGraphError(RuntimeError):
    """Base adapter error."""


class SourcePreflightError(DeepAgentsLangGraphError):
    """Raised when an external source cannot be verified."""


class CheckpointInspectionError(DeepAgentsLangGraphError):
    """Raised when a candidate checkpoint cannot be read."""


@dataclass(frozen=True)
class SourcePreflight:
    deepagents_path: Path
    langgraph_path: Path
    deepagents_commit: str | None
    langgraph_commit: str | None
    deepagents_source_kind: str
    deepagents_archive_path: Path | None
    deepagents_archive_sha256: str | None
    deepagents_version: str | None
    langgraph_version: str | None
    ok: bool
    reasons: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "deepagents_path": str(self.deepagents_path),
            "langgraph_path": str(self.langgraph_path),
            "deepagents_commit": self.deepagents_commit,
            "langgraph_commit": self.langgraph_commit,
            "deepagents_source_kind": self.deepagents_source_kind,
            "deepagents_archive_path": str(self.deepagents_archive_path) if self.deepagents_archive_path else None,
            "deepagents_archive_sha256": self.deepagents_archive_sha256,
            "deepagents_version": self.deepagents_version,
            "langgraph_version": self.langgraph_version,
            "ok": self.ok,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RunResult:
    run_id: str
    returncode: int
    stdout: str
    stderr: str
    events: tuple[Mapping[str, Any], ...]
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass
class _RecordedRun:
    task: Mapping[str, Any]
    events: list[Mapping[str, Any]] = field(default_factory=list)
    result: RunResult | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(run_id: str, event_seq: int, event_type: str, state: str, payload: Mapping[str, Any] | None = None, *, error: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "run_id": run_id,
        "event_seq": event_seq,
        "occurred_at": _now(),
        "candidate": DEFAULT_CANDIDATE,
        "candidate_version": f"deepagents@{DEFAULT_DEEPAGENTS_COMMIT};langgraph@{DEFAULT_LANGGRAPH_COMMIT}",
        "event_type": event_type,
        "previous_state": None,
        "state": state,
        "parent_event_id": None,
        "candidate_event_id": f"dal-{event_seq}-{run_id}",
        "tool_call": None,
        "model_call": None,
        "error": dict(error) if error else None,
        "payload": dict(payload or {}),
    }


def collect_events(run_id: str, stdout: str, stderr: str, returncode: int, *, timed_out: bool = False) -> tuple[Mapping[str, Any], ...]:
    """Map worker JSONL output into normalized events."""

    events: list[Mapping[str, Any]] = [_event(run_id, 1, "process_started", "running", {"transport": "subprocess"})]
    sequence = 2
    for line in stdout.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping) and parsed.get("event_type"):
            event_type = str(parsed["event_type"])
            state = "completed" if event_type == "graph_completed" and returncode == 0 else "running"
            if event_type in {"interrupt_requested", "graph_interrupted", "pause_requested", "graph_paused"}:
                state = "paused"
            if event_type == "worker_error":
                state = "aborted"
            events.append(_event(run_id, sequence, event_type, state, parsed.get("payload") if isinstance(parsed.get("payload"), Mapping) else {"raw": line}))
        else:
            events.append(_event(run_id, sequence, "worker_output", "running", {"text": line[:2000]}))
        sequence += 1
    if stderr:
        for line in stderr.splitlines():
            events.append(_event(run_id, sequence, "worker_stderr", "running", {"text": line[:2000]}))
            sequence += 1
    interrupted = any(event["event_type"] in {"graph_interrupted", "graph_paused"} for event in events)
    if returncode != 0 or timed_out:
        final_state = "aborted"
    elif interrupted:
        final_state = "paused"
    else:
        final_state = "completed"
    events.append(
        _event(
            run_id,
            sequence,
            "process_exited",
            final_state,
            {"returncode": returncode, "timed_out": timed_out},
            error=(
                {"category": "timeout", "message": "candidate worker timed out"}
                if timed_out
                else ({"category": "process_exit", "message": f"exit code {returncode}"} if returncode else None)
            ),
        )
    )
    return tuple(events)


class DeepAgentsLangGraphAdapter:
    """Run one candidate graph in an external Python environment."""

    name = DEFAULT_CANDIDATE

    def __init__(
        self,
        *,
        source_path: str | Path = DEFAULT_DEEPAGENTS_SOURCE,
        langgraph_source_path: str | Path = DEFAULT_LANGGRAPH_SOURCE,
        python_executable: str | Path | None = None,
        expected_deepagents_commit: str | None = DEFAULT_DEEPAGENTS_COMMIT,
        expected_langgraph_commit: str | None = DEFAULT_LANGGRAPH_COMMIT,
        deepagents_archive_path: str | Path | None = None,
        expected_deepagents_archive_sha256: str | None = DEFAULT_DEEPAGENTS_ARCHIVE_SHA256,
        worker_path: str | Path | None = None,
        artifact_root: str | Path | None = None,
        checkpoint_db: str | Path | None = None,
        require_clean_langgraph: bool = True,
    ) -> None:
        self.source_path = Path(source_path).expanduser().resolve()
        self.langgraph_source_path = Path(langgraph_source_path).expanduser().resolve()
        default_python = self.source_path / "libs/deepagents/.venv/bin/python"
        # Keep the venv's ``bin/python`` symlink intact.  Resolving it to the
        # uv-managed interpreter drops the venv prefix and therefore loses the
        # editable Deep Agents installation from ``sys.path``.
        self.python_executable = Path(python_executable or default_python).expanduser()
        if not self.python_executable.is_absolute():
            self.python_executable = self.python_executable.absolute()
        self.expected_deepagents_commit = expected_deepagents_commit
        self.expected_langgraph_commit = expected_langgraph_commit
        default_archive = self.source_path.parent / f"deepagents-{expected_deepagents_commit}.tar.gz" if expected_deepagents_commit else None
        self.deepagents_archive_path = Path(deepagents_archive_path).expanduser() if deepagents_archive_path else default_archive
        if self.deepagents_archive_path is not None and not self.deepagents_archive_path.is_absolute():
            self.deepagents_archive_path = self.deepagents_archive_path.absolute()
        self.expected_deepagents_archive_sha256 = expected_deepagents_archive_sha256
        self.worker_path = Path(worker_path or Path(__file__).with_name("worker.py")).resolve()
        self.artifact_root = Path(artifact_root or os.environ.get("HARNESS_LAB_ARTIFACT_ROOT", "~/harness-lab-data/runs")).expanduser().resolve()
        self.checkpoint_db = Path(checkpoint_db).expanduser() if checkpoint_db is not None else None
        self.require_clean_langgraph = require_clean_langgraph
        self.version = f"deepagents@{expected_deepagents_commit};langgraph@{expected_langgraph_commit}"
        self._runs: dict[str, _RecordedRun] = {}

    def preflight(self, *, raise_on_error: bool = True) -> SourcePreflight:
        reasons: list[str] = []
        deep_pyproject = self.source_path / "libs/deepagents/pyproject.toml"
        lang_pyproject = self.langgraph_source_path / "libs/langgraph/pyproject.toml"
        deep_license = self.source_path / "LICENSE"
        lang_license = self.langgraph_source_path / "LICENSE"
        if not deep_pyproject.exists() or not deep_license.exists():
            reasons.append("Deep Agents source requires libs/deepagents/pyproject.toml and LICENSE")
        if not lang_pyproject.exists() or not lang_license.exists():
            reasons.append("LangGraph source requires libs/langgraph/pyproject.toml and LICENSE")
        if not self.python_executable.exists():
            reasons.append(f"candidate Python does not exist: {self.python_executable}")
        actual_langgraph: str | None = None
        if (self.langgraph_source_path / ".git").exists():
            try:
                actual_langgraph = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.langgraph_source_path, check=True, capture_output=True, text=True).stdout.strip()
                dirty = subprocess.run(["git", "status", "--porcelain"], cwd=self.langgraph_source_path, check=True, capture_output=True, text=True).stdout.strip()
                if self.require_clean_langgraph and dirty:
                    reasons.append("LangGraph checkout has uncommitted changes")
            except (OSError, subprocess.CalledProcessError) as exc:
                reasons.append(f"LangGraph git preflight failed: {exc}")
        else:
            reasons.append("LangGraph source is not a Git checkout")
        if self.expected_langgraph_commit and actual_langgraph != self.expected_langgraph_commit:
            reasons.append(f"LangGraph HEAD {actual_langgraph!r} does not match expected {self.expected_langgraph_commit}")
        deep_version = _project_version(deep_pyproject)
        lang_version = _project_version(lang_pyproject)
        archive_sha256: str | None = None
        if self.deepagents_archive_path and self.deepagents_archive_path.exists():
            archive_sha256 = _sha256(self.deepagents_archive_path)
            if self.expected_deepagents_archive_sha256 and archive_sha256 != self.expected_deepagents_archive_sha256:
                reasons.append(
                    f"Deep Agents archive SHA-256 {archive_sha256} does not match expected {self.expected_deepagents_archive_sha256}"
                )
        report = SourcePreflight(
            self.source_path,
            self.langgraph_source_path,
            self.expected_deepagents_commit,
            actual_langgraph,
            "github-archive",
            self.deepagents_archive_path if self.deepagents_archive_path and self.deepagents_archive_path.exists() else None,
            archive_sha256,
            deep_version,
            lang_version,
            not reasons,
            tuple(reasons),
        )
        if raise_on_error and not report.ok:
            raise SourcePreflightError("; ".join(report.reasons))
        return report

    check_source = preflight

    def submit(self, task_spec: Mapping[str, Any]) -> str:
        prompt = task_spec.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = str(uuid.uuid4())
        self._runs[run_id] = _RecordedRun(dict(task_spec), [_event(run_id, 1, "run_queued", "planned", {"checkpoint_backend": "sqlite"})])
        return run_id

    def run_once(self, task_spec: Mapping[str, Any] | str, *, run_id: str | None = None, timeout: float | None = None, env: Mapping[str, str] | None = None) -> RunResult:
        task = {"prompt": task_spec} if isinstance(task_spec, str) else dict(task_spec)
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = run_id or str(uuid.uuid4())
        request = {"run_id": run_id, "prompt": prompt, "checkpoint_db": str(self._checkpoint_db()), "timeout_seconds": timeout or task.get("timeout_seconds") or 120}
        process_env = os.environ.copy()
        if env:
            process_env.update({str(key): str(value) for key, value in env.items()})
        try:
            completed = subprocess.run([str(self.python_executable), str(self.worker_path), "--request", json.dumps(request, ensure_ascii=False)], cwd=self.worker_path.parent, env=process_env, capture_output=True, text=True, timeout=timeout or task.get("timeout_seconds") or 120, check=False)
            result = RunResult(run_id, completed.returncode, completed.stdout, completed.stderr, collect_events(run_id, completed.stdout, completed.stderr, completed.returncode), False)
        except subprocess.TimeoutExpired as exc:
            stdout = _decode(exc.stdout)
            stderr = _decode(exc.stderr)
            result = RunResult(run_id, -9, stdout, stderr, collect_events(run_id, stdout, stderr, -9, timed_out=True), True)
        self._runs.setdefault(run_id, _RecordedRun(task)).result = result
        self._runs[run_id].events = list(result.events)
        return result

    def run_interruptible(
        self,
        task_spec: Mapping[str, Any] | str,
        *,
        run_id: str | None = None,
        interrupt_on: Sequence[str] = ("fixture_lookup",),
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Start a graph that pauses natively before the listed tools execute."""

        task = {"prompt": task_spec} if isinstance(task_spec, str) else dict(task_spec)
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = run_id or str(uuid.uuid4())
        request = {
            "mode": "run",
            "run_id": run_id,
            "prompt": prompt,
            "checkpoint_db": str(self._checkpoint_db()),
            "interrupt_on": list(interrupt_on),
            "timeout_seconds": timeout or task.get("timeout_seconds") or 120,
        }
        return self._execute_worker(request, run_id, task, timeout, env)

    def run_paused(
        self,
        task_spec: Mapping[str, Any] | str,
        *,
        run_id: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Start a graph that pauses at the next model-node safe point.

        No approval is involved.  The graph checkpoints after the model turn,
        the process may exit, and ``resume_paused`` continues the same thread.
        """

        task = {"prompt": task_spec} if isinstance(task_spec, str) else dict(task_spec)
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = run_id or str(uuid.uuid4())
        request = {
            "mode": "run",
            "run_id": run_id,
            "prompt": prompt,
            "checkpoint_db": str(self._checkpoint_db()),
            "pause_after_model": True,
            "timeout_seconds": timeout or task.get("timeout_seconds") or 120,
        }
        return self._execute_worker(request, run_id, task, timeout, env)

    def resume_paused(
        self,
        run_id: str,
        *,
        checkpoint_id: str | None = None,
        pause_after_model: bool = False,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Resume a non-approval model-safe-point pause.

        With ``pause_after_model=False`` (the default) the graph runs to
        completion.  Set it to true to stop again at the next model boundary.
        """

        request = {
            "mode": "resume",
            "run_id": run_id,
            "checkpoint_db": str(self._checkpoint_db()),
            "checkpoint_id": checkpoint_id,
            "resume": True,
            "pause_after_model": pause_after_model,
            "timeout_seconds": timeout or 120,
        }
        return self._execute_worker(request, run_id, {"run_id": run_id}, timeout, env)

    def resume_run(
        self,
        run_id: str,
        *,
        decision: str = "approve",
        checkpoint_id: str | None = None,
        interrupt_on: Sequence[str] = ("fixture_lookup",),
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Resume an interrupted thread from its SQLite checkpoint.

        A new adapter instance or process can call this method because the only
        shared state is the checkpoint database and ``thread_id``.
        """

        request = {
            "mode": "resume",
            "run_id": run_id,
            "checkpoint_db": str(self._checkpoint_db()),
            "checkpoint_id": checkpoint_id,
            "interrupt_on": list(interrupt_on),
            "resume": {"decisions": [{"type": decision}]},
            "timeout_seconds": timeout or 120,
        }
        return self._execute_worker(request, run_id, {"run_id": run_id}, timeout, env)

    def _execute_worker(
        self,
        request: Mapping[str, Any],
        run_id: str,
        task: Mapping[str, Any],
        timeout: float | None,
        env: Mapping[str, str] | None,
    ) -> RunResult:
        process_env = os.environ.copy()
        if env:
            process_env.update({str(key): str(value) for key, value in env.items()})
        limit = timeout or float(request.get("timeout_seconds") or 120)
        try:
            completed = subprocess.run(
                [str(self.python_executable), str(self.worker_path), "--request", json.dumps(dict(request), ensure_ascii=False)],
                cwd=self.worker_path.parent,
                env=process_env,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
            result = RunResult(
                run_id,
                completed.returncode,
                completed.stdout,
                completed.stderr,
                collect_events(run_id, completed.stdout, completed.stderr, completed.returncode),
                False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _decode(exc.stdout)
            stderr = _decode(exc.stderr)
            result = RunResult(run_id, -9, stdout, stderr, collect_events(run_id, stdout, stderr, -9, timed_out=True), True)
        record = self._runs.setdefault(run_id, _RecordedRun(dict(task)))
        record.result = result
        record.events = list(result.events)
        return result

    def stream_events(self, run_id: str, after_seq: int = 0) -> tuple[int, Iterable[Mapping[str, Any]]]:
        record = self._runs[run_id]
        events = tuple(event for event in record.events if int(event["event_seq"]) > after_seq)
        cursor = int(record.events[-1]["event_seq"]) if record.events else after_seq
        return cursor, events

    def result(self, run_id: str) -> Mapping[str, Any] | None:
        record = self._runs[run_id]
        if record.result is None:
            return None
        return {"ok": record.result.ok, "returncode": record.result.returncode, "timed_out": record.result.timed_out}

    def artifacts(self, run_id: str) -> Sequence[Mapping[str, Any]]:
        if run_id not in self._runs:
            raise KeyError(run_id)
        return ({"kind": "checkpoint_db", "path": str(self._checkpoint_db())},)

    def inspect_checkpoint(self, run_id: str, *, timeout: float = 30) -> Mapping[str, Any]:
        """Read the latest candidate checkpoint for *run_id* without a model call."""

        request = {"mode": "inspect", "run_id": run_id, "checkpoint_db": str(self._checkpoint_db())}
        try:
            completed = subprocess.run(
                [str(self.python_executable), str(self.worker_path), "--request", json.dumps(request, ensure_ascii=False)],
                cwd=self.worker_path.parent,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckpointInspectionError(f"checkpoint inspection failed: {exc}") from exc
        for line in completed.stdout.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping) and parsed.get("event_type") == "checkpoint_inspected":
                payload = parsed.get("payload")
                if isinstance(payload, Mapping):
                    return payload
        raise CheckpointInspectionError(
            f"no checkpoint found for {run_id!r} (returncode={completed.returncode}, stderr={completed.stderr[-500:]!r})"
        )

    def snapshot(self, run_id: str) -> str:
        payload = self.inspect_checkpoint(run_id)
        checkpoint_id = payload.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise CheckpointInspectionError(f"checkpoint for {run_id!r} has no checkpoint_id")
        return checkpoint_id

    def pause(self, run_id: str) -> str:
        raise NotImplementedError("worker is one-shot; native pause requires a live streaming transport")

    def resume(self, run_id: str, checkpoint_id: str | None = None) -> None:
        raise NotImplementedError("resume transport is not wired; SQLite checkpoint is retained for the next worker")

    def restore(self, run_id: str, checkpoint_id: str) -> None:
        raise NotImplementedError("restore transport is not wired")

    def cancel(self, run_id: str) -> None:
        raise NotImplementedError("one-shot worker cancellation is not wired")

    def send_input(self, run_id: str, message: str) -> None:
        raise NotImplementedError("worker accepts one prompt")

    def approve(self, run_id: str, approval_id: str, decision: str) -> None:
        raise NotImplementedError("approval transport is not wired")

    def _checkpoint_db(self) -> Path:
        if self.checkpoint_db is not None:
            return self.checkpoint_db
        return self.artifact_root / "candidate-state" / "deepagents-langgraph.sqlite"


def _project_version(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("version ="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


__all__ = [
    "CheckpointInspectionError",
    "DeepAgentsLangGraphAdapter",
    "DeepAgentsLangGraphError",
    "SourcePreflight",
    "SourcePreflightError",
    "collect_events",
]
