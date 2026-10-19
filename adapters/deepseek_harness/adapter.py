"""A thin, framework-neutral adapter for DeepSeek Harness headless runs.

The adapter deliberately keeps the candidate process at arm's length.  It
checks the external checkout and pinned commit, constructs the documented
``dsh --profile headless`` invocation, and can optionally run one invocation
as a subprocess.  The runner owns lifecycle state; this module only records
the candidate process output and maps it to the lab event shape.

No model credential is accepted as a constructor argument or inserted into a
command line.  The subprocess inherits the caller's environment (plus
explicit, non-secret overrides), so a local ``.env`` or secret manager stays
outside the repository and outside persisted artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Iterable, Mapping, Sequence
import uuid


DEFAULT_SOURCE_PATH = "../deepseek-harness"
DEFAULT_PROFILE = "headless"
DEFAULT_CANDIDATE = "deepseek_harness"


class DeepSeekHarnessError(RuntimeError):
    """Base error for adapter configuration and execution failures."""


class SourcePreflightError(DeepSeekHarnessError):
    """Raised when the external DeepSeek Harness checkout cannot be trusted."""


@dataclass(frozen=True)
class SourcePreflight:
    """Evidence collected from the external candidate checkout."""

    path: Path
    expected_commit: str | None
    actual_commit: str | None
    clean: bool | None
    ok: bool
    reason: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "expected_commit": self.expected_commit,
            "actual_commit": self.actual_commit,
            "clean": self.clean,
            "ok": self.ok,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HeadlessRunResult:
    """Captured result from one optional headless subprocess invocation."""

    run_id: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    events: tuple[Mapping[str, Any], ...]
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "events": [dict(event) for event in self.events],
            "timed_out": self.timed_out,
            "ok": self.ok,
        }


@dataclass
class _RecordedRun:
    task: Mapping[str, Any]
    result: HeadlessRunResult | None = None
    events: list[Mapping[str, Any]] = field(default_factory=list)
    checkpoint_id: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(
    *,
    run_id: str,
    event_seq: int,
    event_type: str,
    state: str,
    payload: Mapping[str, Any] | None = None,
    candidate_event_id: str | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a serialisable normalized event without depending on Runner."""

    return {
        "event_id": str(uuid.uuid4()),
        "run_id": run_id,
        "event_seq": event_seq,
        "occurred_at": _now(),
        "candidate": DEFAULT_CANDIDATE,
        "candidate_version": None,
        "event_type": event_type,
        "previous_state": None,
        "state": state,
        "parent_event_id": None,
        "candidate_event_id": candidate_event_id,
        "tool_call": None,
        "model_call": None,
        "error": dict(error) if error else None,
        "payload": dict(payload or {}),
    }


def _candidate_line_event(
    *,
    run_id: str,
    event_seq: int,
    stream: str,
    line: str,
) -> dict[str, Any]:
    """Map one candidate output line to an event.

    DeepSeek's headless profile writes the final answer to stdout and progress
    (including reasoning) to stderr.  Some future profile may emit JSON lines;
    those are preserved as candidate events when they carry an explicit
    ``type``/``event_type`` field, without guessing at arbitrary JSON output.
    """

    stripped = line.rstrip("\r\n")
    candidate_event_id = f"dsh-{stream}-{event_seq}"
    parsed: Mapping[str, Any] | None = None
    if stripped.startswith("{"):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, Mapping) and ("event_type" in value or "type" in value or "event" in value):
            parsed = value

    if parsed is not None:
        event_type = str(parsed.get("event_type") or parsed.get("type") or parsed.get("event"))
        payload = parsed.get("payload")
        if not isinstance(payload, Mapping):
            payload = {key: value for key, value in parsed.items() if key not in {"event_type", "type", "event"}}
        return _event(
            run_id=run_id,
            event_seq=event_seq,
            event_type=f"candidate/{event_type}",
            state="running",
            candidate_event_id=str(parsed.get("event_id") or candidate_event_id),
            payload={"stream": stream, "raw": stripped, **dict(payload)},
        )

    if stream == "stdout":
        event_type = "assistant_output"
    elif stripped.startswith("dsh: reasoning:"):
        event_type = "reasoning_started"
    elif stripped.startswith("dsh:"):
        event_type = "candidate_error"
    else:
        event_type = "stderr_output"
    error = {"message": stripped, "source": "deepseek_harness"} if event_type == "candidate_error" else None
    return _event(
        run_id=run_id,
        event_seq=event_seq,
        event_type=event_type,
        state="running",
        candidate_event_id=candidate_event_id,
        payload={"stream": stream, "text": stripped},
        error=error,
    )


