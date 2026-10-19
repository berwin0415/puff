"""State and transition helpers kept separate for adapter/CLI consumers."""

from contracts.adapter import RunState
from .lifecycle import LifecycleError

TERMINAL_STATES = frozenset({RunState.COMPLETED.value, RunState.ABORTED.value})

__all__ = ["LifecycleError", "RunState", "TERMINAL_STATES"]
