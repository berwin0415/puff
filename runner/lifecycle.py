"""Framework-neutral task lifecycle for the local validation runner."""

from __future__ import annotations

from typing import Any, Mapping, Sequence
import uuid

from contracts.adapter import Adapter, RunState, TaskSpec
from .storage import SQLiteEventStore


class LifecycleError(RuntimeError):
    """Raised when a run cannot perform the requested lifecycle operation."""


_TRANSITIONS: dict[str, set[str]] = {
    RunState.PLANNED.value: {RunState.RUNNING.value, RunState.ABORTED.value},
    RunState.RUNNING.value: {
        RunState.PAUSED.value,
        RunState.BLOCKED.value,
        RunState.COMPLETED.value,
        RunState.ABORTED.value,
    },
    RunState.PAUSED.value: {RunState.RUNNING.value, RunState.ABORTED.value},
    RunState.BLOCKED.value: {RunState.RUNNING.value, RunState.PAUSED.value, RunState.ABORTED.value},
    RunState.COMPLETED.value: set(),
    RunState.ABORTED.value: set(),
}


class Runner:
    """Small synchronous controller used before candidate adapters are added.

    ``Runner`` persists every lifecycle operation, so it remains useful with no
    candidate SDK installed.  A candidate adapter can be supplied later; its
    calls are deliberately best-effort hooks and never replace the canonical
    runner state or event sequence.
    """

    def __init__(
        self,
        storage: SQLiteEventStore | None = None,
        *,
        adapter: Adapter | None = None,
        candidate: str | None = None,
        candidate_version: str | None = None,
    ) -> None:
        self.storage = storage or SQLiteEventStore()
        self.adapter = adapter
        self.candidate = candidate or getattr(adapter, "name", "unassigned")
        self.candidate_version = candidate_version or getattr(adapter, "version", "unknown")

    def submit(self, task_spec: TaskSpec | Mapping[str, Any], *, run_id: str | None = None, start: bool = True) -> str:
        task = task_spec if isinstance(task_spec, TaskSpec) else TaskSpec.from_mapping(task_spec)
        run_id = run_id or str(uuid.uuid4())
        self.storage.create_run(run_id, task, candidate=self.candidate, candidate_version=self.candidate_version)
        if self.adapter is not None:
            adapter_run = self.adapter.submit(task.to_mapping())
            self.storage.append_event(run_id, event_type="adapter_submitted", state=RunState.PLANNED.value, payload={"adapter_run_id": adapter_run})
        if start:
            self.start(run_id)
        return run_id

    def start(self, run_id: str) -> dict[str, Any]:
        return self._transition(run_id, RunState.RUNNING.value, "run_started", expected={RunState.PLANNED.value})

    def pause(self, run_id: str) -> str:
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.RUNNING.value, RunState.BLOCKED.value}, "pause")
        self.storage.append_event(run_id, event_type="pause_requested", state=run["state"], payload={"safe_point": "after_inflight_call"})
        adapter_checkpoint = None
        if self.adapter is not None and hasattr(self.adapter, "pause"):
            adapter_checkpoint = self.adapter.pause(self._adapter_run_id(run_id))
        checkpoint_id = self.storage.write_checkpoint(
            run_id,
            {
                "state": run["state"],
                "task": run["task"],
                "adapter_checkpoint_id": adapter_checkpoint,
                "safe_point": "after_inflight_call",
            },
        )
        self._transition(
            run_id,
            RunState.PAUSED.value,
            "run_paused",
            expected={run["state"]},
            payload={"checkpoint_id": checkpoint_id, "safe_point": "after_inflight_call"},
        )
        return checkpoint_id

    def resume(self, run_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.PAUSED.value}, "resume")
        selected = checkpoint_id or run["checkpoint_id"]
        if not selected:
            raise LifecycleError(f"run {run_id} has no checkpoint to resume")
        checkpoint = self.storage.read_checkpoint(run_id, selected)
        if self.adapter is not None and hasattr(self.adapter, "resume"):
            self.adapter.resume(self._adapter_run_id(run_id), checkpoint.get("adapter_checkpoint_id") or selected)
        return self._transition(
            run_id,
            RunState.RUNNING.value,
            "run_resumed",
            expected={RunState.PAUSED.value},
            payload={"checkpoint_id": selected},
        )

    def restore(self, run_id: str, checkpoint_id: str) -> dict[str, Any]:
        """Restore a checkpoint and leave the run in ``running`` state."""
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.PAUSED.value, RunState.BLOCKED.value, RunState.PLANNED.value}, "restore")
        checkpoint = self.storage.read_checkpoint(run_id, checkpoint_id)
        if self.adapter is not None and hasattr(self.adapter, "restore"):
            self.adapter.restore(self._adapter_run_id(run_id), checkpoint.get("adapter_checkpoint_id") or checkpoint_id)
        return self._transition(
            run_id,
            RunState.RUNNING.value,
            "run_restored",
            expected={run["state"]},
            payload={"checkpoint_id": checkpoint_id},
        )

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.PLANNED.value, RunState.RUNNING.value, RunState.PAUSED.value, RunState.BLOCKED.value}, "cancel")
        if self.adapter is not None and hasattr(self.adapter, "cancel"):
            self.adapter.cancel(self._adapter_run_id(run_id))
        return self._transition(run_id, RunState.ABORTED.value, "run_cancelled", expected={run["state"]}, payload={"reason": "cancel_requested"})

    def complete(self, run_id: str, result: Mapping[str, Any] | None = None, *, artifacts: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.RUNNING.value, RunState.PAUSED.value, RunState.BLOCKED.value}, "complete")
        self.storage.set_result(run_id, result or {})
        self.storage.set_artifacts(run_id, artifacts)
        return self._transition(run_id, RunState.COMPLETED.value, "run_completed", expected={run["state"]}, payload={"artifact_count": len(artifacts)})

    def block(self, run_id: str, *, reason: str) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.RUNNING.value}, "block")
        return self._transition(run_id, RunState.BLOCKED.value, "run_blocked", expected={run["state"]}, payload={"reason": reason})

    def snapshot(self, run_id: str) -> str:
        run = self.storage.get_run(run_id)
        if run["state"] in {RunState.COMPLETED.value, RunState.ABORTED.value}:
            raise LifecycleError(f"cannot snapshot terminal run {run_id}")
        adapter_checkpoint = None
        if self.adapter is not None and hasattr(self.adapter, "snapshot"):
            adapter_checkpoint = self.adapter.snapshot(self._adapter_run_id(run_id))
        checkpoint_id = self.storage.write_checkpoint(
            run_id,
            {"state": run["state"], "task": run["task"], "adapter_checkpoint_id": adapter_checkpoint, "safe_point": "explicit_snapshot"},
        )
        self.storage.append_event(run_id, event_type="checkpoint_created", state=run["state"], payload={"checkpoint_id": checkpoint_id, "safe_point": "explicit_snapshot"})
        return checkpoint_id

    def send_input(self, run_id: str, message: str) -> None:
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.RUNNING.value, RunState.PAUSED.value, RunState.BLOCKED.value}, "send_input")
        if self.adapter is not None and hasattr(self.adapter, "send_input"):
            self.adapter.send_input(self._adapter_run_id(run_id), message)
        self.storage.append_event(run_id, event_type="input_received", state=run["state"], payload={"message": message})

    def approve(self, run_id: str, approval_id: str, decision: str) -> None:
        run = self.storage.get_run(run_id)
        self._require_state(run, {RunState.RUNNING.value, RunState.PAUSED.value, RunState.BLOCKED.value}, "approve")
        if self.adapter is not None and hasattr(self.adapter, "approve"):
            self.adapter.approve(self._adapter_run_id(run_id), approval_id, decision)
        self.storage.append_event(run_id, event_type="approval_decided", state=run["state"], payload={"approval_id": approval_id, "decision": decision})

    def stream_events(self, run_id: str, after_seq: int | None = 0) -> tuple[int, list[dict[str, Any]]]:
        cursor, events = self.storage.stream_events(run_id, after_seq)
        return cursor, [event.to_mapping() for event in events]

    def result(self, run_id: str) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        adapter_result = None
        if self.adapter is not None and hasattr(self.adapter, "result"):
            adapter_result = self.adapter.result(self._adapter_run_id(run_id))
        if adapter_result is not None and run["result"] is None:
            self.storage.set_result(run_id, adapter_result)
            run = self.storage.get_run(run_id)
        return {
            "run_id": run["run_id"],
            "state": run["state"],
            "result": run["result"],
            "artifacts": run["artifacts"],
            "checkpoint_id": run["checkpoint_id"],
        }

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.storage.get_run(run_id)["artifacts"])

    def status(self, run_id: str) -> dict[str, Any]:
        return self.storage.get_run(run_id)

    def _transition(
        self,
        run_id: str,
        target: str,
        event_type: str,
        *,
        expected: set[str],
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = self.storage.get_run(run_id)
        if run["state"] not in expected:
            raise LifecycleError(f"run {run_id} is {run['state']}, expected one of {sorted(expected)}")
        if target not in _TRANSITIONS.get(run["state"], set()):
            raise LifecycleError(f"invalid lifecycle transition {run['state']} -> {target}")
        self.storage.update_state(run_id, target, expected=tuple(expected))
        self.storage.append_event(run_id, event_type=event_type, state=target, previous_state=run["state"], payload=payload or {})
        return self.storage.get_run(run_id)

    @staticmethod
    def _require_state(run: Mapping[str, Any], allowed: set[str], operation: str) -> None:
        if run["state"] not in allowed:
            raise LifecycleError(f"cannot {operation} run {run['run_id']} while state is {run['state']}")

    def _adapter_run_id(self, run_id: str) -> str:
        """Return the candidate run identity recorded at submission time.

        Candidate processes may allocate their own run IDs.  Keeping the
        mapping in the append-only event stream makes it survive Runner
        recreation without adding candidate-specific columns to SQLite.
        Adapters that intentionally reuse the canonical ID get the same value
        through the fallback.
        """

        if self.adapter is None:
            return run_id
        _cursor, events = self.storage.stream_events(run_id)
        for event in events:
            if event.event_type == "adapter_submitted":
                adapter_run_id = event.payload.get("adapter_run_id")
                if isinstance(adapter_run_id, str) and adapter_run_id:
                    return adapter_run_id
        return run_id


Lifecycle = Runner
