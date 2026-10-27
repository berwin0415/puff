"""连接信号、证据与公开 LangGraph RunControl，绝不调用 task.cancel()。"""
from __future__ import annotations

import asyncio
import hashlib
import signal


class NativeRunControl:
    def __init__(self, control, evidence, run_id, scope, *, lifecycle=None):
        self.control, self.evidence, self.run_id, self.scope = control, evidence, run_id, scope
        self.active = False
        self.tasks = {}
        self.latest_activity = None
        self.request = None
        self.sequence = 0
        self.loop = None
        self.lifecycle = lifecycle

    def install(self):
        self.loop = asyncio.get_running_loop()
        self.loop.add_signal_handler(signal.SIGUSR1, self.request_cancel)
        if self.lifecycle:
            self.loop.add_signal_handler(signal.SIGUSR2, self.request_pause)
            self.evidence.append("control-events.jsonl", {"time": self.evidence.now(),
                "phase": "pause_handler_installed", "signal": "SIGUSR2", "scope": self.scope,
                "run_id": self.run_id, "chain_id": self.lifecycle["chain_id"],
                "attempt_id": self.lifecycle["attempt_id"], "action": "pause"})

    def close(self):
        self.active = False
        if self.loop is not None:
            self.loop.remove_signal_handler(signal.SIGUSR1)
            if self.lifecycle:
                self.loop.remove_signal_handler(signal.SIGUSR2)
            self.loop = None

    def observe(self, mode, event, source):
        if mode == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
            if "input" in event and "result" not in event:
                self.tasks[event["id"]] = {"id": event["id"], "name": event.get("name"), "source": source}
                self.latest_activity = source
            elif "result" in event or "error" in event or "interrupts" in event:
                self.tasks.pop(event["id"], None)
        if mode == "messages" and self.tasks:
            self.latest_activity = source

    def request_cancel(self):
        return self._request_drain("cancel", "SIGUSR1", "experiment_cancel")

    def request_pause(self):
        return self._request_drain("pause", "SIGUSR2", "experiment_pause")

    def _request_drain(self, action, signal_name, reason):
        self.sequence += 1
        active = self.active and bool(self.tasks) and self.latest_activity is not None
        admitted = active and self.request is None
        request_id = self.run_id + ":drain:" + str(self.sequence)
        common = {"scope": self.scope, "run_id": self.run_id,
            "session_id": self.scope.get("session_id") if self.scope else None,
            "request_id": request_id, "signal": signal_name, "action": action,
            "native_api": "RunControl.request_drain", "reason": reason,
            "semantics": "cooperative_drain_at_superstep_boundary", "immediate_cancel": False}
        if self.lifecycle:
            common.update({"chain_id": self.lifecycle["chain_id"], "attempt_id": self.lifecycle["attempt_id"]})
        source = self.evidence.append("control-events.jsonl", {**common, "time": self.evidence.now(),
            "phase": "requested", "active_native_task_observed": bool(active), "api_executed": False,
            "active_tasks": list(self.tasks.values()), "source": self.latest_activity,
            "admitted": bool(admitted), "refusal_reason": None if admitted else "duplicate_request" if self.request is not None else "no_active_native_task"})
        if not admitted:
            return
        # 这是官方公开方法，不会取消 asyncio
        # 任务、终止在途 HTTP 请求或结束进程。
        returned = self.control.request_drain(reason)
        native_source = self.evidence.append("raw-events.jsonl", {
            "type": "native_control_api_returned", "time": self.evidence.now(), **common,
            "arguments": {"reason": reason}, "returned": returned,
            "drain_requested": self.control.drain_requested, "drain_reason": self.control.drain_reason})
        api_source = self.evidence.append("control-events.jsonl", {**common, "time": self.evidence.now(),
            "phase": "native_api_returned", "api_executed": True, "request_source": source,
            "source": native_source, "drain_requested": self.control.drain_requested,
            "drain_reason": self.control.drain_reason})
        self.request = {**common, "request_source": source, "api_source": api_source}

    def drained(self, error, snapshot):
        """仅从实际的 `except GraphDrained` 分支调用。"""
        self.active = False
        reason = error.reason
        native_source = self.evidence.append("raw-events.jsonl", {
            "type": "native_graph_drained", "time": self.evidence.now(),
            "scope": self.scope, "run_id": self.run_id,
            "session_id": self.scope.get("session_id") if self.scope else None,
            "exception_type": type(error).__module__ + "." + type(error).__qualname__,
            "message": str(error), "reason": reason,
            "drain_requested": self.control.drain_requested, "drain_reason": self.control.drain_reason})
        data = (self.evidence.directory / "state-snapshot.json").read_bytes()
        checkpoint_source = {"path": "state-snapshot.json", "sha256": hashlib.sha256(data).hexdigest(),
                             "json_pointer": "/config/configurable/checkpoint_id"}
        checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
        confirmed = (self.request is not None and reason == self.control.drain_reason == "experiment_cancel"
                     and self.control.drain_requested and isinstance(checkpoint_id, str) and bool(checkpoint_id))
        pause_boundary = (self.lifecycle is not None and self.request is not None
            and self.request["action"] == "pause" and reason == self.control.drain_reason == "experiment_pause"
            and self.control.drain_requested and isinstance(checkpoint_id, str) and bool(checkpoint_id)
            and not self.tasks)
        observation = {**(self.request or {"scope": self.scope}), "time": self.evidence.now(),
            "phase": "native_drained_observed", "api_executed": self.request is not None,
            "cancel_confirmed": bool(confirmed), "native_status_after": "drained",
            "pause_boundary_observed": bool(pause_boundary),
            "observed_active_tasks": list(self.tasks.values()),
            "source": native_source, "checkpoint_source": checkpoint_source,
            "native_exception_reason": reason,
            "checkpoint_id": checkpoint_id,
            "native_next": snapshot.next, "native_tasks": snapshot.tasks,
            "semantics": "cooperative_drain_at_superstep_boundary", "immediate_cancel": False,
            "inflight_operations_may_finish_after_request": True,
            "task_continuation_verified": False}
        self.evidence.append("control-events.jsonl", observation)
        return observation
