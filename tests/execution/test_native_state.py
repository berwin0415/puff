"""使用合成状态和 Docker 传输替身进行对抗性校准。

在 Docker 验收镜像中运行，不调用候选 SDK、Docker 服务或模型；
这里只校验拒绝行为，不证明候选能力。
"""
from argparse import Namespace
from contextlib import closing
from copy import deepcopy
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from runner import native_state as probe
from scripts.probe_native_state import NativeStateLab


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def export(directory, name, scope, native, state, pointer="/context/context/business_state"):
    write(directory / "native-state.json", native)
    write(directory / name, {"scope": scope, "business_state": state, "source": {
        "path": "native-state.json", "sha256": hashlib.sha256((directory / "native-state.json").read_bytes()).hexdigest(),
        "json_pointer": pointer}})


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    paths = Namespace(**{key: tmp_path / key for key in ("run", "state", "workspace", "fixture", "budget")})
    for path in vars(paths).values():
        path.mkdir()
    original, replacement = paths.run / "candidate", tmp_path / "probe/candidate"
    original.mkdir()
    replacement.mkdir(parents=True)
    scope = {"experiment_id": "detailed", "variant": "synthetic", "user_id": "alice", "session_id": "main"}
    receipt = {**scope, "operation_id": "order-one", "item_id": "one", "quantity": 2, "total_cents": 20}
    actual = {"operation_id": "order-one", "write_count": 1, "receipts": [receipt]}
    state = {"schema_version": 1, "operation_id": "order-one", "receipt_confirmed": True, "operations": {
        "order-one": {"operation_id": "order-one", "receipt_confirmed": True, "write_count": 1,
                      "receipts": [receipt], "source": {"path": "http-tool-events.jsonl", "line": 1}}}}
    native = {"context": {"context": {"scope": scope, "session_id": "main", "business_state": state}}}
    write(paths.state / "run-state.json", native)
    export(original, "business-state.json", scope, native, state)
    export(replacement, "business-state-reconnected.json", scope, native, state)
    finish = {"scope": scope, "phase": "finish", "call_id": "candidate-call", "tool": "receipt", "status": 200,
        "exposed_tool": "query_order_status", "arguments": {"operation_id": "order-one"}, "result": {"result": actual}}
    jsonl(original / "http-tool-events.jsonl", [finish])
    audits = [{"scope": scope, "phase": "start", "call_id": "independent-call", "tool": "receipt", "arguments": finish["arguments"]},
              {"scope": scope, "phase": "finish", "call_id": "independent-call", "tool": "receipt", "status": 200, "result": actual}]
    jsonl(paths.run / "tool-events.jsonl", audits)
    (paths.budget / "calls.sqlite").write_bytes(b"synthetic immutable model audit")
    (paths.workspace / "report.json").write_text("{}\n")
    old = {"id": "old-container", "image_id": "sha256:same-image", "mounts": [
        {"Source": str(paths.state), "Destination": "/state", "RW": True},
        {"Source": str(paths.workspace), "Destination": "/workspace", "RW": True},
        {"Source": str(original), "Destination": "/evidence", "RW": True}]}
    new = {**deepcopy(old), "id": "new-container", "memory_limit": 2 ** 31, "cpu_nano": 10 ** 9,
        "read_only": True, "tmpfs": {"/tmp": "size=128m"}, "credential_env_present": False,
        "state": {"ExitCode": 0, "Running": False}, "network_mode": "none"}
    new["mounts"][0]["RW"] = new["mounts"][1]["RW"] = False
    new["mounts"][2]["Source"] = str(replacement)
    manifest = {"candidate": "openai_agents_sdk", "case": "extension", "run_id": "synthetic",
        "experiment_id": "detailed", "candidate_exit_code": 0, "grade_exit_code": 0, "containers": [old]}
    write(paths.run / "manifest.json", manifest)
    orchestration = {"probe_id": "read-probe", "candidate": manifest["candidate"], "original_worker": old, "new_worker": new,
        "original_worker_absent_before": {"id": True, "name": True}, "original_worker_absent_after": {"id": True, "name": True},
        "candidate_exit_code": 0, "model_proxy_started": False, "real_model_credential_injected": False,
        "task_continuation_claim": False, "errors": []}
    write(replacement.parent / "orchestration.json", orchestration)
    write(replacement / "result.json", {"status": "validated", "runner_started": False, "model_calls": 0,
        "http_discovery_requests": 0, "model_client_request_attempts": [], "task_continuation_verified": False})
    baseline = {"probe_id": "read-probe", "candidate": manifest["candidate"], "scope": scope, "business_state": state,
        "files": probe.baseline_files(paths), "native_binding": {"path": "run-state.json"},
        "source_manifest_sha256": hashlib.sha256((paths.run / "manifest.json").read_bytes()).hexdigest()}
    write(replacement.parent / "baseline.json", baseline)
    args = Namespace(probe_id="read-probe", run_dir=paths.run)
    monkeypatch.setattr(probe, "PROBE_ROOT", replacement.parent)
    monkeypatch.setattr(probe, "context", lambda _args: (None, paths, scope, manifest))
    monkeypatch.setattr(probe, "environment", lambda: {"synthetic_calibration_only": True})
    return Namespace(paths=paths, original=original, replacement=replacement, scope=scope, state=state,
        native=native, manifest=manifest, orchestration=orchestration, args=args, finish=finish, audits=audits)


