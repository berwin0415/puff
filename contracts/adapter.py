"""Reference protocol for candidate Harness adapters.

The protocol intentionally contains only serialisable values.  Adapters may be
implemented in another process or language; the JSON schemas are the wire
contract and this module is a small Python convenience layer for the runner and
contract tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol, Sequence
import uuid


class RunState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    tenant_scope: str | Mapping[str, Any]
    session_scope: str | Mapping[str, Any]
    prompt: str
    tools: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    browser: Mapping[str, Any] | None = None
    timeout_seconds: float | None = None
    budget: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_policy: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TaskSpec":
        required = ("task_id", "tenant_scope", "session_scope", "prompt")
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"task_spec missing required fields: {', '.join(missing)}")
        timeout = value.get("timeout_seconds")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ValueError("timeout_seconds must be a positive number")
        if not isinstance(value["prompt"], str) or not value["prompt"].strip():
            raise ValueError("prompt must be a non-empty string")
        return cls(
            task_id=str(value["task_id"]),
            tenant_scope=value["tenant_scope"],
            session_scope=value["session_scope"],
            prompt=value["prompt"],
            tools=tuple(value.get("tools") or ()),
            browser=value.get("browser"),
            timeout_seconds=float(timeout) if timeout is not None else None,
            budget=dict(value.get("budget") or {}),
            checkpoint_policy=dict(value.get("checkpoint_policy") or {}),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tenant_scope": self.tenant_scope,
            "session_scope": self.session_scope,
            "prompt": self.prompt,
            "tools": list(self.tools),
            "browser": self.browser,
            "timeout_seconds": self.timeout_seconds,
            "budget": dict(self.budget),
            "checkpoint_policy": dict(self.checkpoint_policy),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Event:
    run_id: str
    event_seq: int
    event_type: str
    state: str
    previous_state: str | None = None
    candidate: str | None = None
    candidate_version: str | None = None
    parent_event_id: str | None = None
    candidate_event_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    tool_call: Mapping[str, Any] | None = None
    model_call: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_mapping(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event_seq": self.event_seq,
            "occurred_at": self.occurred_at,
            "candidate": self.candidate,
            "candidate_version": self.candidate_version,
            "event_type": self.event_type,
            "previous_state": self.previous_state,
            "state": self.state,
            "parent_event_id": self.parent_event_id,
            "candidate_event_id": self.candidate_event_id,
            "tool_call": self.tool_call,
            "model_call": self.model_call,
            "error": self.error,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Event":
        return cls(
            event_id=str(value["event_id"]),
            run_id=str(value["run_id"]),
            event_seq=int(value["event_seq"]),
            occurred_at=str(value["occurred_at"]),
            event_type=str(value["event_type"]),
            state=str(value["state"]),
            previous_state=value.get("previous_state"),
            candidate=value.get("candidate"),
            candidate_version=value.get("candidate_version"),
            parent_event_id=value.get("parent_event_id"),
            candidate_event_id=value.get("candidate_event_id"),
            payload=dict(value.get("payload") or {}),
            tool_call=value.get("tool_call"),
            model_call=value.get("model_call"),
            error=value.get("error"),
        )


class Adapter(Protocol):
    """Candidate adapter RPC surface.

    Implementations may return mappings instead of these dataclasses.  The
    runner serialises all values before persisting them.
    """

    name: str
    version: str

    def submit(self, task_spec: Mapping[str, Any]) -> str: ...

    def stream_events(self, run_id: str, after_seq: int = 0) -> tuple[int, Iterable[Mapping[str, Any]]]: ...

    def send_input(self, run_id: str, message: str) -> None: ...

    def approve(self, run_id: str, approval_id: str, decision: str) -> None: ...

    def pause(self, run_id: str) -> str: ...

    def resume(self, run_id: str, checkpoint_id: str | None = None) -> None: ...

    def cancel(self, run_id: str) -> None: ...

    def snapshot(self, run_id: str) -> str: ...

    def restore(self, run_id: str, checkpoint_id: str) -> None: ...

    def result(self, run_id: str) -> Mapping[str, Any] | None: ...

    def artifacts(self, run_id: str) -> Sequence[Mapping[str, Any]]: ...
