"""I22 验收测试初稿，最初只编写源码，未导入或执行。

复核应用后在新固定验收镜像中运行。使用合成材料测试证据不足
时拒绝通过的规则，不运行 Agent 或真实模型。合成通过不算 I22 能力结果。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runner import pause_recovery as p


SCOPE = {"experiment_id": "pause-recovery", "variant": "chain-101", "user_id": "alice", "session_id": "main"}
ZERO = "0" * 64
ONE = "1" * 64
TWO = "2" * 64
BASE = datetime(2026, 10, 20, 0, 0, 0, tzinfo=timezone.utc)


def stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(p.canonical(value) + b"\n")


def put_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(p.canonical(row) + b"\n" for row in rows))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="i22-bench-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = SimpleNamespace(**{name: self.root / name for name in ("run", "state", "workspace", "fixture", "budget")})
        for path in vars(self.paths).values():
            path.mkdir()
        for attempt in ("worker-a", "worker-b"):
            (self.paths.run / "attempts" / attempt / "candidate").mkdir(parents=True)
            (self.paths.fixture / "attempts" / attempt).mkdir(parents=True)
        (self.paths.run / "trusted").mkdir()
        self.args = argparse.Namespace(candidate="deepagents_langgraph", chain_id="chain-101", seed=101, date="2026-10-20")
        self.config = SimpleNamespace(host_data_root=Path("/host-data"), container_data_root=self.root)

    def expect_failure(self, code, callback):
        with self.assertRaisesRegex(ValueError, code):
            callback()


class StrictMaterialTests(Fixture):
    def test_duplicate_and_partial_json_are_rejected(self):
        target = self.paths.run / "events.jsonl"
        target.write_bytes(b'{"id":1,"id":2}\n')
        self.expect_failure("duplicate_json_key", lambda: p.jsonl(target))
        target.write_bytes(b'{"id":1}')
        self.expect_failure("partial_jsonl_line", lambda: p.jsonl(target))

    def test_references_bind_physical_line_not_parsed_equivalent(self):
        target = self.paths.run / "events.jsonl"
        target.write_bytes(b'{ "id": 1 }\n')
        ref = {"path": "events.jsonl", "line": 1, "line_sha256": p.sha(target.read_bytes())}
        self.assertEqual(p.reference(self.paths.run, ref, required_hash=True)[0], {"id": 1})
        target.write_bytes(b'{"id":1}\n')
        self.expect_failure("source_line_hash_mismatch", lambda: p.reference(self.paths.run, ref, required_hash=True))

    def test_full_native_inventory_detects_empty_directory_and_extra_file(self):
        (self.paths.state / "sessions").mkdir()
        (self.paths.workspace / "request.json").write_text("{}")
        before = p.inventory(self.paths.state, self.paths.workspace)
        self.assertEqual(before["directories"], [
            {"root": "state", "path": ""}, {"root": "state", "path": "sessions"}, {"root": "workspace", "path": ""}])
        (self.paths.state / "sessions" / "unexpected-empty-dir").mkdir()
        self.assertNotEqual(before, p.inventory(self.paths.state, self.paths.workspace))
        (self.paths.state / "unexpected-file").write_text("x")
        self.assertNotEqual(before["files"], p.inventory(self.paths.state, self.paths.workspace)["files"])

    def test_native_symlinks_and_hardlinks_rejected(self):
        real = self.paths.state / "real"
        real.write_text("state")
        linked = self.paths.state / "linked"
        linked.symlink_to(real)
        self.expect_failure("special_or_linked_native_file", lambda: p.inventory(self.paths.state, self.paths.workspace))
        linked.unlink()
        os.link(real, linked)
        self.expect_failure("special_or_linked_native_file", lambda: p.inventory(self.paths.state, self.paths.workspace))

    def test_native_da_snapshot_cannot_be_completed_or_dirty(self):
        snapshot = {"config": {"configurable": {"thread_id": "main", "checkpoint_ns": "", "checkpoint_id": "cp-1"}},
                    "next": ["model"], "tasks": [["t", "model", ["__pregel_pull", "model"], None, [], None, None]], "interrupts": []}
        p.clean_da_snapshot(snapshot, SCOPE, "cp-1")
        snapshot["next"] = []
        self.expect_failure("native_checkpoint_not_clean_unfinished", lambda: p.clean_da_snapshot(snapshot, SCOPE, "cp-1"))
        snapshot["next"] = ["model"]
        snapshot["tasks"][0][3] = "native-error"
        self.expect_failure("native_checkpoint_not_clean_unfinished", lambda: p.clean_da_snapshot(snapshot, SCOPE, "cp-1"))

    def test_native_da_task_projection_matches_pinned_namedtuple_wire_shape(self):
        snapshot = {"config": {"configurable": {"thread_id": "main", "checkpoint_ns": "", "checkpoint_id": "cp-1"}},
                    "next": ["tools"], "tasks": [["t", "tools", ["__pregel_push", 0, False], None, [], None, None]], "interrupts": []}
        p.clean_da_snapshot(snapshot, SCOPE, "cp-1")
        for task in ({"id": "t", "error": None, "interrupts": []},
                     ["t", "tools", ["push"], None, []],
                     ["t", "wrong-node", ["push"], None, [], None, None],
                     ["t", "tools", ["push"], None, [{"value": "interrupted"}], None, None],
                     ["t", "tools", ["push"], "error", [], None, None]):
            snapshot["tasks"] = [task]
            self.expect_failure("native_checkpoint_not_clean_unfinished", lambda: p.clean_da_snapshot(snapshot, SCOPE, "cp-1"))


class FinalCheckpointTests(Fixture):
    def test_completed_checkpoint_has_no_remaining_native_tasks(self):
        p.clean_da_final_snapshot({"next": [], "tasks": [], "interrupts": []})
        for snapshot in (
            {"next": ["tools"], "tasks": [], "interrupts": []},
            {"next": [], "tasks": [["t", "tools", ["push"], None, [], None, None]], "interrupts": []},
            {"next": [], "tasks": [{"error": None, "interrupts": []}], "interrupts": []},
            {"next": [], "tasks": [], "interrupts": [{"value": "interrupted"}]},
            {"next": [], "interrupts": []},
            {"next": [], "tasks": [], "interrupts": None},
            None,
        ):
            self.expect_failure("native_da_final_checkpoint_unfinished", lambda: p.clean_da_final_snapshot(snapshot))


class RestoredHeaderTests(Fixture):
    def test_official_persistence_materializes_only_default_delegation_depth(self):
        header = {"version": 0, "id": "main", "createdAt": 100, "cwd": "/workspace"}
        p.restored_dsh_header(header, {**header, "delegationDepth": 0})
        explicit = {**header, "delegationDepth": 2, "parentSession": "parent"}
        p.restored_dsh_header(explicit, explicit)

    def test_changed_identity_metadata_or_depth_cannot_be_hidden_by_defaulting(self):
        header = {"version": 0, "id": "main", "createdAt": 100, "cwd": "/workspace"}
        for restored in (header, {**header, "delegationDepth": 1}, {**header, "delegationDepth": False},
                         {**header, "delegationDepth": 0, "id": "other"},
                         {**header, "delegationDepth": 0, "cwd": "/other"},
                         {**header, "delegationDepth": 0, "parentSession": "other"}):
            self.expect_failure("native_dsh_restored_header_invalid", lambda: p.restored_dsh_header(header, restored))
        for depth in (None, False, -1, 0.5, 2**53):
            value = {**header, "delegationDepth": depth}
            self.expect_failure("native_dsh_restored_header_invalid", lambda: p.restored_dsh_header(value, value))


class TriggerTests(Fixture):
    def trigger_fixture(self, candidate):
        start = {"time": stamp(10), "scope": SCOPE, "stream_mode": "tasks", "data": {"id": "task", "name": "model", "input": {}}}
        finish = {"time": stamp(21), "scope": SCOPE, "stream_mode": "tasks", "data": {"id": "task", "result": {}}}
        if candidate == "deepseek_harness":
            start = {"method": "session.event", "params": {"sessionId": "main", "event": {"type": "turn/start", "data": {"turn": 1}}}}
            finish = {"method": "session.event", "params": {"sessionId": "main", "event": {"type": "turn/end", "data": {"turn": 1}}}}
            put(self.paths.run / "attempts/worker-a/candidate/session-before-followup.json", {"header": {"id": "main", "cwd": "/workspace"}})
        relative = "attempts/worker-a/candidate/raw-events.jsonl"
        put_lines(self.paths.run / relative, [start, finish])
        write = {"time": stamp(9), "scope": SCOPE, "tool": "write", "phase": "finish", "status": 200,
                 "result": {"receipt": {"operation_id": "original-write"}}}
        put_lines(self.paths.run / "tool-events.jsonl", [write])
        prefix = p.canonical(start) + b"\n"
        return {"sent_at": stamp(20), "activity_source": {"path": relative, "line": 1,
            "line_sha256": p.sha(prefix), "observed_through_line": 1, "observed_prefix_sha256": p.sha(prefix)},
            "write_source": {"path": "tool-events.jsonl", "line": 1, "line_sha256": p.sha(p.canonical(write) + b"\n")}}

    def test_da_running_at_signal_uses_observed_prefix_not_final_file(self):
        trigger = self.trigger_fixture("deepagents_langgraph")
        p.validate_trigger(self.paths.run, SCOPE, "deepagents_langgraph", trigger)
        source = trigger["activity_source"]
        source["observed_through_line"] = 2
        source["observed_prefix_sha256"] = p.sha((self.paths.run / source["path"]).read_bytes())
        self.expect_failure("trigger_native_prefix_scope_or_time", lambda: p.validate_trigger(self.paths.run, SCOPE, "deepagents_langgraph", trigger))

    def test_dsh_native_events_do_not_need_invented_scope_or_timestamp(self):
        trigger = self.trigger_fixture("deepseek_harness")
        p.validate_trigger(self.paths.run, SCOPE, "deepseek_harness", trigger)
        source = trigger["activity_source"]
        source["observed_through_line"] = 2
        source["observed_prefix_sha256"] = p.sha((self.paths.run / source["path"]).read_bytes())
        self.expect_failure("pause_was_not_during_actual_native_activity", lambda: p.validate_trigger(self.paths.run, SCOPE, "deepseek_harness", trigger))

    def test_missing_prefix_or_confirmed_write_cannot_pass(self):
        trigger = self.trigger_fixture("deepseek_harness")
        del trigger["activity_source"]["observed_through_line"]
        self.expect_failure("trigger_observed_native_prefix_missing", lambda: p.validate_trigger(self.paths.run, SCOPE, "deepseek_harness", trigger))

    def causal_fixture(self, candidate='deepseek_harness'):
        trigger = self.trigger_fixture(candidate)
        trigger.update(observation_clock='host_monotonic_prefix/1', observation_started_monotonic=100.0,
                       observation_finished_monotonic=100.1, signal_sent_monotonic=100.2,
                       signal_acknowledged_monotonic=100.3)
        ref = trigger['write_source']
        ref.update(observed_through_line=1, observed_prefix_sha256=p.sha((self.paths.run / ref['path']).read_bytes()))
        filename = 'control-events.jsonl' if candidate == 'deepagents_langgraph' else 'pause-events.jsonl'
        relative = 'attempts/worker-a/candidate/' + filename
        put_lines(self.paths.run / relative, [{'phase': 'requested', 'time': stamp(20), 'scope': SCOPE}])
        trigger['control_source'] = {'path': relative, 'observed_through_line': 0, 'observed_prefix_sha256': p.sha(b'')}
        return trigger

    def test_host_observed_write_survives_vm_clock_skew_without_tolerance(self):
        for candidate in ('deepagents_langgraph', 'deepseek_harness'):
            trigger = self.causal_fixture(candidate)
            path = self.paths.run / 'tool-events.jsonl'
            write = json.loads(path.read_bytes())
            write['time'] = stamp(20.002)  # 真实失败时间早约2毫秒。
            put_lines(path, [write])
            trigger['write_source'].update(line_sha256=p.sha(path.read_bytes()), observed_prefix_sha256=p.sha(path.read_bytes()))
            p.validate_trigger(self.paths.run, SCOPE, candidate, trigger)
            filename = Path(trigger['control_source']['path']).name
            self.assertTrue(p.validate_handler_observation(self.paths.run, candidate, trigger, {'path': filename, 'line': 1}))

    def test_later_write_cannot_be_added_to_the_observed_prefix(self):
        trigger = self.causal_fixture()
        put_lines(self.paths.run / 'tool-events.jsonl', [{'scope': SCOPE, 'tool': 'write', 'phase': 'finish',
            'status': 200, 'time': stamp(30), 'result': {'receipt': {'operation_id': 'different-write'}}}])
        self.expect_failure('source_line_hash_mismatch', lambda: p.validate_trigger(self.paths.run, SCOPE, 'deepseek_harness', trigger))

    def test_missing_or_changed_observed_write_prefix_is_rejected(self):
        trigger = self.causal_fixture()
        trigger['write_source']['observed_prefix_sha256'] = ZERO
        self.expect_failure('trigger_observed_native_prefix_changed', lambda: p.validate_trigger(self.paths.run, SCOPE, 'deepseek_harness', trigger))

    def test_reversed_host_observation_order_is_rejected(self):
        trigger = self.causal_fixture()
        trigger['signal_sent_monotonic'] = 99
        self.expect_failure('trigger_host_observation_order_invalid', lambda: p.validate_trigger(self.paths.run, SCOPE, 'deepseek_harness', trigger))

    def test_pause_already_requested_at_observation_cannot_pass(self):
        trigger = self.causal_fixture()
        ref = trigger['control_source']
        ref.update(observed_through_line=1, observed_prefix_sha256=p.sha((self.paths.run / ref['path']).read_bytes()))
        self.expect_failure('pause_handler_already_requested_before_signal', lambda: p.validate_trigger(self.paths.run, SCOPE, 'deepseek_harness', trigger))

    def test_ended_native_activity_cannot_pass_with_host_causality(self):
        trigger = self.causal_fixture()
        ref = trigger['activity_source']
        ref.update(observed_through_line=2, observed_prefix_sha256=p.sha((self.paths.run / ref['path']).read_bytes()))
        self.expect_failure('pause_was_not_during_actual_native_activity', lambda: p.validate_trigger(self.paths.run, SCOPE, 'deepseek_harness', trigger))


class ModelBudgetTests(Fixture):
    def budget(self, outcome="forwarded"):
        (self.paths.budget / "run.token").write_text("private-test-token\n")
        with sqlite3.connect(self.paths.budget / "calls.sqlite") as db:
            db.execute("CREATE TABLE budget(id INTEGER,token_hash TEXT,cap INTEGER)")
            db.execute("CREATE TABLE calls(id INTEGER,started_at TEXT,path TEXT,outcome TEXT,upstream_status INTEGER)")
            db.execute("INSERT INTO budget VALUES(1,?,20)", (p.sha(b"private-test-token"),))
            db.execute("INSERT INTO calls VALUES(1,?,?,?,?)", (stamp(1), "/chat/completions", outcome, 200 if outcome == "forwarded" else None))

    def test_budget_roundtrip_preserves_prefix_types_and_never_serializes_token(self):
        self.budget()
        before = p.budget_snapshot(self.paths.budget)
        encoded = p.canonical(before)
        self.assertNotIn(b"private-test-token", encoded)
        self.assertEqual(before, p.decode(encoded))
        self.assertIsInstance(before["rows"][0], list)

    def test_reserved_or_new_token_is_not_safe_pause_budget(self):
        self.budget("reserved")
        self.expect_failure("inflight_or_unknown_model_result", lambda: p.budget_snapshot(self.paths.budget))
        (self.paths.budget / "run.token").write_text("replacement-token\n")
        self.expect_failure("original_model_budget_binding_mismatch", lambda: p.budget_snapshot(self.paths.budget))


class ProcessTests(Fixture):
    def host_fixture(self):
        pins = {"candidate_image_id": "sha256:" + ZERO, "candidate_image_tag": "candidate:fixed",
                "worker_names": {"worker-a": "exact-a", "worker-b": "exact-b"}}
        mappings = {"/state": (self.paths.state, True), "/workspace": (self.paths.workspace, True),
            "/evidence": (self.paths.run / "attempts/worker-a/candidate", True),
            "/config/request.json": (self.paths.fixture / "attempts/worker-a/request.json", False),
            "/config/lifecycle.json": (self.paths.fixture / "attempts/worker-a/lifecycle.json", False),
            "/config/scope.json": (self.paths.fixture / "scope.json", False),
            "/config/scope.token": (self.paths.fixture / "scope.token", False),
            "/config/model-run.token": (self.paths.budget / "run.token", False)}
        before = {"id": ONE, "name": "/exact-a", "image_id": pins["candidate_image_id"], "image_tag": pins["candidate_image_id"],
                  "state": {"Status": "created", "Running": False}, "memory_limit": 2 * 1024**3, "cpu_nano": 1_000_000_000,
                  "mounts": [{"Source": p.source_on_host(self.config, path), "Destination": dest, "RW": rw}
                             for dest, (path, rw) in mappings.items()]}
        after = p.decode(p.canonical(before))
        after["state"] = {"Status": "exited", "Pid": 0, "ExitCode": 0, "OOMKilled": False,
                          "Dead": False, "Running": False, "Paused": False, "Restarting": False, "Error": ""}
        host = {"schema": "harness-lab-pause-host/1", "candidate": self.args.candidate, "chain_id": self.args.chain_id,
                "scope": SCOPE, "attempt_id": "worker-a", "errors": [], "process_exit": 0, "attach_exit": 0,
                "before_start": before, "after_exit": after, "started_at": stamp(0), "finished_at": stamp(42),
                "active_seconds": 42.0, "pause_request": None, "deleted": None}
        put(self.paths.run / "trusted/worker-a.json", host)
        return pins, host

    def test_immutable_image_reference_passes_but_oom_and_replaced_id_fail(self):
        pins, host = self.host_fixture()
        p.host_record(self.args, self.config, self.paths, SCOPE, pins, "worker-a", paused=False)
        host["after_exit"]["state"]["OOMKilled"] = True
        put(self.paths.run / "trusted/worker-a.json", host)
        self.expect_failure("worker_not_healthy_exited", lambda: p.host_record(self.args, self.config, self.paths, SCOPE, pins, "worker-a", paused=False))
        host["after_exit"]["state"]["OOMKilled"] = False
        host["after_exit"]["id"] = TWO
        put(self.paths.run / "trusted/worker-a.json", host)
        self.expect_failure("created_worker_id_mismatch", lambda: p.host_record(self.args, self.config, self.paths, SCOPE, pins, "worker-a", paused=False))

    def test_native_state_must_be_same_host_mount_and_controls_readonly(self):
        pins, host = self.host_fixture()
        next(row for row in host["after_exit"]["mounts"] if row["Destination"] == "/config/lifecycle.json")["RW"] = True
        put(self.paths.run / "trusted/worker-a.json", host)
        self.expect_failure("worker_mount_binding_mismatch", lambda: p.host_record(self.args, self.config, self.paths, SCOPE, pins, "worker-a", paused=False))


class WaitTests(Fixture):
    def wait_fixture(self):
        pins = {"candidate": self.args.candidate, "chain_id": self.args.chain_id}
        paused = {"budget": {"count": 4, "rows_sha256": ZERO, "token_sha256": ONE},
                  "audit": {"lines": 20, "bytes": 128, "sha256": TWO}}
        baseline = {"model_calls": 4, "model_rows_sha256": ZERO, "model_token_sha256": ONE, "model_cap": 20,
                    "tool_lines": 20, "tool_bytes": 128, "tool_sha256": TWO}
        put(self.paths.run / "trusted/worker-a.json", {"after_exit": {"id": ONE}, "deleted": {"observed_at": stamp(43)}})
        samples = [{"at": stamp(50 + elapsed), "elapsed_seconds": elapsed, **baseline} for elapsed in range(0, 601, 2)]
        put_lines(self.paths.run / "wait-budget-samples.jsonl", samples)
        put(self.paths.run / "wait-budget-summary.json", {
            "schema": "harness-lab-pause-wait-budget/1", **pins, "passed": True,
            "started_at": stamp(50), "finished_at": stamp(650), "elapsed_seconds": 600,
            "samples": len(samples), "baseline": baseline,
            "samples_sha256": p.sha((self.paths.run / "wait-budget-samples.jsonl").read_bytes())})
        resources = [{"at": stamp(49 + elapsed), "elapsed_seconds": elapsed, "worker_ids": [], "error": None,
            "containers": [{"id": ZERO, "role": "shared_service", "memory_limit": 1024**3, "cpu_nano": 500_000_000,
                            "CPUPerc": "0.1%", "MemUsage": "10MiB / 1GiB", "MemPerc": "1%", "PIDs": "8"},
                           {"id": TWO, "role": "bench", "memory_limit": 512 * 1024**2, "cpu_nano": 500_000_000,
                            "CPUPerc": "0.1%", "MemUsage": "10MiB / 512MiB", "MemPerc": "2%", "PIDs": "1"}]}
                     for elapsed in range(0, 605, 2)]
        put_lines(self.paths.run / "trusted/wait-resource-samples.jsonl", resources)
        put(self.paths.run / "trusted/wait.json", {"schema": "harness-lab-pause-wait-host/1", **pins,
            "started_at": stamp(49), "finished_at": stamp(655), "errors": [],
            "worker_a_deleted": {"id": ONE, "id_absent": True, "name_absent": True},
            "resources_file": "trusted/wait-resource-samples.jsonl", "budget_samples_file": "wait-budget-samples.jsonl",
            "budget_summary_file": "wait-budget-summary.json", "proxy_restarts": []})
        return pins, paused, resources, samples

    def test_two_independent_monitors_cover_whole_worker_free_wait(self):
        pins, paused, _, _ = self.wait_fixture()
        p.validate_wait(self.paths, pins, paused)

    def test_worker_reappearance_and_total_service_cap_each_fail(self):
        pins, paused, resources, _ = self.wait_fixture()
        resources[100]["worker_ids"] = [ONE]
        put_lines(self.paths.run / "trusted/wait-resource-samples.jsonl", resources)
        self.expect_failure("worker_or_sampling_error_during_wait", lambda: p.validate_wait(self.paths, pins, paused))
        resources[100]["worker_ids"] = []
        resources[100]["containers"][0]["memory_limit"] = p.MEMORY
        put_lines(self.paths.run / "trusted/wait-resource-samples.jsonl", resources)
        self.expect_failure("wait_total_resource_cap_exceeded", lambda: p.validate_wait(self.paths, pins, paused))

    def test_claimed_success_does_not_hide_model_activity_in_wait(self):
        pins, paused, _, samples = self.wait_fixture()
        samples[150]["model_calls"] = 5
        target = self.paths.run / "wait-budget-samples.jsonl"
        put_lines(target, samples)
        summary = p.read(self.paths.run / "wait-budget-summary.json")
        summary["samples_sha256"] = p.sha(target.read_bytes())
        put(self.paths.run / "wait-budget-summary.json", summary)
        self.expect_failure("wait_budget_activity_changed", lambda: p.validate_wait(self.paths, pins, paused))


class GradeVariantTests(Fixture):
    def test_uninterrupted_does_not_require_pause_b_or_wait_but_still_grades_business(self):
        put(self.paths.fixture / "attempts/worker-a/request.json", {"prompt": "original"})
        (self.paths.run / "tool-events.jsonl").write_bytes(b"")
        with patch.object(p, "pinned", return_value={"variant": "uninterrupted"}), \
             patch.object(p, "control_integrity", return_value={}), \
             patch.object(p, "host_record", return_value={"active_seconds": 42}) as process, \
             patch.object(p, "final_native", return_value={"status": "completed"}), \
             patch.object(p, "budget_snapshot", return_value={"count": 6}), \
             patch.object(p, "audit_snapshot", return_value={"lines": 20}), \
             patch.object(p, "ledger_request", return_value={"writes": []}), \
             patch.object(p.detailed, "task_definition", return_value=({"workspace_inputs": {"request.json": {}}}, ZERO)), \
             patch.object(p, "grade_tools", return_value={"passed": True, "metrics": {"actual_writes": 3}}) as business:
            self.assertTrue(p.grade(self.args, self.config, self.paths, SCOPE))
        self.assertEqual(process.call_args.args[-1], "worker-a")
        self.assertEqual(business.call_count, 1)
        verdict = p.read(self.paths.run / "verdict.json")
        self.assertTrue(verdict["passed"])
        self.assertFalse((self.paths.run / "pause-validated.json").exists())

    def test_missing_pause_cannot_be_upgraded_by_completed_business_or_a_only(self):
        put(self.paths.fixture / "attempts/worker-b/request.json", {"lifecycle": {"mode": "resume"}})
        (self.paths.run / "tool-events.jsonl").write_bytes(b"")
        with patch.object(p, "pinned", return_value={"variant": "requested-pause"}), \
             patch.object(p, "control_integrity", return_value={}), \
             patch.object(p, "host_record", return_value={"active_seconds": 42}), \
             patch.object(p, "final_native", return_value={"status": "completed"}), \
             patch.object(p, "budget_snapshot", return_value={"count": 6}), \
             patch.object(p, "audit_snapshot", return_value={"lines": 20}), \
             patch.object(p, "ledger_request", return_value={"writes": []}), \
             patch.object(p.detailed, "task_definition", return_value=({"workspace_inputs": {"request.json": {}}}, ZERO)), \
             patch.object(p, "grade_tools", return_value={"passed": True, "metrics": {}}):
            self.assertFalse(p.grade(self.args, self.config, self.paths, SCOPE))
        failed = [row["name"] for row in p.read(self.paths.run / "verdict.json")["checks"] if not row["passed"]]
        self.assertIn("accepted_original_pause", failed)


class ProxyReplacementTests(Fixture):
    def replacement(self):
        before = {'id': ZERO, 'image_id': 'sha256:' + TWO, 'mounts': [{'Source': '/host/budget', 'Destination': '/budget', 'RW': True}],
                  'memory_limit': 256 * 1024**2, 'cpu_nano': 250000000, 'state': {'Running': True}}
        stopped = {**before, 'state': {'Status': 'exited', 'Pid': 0, 'Running': False, 'OOMKilled': False, 'Error': '', 'ExitCode': 137}}
        created = {**before, 'id': ONE, 'state': {'Status': 'created'}}
        command = ['-m', 'execution.model_proxy', '--database', '/budget/calls.sqlite', '--run-token-file', '/budget/run.token', '--cap', '20', '--port', '8082']
        return {'before_id': ZERO, 'after_id': ONE, 'before': before, 'old_after_stop': stopped,
                'replacement_created': created, 'replacement': {**created, 'name': '/harness-lab-model-proxy-1', 'state': {'Running': True}},
                'old_id_absent': True, 'command': command, 'replacement_command': list(command),
                'environment_sha256': TWO, 'replacement_environment_sha256': TWO, 'sampling_lock_seconds': 1.3,
                'transition': 'replacement_ready_before_old_proxy_removed; same_budget_no_worker'}

    def test_expected_proxy_forced_stop_is_explicit_and_budget_binding_unchanged(self):
        p.validate_proxy_replacement(self.replacement(), 'sha256:' + TWO, '/host/budget')

    def test_replacement_rejects_oom_reset_budget_unknown_exit_and_config_drift(self):
        for key in ('image', 'budget', 'cap', 'environment', 'command', 'oom', 'running', 'missing', 'identity', 'gap'):
            with self.subTest(key=key):
                value = self.replacement()
                if key == 'image': value['replacement']['image_id'] = 'sha256:' + ONE
                elif key == 'budget': value['replacement']['mounts'] = []
                elif key == 'cap': value['replacement']['memory_limit'] = 0
                elif key == 'environment': value['replacement_environment_sha256'] = ONE
                elif key == 'command': value['replacement_command'][-3] = '200'
                elif key == 'oom': value['old_after_stop']['state']['OOMKilled'] = True
                elif key == 'running': value['old_after_stop']['state']['Running'] = True
                elif key == 'missing': value['old_id_absent'] = False
                elif key == 'identity': value['replacement']['id'] = TWO
                else: value['sampling_lock_seconds'] = 21
                with self.assertRaises(ValueError):
                    p.validate_proxy_replacement(value, 'sha256:' + TWO, '/host/budget')


class DshGoalProjectionTests(Fixture):
    """官方真实传输结构：snapshot 与同级计数器，不执行激活。"""
    def changes(self):
        goal = {'id': 'goal-original', 'revision': 1, 'objective': 'original', 'phase': 'active', 'maxGoalRounds': p.CAP}
        create = {'kind': 'goal/change', 'version': 1, 'operation': 'create', 'goal': goal,
                  'roundsStarted': 0, 'createdAt': 1000, 'updatedAt': 1000}
        complete = {**create, 'operation': 'complete', 'goal': {**goal, 'revision': 2, 'phase': 'complete'},
                    'roundsStarted': 1, 'updatedAt': 2000}
        return [
            {'seq': 0, 'type': 'goal/change', 'data': create},
            {'seq': 1, 'type': 'user/message', 'data': {'source': {'kind': 'goal', 'goalId': goal['id'], 'revision': 1, 'round': 1}, 'content': []}},
            {'seq': 2, 'type': 'goal/change', 'data': complete},
            {'seq': 3, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}},
        ]

    def dsh_fixture(self, events=None, *, attempt='worker-a', live_from=0, live_goal=None):
        events = self.changes() if events is None else events
        directory = self.paths.run / 'attempts' / attempt / 'candidate'
        physical = self.paths.state / 'sessions/main.jsonl'
        put_lines(physical, [{'type': 'session', 'id': 'main', 'cwd': '/workspace'}, *events])
        put(directory / 'session-artifact-after-flush.json', {'native_location': {'path': '/state/sessions/main.jsonl'},
             'physical_sha256': p.sha(physical.read_bytes()), 'scope': SCOPE})
        put_lines(directory / 'raw-events.jsonl', [{'method': 'session.event', 'params': {'sessionId': 'main', 'event': event}}
                  for event in events if event['seq'] >= live_from])
        last = [event['data'] for event in events if event['type'] == 'goal/change'][-1]
        goal = {**last['goal'], **{key: last[key] for key in ('roundsStarted', 'createdAt', 'updatedAt')}, 'activation': 'disarmed'}
        put(directory / 'result.json', {'status': 'completed', 'native_status': 'completed', 'stop_reason': 'completed',
            'native_result': {'lifecycleOutcome': {'kind': 'completed', 'goal': live_goal or goal}}})
        put(self.paths.fixture / 'candidate-request.json', {'prompt': 'original'})
        return directory

    def test_official_snapshot_sibling_counters_are_independently_replayed(self):
        directory = self.dsh_fixture()
        persisted = p.dsh_storage(self.paths, directory, SCOPE)
        goal = persisted['goal']
        self.assertEqual(goal['phase'], 'complete')
        self.assertEqual(goal['roundsStarted'], 1)
        self.assertEqual(goal['createdAt'], 1000)
        self.assertEqual(goal['updatedAt'], 2000)
        self.assertNotIn('activation', goal)
        result = p.final_native(self.paths, SCOPE, {'candidate': 'deepseek_harness'}, 'worker-a')
        self.assertFalse(result['goal_evidence']['activation_persisted'])

    def test_candidate_success_cannot_replace_durable_phase_or_round_admissions(self):
        for mutation, code in (
            ('missing_round', 'dsh_goal_counter_transition'),
            ('wrong_round', 'dsh_goal_round_not_next_admitted'),
            ('bad_revision', 'dsh_goal_revision_transition'),
            ('changed_definition', 'dsh_goal_definition_changed'),
            ('forged_counter', 'dsh_goal_counter_transition'),
            ('backdated', 'dsh_goal_counter_transition'),
        ):
            with self.subTest(mutation=mutation):
                events = self.changes()
                if mutation == 'missing_round':
                    events[1] = {'seq': 1, 'type': 'note', 'data': {}}
                elif mutation == 'wrong_round':
                    events[1]['data']['source']['round'] = 2
                elif mutation == 'bad_revision':
                    events[2]['data']['goal']['revision'] = 3
                elif mutation == 'changed_definition':
                    events[2]['data']['goal']['objective'] = 'changed'
                elif mutation == 'forged_counter':
                    events[2]['data']['roundsStarted'] = 2
                elif mutation == 'backdated':
                    events[2]['data']['createdAt'] = 0
                directory = self.dsh_fixture(events)
                self.expect_failure(code, lambda: p.dsh_storage(self.paths, directory, SCOPE))

    def test_forged_full_goalview_inside_persistent_snapshot_is_rejected(self):
        events = self.changes()
        events[2]['data']['goal']['activation'] = 'disarmed'
        directory = self.dsh_fixture(events)
        self.expect_failure('dsh_goal_snapshot_fields', lambda: p.dsh_storage(self.paths, directory, SCOPE))

    def test_live_activation_stays_separate_and_must_match_complete_api_semantics(self):
        directory = self.dsh_fixture()
        result = p.read(directory / 'result.json')
        result['native_result']['lifecycleOutcome']['goal']['activation'] = 'armed'
        put(directory / 'result.json', result)
        self.expect_failure('native_live_goal_view_not_complete_disarmed',
            lambda: p.final_native(self.paths, SCOPE, {'candidate': 'deepseek_harness'}, 'worker-a'))
        # 仅有物理完成不能证明未被观察的运行时视图。
        result['native_result'] = {}
        put(directory / 'result.json', result)
        self.expect_failure('native_live_goal_view_not_complete_disarmed',
            lambda: p.final_native(self.paths, SCOPE, {'candidate': 'deepseek_harness'}, 'worker-a'))

    def test_forged_live_round_count_is_not_accepted_over_physical_fold(self):
        directory = self.dsh_fixture()
        result = p.read(directory / 'result.json')
        result['native_result']['lifecycleOutcome']['goal']['roundsStarted'] = 2
        put(directory / 'result.json', result)
        self.expect_failure('native_live_goal_view_not_complete_disarmed',
            lambda: p.final_native(self.paths, SCOPE, {'candidate': 'deepseek_harness'}, 'worker-a'))

    def test_paused_resumed_physical_session_accepts_only_live_b_suffix(self):
        events = self.changes()
        pause = {**events[2]['data'], 'operation': 'pause', 'goal': {**events[2]['data']['goal'], 'phase': 'paused'}}
        resume = {**pause, 'operation': 'resume', 'goal': {**pause['goal'], 'revision': 3, 'phase': 'active'}, 'updatedAt': 3000}
        complete = {**resume, 'operation': 'complete', 'goal': {**resume['goal'], 'revision': 4, 'phase': 'complete'},
                    'roundsStarted': 2, 'updatedAt': 4000}
        events = [events[0], events[1], {'seq': 2, 'type': 'goal/change', 'data': pause},
                  {'seq': 3, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'blocked'}}},
                  {'seq': 4, 'type': 'session/end-seed', 'data': {}},
                  {'seq': 5, 'type': 'goal/change', 'data': resume},
                  {'seq': 6, 'type': 'user/message', 'data': {'source': {'kind': 'goal', 'goalId': 'goal-original', 'revision': 3, 'round': 2}, 'content': []}},
                  {'seq': 7, 'type': 'goal/change', 'data': complete},
                  {'seq': 8, 'type': 'turn/end', 'data': {'turn': 2, 'reason': {'kind': 'completed'}}}]
        self.dsh_fixture(events, attempt='worker-b', live_from=5)
        result = p.final_native(self.paths, SCOPE, {'candidate': 'deepseek_harness'}, 'worker-b')
        self.assertEqual(result['status'], 'completed')

    def test_pause_view_matches_snapshot_plus_outer_fields_not_snapshot_alone(self):
        pause = {**self.changes()[2]['data'], 'operation': 'pause'}
        pause['goal'] = {**pause['goal'], 'phase': 'paused'}
        projection = p.change_projection(pause)
        view = {**projection, 'activation': 'disarmed'}
        self.assertTrue(p.matches_live_view(view, projection, 'disarmed'))
        self.assertFalse(p.matches_live_view(view, pause['goal'], 'disarmed'))
        self.assertNotIn('activation', projection)

    def test_clear_keeps_goal_id_tombstone_and_blocks_reuse(self):
        events = self.changes()
        clear = {'seq': 4, 'type': 'goal/change', 'data': {'kind': 'goal/change', 'version': 1,
                 'operation': 'clear', 'cleared': {'id': 'goal-original', 'revision': 3}, 'clearedAt': 3000}}
        self.assertIsNone(p.fold_durable_goal([*events, clear]))
        self.expect_failure('dsh_goal_create_transition', lambda: p.fold_durable_goal([*events, clear, self.changes()[0]]))


if __name__ == "__main__":
    unittest.main()