def test_equal_native_state_in_a_different_deleted_worker_chain_passes(evidence):
    assert probe.grade(evidence.args) == 0
    verdict = json.loads((evidence.replacement.parent / "verdict.json").read_text())
    assert verdict["passed"] and '未验证任务继续执行' in verdict["claim"]


@pytest.mark.parametrize("corruption", ["source_hash", "source_pointer", "foreign_scope", "same_container",
    "old_worker_not_deleted", "business_state_changed", "different_actual_native_state", "extra_model_call",
    "blocked_model_attempt", "extra_original_http_call", "new_tool_call", "wrong_image", "credential_injected"])
def test_rejects_a_plausible_but_invalid_replacement_claim(evidence, corruption):
    item = evidence
    if corruption in {"source_hash", "source_pointer", "foreign_scope"}:
        path = item.replacement / "business-state-reconnected.json"
        record = json.loads(path.read_text())
        if corruption == "source_hash":
            record["source"]["sha256"] = "0" * 64
        elif corruption == "source_pointer":
            record["source"]["json_pointer"] = "/context"
        else:
            record["scope"]["user_id"] = "another-tenant"
        write(path, record)
    elif corruption == "business_state_changed":
        state = deepcopy(item.state)
        state["operation_id"] = "substituted-operation"
        native = deepcopy(item.native)
        native["context"]["context"]["business_state"] = state
        export(item.replacement, "business-state-reconnected.json", item.scope, native, state)
    elif corruption == "different_actual_native_state":
        native = deepcopy(item.native)
        native["context"]["context"]["business_state"]["receipt_confirmed"] = False
        write(item.paths.state / "run-state.json", native)
    elif corruption == "extra_model_call":
        with (item.paths.budget / "calls.sqlite").open("ab") as stream:
            stream.write(b"a new model attempt")
    elif corruption == "blocked_model_attempt":
        path = item.replacement / "result.json"
        result = json.loads(path.read_text())
        result["model_client_request_attempts"] = [{"method": "POST", "path": "/v1/chat/completions"}]
        write(path, result)
    elif corruption == "extra_original_http_call":
        jsonl(item.paths.run / "tool-events.jsonl", item.audits + [
            {**item.audits[0], "call_id": "unexpected"}, {**item.audits[1], "call_id": "unexpected"}])
    elif corruption == "new_tool_call":
        jsonl(item.replacement / "http-tool-events.jsonl", [item.finish])
    else:
        row = item.orchestration
        if corruption == "same_container":
            row["new_worker"]["id"] = row["original_worker"]["id"]
        elif corruption == "old_worker_not_deleted":
            row["original_worker_absent_before"]["id"] = False
        elif corruption == "wrong_image":
            row["new_worker"]["image_id"] = "sha256:different-image"
        elif corruption == "credential_injected":
            row["new_worker"]["credential_env_present"] = True
        write(item.replacement.parent / "orchestration.json", row)
    assert probe.grade(item.args) == 1


def test_matching_candidate_receipt_values_cannot_replace_independent_audit(evidence):
    altered = deepcopy(evidence.audits)
    altered[1]["result"]["receipts"][0]["quantity"] = 99
    jsonl(evidence.paths.run / "tool-events.jsonl", altered)
    with pytest.raises(ValueError, match='独立审计'):
        probe.receipt_sources(evidence.state, evidence.original, evidence.scope)


