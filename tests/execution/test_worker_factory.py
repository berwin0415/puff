"""worker 入口按 candidate 分派的离线验收。"""

import pytest

from execution.worker_control import SingleAttemptWorker, worker_class_for


def test_deepagents_selects_single_attempt_worker():
    assert worker_class_for("deepagents_langgraph") is SingleAttemptWorker


def test_deepseek_selects_dsh_worker():
    from execution.worker_control_dsh import DSHAttemptWorker
    assert worker_class_for("deepseek_harness") is DSHAttemptWorker


def test_unknown_candidate_is_rejected():
    with pytest.raises(ValueError, match="unsupported worker candidate"):
        worker_class_for("not-a-candidate")
