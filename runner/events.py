"""Event stream helpers for callers that do not need the storage internals."""

from contracts.adapter import Event
from .storage import SQLiteEventStore


def stream_events(store: SQLiteEventStore, run_id: str, after_seq: int | None = 0) -> tuple[int, list[dict]]:
    cursor, events = store.stream_events(run_id, after_seq)
    return cursor, [event.to_mapping() for event in events]


__all__ = ["Event", "stream_events"]