def graph_evidence(item, actual_business):
    """独立于导出 JSON 的真实原生格式 SQLite 记录。"""
    import ormsgpack

    checkpoint_id = "checkpoint-current"
    native = {"config": {"configurable": {"thread_id": "main", "checkpoint_ns": "", "checkpoint_id": checkpoint_id}},
        "checkpoint": {"id": checkpoint_id, "channel_values": {"business_state": item.state}}}
    # 普通业务通道之外可以有不透明 LangGraph 消息扩展；
    # 验收程序不能导入或重建这些扩展类。
    stored = {"id": checkpoint_id, "channel_values": {"business_state": actual_business,
        "messages": [ormsgpack.Ext(5, ormsgpack.packb(["unimportable.module", "SyntheticMessage", {}]))]}}
    database = item.paths.state / "checkpoints.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE checkpoints(thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, type TEXT, checkpoint BLOB)")
        db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", ("main", "", checkpoint_id, "msgpack", ormsgpack.packb(stored)))
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    for directory, name in ((item.original, "business-state.json"), (item.replacement, "business-state-reconnected.json")):
        export(directory, name, item.scope, native, item.state, "/checkpoint/channel_values/business_state")
        record = json.loads((directory / name).read_text())
        record.update({"database_sha256_before": digest, "database_sha256_after": digest})
        write(directory / name, record)
    item.manifest["candidate"] = item.orchestration["candidate"] = "deepagents_langgraph"
    write(item.paths.run / "manifest.json", item.manifest)
    write(item.replacement / "result.json", {"status": "validated", "native_status": "checkpoint_read_only",
        "model_started": False, "graph_started": False, "task_continuation_verified": False})
    write(item.replacement.parent / "orchestration.json", item.orchestration)
    baseline = json.loads((item.replacement.parent / "baseline.json").read_text())
    baseline.update({"candidate": "deepagents_langgraph", "files": probe.baseline_files(item.paths),
        "native_binding": {"path": "checkpoints.sqlite", "checkpoint_id": checkpoint_id},
        "source_manifest_sha256": hashlib.sha256((item.paths.run / "manifest.json").read_bytes()).hexdigest()})
    write(item.replacement.parent / "baseline.json", baseline)
    return native, database


def test_graph_business_values_are_independently_read_from_real_sqlite_with_opaque_messages(evidence):
    native, _ = graph_evidence(evidence, evidence.state)
    binding = probe.native_binding("deepagents_langgraph", {}, native, evidence.state, evidence.paths, evidence.scope)
    assert binding["checkpoint_id"] == "checkpoint-current"
    assert probe.grade(evidence.args) == 0


