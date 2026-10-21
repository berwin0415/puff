"""I22 wiring calibration only; no candidate SDK, real graph or model required.

Run after the adapter patch is applied, in the normal bench test image. SQLite
bytes and graph snapshots below are explicit synthetic boundary inputs. Passing
these tests does not prove that a real GraphDrained/checkpoint/resume occurred.
"""
from __future__ import annotations

import asyncio
from collections import namedtuple
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ADAPTER = Path(__file__).resolve().parents[2] / "adapters" / "deepagents_langgraph"


def load(filename):
    spec = spec_from_file_location("harness_lab_i22_" + filename.replace(".py", ""), ADAPTER / filename)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAUSE = load("pause_recovery.py")
CONTROL = load("run_control.py")
SUPPORT = load("detailed_support.py")
Snapshot = namedtuple("Snapshot", "values next config metadata created_at parent_config tasks interrupts")
Task = namedtuple("Task", "id name path error interrupts state result")


def native_json(value):
    if hasattr(value, "_asdict"):
        return value._asdict()
    raise TypeError(type(value).__name__)


def canonical(value):
    # Independent standard JSON encoding of fixture inputs, not an adapter dump.
    return json.dumps(value, default=native_json, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()


def snapshot(*, checkpoint_id="checkpoint-a", head=False, pending=True):
    return Snapshot(
        values={"messages": [{"role": "assistant", "content": "live-applied" if head else "historical"}]},
        next=("tools",) if pending else (),
        config={"configurable": {"thread_id": "main", "checkpoint_ns": "", "checkpoint_id": checkpoint_id}},
        metadata={"step": 4, "source": "loop"}, created_at="2026-10-20T00:00:00Z", parent_config=None,
        tasks=(Task("future-task", "tools", ("pull", "tools"), None, (), None, None),) if pending else (), interrupts=())


class SnapshotReaderDouble:
    """Only returns supplied API boundary values; cannot run business work."""
    def __init__(self, exact, head):
        self.exact, self.head, self.calls = exact, head, []

    async def aget_state(self, config):
        self.calls.append(deepcopy(config))
        return self.exact if "checkpoint_id" in config["configurable"] else self.head

    def astream(self, *args, **kwargs):
        raise AssertionError("Helper calibration must never execute a graph")


class PauseFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-lab-i22-deepagents-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("state", "workspace", "out-a", "out-b"):
            (self.root / name).mkdir()
        self.scope = {"experiment_id": "pause-recovery", "variant": "test-chain", "user_id": "alice", "session_id": "main"}
        self.provenance = {"source_export_sha256": "1" * 64, "adapter_export_sha256": "2" * 64}
        self.definitions = [{"name": "read", "description": "Read a fixture file", "input_schema": {"type": "object",
            "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
        self.request = {"run_id": "test-chain", "session_id": "main", "prompt": "Perform the frozen task",
            "workspace": str(self.root / "workspace"), "state_dir": str(self.root / "state"), "output_dir": str(self.root / "out-a"),
            "tools_url": "http://tools:8081", "tool_token_file": str(self.root / "tool-token"),
            "max_output_tokens": 2048, "timeout_seconds": 300, "max_retries": 0,
            "candidate_options": {"case": "tools", "trusted_scope_file": "/config/scope.json"},
            "lifecycle": {"mode": "new", "chain_id": "test-chain", "attempt_id": "worker-a", "binding_file": "/config/lifecycle.json"}}
        (self.root / "tool-token").write_text("synthetic-not-a-credential")
        self.binding = {"schema": "harness-lab-lifecycle/1", "candidate": "deepagents_langgraph", "mode": "new",
            "chain_id": "test-chain", "attempt_id": "worker-a", "scope": deepcopy(self.scope), "session_id": "main", "checkpoint_ns": "",
            **self.provenance, "runtime_sha256": self.runtime_hash(self.definitions),
            "budget": {"active_limit_seconds": 300, "used_active_seconds": 0, "remaining_seconds": 300}, "recovery": None}
        actual_read = PAUSE._read_file

        def trusted_binding_read(path, limit):
            if Path(path) == Path("/config/lifecycle.json"):
                return canonical(self.binding)
            return actual_read(path, limit)

        # Narrow mount-boundary substitutes only: all state/workspace/index,
        # file hashes, atomic writes, fsync and flock below are real filesystem IO.
        self.read_patch = patch.object(PAUSE, "_read_file", side_effect=trusted_binding_read)
        self.mount_patch = patch.object(PAUSE.os, "statvfs", return_value=SimpleNamespace(f_flag=os.ST_RDONLY))
        self.read_patch.start()
        self.mount_patch.start()
        self.addCleanup(self.read_patch.stop)
        self.addCleanup(self.mount_patch.stop)

    def runtime_hash(self, definitions):
        return PAUSE.runtime_identity_sha256(model_name="frozen-model", max_output_tokens=2048,
            max_retries=0, candidate_options={"case": "tools", "trusted_scope_file": "/config/scope.json"},
            tool_definitions=definitions)

    def acquire(self, request=None):
        helper = PAUSE.PauseRecovery(request or self.request, self.scope, self.provenance, native_json, time.monotonic())
        self.addCleanup(helper.close)
        return helper

    def assert_lock_available(self):
        descriptor = os.open(self.root / "state" / PAUSE.LOCK, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    async def prepare(self, helper, *, exact=None, head=None):
        (self.root / "state" / "checkpoints.sqlite").write_bytes(b"synthetic SQL boundary; not a native checkpoint")
        (self.root / "workspace" / "already-written.txt").write_text("original output")
        graph = SnapshotReaderDouble(exact or snapshot(), head or snapshot(head=True))
        await helper.prepare_pause(graph, helper.execution_config(), SimpleNamespace(tasks={}), {"pause_boundary_observed": True})
        return graph

    async def close_a_and_bind_b(self):
        helper = self.acquire()
        await self.prepare(helper)
        helper.publish(saver_closed=True, tools_closed=True)
        data = (self.root / "state" / PAUSE.INDEX).read_bytes()
        index = json.loads(data)
        helper.close()
        self.binding.update({"mode": "resume", "attempt_id": "worker-b",
            "budget": {"active_limit_seconds": 300, "used_active_seconds": 60, "remaining_seconds": 240},
            "recovery": {"attempt_id": "worker-a", "pause_index_sha256": hashlib.sha256(data).hexdigest(),
                **{key: index[key] for key in ("checkpoint_id", "exact_snapshot_sha256", "head_snapshot_sha256",
                    "state_files_sha256", "workspace_files_sha256")}}})
        request = deepcopy(self.request)
        request.pop("prompt")
        request.update({"output_dir": str(self.root / "out-b"), "timeout_seconds": 240,
            "lifecycle": {**request["lifecycle"], "mode": "resume", "attempt_id": "worker-b"}})
        return request, index

    async def test_distinct_historical_and_live_views_are_checked_in_their_own_mode(self):
        request, _ = await self.close_a_and_bind_b()
        helper = self.acquire(request)
        graph = SnapshotReaderDouble(snapshot(), snapshot(head=True))
        result = await helper.check_resume(graph, helper.execution_config())
        self.assertTrue(result["exact_and_head_verified"])
        self.assertEqual(graph.calls[0]["configurable"]["checkpoint_id"], "checkpoint-a")
        self.assertNotIn("checkpoint_id", graph.calls[1]["configurable"])
        self.assertNotEqual(graph.exact.values, graph.head.values)

    async def test_changed_exact_checkpoint_content_is_refused(self):
        request, _ = await self.close_a_and_bind_b()
        helper = self.acquire(request)
        altered = snapshot()._replace(values={"messages": [{"role": "assistant", "content": "tampered"}]})
        with self.assertRaisesRegex(ValueError, "exact_snapshot"):
            await helper.check_resume(SnapshotReaderDouble(altered, snapshot(head=True)), helper.execution_config())

    async def test_advanced_head_is_refused_even_when_original_checkpoint_exists(self):
        request, _ = await self.close_a_and_bind_b()
        helper = self.acquire(request)
        with self.assertRaisesRegex(ValueError, "current head"):
            await helper.check_resume(SnapshotReaderDouble(snapshot(), snapshot(checkpoint_id="later-checkpoint", head=True)), helper.execution_config())

    async def test_same_id_with_changed_live_content_is_refused(self):
        request, _ = await self.close_a_and_bind_b()
        helper = self.acquire(request)
        altered = snapshot(head=True)._replace(values={"messages": []})
        with self.assertRaisesRegex(ValueError, "head_snapshot"):
            await helper.check_resume(SnapshotReaderDouble(snapshot(), altered), helper.execution_config())

    async def test_execution_config_cannot_supply_checkpoint_id_or_checkpoint_map(self):
        request, _ = await self.close_a_and_bind_b()
        helper = self.acquire(request)
        for extra in ({"checkpoint_id": "checkpoint-a"}, {"checkpoint_map": {"": "checkpoint-a"}}):
            with self.subTest(extra=extra):
                graph = SnapshotReaderDouble(snapshot(), snapshot(head=True))
                config = helper.execution_config()
                config["configurable"].update(extra)
                with self.assertRaisesRegex(ValueError, "no checkpoint_id"):
                    await helper.check_resume(graph, config)
                self.assertEqual(graph.calls, [])
        self.assertEqual(helper.execution_config(), {"configurable": {"thread_id": "main", "checkpoint_ns": ""}, "recursion_limit": 64})

    async def test_resume_rejects_any_new_prompt_including_empty_or_null(self):
        request, _ = await self.close_a_and_bind_b()
        for prompt in ("original task", "continue", "", None):
            with self.subTest(prompt=prompt):
                with self.assertRaisesRegex(ValueError, "omit prompt"):
                    PAUSE.validate_lifecycle({**request, "prompt": prompt})
        self.assertEqual(PAUSE.validate_lifecycle(request)["mode"], "resume")

    async def test_changed_sqlite_or_companion_is_refused_before_graph_access(self):
        request, _ = await self.close_a_and_bind_b()
        (self.root / "state" / "checkpoints.sqlite").write_bytes(b"modified state")
        with self.assertRaisesRegex(ValueError, "state/workspace changed"):
            self.acquire(request)
        self.assert_lock_available()

    async def test_unexpected_wal_is_not_silently_ignored(self):
        request, _ = await self.close_a_and_bind_b()
        (self.root / "state" / "checkpoints.sqlite-wal").write_bytes(b"unprotected WAL contents")
        with self.assertRaisesRegex(ValueError, "state/workspace changed"):
            self.acquire(request)
        self.assert_lock_available()

    async def test_changed_workspace_is_refused(self):
        request, _ = await self.close_a_and_bind_b()
        (self.root / "workspace" / "already-written.txt").write_text("overwritten")
        with self.assertRaisesRegex(ValueError, "state/workspace changed"):
            self.acquire(request)
        self.assert_lock_available()

    async def test_replaced_private_index_is_refused_without_rebinding_trust(self):
        request, _ = await self.close_a_and_bind_b()
        path = self.root / "state" / PAUSE.INDEX
        value = json.loads(path.read_bytes())
        value["checkpoint_id"] = "forged-checkpoint"
        path.write_bytes(canonical(value))
        with self.assertRaisesRegex(ValueError, "protected recovery binding"):
            self.acquire(request)
        self.assert_lock_available()

    async def test_real_kernel_lock_rejects_a_second_executor_and_releases_on_close(self):
        first = self.acquire()
        with self.assertRaisesRegex(ValueError, "Another executor"):
            self.acquire()
        first.close()
        self.assert_lock_available()
        second = self.acquire()
        self.assertIsNotNone(second.lock_descriptor)

    async def test_chain_lock_excludes_a_separate_process(self):
        helper = self.acquire()
        probe = """import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(73)
finally:
    os.close(fd)
"""
        arguments = [sys.executable, "-c", probe, str(self.root / "state" / PAUSE.LOCK)]
        first = subprocess.run(arguments, capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(first.returncode, 73, first.stderr)
        helper.close()
        second = subprocess.run(arguments, capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(second.returncode, 0, second.stderr)

    async def test_initialization_failure_after_lock_releases_it(self):
        (self.root / "state" / "unexpected.sqlite").write_bytes(b"existing state")
        with self.assertRaisesRegex(ValueError, "empty native state"):
            self.acquire()
        self.assert_lock_available()

    async def test_remaining_budget_cannot_reset_to_300_on_resume(self):
        request, _ = await self.close_a_and_bind_b()
        self.binding["budget"]["remaining_seconds"] = 300
        request["timeout_seconds"] = 300
        with self.assertRaisesRegex(ValueError, "remaining chain budget"):
            self.acquire(request)

    async def test_budget_cannot_erase_previously_recorded_active_use(self):
        request, _ = await self.close_a_and_bind_b()
        self.binding["budget"] = {"active_limit_seconds": 300, "used_active_seconds": 0, "remaining_seconds": 300}
        request["timeout_seconds"] = 300
        with self.assertRaisesRegex(ValueError, "omits the previous active"):
            self.acquire(request)
        self.assert_lock_available()

    async def test_local_setup_time_consumes_the_provided_remaining_budget(self):
        helper = self.acquire()
        helper.started = time.monotonic() - 301
        with self.assertRaises(TimeoutError):
            helper.remaining()

    async def test_request_cannot_claim_more_than_trusted_remaining_budget(self):
        self.binding["budget"]["remaining_seconds"] = 200
        with self.assertRaisesRegex(ValueError, "remaining chain budget"):
            self.acquire()

    async def test_failed_native_or_tool_close_never_publishes_an_index(self):
        helper = self.acquire()
        await self.prepare(helper)
        for saver_closed, tools_closed in ((False, True), (True, False), (False, False)):
            with self.subTest(saver_closed=saver_closed, tools_closed=tools_closed):
                with self.assertRaisesRegex(ValueError, "successful native/tool close"):
                    helper.publish(saver_closed=saver_closed, tools_closed=tools_closed)
                self.assertFalse((self.root / "state" / PAUSE.INDEX).exists())
        self.assertEqual(list((self.root / "state").glob(".pause-index.json.tmp.*")), [])

    async def test_index_fsync_failure_is_not_reported_as_a_saved_pause(self):
        helper = self.acquire()
        await self.prepare(helper)
        with patch.object(PAUSE.os, "fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                helper.publish(saver_closed=True, tools_closed=True)
        self.assertFalse((self.root / "state" / PAUSE.INDEX).exists())

    async def test_directory_fsync_failure_withdraws_the_published_index(self):
        helper = self.acquire()
        await self.prepare(helper)
        actual_fsync = PAUSE.os.fsync

        def fail_directory_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("synthetic directory fsync failure")
            return actual_fsync(descriptor)

        with patch.object(PAUSE.os, "fsync", side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(OSError, "directory fsync"):
                helper.publish(saver_closed=True, tools_closed=True)
        self.assertFalse((self.root / "state" / PAUSE.INDEX).exists())
        self.assertEqual(list((self.root / "state").glob(".pause-index.json.tmp.*")), [])

    async def test_inflight_native_task_blocks_safe_pause(self):
        helper = self.acquire()
        with self.assertRaisesRegex(ValueError, "zero active observed operations"):
            await helper.prepare_pause(SnapshotReaderDouble(snapshot(), snapshot(head=True)),
                helper.execution_config(), SimpleNamespace(tasks={"task-a": {"name": "model"}}), {"pause_boundary_observed": True})
        self.assertIsNone(helper.pending)

    async def test_completed_task_or_native_interrupt_cannot_be_published_as_pause(self):
        helper = self.acquire()
        for invalid in (snapshot(pending=False), snapshot(head=True)._replace(interrupts=("needs-input",))):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "unfinished clean drain boundary"):
                    await self.prepare(helper, head=invalid)
                self.assertIsNone(helper.pending)
        self.assertFalse((self.root / "state" / PAUSE.INDEX).exists())

    async def test_inflight_or_unknown_http_result_blocks_safe_pause(self):
        helper = self.acquire()
        helper.observe_http({"phase": "start", "call_id": "write-1", "tool": "write"})
        with self.assertRaisesRegex(ValueError, "zero active observed operations"):
            await self.prepare(helper)
        helper.observe_http({"phase": "error", "call_id": "write-1", "tool": "write"})
        with self.assertRaisesRegex(ValueError, "zero active observed operations"):
            await self.prepare(helper)
        self.assertIsNone(helper.pending)

    async def test_actual_runtime_tool_schema_change_is_refused(self):
        helper = self.acquire()
        tools = [SimpleNamespace(name="lab_read", description="Read a fixture file", args_schema={"type": "object",
            "properties": {"path": {"type": "integer"}}, "required": ["path"]})]
        with patch.dict(os.environ, {"MODEL_NAME": "frozen-model", "MODEL_API_BASE_URL": "http://model-proxy:8082/v1"}):
            with self.assertRaisesRegex(ValueError, "differs from trusted binding"):
                helper.bind_runtime(tools, PAUSE.TOOLS_SYSTEM_PROMPT, PAUSE.BUILTIN_TOOL_EXCLUSIONS)

    async def test_scope_or_source_change_is_refused_before_lock_acquisition(self):
        self.binding["scope"]["user_id"] = "bob"
        with self.assertRaisesRegex(ValueError, "scope/thread/namespace"):
            self.acquire()
        self.assertFalse((self.root / "state" / PAUSE.LOCK).exists())
        self.binding["scope"] = deepcopy(self.scope)
        self.binding["source_export_sha256"] = "3" * 64
        with self.assertRaisesRegex(ValueError, "source/adapter mismatch"):
            self.acquire()
        self.assertFalse((self.root / "state" / PAUSE.LOCK).exists())

    async def test_read_write_binding_mount_is_refused(self):
        with patch.object(PAUSE.os, "statvfs", return_value=SimpleNamespace(f_flag=0)):
            with self.assertRaisesRegex(ValueError, "read-only mount"):
                self.acquire()
        self.assertFalse((self.root / "state" / PAUSE.LOCK).exists())


class SignalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-lab-i22-deepagents-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = {"experiment_id": "pause-recovery", "variant": "chain", "user_id": "alice", "session_id": "main"}
        self.evidence = SUPPORT.Evidence(self.root, lambda value: json.dumps(value, default=native_json) + "\n", self.scope)
        self.native = SimpleNamespace(calls=[], drain_requested=False, drain_reason=None)

        def request_drain(reason):
            self.native.calls.append(reason)
            self.native.drain_requested = True
            self.native.drain_reason = reason

        self.native.request_drain = request_drain
        self.glue = CONTROL.NativeRunControl(self.native, self.evidence, "chain", self.scope,
            lifecycle={"mode": "new", "chain_id": "chain", "attempt_id": "worker-a", "binding_file": "/config/lifecycle.json"})
        self.addCleanup(self.glue.close)

    def start(self, task_id="task-1"):
        self.glue.active = True
        event = {"id": task_id, "name": "model", "input": {}}
        source = self.evidence.append("raw-events.jsonl", {"stream_mode": "tasks", "data": event})
        self.glue.observe("tasks", event, source)

    def finish(self, task_id="task-1"):
        event = {"id": task_id, "result": {}, "error": None, "interrupts": []}
        source = self.evidence.append("raw-events.jsonl", {"stream_mode": "tasks", "data": event})
        self.glue.observe("tasks", event, source)

    def events(self):
        return [json.loads(line) for line in (self.root / "control-events.jsonl").read_text().splitlines()]

    def test_early_pause_is_rejected_and_does_not_queue_for_the_next_task(self):
        self.glue.request_pause()
        self.assertEqual(self.native.calls, [])
        self.start()
        self.assertEqual(self.native.calls, [])
        self.glue.request_pause()
        self.assertEqual(self.native.calls, ["experiment_pause"])
        self.assertEqual(self.events()[0]["refusal_reason"], "no_active_native_task")

    def test_late_pause_cannot_use_stale_task_activity(self):
        self.start()
        self.finish()
        self.glue.request_pause()
        self.assertEqual(self.native.calls, [])
        self.assertIsNone(self.glue.request)
        self.assertEqual(self.events()[-1]["refusal_reason"], "no_active_native_task")

    def test_duplicate_pause_calls_the_public_api_only_once(self):
        self.start()
        self.glue.request_pause()
        self.glue.request_pause()
        self.assertEqual(self.native.calls, ["experiment_pause"])
        self.assertEqual(self.events()[-1]["refusal_reason"], "duplicate_request")

    def test_pause_then_cancel_keeps_the_original_pause_reason(self):
        self.start()
        self.glue.request_pause()
        self.glue.request_cancel()
        self.assertEqual(self.native.calls, ["experiment_pause"])
        self.assertEqual(self.events()[-1]["refusal_reason"], "duplicate_request")
        self.assertEqual(self.glue.request["reason"], "experiment_pause")

    def test_cancel_then_pause_keeps_the_original_cancel_reason(self):
        self.start()
        self.glue.request_cancel()
        self.glue.request_pause()
        self.assertEqual(self.native.calls, ["experiment_cancel"])
        self.assertEqual(self.events()[-1]["refusal_reason"], "duplicate_request")
        self.assertEqual(self.glue.request["reason"], "experiment_cancel")

    def test_wrong_reason_or_active_task_never_confirms_a_pause_boundary(self):
        self.start()
        self.glue.request_pause()
        config = {"configurable": {"thread_id": "main", "checkpoint_id": "checkpoint-a"}}
        (self.root / "state-snapshot.json").write_text(json.dumps({"config": config}))
        value = SimpleNamespace(config=config, next=("tools",), tasks=())
        class DrainedDouble(Exception):
            reason = "experiment_pause"
        observation = self.glue.drained(DrainedDouble(), value)
        self.assertFalse(observation["pause_boundary_observed"])
        self.finish()
        class WrongReasonDouble(Exception):
            reason = "experiment_cancel"
        observation = self.glue.drained(WrongReasonDouble(), value)
        self.assertFalse(observation["pause_boundary_observed"])
        self.assertFalse(observation["cancel_confirmed"])

    def test_matching_pause_boundary_is_separate_from_durable_pause_confirmation(self):
        self.start()
        self.glue.request_pause()
        self.finish()
        config = {"configurable": {"thread_id": "main", "checkpoint_id": "checkpoint-a"}}
        (self.root / "state-snapshot.json").write_text(json.dumps({"config": config}))
        class DrainedDouble(Exception):
            reason = "experiment_pause"
        observation = self.glue.drained(DrainedDouble(), SimpleNamespace(config=config, next=("tools",), tasks=()))
        self.assertTrue(observation["pause_boundary_observed"])
        self.assertFalse(observation["cancel_confirmed"])
        self.assertNotIn("pause_confirmed", observation)
        self.assertFalse((self.root / "pause-index.json").exists())


if __name__ == "__main__":
    unittest.main()
