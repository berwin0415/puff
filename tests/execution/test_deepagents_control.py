"""在 Docker 验收程序中校准信号接线，不运行候选 SDK 或模型。

公开控制替身只核对证据接线；真实 GraphDrained 与 checkpoint
行为仍需在候选容器中验证。
"""
from importlib.util import module_from_spec, spec_from_file_location
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(name):
    path = Path(__file__).resolve().parents[2] / "adapters/deepagents_langgraph" / name
    spec = spec_from_file_location(name.replace(".py", ""), path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicControlDouble:
    def __init__(self):
        self.calls = []
        self.drain_requested = False
        self.drain_reason = None

    def request_drain(self, reason):
        self.calls.append(reason)
        self.drain_requested = True
        self.drain_reason = reason
        return "actual-double-return"


@pytest.fixture
def glue(tmp_path):
    support = load("detailed_support.py")
    scope = {"experiment_id": "synthetic", "variant": "glue", "user_id": "alice", "session_id": "main"}
    evidence = support.Evidence(tmp_path, lambda value: json.dumps(value) + "\n", scope)
    control = PublicControlDouble()
    glue = load("run_control.py").NativeRunControl(control, evidence, "synthetic-run", scope)
    return SimpleNamespace(glue=glue, control=control, evidence=evidence, root=tmp_path)


def records(item, name):
    return [json.loads(row) for row in (item.root / name).read_text().splitlines()]


def start(item):
    source = item.evidence.append("raw-events.jsonl", {"stream_mode": "tasks", "data": {
        "id": "native-task", "name": "model", "input": {}, "triggers": ["branch:to:model"]}})
    item.glue.active = True
    item.glue.observe("tasks", {"id": "native-task", "name": "model", "input": {}, "triggers": []}, source)
    return source


def test_idle_or_unobserved_graph_cannot_call_native_control(glue):
    glue.glue.request_cancel()
    glue.glue.active = True
    glue.glue.request_cancel()
    assert glue.control.calls == []
    assert all(row["admitted"] is False and row["api_executed"] is False
               for row in records(glue, "control-events.jsonl"))


def test_actual_task_source_and_actual_api_return_are_preserved_once(glue):
    source = start(glue)
    glue.glue.request_cancel()
    glue.glue.request_cancel()
    assert glue.control.calls == ["experiment_cancel"]
    events = records(glue, "control-events.jsonl")
    assert events[0]["admitted"] is True and events[0]["source"] == source
    assert events[1]["phase"] == "native_api_returned"
    assert events[2]["admitted"] is False and events[2]["refusal_reason"] == "duplicate_request"
    raw = records(glue, "raw-events.jsonl")
    assert raw[1]["returned"] == "actual-double-return"
    assert raw[1]["native_api"] == "RunControl.request_drain"
    assert raw[1]["immediate_cancel"] is False


def test_task_finish_prevents_stale_activity_from_admitting_a_request(glue):
    start(glue)
    glue.glue.observe("tasks", {"id": "native-task", "result": {}, "error": None, "interrupts": []},
                      {"path": "raw-events.jsonl", "line": 2})
    glue.glue.request_cancel()
    assert glue.control.calls == []
    assert records(glue, "control-events.jsonl")[0]["refusal_reason"] == "no_active_native_task"


@pytest.mark.parametrize("defect", [None, "no_request", "wrong_reason", "missing_checkpoint"])
def test_drain_confirmation_requires_request_reason_and_checkpoint(glue, defect):
    start(glue)
    if defect != "no_request":
        glue.glue.request_cancel()
    config = {"configurable": {"thread_id": "main", "checkpoint_id": "native-checkpoint"}}
    if defect == "missing_checkpoint":
        config["configurable"].pop("checkpoint_id")
    snapshot = SimpleNamespace(config=config, next=("tools",), tasks=())
    data = json.dumps({"config": config}) + "\n"
    (glue.root / "state-snapshot.json").write_text(data)
    class NativeExceptionDouble(Exception):
        reason = "different" if defect == "wrong_reason" else "experiment_cancel"
    observation = glue.glue.drained(NativeExceptionDouble("actual-double-error"), snapshot)
    assert observation["cancel_confirmed"] is (defect is None)
    assert observation["native_next"] == ("tools",)
    assert observation["checkpoint_source"]["sha256"] == hashlib.sha256(data.encode()).hexdigest()
    assert observation["task_continuation_verified"] is False
    assert observation["immediate_cancel"] is False
    assert observation["inflight_operations_may_finish_after_request"] is True