def deepagents_reader(monkeypatch):
    """仅加载标准库入口模块，不导入或运行候选 SDK。"""
    root = Path(__file__).resolve().parents[2] / "adapters/deepagents_langgraph"
    for name in ("verify_source", "detailed_support", "run_control", "pause_recovery"):
        spec = spec_from_file_location(name, root / (name + ".py"))
        module = module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    spec = spec_from_file_location("deepagents_cold_entrypoint", root / "entrypoint.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cold_inventory(reader, database, monkeypatch):
    if reader == "bench":
        return probe.cold_sqlite_inventory(database, database.parent)
    return deepagents_reader(monkeypatch).cold_sqlite_inventory(database)


@pytest.mark.parametrize("reader", ["bench", "adapter"])
def test_cold_sqlite_gate_retains_empty_wal_journal_and_regular_shm(tmp_path, monkeypatch, reader):
    database = tmp_path / "checkpoints.sqlite"
    database.write_bytes(b"standalone closed primary")
    for suffix in ("-wal", "-journal"):
        database.with_name(database.name + suffix).touch()
    database.with_name(database.name + "-shm").write_bytes(b"retained shared-memory index; no writer")
    before = probe.inventory(tmp_path)
    observed = cold_inventory(reader, database, monkeypatch)
    assert set(observed["files"]) == {database.name + suffix for suffix in ("", "-wal", "-journal", "-shm")}
    assert observed["files"][database.name + "-wal"]["bytes"] == 0
    assert observed["files"][database.name + "-journal"]["bytes"] == 0
    assert probe.inventory(tmp_path) == before


@pytest.mark.parametrize("reader", ["bench", "adapter"])
@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_cold_sqlite_gate_never_discards_possible_committed_sidecar(tmp_path, monkeypatch, reader, suffix):
    database = tmp_path / "checkpoints.sqlite"
    database.write_bytes(b"apparently complete primary")
    sidecar = database.with_name(database.name + suffix)
    sidecar.write_bytes(b"possible transaction not represented in the primary")
    before = probe.inventory(tmp_path)
    with pytest.raises(ValueError, match='非空 SQLite WAL 或日志'):
        cold_inventory(reader, database, monkeypatch)
    assert probe.inventory(tmp_path) == before


@pytest.mark.parametrize("reader", ["bench", "adapter"])
def test_cold_sqlite_gate_rejects_a_real_committed_transaction_still_in_wal(tmp_path, monkeypatch, reader):
    database = tmp_path / "checkpoints.sqlite"
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE actual_state(value TEXT)")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("INSERT INTO actual_state VALUES('committed only in the WAL')")
        db.commit()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        # 意外存活的写入连接违反冷读前提；
        # 检查不能静默读取只有主库文件的旧状态。
        before = probe.inventory(tmp_path)
        with pytest.raises(ValueError, match='非空 SQLite WAL 或日志'):
            cold_inventory(reader, database, monkeypatch)
        assert probe.inventory(tmp_path) == before


@pytest.mark.parametrize("reader", ["bench", "adapter"])
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("kind", ["link", "dangling_link", "directory", "fifo"])
def test_cold_sqlite_gate_rejects_unsafe_primary_and_companions(tmp_path, monkeypatch, reader, suffix, kind):
    database = tmp_path / "checkpoints.sqlite"
    database.write_bytes(b"closed primary")
    target = tmp_path / "external-target"
    target.write_bytes(b"must not be traversed")
    path = database.with_name(database.name + suffix)
    if not suffix:
        path.unlink()
    if kind in {"link", "dangling_link"}:
        path.symlink_to(target if kind == "link" else tmp_path / "missing-target")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    # 读取 FIFO 或沿软链接访问之前，先由 lstat 检查拒绝。
    with pytest.raises(ValueError):
        cold_inventory(reader, database, monkeypatch)
    assert target.read_bytes() == b"must not be traversed"


def closed_wal_header(database):
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("PRAGMA user_version=1")
        db.commit()
    assert database.read_bytes()[18:20] == b"\x02\x02"
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()


def test_graph_closed_wal_header_without_sidecars_uses_immutable_and_preserves_state(evidence, monkeypatch):
    native, database = graph_evidence(evidence, evidence.state)
    closed_wal_header(database)
    before = probe.inventory(evidence.paths.state)
    original_connect, calls = sqlite3.connect, []
    def connect(database_uri, **kwargs):
        calls.append((database_uri, kwargs))
        return original_connect(database_uri, **kwargs)
    monkeypatch.setattr(probe.sqlite3, "connect", connect)
    binding = probe.native_binding("deepagents_langgraph", {}, native, evidence.state, evidence.paths, evidence.scope)
    assert binding["checkpoint_id"] == "checkpoint-current"
    assert calls == [(database.as_uri() + "?mode=ro&immutable=1", {"uri": True})]
    assert binding["sqlite_files_before"] == binding["sqlite_files_after"]
    assert probe.inventory(evidence.paths.state) == before


@pytest.mark.parametrize("change", ["shm_appears", "empty_wal_appears", "nonempty_journal_appears", "primary_changes"])
def test_graph_immutable_inspection_detects_changes_beyond_the_initial_primary_hash(evidence, monkeypatch, change):
    native, database = graph_evidence(evidence, evidence.state)
    original_connect = sqlite3.connect
    class ChangingConnection:
        def __init__(self, database_uri, **kwargs):
            self.connection = original_connect(database_uri, **kwargs)
        def execute(self, *args):
            cursor = self.connection.execute(*args)
            if change == "primary_changes":
                with database.open("ab") as stream:
                    stream.write(b"unexpected modification")
            else:
                suffix, content = {"shm_appears": ("-shm", b"new index"), "empty_wal_appears": ("-wal", b""),
                                   "nonempty_journal_appears": ("-journal", b"possible commit")}[change]
                database.with_name(database.name + suffix).write_bytes(content)
            return cursor
        def close(self):
            self.connection.close()
    monkeypatch.setattr(probe.sqlite3, "connect", ChangingConnection)
    with pytest.raises(ValueError, match='清单在冷检查期间变化|非空 SQLite WAL 或日志'):
        probe.native_binding("deepagents_langgraph", {}, native, evidence.state, evidence.paths, evidence.scope)


@pytest.mark.parametrize("corruption", ["different_business", "missing_business", "wrong_format", "malformed_blob",
    "blob_id_mismatch", "wrong_thread", "opaque_business"])
def test_graph_same_checkpoint_id_and_hash_cannot_make_a_fake_snapshot_authoritative(evidence, corruption):
    import ormsgpack

    wrong = deepcopy(evidence.state)
    wrong["receipt_confirmed"] = False
    native, database = graph_evidence(evidence, wrong if corruption == "different_business" else evidence.state)
    if corruption in {"missing_business", "wrong_format", "malformed_blob", "blob_id_mismatch", "opaque_business"}:
        actual = {"id": "checkpoint-current", "channel_values": {"business_state": evidence.state}}
        if corruption == "missing_business":
            actual["channel_values"].pop("business_state")
        elif corruption == "blob_id_mismatch":
            actual["id"] = "a-different-checkpoint"
        elif corruption == "opaque_business":
            actual["channel_values"]["business_state"] = ormsgpack.Ext(5, b"not a plain business map")
        with sqlite3.connect(database) as db:
            db.execute("UPDATE checkpoints SET type=?,checkpoint=?", (
                "pickle" if corruption == "wrong_format" else "msgpack",
                b"malformed native checkpoint" if corruption == "malformed_blob" else ormsgpack.packb(actual)))
    if corruption == "wrong_thread":
        native["config"]["configurable"]["thread_id"] = "foreign-thread"
        for directory, name in ((evidence.original, "business-state.json"), (evidence.replacement, "business-state-reconnected.json")):
            export(directory, name, evidence.scope, native, evidence.state, "/checkpoint/channel_values/business_state")
    # 将全部合理校验和及不可变基线重新绑定到实际数据库。
    # 因此拒绝必须来自真实存储的业务值或身份，
    # 不能来自无关的过期校验和或文件变化比较。
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    for directory, name in ((evidence.original, "business-state.json"), (evidence.replacement, "business-state-reconnected.json")):
        record = json.loads((directory / name).read_text())
        record.update({"database_sha256_before": digest, "database_sha256_after": digest})
        write(directory / name, record)
    baseline_path = evidence.replacement.parent / "baseline.json"
    baseline = json.loads(baseline_path.read_text())
    baseline["files"] = probe.baseline_files(evidence.paths)
    write(baseline_path, baseline)
    assert probe.grade(evidence.args) == 1
    verdict = json.loads((evidence.replacement.parent / "verdict.json").read_text())
    assert verdict["checks"]["native_source_hash_pointer_and_actual_state"] is False
    assert verdict["checks"]["native_state_read_only_and_unchanged"] is True


def dsh_evidence(item):
    event = {"type": "harness-lab/business-state", "seq": 0, "time": 1, "data": item.state}
    session = item.paths.state / "sessions/main/session.jsonl"
    write(session, event)
    (item.paths.state / "other-session.json").write_text("original unrelated state\n")
    for directory, name in ((item.original, "business-state.json"), (item.replacement, "business-state-reconnected.json")):
        jsonl(directory / "native-business-state-record.jsonl", [event])
        write(directory / name, {"scope": item.scope, "business_state": item.state,
            "source": {"path": "native-business-state-record.jsonl", "line": 1,
                "sha256": hashlib.sha256((directory / "native-business-state-record.jsonl").read_bytes()).hexdigest(), "json_pointer": "/data"},
            "native_origin": {"path": "/state/sessions/main/session.jsonl", "line": 1, "session_id": "main",
                "physical_sha256": hashlib.sha256(session.read_bytes()).hexdigest()}})
    item.manifest["candidate"] = item.orchestration["candidate"] = "deepseek_harness"
    item.orchestration["new_worker"]["mounts"][0]["RW"] = True
    item.orchestration["new_worker"]["network_mode"] = "isolated-synthetic-network"
    item.orchestration["network"] = {"name": "isolated-synthetic-network", "internal": True}
    write(item.paths.run / "manifest.json", item.manifest)
    write(item.replacement.parent / "orchestration.json", item.orchestration)
    write(item.replacement / "result.json", {"status": "session_restored", "stop_reason": "resume_inspection_only",
        "session_action": "resume", "followup_submitted": False, "native_result": {"inspectionOnly": True}})
    write(item.replacement / "session-before-followup.json", {"agent_status": "idle", "inbox": {"has_pending": False}})
    baseline_path = item.replacement.parent / "baseline.json"
    baseline = json.loads(baseline_path.read_text())
    baseline.update({"candidate": "deepseek_harness", "files": probe.baseline_files(item.paths),
        "native_binding": {"path": "sessions/main/session.jsonl"},
        "source_manifest_sha256": hashlib.sha256((item.paths.run / "manifest.json").read_bytes()).hexdigest()})
    write(baseline_path, baseline)
    return session


def dsh_metadata_evidence(item):
    """与真实调用及结果来源同结构的合成记录，不运行候选。"""
    session = dsh_evidence(item)
    item.state = {**deepcopy(item.state), "session_id": "main", "scope": deepcopy(item.scope)}
    entry = item.state["operations"]["order-one"]
    entry.update({"write_acknowledged": False, "last_observation": "actual_receipt_query_confirmed"})
    call = {"type": "tool/call", "seq": 0, "time": 1, "data": {"turn": 1, "step": 1,
        "callId": item.finish["call_id"], "name": "query_order_status", "arguments": json.dumps(item.finish["arguments"])}}
    result = {"type": "tool/result", "seq": 1, "time": 2, "surfaceOp": "append", "sourceEventSeqs": [0],
        "data": {"turn": 1, "step": 1, "message": {"id": "native-result", "role": "user",
            "source": {"kind": "tool", "callId": item.finish["call_id"]}, "content": [{"type": "tool-result",
                "toolCallId": item.finish["call_id"], "isError": False, "content": [{"type": "text",
                    "text": json.dumps(item.finish["result"]["result"])}]}]},
            "meta": {"kind": "harness-lab/business-state/v1", "business_state": item.state}}}
    rows = [call, result]
    rebind_dsh_metadata(item, session, rows)
    return session, rows


def rebind_dsh_metadata(item, session, rows, pointer="/data/meta/business_state"):
    """重新绑定校验和与基线，确保失败来自来源校验而非陈旧哈希。"""
    jsonl(session, rows)
    event = next(row for row in rows if row.get("seq") == 1)
    for directory, name in ((item.original, "business-state.json"), (item.replacement, "business-state-reconnected.json")):
        jsonl(directory / "native-business-state-record.jsonl", [event])
        write(directory / name, {"scope": item.scope, "business_state": item.state,
            "source": {"path": "native-business-state-record.jsonl", "line": 1, "json_pointer": pointer,
                "sha256": hashlib.sha256((directory / "native-business-state-record.jsonl").read_bytes()).hexdigest()},
            "native_origin": {"path": "/state/sessions/main/session.jsonl", "line": 2,
                "session_id": "main", "event_seq": 1, "physical_sha256": hashlib.sha256(session.read_bytes()).hexdigest()}})
    baseline_path = item.replacement.parent / "baseline.json"
    baseline = json.loads(baseline_path.read_text())
    baseline.update({"business_state": item.state, "files": probe.baseline_files(item.paths)})
    write(baseline_path, baseline)


@pytest.mark.parametrize("append_boundary", [False, True])
def test_dsh_real_tool_result_metadata_has_exact_native_and_receipt_sources(evidence, append_boundary):
    session, _ = dsh_metadata_evidence(evidence)
    if append_boundary:
        with session.open("a") as stream:
            stream.write(json.dumps({"type": "session/end-seed", "seq": 2, "time": 3, "data": {}}) + "\n")
        path = evidence.replacement / "business-state-reconnected.json"
        record = json.loads(path.read_text())
        record["native_origin"]["physical_sha256"] = hashlib.sha256(session.read_bytes()).hexdigest()
        write(path, record)
    assert probe.grade(evidence.args) == 0


@pytest.mark.parametrize("corruption", ["metadata_kind", "wrong_pointer", "unbound_call", "wrong_alias",
    "wrong_operation", "wrong_result_body", "failed_result", "missing_call_reference", "receipt_source_callid",
    "stale_native_write", "later_native_metadata", "origin_seq"])
def test_dsh_metadata_cannot_replace_actual_call_operation_or_receipt_with_matching_hashes(evidence, corruption):
    session, rows = dsh_metadata_evidence(evidence)
    call, result = rows
    pointer = "/data/meta/business_state"
    if corruption == "metadata_kind":
        result["data"]["meta"]["kind"] = "unrelated-presentation"
    elif corruption == "wrong_pointer":
        result["data"]["meta"]["another_state"] = evidence.state
        pointer = "/data/meta/another_state"
    elif corruption == "unbound_call":
        call["data"]["callId"] = "foreign-call"
    elif corruption == "wrong_alias":
        call["data"]["name"] = "receipt"
    elif corruption == "wrong_operation":
        call["data"]["arguments"] = json.dumps({"operation_id": "foreign-operation"})
    elif corruption == "wrong_result_body":
        result["data"]["message"]["content"][0]["content"][0]["text"] = json.dumps({"operation_id": "foreign-operation"})
    elif corruption == "failed_result":
        result["data"]["message"]["content"][0]["isError"] = True
    elif corruption == "missing_call_reference":
        result["sourceEventSeqs"] = []
    elif corruption == "receipt_source_callid":
        evidence.finish["call_id"] = "foreign-call"
        jsonl(evidence.original / "http-tool-events.jsonl", [evidence.finish])
    elif corruption == "stale_native_write":
        rows.append({"type": "tool/call", "seq": 2, "time": 3, "data": {"turn": 1, "step": 2,
            "callId": "later-write", "name": "write", "arguments": json.dumps({"operation_id": "order-one"})}})
    elif corruption == "later_native_metadata":
        rows.append({**deepcopy(result), "seq": 2, "data": {"meta": deepcopy(result["data"]["meta"])}})
    rebind_dsh_metadata(evidence, session, rows, pointer)
    if corruption == "origin_seq":
        path = evidence.replacement / "business-state-reconnected.json"
        record = json.loads(path.read_text())
        record["native_origin"]["event_seq"] = 99
        write(path, record)
    assert probe.grade(evidence.args) == 1
    verdict = json.loads((evidence.replacement.parent / "verdict.json").read_text())
    assert verdict["checks"]["native_source_hash_pointer_and_actual_state"] is False
    assert verdict["checks"]["native_session_appends_only_load_boundary"] is True


@pytest.mark.parametrize("append_boundary", [False, True])
def test_dsh_allows_only_unchanged_state_or_one_actual_native_load_boundary(evidence, append_boundary):
    session = dsh_evidence(evidence)
    if append_boundary:
        with session.open("a") as stream:
            stream.write(json.dumps({"type": "session/end-seed", "seq": 1, "time": 2, "data": {}}) + "\n")
        path = evidence.replacement / "business-state-reconnected.json"
        record = json.loads(path.read_text())
        record["native_origin"]["physical_sha256"] = hashlib.sha256(session.read_bytes()).hexdigest()
        write(path, record)
    assert probe.grade(evidence.args) == 0


@pytest.mark.parametrize("corruption", ["added_file", "added_directory", "added_symlink", "removed_other_file",
    "rewritten_other_file", "session_mode_change", "boundary_payload", "boundary_seq", "multiple_boundaries", "missing_boundary_newline"])
def test_dsh_cannot_change_other_native_state_under_cover_of_a_valid_load_marker(evidence, corruption):
    session = dsh_evidence(evidence)
    marker = {"type": "session/end-seed", "seq": 1, "time": 2, "data": {}}
    if corruption == "added_file":
        (evidence.paths.state / "unexpected-state.json").write_text("a replacement state\n")
    elif corruption == "added_directory":
        (evidence.paths.state / "unexpected-directory").mkdir()
    elif corruption == "added_symlink":
        (evidence.paths.state / "unexpected-link").symlink_to("missing-target")
    elif corruption == "removed_other_file":
        (evidence.paths.state / "other-session.json").unlink()
    elif corruption == "rewritten_other_file":
        (evidence.paths.state / "other-session.json").write_text("rewritten unrelated state\n")
    elif corruption == "boundary_payload":
        marker["data"] = {"business_state": "substituted"}
    elif corruption == "boundary_seq":
        marker["seq"] = 99
    with session.open("a") as stream:
        stream.write(json.dumps(marker) + ("" if corruption == "missing_boundary_newline" else "\n"))
        if corruption == "multiple_boundaries":
            stream.write(json.dumps({**marker, "seq": 2}) + "\n")
    if corruption == "session_mode_change":
        session.chmod(0o400)
    path = evidence.replacement / "business-state-reconnected.json"
    record = json.loads(path.read_text())
    record["native_origin"]["physical_sha256"] = hashlib.sha256(session.read_bytes()).hexdigest()
    write(path, record)
    assert probe.grade(evidence.args) == 1
    verdict = json.loads((evidence.replacement.parent / "verdict.json").read_text())
    assert verdict["checks"]["native_source_hash_pointer_and_actual_state"] is True
    assert verdict["checks"]["native_session_appends_only_load_boundary"] is False


def test_inventory_records_links_without_following_their_targets(tmp_path):
    root, outside = tmp_path / "state", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "must-not-be-read").write_text("outside the declared state")
    (root / "directory-link").symlink_to(outside, target_is_directory=True)
    (root / "file-link").symlink_to(outside / "must-not-be-read")
    (root / "dangling-link").symlink_to("missing-target")
    inventory = probe.inventory(root)
    assert set(inventory) == {"directory-link", "file-link", "dangling-link"}
    assert all(row["type"] == "symlink" and "sha256" not in row for row in inventory.values())
    assert inventory["directory-link"]["target"] == str(outside)
    assert inventory["dangling-link"]["target"] == "missing-target"


