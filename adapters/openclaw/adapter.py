"""Thin subprocess adapter for OpenClaw's headless ``agent exec`` entrypoint.

OpenClaw stays an external checkout.  The adapter runs one agent turn with
``openclaw agent exec --json``, maps the stable JSON envelope to the unified
event contract, and retains a redacted manifest plus events.  Lifecycle
operations that the CLI does not expose are reported explicitly instead of
being simulated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence
import uuid


DEFAULT_COMMIT = "b6e0872bdb7228e22fac7608345e0529a35ec46e"
DEFAULT_PROVIDER_ID = "lab"
DEFAULT_CANDIDATE = "openclaw"
DEFAULT_PROMPT_TIMEOUT = 600.0


class OpenClawAdapterError(RuntimeError):
    """Base adapter error."""


class OpenClawPreflightError(OpenClawAdapterError):
    """Raised when the external checkout cannot be verified."""


@dataclass(frozen=True)
class SourcePreflight:
    path: Path
    cli_path: Path
    version: str | None
    commit: str | None
    license_present: bool
    ok: bool
    reasons: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "cli_path": str(self.cli_path),
            "version": self.version,
            "commit": self.commit,
            "license_present": self.license_present,
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
    envelope: Mapping[str, Any] | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and bool(self.envelope and self.envelope.get("ok"))


@dataclass
class _RecordedRun:
    task: Mapping[str, Any]
    events: list[Mapping[str, Any]] = field(default_factory=list)
    result: RunResult | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(
    run_id: str,
    event_seq: int,
    event_type: str,
    state: str,
    payload: Mapping[str, Any] | None = None,
    *,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "run_id": run_id,
        "event_seq": event_seq,
        "occurred_at": _now(),
        "candidate": DEFAULT_CANDIDATE,
        "candidate_version": f"openclaw@{DEFAULT_COMMIT}",
        "event_type": event_type,
        "previous_state": None,
        "state": state,
        "parent_event_id": None,
        "candidate_event_id": f"oc-{event_seq}-{run_id}",
        "tool_call": None,
        "model_call": None,
        "error": dict(error) if error else None,
        "payload": dict(payload or {}),
    }


def collect_events(
    run_id: str,
    envelope: Mapping[str, Any] | None,
    returncode: int,
    stderr: str = "",
    *,
    timed_out: bool = False,
) -> tuple[Mapping[str, Any], ...]:
    """Map the OpenClaw JSON envelope to normalized events."""

    events: list[Mapping[str, Any]] = [
        _event(run_id, 1, "process_started", "running", {"transport": "openclaw-cli"})
    ]
    sequence = 2
    if envelope is not None:
        if envelope.get("ok"):
            events.append(
                _event(
                    run_id,
                    sequence,
                    "assistant_output",
                    "completed",
                    {
                        "text": str(envelope.get("final", ""))[:4000],
                        "model": envelope.get("model"),
                        "provider": envelope.get("provider"),
                        "session_id": envelope.get("sessionId"),
                        "usage": envelope.get("usage"),
                        "tool_summary": envelope.get("toolSummary"),
                    },
                )
            )
        else:
            pending_error = envelope.get("error")
            error = {
                "category": str((pending_error or {}).get("kind", envelope.get("status", "candidate_error"))),
                "message": str((pending_error or {}).get("message", "OpenClaw run failed"))[:1000],
            }
            events.append(_event(run_id, sequence, "candidate_error", "aborted", {"status": envelope.get("status")}, error=error))
        sequence += 1
    elif stderr:
        events.append(_event(run_id, sequence, "worker_stderr", "running", {"text": stderr[-2000:]}))
        sequence += 1

    ok = returncode == 0 and not timed_out and bool(envelope and envelope.get("ok"))
    if timed_out:
        final_state = "aborted"
        error = {"category": "timeout", "message": "OpenClaw agent exec timed out"}
    elif ok:
        final_state = "completed"
        error = None
    else:
        final_state = "aborted"
        error = {"category": "process_exit", "message": f"exit code {returncode}"}
    events.append(
        _event(
            run_id,
            sequence,
            "process_exited",
            final_state,
            {"returncode": returncode, "timed_out": timed_out},
            error=error,
        )
    )
    return tuple(events)


def build_config(model: str, base_url: str, *, provider_id: str = DEFAULT_PROVIDER_ID) -> dict[str, Any]:
    """Build a pinned OpenClaw config for an OpenAI-compatible provider."""

    return {
        "models": {
            "mode": "merge",
            "providers": {
                provider_id: {
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "apiKey": "${MODEL_API_KEY}",
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "api": "openai-completions",
                            "input": ["text"],
                            "contextWindow": 128000,
                            "maxTokens": 4096,
                        }
                    ],
                }
            },
        },
        "agents": {"defaults": {"model": {"primary": f"{provider_id}/{model}"}}},
    }


class OpenClawAdapter:
    """Run OpenClaw ``agent exec`` and expose the unified read-side protocol."""

    name = DEFAULT_CANDIDATE

    def __init__(
        self,
        *,
        source_path: str | Path | None = None,
        node_executable: str = "node",
        cli_command: Sequence[str] | None = None,
        config_path: str | Path | None = None,
        artifact_root: str | Path | None = None,
        expected_commit: str | None = DEFAULT_COMMIT,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        default_source = repo_root.parent / "openclaw"
        self.source_path = Path(source_path).expanduser().resolve() if source_path else default_source
        self.node_executable = node_executable
        self.cli_command = list(cli_command) if cli_command else [node_executable, str(self.source_path / "openclaw.mjs")]
        self.config_path = Path(config_path).expanduser().resolve() if config_path else None
        self.artifact_root = Path(
            artifact_root or os.environ.get("HARNESS_LAB_ARTIFACT_ROOT", "~/harness-lab-data/runs")
        ).expanduser().resolve()
        self.expected_commit = expected_commit
        self.version = f"openclaw@{expected_commit}"
        self._runs: dict[str, _RecordedRun] = {}

    def preflight(self, *, raise_on_error: bool = True) -> SourcePreflight:
        reasons: list[str] = []
        cli_path = self.source_path / "openclaw.mjs"
        package_path = self.source_path / "package.json"
        license_present = (self.source_path / "LICENSE").exists()
        version: str | None = None
        if not cli_path.exists():
            reasons.append(f"OpenClaw CLI does not exist: {cli_path}")
        if package_path.exists():
            try:
                version = str(json.loads(package_path.read_text(encoding="utf-8")).get("version"))
            except (OSError, ValueError):
                reasons.append("OpenClaw package.json is not valid JSON")
        else:
            reasons.append(f"OpenClaw package.json does not exist: {package_path}")
        if not license_present:
            reasons.append("OpenClaw LICENSE is missing")
        commit: str | None = None
        if (self.source_path / ".git").exists():
            try:
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.source_path,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError) as exc:
                reasons.append(f"OpenClaw git preflight failed: {exc}")
            if self.expected_commit and commit != self.expected_commit:
                reasons.append(f"OpenClaw HEAD {commit!r} does not match expected {self.expected_commit}")
        else:
            reasons.append("OpenClaw source is not a Git checkout")
        report = SourcePreflight(self.source_path, cli_path, version, commit, license_present, not reasons, tuple(reasons))
        if raise_on_error and not report.ok:
            raise OpenClawPreflightError("; ".join(report.reasons))
        return report

    check_source = preflight

    def submit(self, task_spec: Mapping[str, Any]) -> str:
        prompt = task_spec.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = str(uuid.uuid4())
        self._runs[run_id] = _RecordedRun(
            dict(task_spec),
            [_event(run_id, 1, "run_queued", "planned", {"transport": "openclaw-cli"})],
        )
        return run_id

    def run_once(
        self,
        task_spec: Mapping[str, Any] | str,
        *,
        run_id: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        task = {"prompt": task_spec} if isinstance(task_spec, str) else dict(task_spec)
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("task_spec.prompt must be a non-empty string")
        run_id = run_id or str(uuid.uuid4())
        run_dir = self.artifact_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state_dir = run_dir / "state"
        state_dir.mkdir(exist_ok=True)

        process_env = os.environ.copy()
        if env:
            process_env.update({str(key): str(value) for key, value in env.items()})

        config_path = self.config_path
        if config_path is None:
            model = process_env.get("MODEL_NAME", "")
            base_url = process_env.get("MODEL_API_BASE_URL", "")
            if not model or not base_url:
                raise OpenClawAdapterError("MODEL_NAME and MODEL_API_BASE_URL are required to build an OpenClaw config")
            if not process_env.get("MODEL_API_KEY"):
                raise OpenClawAdapterError("MODEL_API_KEY is required for the OpenClaw provider config")
            config_path = run_dir / "openclaw.json5"
            config_path.write_text(
                json.dumps(build_config(model, base_url), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        command = [
            *self.cli_command,
            "agent",
            "exec",
            "--message-file",
            "-",
            "--json",
            "--config",
            str(config_path),
            "--state-dir",
            str(state_dir),
        ]
        limit = timeout or float(task.get("timeout_seconds") or DEFAULT_PROMPT_TIMEOUT)
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                cwd=str(self.source_path),
                env=process_env,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
            envelope = _parse_envelope(completed.stdout)
            result = RunResult(
                run_id,
                completed.returncode,
                completed.stdout,
                completed.stderr,
                collect_events(run_id, envelope, completed.returncode, completed.stderr),
                envelope,
                False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _decode(exc.stdout)
            stderr = _decode(exc.stderr)
            result = RunResult(
                run_id,
                -9,
                stdout,
                stderr,
                collect_events(run_id, None, -9, stderr, timed_out=True),
                None,
                True,
            )

        self._write_evidence(run_dir, run_id, task, result, config_path)
        record = self._runs.setdefault(run_id, _RecordedRun(task))
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
        envelope = record.result.envelope or {}
        return {
            "ok": record.result.ok,
            "returncode": record.result.returncode,
            "timed_out": record.result.timed_out,
            "final": envelope.get("final"),
            "status": envelope.get("status"),
            "model": envelope.get("model"),
            "provider": envelope.get("provider"),
            "session_id": envelope.get("sessionId"),
            "usage": envelope.get("usage"),
        }

    def artifacts(self, run_id: str) -> Sequence[Mapping[str, Any]]:
        if run_id not in self._runs:
            raise KeyError(run_id)
        run_dir = self.artifact_root / run_id
        return (
            {"kind": "manifest", "path": str(run_dir / "manifest.json")},
            {"kind": "events", "path": str(run_dir / "events.jsonl")},
            {"kind": "envelope", "path": str(run_dir / "envelope.json")},
        )

    def _write_evidence(
        self,
        run_dir: Path,
        run_id: str,
        task: Mapping[str, Any],
        result: RunResult,
        config_path: Path,
    ) -> None:
        (run_dir / "events.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in result.events),
            encoding="utf-8",
        )
        if result.envelope is not None:
            (run_dir / "envelope.json").write_text(
                json.dumps(result.envelope, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "candidate": self.name,
            "candidate_version": self.version,
            "task_id": task.get("task_id"),
            "state": result.events[-1]["state"] if result.events else "aborted",
            "runtime": {"version": self.preflight(raise_on_error=False).version, "node": self.node_executable},
            "model": {
                "provider": (result.envelope or {}).get("provider"),
                "model": (result.envelope or {}).get("model"),
                "base_url_source": "environment:MODEL_API_BASE_URL",
                "credential_source": "environment:MODEL_API_KEY",
            },
            "validation": {
                "prompt": task.get("prompt"),
                "answer": (result.envelope or {}).get("final"),
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "usage": (result.envelope or {}).get("usage"),
            },
            "config_path": str(config_path),
            "evidence": {"events": "events.jsonl", "envelope": "envelope.json"},
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def pause(self, run_id: str) -> str:
        raise NotImplementedError("OpenClaw agent exec is one-shot; gateway-backed pause is not wired")

    def resume(self, run_id: str, checkpoint_id: str | None = None) -> None:
        raise NotImplementedError("OpenClaw CLI resume through a Gateway session is not wired")

    def restore(self, run_id: str, checkpoint_id: str) -> None:
        raise NotImplementedError("OpenClaw checkpoint restore is not wired")

    def cancel(self, run_id: str) -> None:
        raise NotImplementedError("OpenClaw one-shot cancellation is not wired")

    def send_input(self, run_id: str, message: str) -> None:
        raise NotImplementedError("OpenClaw one-shot runs accept a single message")

    def approve(self, run_id: str, approval_id: str, decision: str) -> None:
        raise NotImplementedError("OpenClaw approval transport is not wired")


def _parse_envelope(stdout: str) -> Mapping[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, Mapping) else None


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


__all__ = [
    "OpenClawAdapter",
    "OpenClawAdapterError",
    "OpenClawPreflightError",
    "RunResult",
    "SourcePreflight",
    "build_config",
    "collect_events",
]
