"""Offline checkpoint inspection for the Deep Agents adapter."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from adapters.deepagents_langgraph import CheckpointInspectionError, DeepAgentsLangGraphAdapter


EXTERNAL_PYTHON = Path(
    os.environ.get(
        "DEEPAGENTS_PYTHON",
        "~/harness-lab-sources/deepagents/libs/deepagents/.venv/bin/python",
    )
).expanduser()

pytestmark = pytest.mark.skipif(
    not EXTERNAL_PYTHON.exists(),
    reason="external Deep Agents virtualenv is not available",
)


SEED_GRAPH = """
import sys
from typing import TypedDict
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver


class State(TypedDict):
    value: int


def increment(state):
    return {"value": state.get("value", 0) + 1}


builder = StateGraph(State)
builder.add_node("increment", increment)
builder.set_entry_point("increment")
builder.add_edge("increment", END)

db_path, thread_id = sys.argv[1], sys.argv[2]
with SqliteSaver.from_conn_string(db_path) as checkpointer:
    graph = builder.compile(checkpointer=checkpointer)
    graph.invoke({"value": 0}, {"configurable": {"thread_id": thread_id}})
"""


def _adapter(checkpoint_db: Path) -> DeepAgentsLangGraphAdapter:
    return DeepAgentsLangGraphAdapter(
        python_executable=EXTERNAL_PYTHON,
        checkpoint_db=checkpoint_db,
    )


def test_snapshot_reads_candidate_checkpoint_without_a_model(tmp_path: Path) -> None:
    db = tmp_path / "checkpoints.sqlite"
    script = tmp_path / "seed_graph.py"
    script.write_text(SEED_GRAPH, encoding="utf-8")
    subprocess.run([str(EXTERNAL_PYTHON), str(script), str(db), "thread-1"], check=True, capture_output=True, text=True)

    adapter = _adapter(db)
    payload = adapter.inspect_checkpoint("thread-1")
    assert payload["thread_id"] == "thread-1"
    assert payload["checkpoint_count"] >= 1
    assert payload["state"]["value"] == 1
    assert adapter.snapshot("thread-1") == payload["checkpoint_id"]


def test_snapshot_reports_a_missing_checkpoint(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "missing.sqlite")
    with pytest.raises(CheckpointInspectionError):
        adapter.snapshot("no-such-thread")