def collect_events(
    run_id: str,
    stdout: str,
    stderr: str,
    returncode: int,
    *,
    timed_out: bool = False,
    candidate_version: str | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Collect deterministic normalized events from captured process output.

    ``subprocess`` exposes stdout and stderr independently, so exact
    cross-stream ordering is unavailable after the process exits.  We retain
    stream identity and use stdout-then-stderr order for a stable offline
    record; live transports can replace this function when ordering matters.
    """

    events: list[Mapping[str, Any]] = [
        _event(
            run_id=run_id,
            event_seq=1,
            event_type="process_started",
            state="running",
            candidate_event_id=f"dsh-process-{run_id}",
            payload={"transport": "subprocess"},
        )
    ]
    seq = 2
    for stream, content in (("stdout", stdout), ("stderr", stderr)):
        for line in content.splitlines():
            events.append(_candidate_line_event(run_id=run_id, event_seq=seq, stream=stream, line=line))
            seq += 1
    final_state = "completed" if returncode == 0 and not timed_out else "aborted"
    events.append(
        _event(
            run_id=run_id,
            event_seq=seq,
            event_type="process_exited",
            state=final_state,
            candidate_event_id=f"dsh-process-exit-{run_id}",
            payload={"returncode": returncode, "timed_out": timed_out},
            error=(
                {"category": "timeout", "message": "headless subprocess timed out"}
                if timed_out
                else ({"category": "process_exit", "message": f"exit code {returncode}"} if returncode != 0 else None)
            ),
        )
    )
    if candidate_version is not None:
        events = [dict(event, candidate_version=candidate_version) for event in events]
    return tuple(events)


class DeepSeekHarnessAdapter:
    """Adapter for the local DeepSeek Harness headless profile.

    The adapter is intentionally one-shot.  ``execute_on_submit=False`` keeps
    ``submit`` side-effect free for protocol/lifecycle tests; callers can use
    :meth:`run_once` explicitly, or opt into execution on submit for a local
    smoke run.  Native pause/resume is not exposed by DeepSeek's one-shot CLI.
    """

    name = DEFAULT_CANDIDATE

    def __init__(
        self,
        *,
        source_path: str | Path = DEFAULT_SOURCE_PATH,
        expected_commit: str | None = None,
        command: str | Sequence[str] = ("pnpm", "dsh"),
        profile: str = DEFAULT_PROFILE,
        execute_on_submit: bool = False,
        require_clean_source: bool = True,
    ) -> None:
        self.source_path = Path(source_path).expanduser().resolve()
        self.expected_commit = expected_commit
        self.command = tuple(shlex.split(command) if isinstance(command, str) else command)
        if not self.command or any(not isinstance(part, str) or not part for part in self.command):
            raise ValueError("command must contain a non-empty executable")
        self.profile = profile
        self.execute_on_submit = execute_on_submit
        self.require_clean_source = require_clean_source
        self.version = expected_commit or "un-pinned"
        self._runs: dict[str, _RecordedRun] = {}

    def preflight(self, *, raise_on_error: bool = True) -> SourcePreflight:
        """Verify checkout existence, Git revision, and optional cleanliness."""

        path = self.source_path
        if not path.exists():
            report = SourcePreflight(path, self.expected_commit, None, None, False, "source path does not exist")
            return self._finish_preflight(report, raise_on_error)
        if not (path / ".git").exists():
            report = SourcePreflight(path, self.expected_commit, None, None, False, "source path is not a Git checkout")
            return self._finish_preflight(report, raise_on_error)
        try:
            actual = self._git("rev-parse", "HEAD").strip()
            status = self._git("status", "--porcelain")
        except (OSError, subprocess.CalledProcessError) as exc:
            report = SourcePreflight(path, self.expected_commit, None, None, False, f"git preflight failed: {exc}")
            return self._finish_preflight(report, raise_on_error)
        clean = status.strip() == ""
        reasons: list[str] = []
        if self.expected_commit and actual != self.expected_commit:
            reasons.append(f"HEAD {actual} does not match expected commit {self.expected_commit}")
        if self.require_clean_source and not clean:
            reasons.append("source checkout has uncommitted changes")
        report = SourcePreflight(path, self.expected_commit, actual, clean, not reasons, "; ".join(reasons) or None)
        return self._finish_preflight(report, raise_on_error)

    def _finish_preflight(self, report: SourcePreflight, raise_on_error: bool) -> SourcePreflight:
        if raise_on_error and not report.ok:
            raise SourcePreflightError(report.reason or "source preflight failed")
        return report

    # Keep both names discoverable for callers that use "check" terminology
    # for preflight gates.  They intentionally share the same implementation.
    check_source = preflight

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.source_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    def build_headless_command(
        self,
        prompt: str,
        *,
        extra_args: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Build the candidate's one-shot command without credentials."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        return (*self.command, "--profile", self.profile, *tuple(extra_args), prompt)

    headless_command = build_headless_command

    def submit(self, task_spec: Mapping[str, Any]) -> str:
        """Allocate a candidate run ID and optionally execute it immediately."""

        prompt = task_spec.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = str(uuid.uuid4())
        record = _RecordedRun(task=dict(task_spec))
        self._runs[run_id] = record
        if self.execute_on_submit:
            self.run_once(task_spec, run_id=run_id)
        else:
            queued = _event(
                run_id=run_id,
                event_seq=1,
                event_type="run_queued",
                state="planned",
                candidate_event_id=f"dsh-queued-{run_id}",
                payload={"profile": self.profile},
            )
            queued["candidate_version"] = self.version
            record.events.append(queued)
        return run_id

    def run_once(
        self,
        task_spec: Mapping[str, Any] | str,
        *,
        run_id: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        cwd: str | Path | None = None,
        extra_args: Sequence[str] = (),
    ) -> HeadlessRunResult:
        """Run one headless task and collect stdout/stderr into events."""

        if isinstance(task_spec, str):
            prompt = task_spec
            task: Mapping[str, Any] = {"prompt": task_spec}
        else:
            task = dict(task_spec)
            prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = run_id or str(uuid.uuid4())
        command = self.build_headless_command(prompt, extra_args=extra_args)
        process_env = os.environ.copy()
        if env:
            process_env.update({str(key): str(value) for key, value in env.items()})
        run_cwd = Path(cwd).expanduser().resolve() if cwd is not None else self.source_path
        timed_out = False
        try:
            completed = subprocess.run(
                list(command),
                cwd=run_cwd,
                env=process_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = -9
            stdout = _decode_output(exc.stdout)
            stderr = _decode_output(exc.stderr)
            stderr += ("\n" if stderr and not stderr.endswith("\n") else "") + "dsh: headless subprocess timed out"
        except OSError as exc:
            returncode = 127
            stdout = ""
            stderr = f"dsh: failed to start headless subprocess: {exc}"
        events = collect_events(
            run_id,
            stdout,
            stderr,
            returncode,
            timed_out=timed_out,
            candidate_version=self.version,
        )
        result = HeadlessRunResult(run_id, command, returncode, stdout, stderr, events, timed_out)
        record = self._runs.setdefault(run_id, _RecordedRun(task=task))
        record.result = result
        record.events = list(events)
        return result

    def stream_events(self, run_id: str, after_seq: int = 0) -> tuple[int, Iterable[Mapping[str, Any]]]:
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError(f"unknown run_id: {run_id}")
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        events = [event for event in record.events if int(event["event_seq"]) > after_seq]
        cursor = int(record.events[-1]["event_seq"]) if record.events else after_seq
        return cursor, tuple(events)

    def result(self, run_id: str) -> Mapping[str, Any] | None:
        record = self._runs.get(run_id)
        if record is None:
            raise KeyError(f"unknown run_id: {run_id}")
        if record.result is None:
            return None
        return {
            "ok": record.result.ok,
            "returncode": record.result.returncode,
            "stdout": record.result.stdout,
            "stderr": record.result.stderr,
            "timed_out": record.result.timed_out,
        }

    def artifacts(self, run_id: str) -> Sequence[Mapping[str, Any]]:
        if run_id not in self._runs:
            raise KeyError(f"unknown run_id: {run_id}")
        return ()

    def pause(self, run_id: str) -> str:
        raise NotImplementedError("DeepSeek Harness headless profile has no native pause/resume API")

    def resume(self, run_id: str, checkpoint_id: str | None = None) -> None:
        raise NotImplementedError("DeepSeek Harness headless profile has no native pause/resume API")

    def snapshot(self, run_id: str) -> str:
        raise NotImplementedError("DeepSeek Harness headless profile has no native checkpoint API")

    def restore(self, run_id: str, checkpoint_id: str) -> None:
        raise NotImplementedError("DeepSeek Harness headless profile has no native checkpoint API")

    def cancel(self, run_id: str) -> None:
        raise NotImplementedError("one-shot subprocess cancellation is not wired in this adapter")

    def send_input(self, run_id: str, message: str) -> None:
        raise NotImplementedError("headless profile accepts one task positional and no live input")

    def approve(self, run_id: str, approval_id: str, decision: str) -> None:
        raise NotImplementedError("headless profile approval transport is not wired in this adapter")


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


__all__ = [
    "DeepSeekHarnessAdapter",
    "DeepSeekHarnessError",
    "HeadlessRunResult",
    "SourcePreflight",
    "SourcePreflightError",
    "collect_events",
]
