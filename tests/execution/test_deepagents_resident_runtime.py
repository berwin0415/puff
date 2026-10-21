"""I25 binding/projection calibration, not a real-candidate lifecycle test.

Only the stdlib adapter module and explicit boundary doubles are loaded here.
No model client, DeepAgents factory or native SQLite saver is instantiated.
These tests must still be run in the frozen Docker bench image; passing them
cannot prove native history creation, framework residency, cost or isolation.
"""
from collections import namedtuple
from copy import deepcopy
import asyncio
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ADAPTER = Path(__file__).resolve().parents[2] / "adapters" / "deepagents_langgraph"


def load(filename):
    spec = spec_from_file_location("harness_lab_i25_" + filename.removesuffix(".py"), ADAPTER / filename)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = load("resident_runtime.py")
SUPPORT = load("detailed_support.py")
Snapshot = namedtuple("Snapshot", "values next config metadata created_at parent_config tasks interrupts")
Task = namedtuple("Task", "id name path error interrupts state result")


@pytest.fixture
def material(tmp_path):
    root = tmp_path.resolve()
    config = root / "config"
    config.mkdir()
    entries = []
    for task_id in ("history-a", "business-a-1", "business-a-2"):
        entry = {"task_id": task_id,
            "scope": {"experiment_id": "instance-cost", "variant": task_id, "user_id": "user-a", "session_id": task_id},
            **{key: str(root / task_id / key) for key in ("state_dir", "workspace", "output_dir")},
            **{key: str(config / (task_id + "-" + key)) for key in ("tool_token_file", "model_token_file", "tools_schema_file")}}
        for key in ("state_dir", "workspace", "output_dir"):
            Path(entry[key]).mkdir(parents=True)
        if task_id == "history-a":
            entry["sentinel"] = "synthetic-business-history-for-user-a"
        else:
            entry["prompt_file"] = str(config / (task_id + "-prompt"))
        entries.append(entry)
    value = {"schema": RUNTIME.SCHEMA, "candidate": "deepagents_langgraph", "owner": "user-a",
        "model_name": "synthetic-no-model-is-loaded", "limits": {"max_output_tokens": 2048, "timeout_seconds": 300, "max_retries": 0},
        "history": entries[0], "tasks": entries[1:]}
    return value


@pytest.mark.parametrize("alter", [
    lambda value: value.update(client_pid=1234),
    lambda value: value["tasks"][0].update(tool_token="client-selected"),
    lambda value: value["tasks"][0]["scope"].update(user_id="user-b"),
    lambda value: value["tasks"][0]["scope"].update(experiment_id="another-experiment"),
    lambda value: value["tasks"][0]["scope"].update(session_id=value["history"]["scope"]["session_id"]),
    lambda value: value["tasks"][0]["scope"].update(variant=value["history"]["scope"]["variant"]),
    lambda value: value["tasks"][0].update(workspace=value["history"]["workspace"] + "/child"),
    lambda value: value["tasks"][0].update(model_token_file=value["history"]["output_dir"] + "/credential"),
    lambda value: value["tasks"][0].update(state_dir="/data/old/archived"),
])
def test_trusted_inventory_rejects_route_or_scope_substitution(material, alter):
    alter(material)
    with pytest.raises(ValueError):
        RUNTIME.validate_binding(material)


def test_different_new_task_variants_and_separate_native_stores_are_valid(material):
    assert RUNTIME.validate_binding(material) == material


def test_duplicate_or_nonfinite_json_cannot_change_a_command():
    for data in ('{"action":"state","action":"run_task"}', '{"time":NaN}'):
        with pytest.raises(ValueError):
            RUNTIME._decode(data)


def test_native_named_tuple_tasks_keep_field_names_and_actual_error_values():
    task = Task("native-task", "tools", ("pull", "tools"), RuntimeError("synthetic-boundary-failure"), (), None, None)
    snapshot = Snapshot({"messages": []}, ("tools",), {"configurable": {"checkpoint_id": "native-id"}}, {}, None, None, (task,), ())
    result = json.loads(json.dumps(RUNTIME._native(snapshot)))
    assert isinstance(result, dict)
    assert result["tasks"][0]["id"] == "native-task"
    assert result["tasks"][0]["error"]["exception_type"].endswith("RuntimeError")
    assert result["tasks"][0]["error"]["message"] == "synthetic-boundary-failure"


def test_startup_material_change_is_refused_before_native_construction(material, monkeypatch):
    runtime = RUNTIME.ResidentRuntime(material, {}, "0" * 64)
    content = {"/config/frozen": b"original synthetic material"}
    monkeypatch.setattr(RUNTIME, "_read_file", lambda filename, **kwargs: content[filename])
    assert runtime._material("/config/frozen") == content["/config/frozen"]
    content["/config/frozen"] = b"changed synthetic material"
    with pytest.raises(ValueError, match="material changed"):
        runtime._material("/config/frozen")