@pytest.mark.parametrize("change", ["link_target", "file_to_link", "link_to_file"])
def test_state_difference_records_link_changes_without_reading_link_targets(tmp_path, change):
    root = tmp_path / "state"
    root.mkdir()
    path = root / "session.jsonl"
    if change == "file_to_link":
        path.write_text('{"type":"original"}\n')
    else:
        path.symlink_to("missing-before")
    before = probe.inventory(root)
    path.unlink()
    if change == "link_to_file":
        path.write_text('{"type":"replacement"}\n')
    else:
        path.symlink_to("missing-after")
    difference = probe.state_difference(before, Namespace(state=root))
    assert difference["added"] == difference["removed"] == []
    row, = difference["changed"]
    assert row["path"] == "session.jsonl" and row["original_prefix_preserved"] is False
    assert "appended_event_types" not in row


@pytest.mark.parametrize("linked_parent", [False, True])
def test_native_source_still_rejects_file_and_directory_symlinks(evidence, tmp_path, linked_parent):
    item = evidence
    source = item.replacement / "native-state.json"
    outside = tmp_path / "outside-source"
    outside.mkdir()
    (outside / source.name).write_bytes(source.read_bytes())
    source.unlink()
    if linked_parent:
        (item.replacement / "linked").symlink_to(outside, target_is_directory=True)
        record = json.loads((item.replacement / "business-state-reconnected.json").read_text())
        record["source"]["path"] = "linked/native-state.json"
        write(item.replacement / "business-state-reconnected.json", record)
    else:
        source.symlink_to(outside / source.name)
    with pytest.raises(ValueError, match='不能跟随符号链接'):
        probe.projection(item.replacement, "business-state-reconnected.json", item.scope)
    assert probe.grade(item.args) == 1


