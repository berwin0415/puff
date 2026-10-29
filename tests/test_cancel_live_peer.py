"""取消组 live-peer 判据的离线验收。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runner import multi_user as m


def task(name):
    return SimpleNamespace(task_id=name, paths=SimpleNamespace(run=Path("/tmp/cancel-live-peer")), scope={"user_id": name})


def record(before_a=1, after_a=2):
    def sample(lines):
        return {"native_prefix_lines": lines, "activity_source": {"path": "raw-events.jsonl"},
                "tools_source": {"path": "tool-events.jsonl"}, "successful_tools": 0}
    return {"before": {"tasks": {"a": sample(before_a), "b": sample(1)}},
            "after": {"tasks": {"a": sample(after_a), "b": sample(2)}}}


def test_cancel_live_peer_accepts_native_growth_without_tool_completion():
    tasks = [task("a"), task("b")]
    with patch.object(m.p, "reference"), patch.object(m.p, "observed_prefix", return_value=[]):
        assert m.validate_cancel_live_peer(tasks, record()) == {
            "cancel_peer_live": True, "requires_tool_completion": False}


def test_cancel_live_peer_rejects_stalled_task():
    tasks = [task("a"), task("b")]
    with patch.object(m.p, "reference"), patch.object(m.p, "observed_prefix", return_value=[]):
        with pytest.raises(ValueError, match="cancel_task_not_live_during_peer_window"):
            m.validate_cancel_live_peer(tasks, record(before_a=2, after_a=2))