def test_readonly_configuration_is_enforced_on_the_actual_mount(tmp_path):
    path = tmp_path / "mutable-config"
    path.write_text("synthetic")
    with pytest.raises(ValueError, match="mounted read-only"):
        RUNTIME._read_file(str(path.resolve()), readonly=True)


def test_duplicate_scoped_credentials_fail_before_any_factory(material, monkeypatch):
    async def scenario():
        runtime = RUNTIME.ResidentRuntime(material, {}, "0" * 64)
        content = {}
        for entry in [material["history"], *material["tasks"]]:
            content[entry["tool_token_file"]] = b"duplicate-synthetic-scope-token"
            content[entry["model_token_file"]] = ("synthetic-model-" + entry["task_id"]).encode()
            content[entry["tools_schema_file"]] = b'{"tools":[]}'
            if "prompt_file" in entry:
                content[entry["prompt_file"]] = b"synthetic prompt"
        monkeypatch.setattr(RUNTIME, "_read_file", lambda filename, **kwargs: content[filename])
        factory = AsyncMock(side_effect=AssertionError("Native factory must not be reached"))
        monkeypatch.setattr(runtime, "_build", factory)
        with pytest.raises(ValueError, match="distinct scoped"):
            await runtime.open()
        factory.assert_not_called()
    asyncio.run(scenario())


class HumanMessageDouble:
    def __init__(self, *, content, id):
        self.content, self.id = content, id

    def model_dump(self, **kwargs):
        return {"type": "synthetic-human-boundary-double", "content": self.content, "id": self.id}


class PreparationGraphDouble:
    """Models only public boundary values; never executes native nodes."""
    def __init__(self, session_id, *, existing=False):
        self.config = {"configurable": {"thread_id": session_id, "checkpoint_id": "synthetic-checkpoint-id"}}
        self.values = {"messages": [HumanMessageDouble(content="existing synthetic state", id="existing")]} if existing else {}
        self.pending = ()
        self.calls = []

    async def aget_state(self, config):
        self.calls.append(("aget_state", deepcopy(config)))
        return Snapshot(self.values, self.pending, self.config, {"source": "synthetic-public-api-boundary-double"}, None, None, (), ())

    async def aupdate_state(self, config, values, *, as_node):
        self.calls.append(("aupdate_state", deepcopy(config), values, as_node))
        if as_node == "__start__":
            self.values = values
            self.pending = ("synthetic-model-never-executed",)
        elif as_node == "__end__" and values is None:
            self.pending = ()
        else:
            raise AssertionError("Unexpected preparation update")
        return deepcopy(self.config)

    def astream(self, *args, **kwargs):
        raise AssertionError("History preparation must not execute a task")


def preparation_fixture(material, monkeypatch, *, existing=False):
    runtime = RUNTIME.ResidentRuntime(material, {}, "0" * 64)
    runtime._loop = asyncio.get_running_loop()
    messages = ModuleType("langchain_core.messages")
    messages.HumanMessage = HumanMessageDouble
    constants = ModuleType("langgraph.constants")
    constants.START, constants.END = "__start__", "__end__"
    monkeypatch.setitem(sys.modules, "langchain_core.messages", messages)
    monkeypatch.setitem(sys.modules, "langgraph.constants", constants)
    monkeypatch.setitem(sys.modules, "detailed_support", SUPPORT)
    binding = material["history"]
    graph = PreparationGraphDouble(binding["scope"]["session_id"], existing=existing)
    evidence = SUPPORT.Evidence(binding["output_dir"], runtime.encode, binding["scope"])
    runtime.history = SimpleNamespace(graph=graph, binding=binding, evidence=evidence,
        identity=lambda: {"kind": "synthetic-history-object-double"}, close=AsyncMock())
    return runtime, graph


def test_preparation_records_true_boundary_input_without_claiming_agent_output(material, monkeypatch):
    async def scenario():
        runtime, graph = preparation_fixture(material, monkeypatch)
        result = await runtime.prepare_history()
        updates = [call for call in graph.calls if call[0] == "aupdate_state"]
        assert [call[3] for call in updates] == ["__start__", "__end__"]
        assert updates[1][2] is None
        assert result["agent_task_completed"] is False
        assert result["kind"] == "application_prepared_history_not_agent_business_output"
        assert result["state_is_nonempty"] and not result["next"]
        marker = Path(material["history"]["workspace"]) / "prepared-history.json"
        assert hashlib.sha256(marker.read_bytes()).hexdigest() == result["preparation_artifact"]["sha256"]
        saved = json.loads(marker.read_bytes())
        assert saved["scope"] == material["history"]["scope"]
        assert saved["sentinel"] == material["history"]["sentinel"]
        assert runtime._operation is None
    asyncio.run(scenario())