def test_dsh_prepare_sees_empty_evidence_before_transport_saves_bench_log(evidence, monkeypatch):
    item = evidence
    item.manifest["candidate"] = "deepseek_harness"
    event = {"type": "harness-lab/business-state", "seq": 0, "data": item.state}
    session = item.paths.state / "sessions/main/session.jsonl"
    write(session, event)
    jsonl(item.original / "native-business-state-record.jsonl", [event])
    write(item.original / "business-state.json", {"scope": item.scope, "business_state": item.state,
        "source": {"path": "native-business-state-record.jsonl", "line": 1,
                   "sha256": hashlib.sha256((item.original / "native-business-state-record.jsonl").read_bytes()).hexdigest(), "json_pointer": "/data"},
        "native_origin": {"path": "/state/sessions/main/session.jsonl", "line": 1, "session_id": "main",
                          "physical_sha256": hashlib.sha256(session.read_bytes()).hexdigest()}})
    request = {"run_id": "synthetic", "session_id": "main", "workspace": "/workspace", "state_dir": "/state",
        "output_dir": "/evidence", "tools_url": "http://tools:8081", "candidate_options": {
            "case": "extension", "session_action": "create", "followup": "must not be submitted"}}
    write(item.paths.fixture / "candidate-request.json", request)
    fresh = item.replacement.parent / "fresh"
    fresh.mkdir()
    monkeypatch.setattr(probe, "PROBE_ROOT", fresh)
    lab = object.__new__(NativeStateLab)
    def transport(args, **kwargs):
        if args[:3] == ["docker", "start", "--attach"]:
            assert list(fresh.iterdir()) == []
            assert probe.prepare(item.args) == 0
        return subprocess.CompletedProcess(args, 0, "captured inspection output", "")
    lab.call = transport
    assert lab.execute("synthetic-bench", fresh / "bench-prepare.log", 90) == 0
    restored = json.loads((fresh / "controls/request.json").read_text())
    assert restored["candidate_options"]["session_action"] == "resume"
    assert "followup" not in restored["candidate_options"]
    assert json.loads((item.paths.fixture / "candidate-request.json").read_text()) == request
    assert (fresh / "bench-prepare.log").is_file()
