"""Observe a trusted signal calling the public streaming-result cancellation API.

This stdlib bridge never cancels an asyncio task or blocks a tool. Official
hooks establish live activity; the SDK owns cancellation and stream settlement.
It does not invent the DSH idle/aborted event vocabulary for this SDK.
"""
from datetime import datetime, timezone


class NativeCancelControl:
    def __init__(self, identity, evidence, emit, get_result):
        self.identity = {key: identity[key] for key in ("run_id", "session_id", "scope") if key in identity}
        self.evidence, self.emit, self.get_result = evidence, emit, get_result
        self.input_submitted = False
        self.active = {}
        self.sequence = 0
        self.request_record = None
        self.request_source = self.api_source = None
        self.api_executed = False
        self.stream_exhausted = False
        self.tool_starts = []

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def activity_start(self, kind, source, key=None):
        result = self.get_result()
        self.active[(kind, key)] = {"kind": kind, "source": source,
                                    "turn": result.current_turn if result is not None else None}

    def activity_end(self, kind, key=None):
        self.active.pop((kind, key), None)

    def tool_started(self, source, tool_name, call_id):
        # Observation only: no guard, delay, retry, suppression or replacement.
        self.tool_starts.append({"tool": tool_name, "call_id": call_id, "source": source})
        self.activity_start("tool", source, call_id or tool_name)

    def request(self):
        self.sequence += 1
        result = self.get_result()
        activity = next(reversed(self.active.values()), None)
        before = result.is_complete if result is not None else None
        base = {**self.identity, "request_id": f"{self.identity['run_id']}:cancel:{self.sequence}",
                "requested_at": self.now(), "signal": "SIGUSR1", "action": "cancel",
                "native_api": "RunResultStreaming.cancel", "cancel_mode": "immediate",
                "active_input_submitted": self.input_submitted, "native_is_complete_before": before,
                "turn": result.current_turn if result is not None else None,
                "active_activity": activity["kind"] if activity else None,
                "source": activity["source"] if activity else None}
        inactive = ("no_evidence_context" if self.evidence is None else
                    "no_streaming_result" if result is None else
                    "input_not_submitted" if not self.input_submitted else
                    "stream_already_exhausted" if self.stream_exhausted else
                    "native_result_not_active" if before is not False else
                    "native_run_loop_already_failed" if result.run_loop_exception is not None else
                    "native_final_output_already_present" if result.final_output is not None else
                    "no_live_official_hook" if activity is None else
                    "hook_belongs_to_another_turn" if activity["turn"] != result.current_turn else
                    "cancel_already_requested" if self.request_record is not None else None)
        if inactive:
            if self.evidence is not None:
                self.evidence.append("control-events.jsonl", {**base, "time": self.now(), "phase": "not_cancelled",
                    "reason": inactive, "api_executed": False, "cancel_confirmed": False})
            return
        self.request_record = base
        try:
            self.request_source = self.evidence.append("control-events.jsonl", {
                **base, "time": self.now(), "phase": "requested", "api_executed": False, "cancel_confirmed": False})
            # This is the public SDK API, called in the run's own event loop.
            # Continue consuming its existing stream afterwards as documented.
            result.cancel(mode="immediate")
            self.api_executed = True
            self.api_source = self.evidence.append("control-events.jsonl", {
                **base, "time": self.now(), "phase": "native_api_returned", "api_executed": True,
                "cancel_confirmed": False, "native_is_complete_after": result.is_complete,
                "request_source": self.request_source})
        except Exception as error:
            self.evidence.append("control-events.jsonl", {**base, "time": self.now(), "phase": "control_error",
                "api_executed": self.api_executed, "cancel_confirmed": False,
                "error_type": type(error).__name__, "error": str(error), "request_source": self.request_source})

    def stream_closed(self, agent_end_events):
        """Called only after the original async-for has ended without an error."""
        result = self.get_result()
        error = result.run_loop_exception
        terminal = {"is_complete": result.is_complete, "current_turn": result.current_turn,
                    "interruptions": result.interruptions, "final_output_present": result.final_output is not None,
                    "agent_end_events": agent_end_events, "response_count": len(result.raw_responses),
                    "run_loop_exception": None if error is None else {
                        "error_type": type(error).__name__, "error": str(error)}}
        self.stream_exhausted = True
        source = self.emit("public_stream_observation", {"api": "RunResultStreaming.stream_events",
            "stream_exhausted": True, "native_terminal": terminal})
        confirmed = bool(self.api_executed and self.request_source and self.api_source
                         and result.is_complete is True and error is None and result.final_output is None
                         and not result.interruptions and agent_end_events == 0)
        confirmation = None
        if self.request_record is not None:
            confirmation = self.evidence.append("control-events.jsonl", {
                **self.request_record, "time": self.now(), "phase": "native_stream_closed",
                "api_executed": self.api_executed, "cancel_confirmed": confirmed, "stream_exhausted": True,
                "native_terminal": terminal, "source": source, "activity_source": self.request_record["source"],
                "request_source": self.request_source, "api_source": self.api_source,
                "tool_start_observations": self.tool_starts,
                "confirmation_basis": "public cancel returned; original stream exhausted; no native final output or error",
                "in_flight_effects": "Already dispatched HTTP effects cannot be recalled; independent audit is required"})
        self.active.clear()
        return {"confirmed": confirmed, "native_terminal": terminal, "source": source,
                "control_source": confirmation}