def test_preparation_refuses_existing_business_history_without_clearing_anything(material, monkeypatch):
    async def scenario():
        runtime, graph = preparation_fixture(material, monkeypatch, existing=True)
        before = deepcopy(graph.values)
        with pytest.raises(ValueError, match="new empty"):
            await runtime.prepare_history()
        assert not any(call[0] == "aupdate_state" for call in graph.calls)
        assert graph.values["messages"][0].content == before["messages"][0].content
        assert runtime._operation is None
    asyncio.run(scenario())


def test_foreign_task_cannot_read_another_native_store(material, monkeypatch):
    async def scenario():
        runtime, graph = preparation_fixture(material, monkeypatch)
        factory = AsyncMock(side_effect=AssertionError("Foreign task cannot reach a factory"))
        monkeypatch.setattr(runtime, "_build", factory)
        with pytest.raises(ValueError, match="immutable owner"):
            await runtime.state("business-b")
        factory.assert_not_called()
        assert graph.calls == []
    asyncio.run(scenario())


def test_same_task_result_is_never_overwritten_by_a_new_request(material, monkeypatch):
    async def scenario():
        runtime, _ = preparation_fixture(material, monkeypatch)
        output = Path(material["tasks"][0]["output_dir"]) / "result.json"
        output.write_bytes(b'{"original":"synthetic-original-result"}\n')
        before = output.read_bytes()
        with pytest.raises(ValueError, match="never overwritten"):
            await runtime.run_task(material["tasks"][0]["task_id"])
        assert output.read_bytes() == before
    asyncio.run(scenario())


def test_busy_or_poisoned_worker_never_starts_another_task(material, monkeypatch):
    async def scenario():
        runtime, _ = preparation_fixture(material, monkeypatch)
        runtime._operation = "already-running"
        with pytest.raises(ValueError, match="operation is active"):
            await runtime.run_task(material["tasks"][0]["task_id"])
        with pytest.raises(ValueError, match="Cannot recycle"):
            await runtime.close()
        runtime._operation = None
        runtime.poisoned = True
        with pytest.raises(ValueError, match="must be recycled"):
            await runtime.state(material["history"]["task_id"])
        original = runtime.history
        await runtime.close()
        original.close.assert_awaited_once()
        assert runtime.closed and runtime.history is None
    asyncio.run(scenario())


def test_completed_business_state_reads_the_same_retained_real_object_boundary(material, monkeypatch):
    async def scenario():
        runtime, _ = preparation_fixture(material, monkeypatch)
        binding = material["tasks"][0]
        retained = SimpleNamespace(binding=binding)
        runtime.retained_task = retained
        snapshot = AsyncMock(return_value=(None, {"checkpoint_id": "same-original-business-checkpoint"}))
        factory = AsyncMock(side_effect=AssertionError("Retained graph must not be reconstructed"))
        monkeypatch.setattr(runtime, "_snapshot", snapshot)
        monkeypatch.setattr(runtime, "_build", factory)
        result = await runtime.state(binding["task_id"])
        assert result["checkpoint_id"] == "same-original-business-checkpoint"
        snapshot.assert_awaited_once_with(retained, reason="naturally_completed_task_retained_read")
        factory.assert_not_called()
    asyncio.run(scenario())


def test_normal_recycle_closes_completed_business_objects_and_history(material, monkeypatch):
    async def scenario():
        runtime, _ = preparation_fixture(material, monkeypatch)
        binding = material["tasks"][0]
        evidence = SUPPORT.Evidence(binding["output_dir"], runtime.encode, binding["scope"])
        retained = SimpleNamespace(binding=binding, evidence=evidence, close=AsyncMock())
        runtime.retained_task = retained
        history = runtime.history
        await runtime.close()
        retained.close.assert_awaited_once()
        history.close.assert_awaited_once()
        assert runtime.retained_task is None and runtime.closed
        event = json.loads((Path(binding["output_dir"]) / "resident-lifecycle.jsonl").read_text())
        assert event["reason"] == "normal_runtime_recycle" and event["saver_context_closed"] is True
    asyncio.run(scenario())


def test_failed_completed_saver_close_poisoning_keeps_original_object_reference(material, monkeypatch):
    async def scenario():
        runtime, _ = preparation_fixture(material, monkeypatch)
        retained = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("synthetic saver-close failure")))
        runtime.retained_task = retained
        with pytest.raises(RuntimeError, match="saver-close"):
            await runtime._close_retained_task(reason="before_new_independent_task")
        assert runtime.poisoned and runtime.retained_task is retained
    asyncio.run(scenario())
