"""Bench-only acceptance of native state inspection in a replacement container.

This is a cold read/deserialization check, never a task pause/resume verdict.
The old receipt references deliberately resolve against the original evidence,
because the replacement is forbidden from querying business tools again.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from runner.bench import environment, identifier
from runner.config import load_config
from runner.grading import audited_calls, same_scope

SUPPORTED = frozenset(("deepseek_harness", "openclaw", "deepagents_langgraph", "openai_agents_sdk"))
PROBE_ROOT = Path("/probe")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def safe(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError("Native evidence path escaped its declared mount")
    current = root
    if current.is_symlink():
        raise ValueError("Native evidence cannot follow symlinks")
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Native evidence cannot follow symlinks")
    return path


def bytes_at(path, root):
    path = safe(path, root)
    if not path.is_file():
        raise ValueError("Native evidence must be a regular file")
    return path.read_bytes()


def read(path, root):
    value = json.loads(bytes_at(path, root))
    if not isinstance(value, dict):
        raise ValueError("Expected an evidence object")
    return value


def inventory(directory):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Expected the original mounted directory")
    result = {}
    def visit(parent):
        # Native runtimes may retain links in their state. Inventory the link
        # itself; never traverse it or read the file/directory it points to.
        with os.scandir(parent) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            metadata = path.lstat()
            name = str(path.relative_to(directory))
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                result[name] = {"type": "symlink", "target": os.readlink(path), "mode": mode}
            elif stat.S_ISDIR(metadata.st_mode):
                result[name] = {"type": "directory", "mode": mode}
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                data = bytes_at(path, directory)
                result[name] = {"type": "file", "mode": mode, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            else:
                result[name] = {"type": "special", "mode": metadata.st_mode, "rdev": metadata.st_rdev}
    visit(directory)
    return result


def cold_sqlite_inventory(database, root):
    """Admit immutable reads only for an externally stopped, read-only store.

    immutable=1 ignores WAL/journals and locking. Never use it to hide a
    possible committed transaction: conservatively reject nonempty WAL or
    rollback journal, and retain all companion-file identities before/after.
    The caller must independently exclude writers throughout this inspection.
    """
    database = safe(database, root)
    result = {"directory_entries": sorted(path.name for path in database.parent.iterdir()), "files": {}}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database.with_name(database.name + suffix)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if not suffix:
                raise ValueError("Missing original native SQLite database") from None
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("SQLite database/companion must be a regular file, never a symlink")
        if suffix in {"-wal", "-journal"} and metadata.st_size:
            raise ValueError("Nonempty SQLite WAL/journal requires a separate consistent snapshot; immutable cold read refused")
        data = bytes_at(path, root)
        result["files"][path.name] = {"bytes": len(data), "mode": stat.S_IMODE(metadata.st_mode),
            "sha256": hashlib.sha256(data).hexdigest()}
    return result


def pointer(value, pointer_):
    if not isinstance(pointer_, str) or not pointer_.startswith("/"):
        raise ValueError("Native source requires a JSON pointer")
    for part in pointer_[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def projection(directory, filename, scope):
    record = read(directory / filename, directory)
    source, state = record.get("source"), record.get("business_state")
    if not same_scope(record.get("scope", {}), scope) or not isinstance(source, dict) or not isinstance(state, dict):
        raise ValueError("Business projection has no scoped native source")
    name = source.get("path")
    if not isinstance(name, str):
        raise ValueError("Missing native source path")
    if name.startswith("/state/"):
        path, root = Path(name), Path("/state")
    else:
        relative = Path(name.removeprefix("/evidence/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid native evidence source path")
        path, root = directory / relative, directory
    data = bytes_at(path, root)
    if hashlib.sha256(data).hexdigest() != source.get("sha256"):
        raise ValueError("Native source byte hash mismatch")
    if "line" in source:
        line = source["line"]
        if type(line) is not int or line < 1:
            raise ValueError("Invalid native source line")
        native = json.loads(data.splitlines()[line - 1])
    else:
        native = json.loads(data)
    if pointer(native, source.get("json_pointer")) != state:
        raise ValueError("Business fields differ from the retained native source")
    if state.get("schema_version") != 1 or not isinstance(state.get("operations"), dict) or state.get("receipt_confirmed") is not True:
        raise ValueError("Original extension task must have confirmed native business fields")
    return record, native


def receipt_sources(state, directory, scope):
    audit = [json.loads(line) for line in bytes_at(directory.parent / "tool-events.jsonl", directory.parent).splitlines()]
    audit_ok, pairs = audited_calls(audit, scope)
    if not audit_ok or not state["operations"]:
        raise ValueError("Original independent tool audit is incomplete")
    for operation, entry in state["operations"].items():
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("path") != "http-tool-events.jsonl" or type(source.get("line")) is not int or source["line"] < 1:
            raise ValueError("Confirmed state must reference its actual original receipt finish")
        lines = bytes_at(directory / source["path"], directory).splitlines()
        row = json.loads(lines[source["line"] - 1])
        actual = row.get("result", {}).get("result")
        arguments = row.get("arguments", {})
        if (entry.get("operation_id") != operation or entry.get("receipt_confirmed") is not True
                or not same_scope(row.get("scope", {}), scope) or row.get("phase") != "finish"
                or row.get("status") != 200 or row.get("tool") != "receipt" or row.get("exposed_tool") != "query_order_status"
                or arguments.get("operation_id") != operation or not isinstance(actual, dict)
                or actual.get("operation_id") != operation or entry.get("write_count") != actual.get("write_count")
                or entry.get("receipts") != actual.get("receipts") or type(entry.get("write_count")) is not int
                or entry["write_count"] < 1 or entry["write_count"] != len(entry["receipts"])
                or not any(start.get("tool") == "receipt" and start.get("arguments") == arguments
                           and finish.get("status") == 200 and finish.get("result") == actual for start, finish in pairs)):
            raise ValueError("Native confirmation does not match an independently audited actual receipt")


def context(args):
    config = load_config().for_environment("container")
    run = safe(args.run_dir, config.data_root / "runs")
    manifest = read(run / "manifest.json", run)
    candidate = manifest.get("candidate")
    if candidate not in SUPPORTED or manifest.get("case") != "extension" or manifest.get("candidate_exit_code") != 0 or manifest.get("grade_exit_code") != 0:
        raise ValueError("The original extension run must have completed and passed")
    input_ = read(run / "input.json", run)
    scope = input_.get("scope", {})
    expected_scope = {"experiment_id": manifest["experiment_id"], "variant": manifest["run_id"], "user_id": "alice", "session_id": "main"}
    if scope != expected_scope:
        raise ValueError("Original run scope differs from its manifest")
    paths = config.paths_for(manifest["experiment_id"], manifest["run_id"], candidate, "alice", "main",
        manifest["run_id"], case="extension", date=run.parent.parent.parent.name)
    if run != paths.run:
        raise ValueError("Original evidence path differs from current experiment layout")
    return config, paths, scope, manifest


def state_path(name, paths):
    if not isinstance(name, str) or not name.startswith("/state/"):
        raise ValueError("Native origin must be inside the original state mount")
    return safe(paths.state / Path(name).relative_to("/state"), paths.state)


def dsh_metadata_binding(record, event, state, rows, paths, scope):
    """Bind the real scheduler result to its native call and actual receipt.

    The tool body supplies canonical output, the official presentationMeta
    projection stores its state, and the official scheduler owns tool/result.
    An adapter snapshot or an arbitrary metadata-shaped object is insufficient.
    """
    if (not isinstance(event.get("data"), dict) or not isinstance(event["data"].get("meta"), dict)
            or type(event.get("seq")) is not int or not all(isinstance(row, dict) for row in rows)):
        raise ValueError("DSH metadata source is not a native event object")
    meta = event["data"]["meta"]
    if (record.get("source", {}).get("json_pointer") != "/data/meta/business_state"
            or meta.get("kind") != "harness-lab/business-state/v1" or meta.get("business_state") != state
            or state.get("session_id") != scope["session_id"] or state.get("scope") != scope
            or record.get("native_origin", {}).get("session_id") != scope["session_id"]
            or type(record["native_origin"].get("event_seq")) is not int
            or record["native_origin"]["event_seq"] != event["seq"]):
        raise ValueError("DSH required business state has no exact native metadata identity/pointer")
    operation = state.get("operation_id")
    if operation not in state["operations"] or state.get("receipt_confirmed") is not True:
        raise ValueError("DSH metadata has no current confirmed operation")
    current_result = None
    audit_path = paths.run / "candidate/http-tool-events.jsonl"
    audit = [json.loads(line) for line in bytes_at(audit_path, paths.run / "candidate").splitlines()]
    for operation_id, entry in state["operations"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), dict):
            raise ValueError("DSH metadata operation is not a complete receipt observation")
        source = entry.get("source", {})
        line = source.get("line")
        if source.get("path") != "http-tool-events.jsonl" or type(line) is not int or line < 1:
            raise ValueError("DSH metadata receipt lacks its original HTTP observation")
        finish = audit[line - 1]
        if (not isinstance(finish, dict) or not isinstance(finish.get("result"), dict)
                or not isinstance(finish.get("arguments"), dict)):
            raise ValueError("DSH receipt source is not an actual HTTP finish object")
        call_id = finish.get("call_id")
        actual = finish.get("result", {}).get("result")
        if (not isinstance(call_id, str) or not call_id or finish.get("scope") != scope
                or finish.get("tool") != "receipt" or finish.get("exposed_tool") != "query_order_status"
                or finish.get("phase") != "finish" or finish.get("status") != 200
                or finish.get("arguments", {}).get("operation_id") != operation_id
                or not isinstance(actual, dict) or actual.get("operation_id") != operation_id
                or entry.get("operation_id") != operation_id or entry.get("receipt_confirmed") is not True
                or actual.get("write_count") != entry.get("write_count") or actual.get("receipts") != entry.get("receipts")):
            raise ValueError("DSH metadata operation differs from its actual receipt source")
        results = [row for row in rows if row.get("type") == "tool/result"
                   and isinstance(row.get("data"), dict) and isinstance(row["data"].get("message"), dict)
                   and isinstance(row["data"]["message"].get("source"), dict)
                   and row["data"]["message"]["source"].get("callId") == call_id]
        if len(results) != 1:
            raise ValueError("DSH receipt observation lacks exactly one native tool result")
        result = results[0]
        data = result.get("data", {})
        message = data.get("message", {})
        blocks = message.get("content", [])
        refs = result.get("sourceEventSeqs")
        if (message.get("source") != {"kind": "tool", "callId": call_id} or message.get("role") != "user"
                or not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict)
                or blocks[0].get("type") != "tool-result"
                or blocks[0].get("toolCallId") != call_id or blocks[0].get("isError") is not False
                or "error" in data or result.get("surfaceOp") != "append"
                or not isinstance(refs, list) or len(refs) != 1 or type(refs[0]) is not int
                or type(result.get("seq")) is not int or result["seq"] > event["seq"]
                or type(data.get("turn")) is not int or type(data.get("step")) is not int):
            raise ValueError("DSH native receipt result identity/outcome is invalid")
        content = blocks[0].get("content", [])
        if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str) or json.loads(content[0]["text"]) != actual):
            raise ValueError("DSH native receipt content differs from its actual HTTP result")
        calls = [row for row in rows if row.get("seq") == refs[0]]
        if len(calls) != 1:
            raise ValueError("DSH native receipt result has no unique source call")
        call = calls[0]
        call_data = call.get("data", {})
        if not isinstance(call_data, dict):
            raise ValueError("DSH native receipt call has no argument object")
        if (call.get("type") != "tool/call" or call["seq"] >= result["seq"]
                or call_data.get("name") != "query_order_status" or call_data.get("callId") != call_id
                or call_data.get("turn") != data.get("turn") or call_data.get("step") != data.get("step")
                or json.loads(call_data.get("arguments", "null")) != finish.get("arguments")):
            raise ValueError("DSH native receipt is not the actual alias invocation and operation")
        if operation_id == operation:
            current_result = result
    if current_result != event:
        raise ValueError("DSH metadata record is not the current operation's actual receipt result")
    for row in rows:
        if type(row.get("seq")) is not int or row["seq"] <= event["seq"]:
            continue
        row_data = row.get("data", {})
        if not isinstance(row_data, dict):
            raise ValueError("DSH native event has no data object")
        later_meta = row_data.get("meta")
        if row.get("type") == "tool/result" and isinstance(later_meta, dict) and later_meta.get("kind") == meta["kind"]:
            raise ValueError("DSH source omits a later native business metadata result")
        if row.get("type") == "tool/call" and row_data.get("name") == "write":
            args = json.loads(row_data.get("arguments", "null"))
            if isinstance(args, dict) and args.get("operation_id") in state["operations"]:
                raise ValueError("DSH source precedes an unconfirmed native write")


def native_binding(candidate, record, native, state, paths, scope):
    """Cross-check the reported API source against actual original state files."""
    if candidate == "deepseek_harness":
        origin = record.get("native_origin", {})
        path = state_path(origin.get("path"), paths)
        data = bytes_at(path, paths.state)
        if hashlib.sha256(data).hexdigest() != origin.get("physical_sha256"):
            raise ValueError("Native DSH session physical hash differs from its reported origin")
        line = origin.get("line")
        if type(line) is not int or line < 1:
            raise ValueError("Native DSH record has no exact original line")
        rows = [json.loads(row) for row in data.splitlines()]
        event = rows[line - 1]
        if event != native:
            raise ValueError("DSH public projection differs from the actual original session record")
        if event.get("type") == "harness-lab/business-state":
            # Preserve the earlier custom-event variant as evidence, including
            # its real fixed-upstream reload refusal. Never rewrite old logs.
            if event.get("data") != state or record.get("source", {}).get("json_pointer") != "/data":
                raise ValueError("DSH legacy custom event differs from its retained state")
        elif event.get("type") == "tool/result":
            dsh_metadata_binding(record, event, state, rows, paths, scope)
        else:
            raise ValueError("DSH required business state is not an admitted native source")
        return {"path": str(path.relative_to(paths.state)), "native_session_id": origin.get("session_id"), "event_seq": event.get("seq")}
    if candidate == "openai_agents_sdk":
        path = paths.state / "run-state.json"
        raw = bytes_at(path, paths.state)
        actual = json.loads(raw)
        if pointer(actual, "/context/context/business_state") != state or actual != native:
            raise ValueError("SDK reconstruction differs from the actual original native RunState")
        context_ = actual.get("context", {}).get("context", {})
        if context_.get("scope") != scope or context_.get("session_id") != scope["session_id"]:
            raise ValueError("SDK native RunState has a different original identity")
        return {"path": "run-state.json", "sha256": hashlib.sha256(raw).hexdigest()}
    if candidate == "deepagents_langgraph":
        import ormsgpack

        path = paths.state / "checkpoints.sqlite"
        before = cold_sqlite_inventory(path, paths.state)
        digest = before["files"][path.name]["sha256"]
        configurable = native.get("config", {}).get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id") or record.get("checkpoint_id")
        if configurable.get("thread_id") != scope["session_id"] or configurable.get("checkpoint_ns", "") != "":
            raise ValueError("Public graph source has a different original thread or namespace")
        # A cleanly closed WAL-mode DB may retain the WAL header but no shm.
        # immutable avoids creating shm on the strictly read-only cold mount;
        # the explicit sidecar gate above forbids silently ignoring a WAL.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            row = db.execute("SELECT checkpoint_id,type,checkpoint FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1", (scope["session_id"],)).fetchone()
        after = cold_sqlite_inventory(path, paths.state)
        if before != after:
            raise ValueError("Native SQLite database/companion inventory changed during cold inspection")
        if row is None or row[0] != checkpoint_id:
            raise ValueError("Public graph checkpoint does not identify the original database's current thread checkpoint")
        # Fixed SqliteSaver 3.1.2 stores JsonPlusSerializer 4.3.0's typed
        # checkpoint directly in this row. Its admitted format is msgpack.
        # Use the already locked standard decoder, keeping unrelated message
        # extensions opaque; never import/reconstruct candidate classes or
        # treat the adapter's exported snapshot as the database's values.
        if row[1] != "msgpack" or not isinstance(row[2], bytes):
            raise ValueError("Native graph checkpoint has an unsupported stored serialization format")
        actual = ormsgpack.unpackb(row[2], ext_hook=ormsgpack.Ext, option=ormsgpack.OPT_NON_STR_KEYS)
        if (not isinstance(actual, dict) or actual.get("id") != checkpoint_id
                or not isinstance(actual.get("channel_values"), dict)
                or actual["channel_values"].get("business_state") != state):
            raise ValueError("Graph projection differs from the actual business values in the native checkpoint database")
        if "database_sha256_after" in record and not (record.get("database_sha256_before") == record["database_sha256_after"] == digest):
            raise ValueError("Graph checkpoint database hash changed during inspection")
        return {"path": "checkpoints.sqlite", "sha256": digest, "checkpoint_id": checkpoint_id,
                "sqlite_files_before": before, "sqlite_files_after": after,
                "business_values_read_by": "bench independently decodes actual SQLite checkpoint msgpack; unrelated extensions remain opaque"}
    if candidate == "openclaw":
        entry = native.get("native_entry", {})
        session_key = native.get("session_key")
        matches = []
        for database in sorted(paths.state.rglob("*.sqlite")):
            safe(database, paths.state)
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_nodes'").fetchone():
                    continue
                for session_id, encoded in db.execute("SELECT current_session_id,entry_json FROM session_nodes WHERE session_key=?", (session_key,)):
                    row = json.loads(encoded)
                    if (session_id == entry.get("sessionId") == state.get("native_session_id")
                            and row.get("pluginExtensions", {}).get("harness-lab-tools", {}).get("business") == state):
                        matches.append({"path": str(database.relative_to(paths.state)), "session_key": session_key, "native_session_id": session_id})
        if len(matches) != 1:
            raise ValueError("OpenClaw API snapshot does not match exactly one actual native session row")
        return matches[0]
    raise ValueError("Unsupported native state reader")


def baseline_files(paths):
    if not (paths.budget / "calls.sqlite").is_file() or not (paths.run / "tool-events.jsonl").is_file():
        raise ValueError("Original budget database and independent HTTP audit are required")
    return {"budget": inventory(paths.budget), "http_audit": hashlib.sha256(bytes_at(paths.run / "tool-events.jsonl", paths.run)).hexdigest(),
            "original_candidate": inventory(paths.run / "candidate"), "state": inventory(paths.state), "workspace": inventory(paths.workspace)}


def prepare(args):
    config, paths, scope, manifest = context(args)
    probe = PROBE_ROOT
    if any(probe.iterdir()):
        raise ValueError("Native inspection requires fresh, separate evidence")
    original, native = projection(paths.run / "candidate", "business-state.json", scope)
    state = original["business_state"]
    receipt_sources(state, paths.run / "candidate", scope)
    binding = native_binding(manifest["candidate"], original, native, state, paths, scope)
    request = read(paths.fixture / "candidate-request.json", paths.fixture)
    if request.get("run_id") != manifest["run_id"] or request.get("session_id") != scope["session_id"] or request.get("candidate_options", {}).get("case") != "extension":
        raise ValueError("Original trusted request does not match the source run")
    if any(request.get(key) != value for key, value in {"workspace": "/workspace", "state_dir": "/state", "output_dir": "/evidence", "tools_url": "http://tools:8081"}.items()):
        raise ValueError("Original request changed its admitted mount/service paths")
    if manifest["candidate"] == "deepseek_harness":
        request["candidate_options"] = {**request["candidate_options"], "session_action": "resume"}
        request["candidate_options"].pop("followup", None)
    (probe / "controls").mkdir()
    (probe / "candidate").mkdir()
    save(probe / "controls/request.json", request)
    save(probe / "baseline.json", {"probe_id": args.probe_id, "candidate": manifest["candidate"], "scope": scope,
        "source_run": str(paths.run), "source_manifest_sha256": hashlib.sha256(bytes_at(paths.run / "manifest.json", paths.run)).hexdigest(),
        "business_state": state, "native_binding": binding, "files": baseline_files(paths),
        "environment": environment(), "task_continuation_claim": False})
    return 0


def state_difference(before, paths):
    after = inventory(paths.state)
    changes = {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)), "changed": []}
    for name in sorted(set(before) & set(after)):
        if before[name] == after[name]:
            continue
        row = {"path": name, "before": before[name], "after": after[name]}
        row["original_prefix_preserved"] = False
        if before[name].get("type") == after[name].get("type") == "file":
            data = bytes_at(paths.state / name, paths.state)
            size = before[name]["bytes"]
            row["original_prefix_preserved"] = len(data) >= size and hashlib.sha256(data[:size]).hexdigest() == before[name]["sha256"]
            if row["original_prefix_preserved"] and name.endswith(".jsonl"):
                row["appended_event_types"] = [json.loads(line).get("type") for line in data[size:].splitlines()]
        changes["changed"].append(row)
    return changes


def dsh_load_boundary_only(difference, original_path, paths):
    """Allow only the fixed Session constructor's single native load marker."""
    if difference["added"] or difference["removed"]:
        return False
    if not difference["changed"]:
        return True
    if len(difference["changed"]) != 1:
        return False
    changed = difference["changed"][0]
    before, after = changed["before"], changed["after"]
    if (changed["path"] != original_path or not changed["original_prefix_preserved"]
            or before.get("type") != "file" or after.get("type") != "file"
            or before["mode"] != after["mode"] or after["bytes"] <= before["bytes"]):
        return False
    data = bytes_at(paths.state / original_path, paths.state)
    original, appended = data[:before["bytes"]], data[before["bytes"]:]
    if not original.endswith(b"\n") or not appended.endswith(b"\n"):
        return False
    rows = [json.loads(line) for line in appended.splitlines()]
    last = json.loads(original.splitlines()[-1])
    if len(rows) != 1 or not isinstance(last, dict) or type(last.get("seq")) is not int or last.get("type") == "session/end-seed":
        return False
    marker = rows[0]
    return (isinstance(marker, dict) and set(marker) == {"type", "seq", "time", "data"}
        and marker["type"] == "session/end-seed" and type(marker["seq"]) is int
        and marker["seq"] == last["seq"] + 1 and type(marker["time"]) is int
        and marker["time"] >= 0 and marker["data"] == {})


def no_task_execution(candidate, result, directory):
    if candidate == "openclaw":
        return result.get("status") == "inspected" and result.get("stop_reason") == "native_business_state_read_only" and all(result.get(key) is False for key in (
            "task_resume_claim", "prompt_submitted", "gateway_started", "model_called_by_entry", "business_tools_called_by_entry"))
    if candidate == "deepagents_langgraph":
        return result.get("status") == "validated" and result.get("native_status") == "checkpoint_read_only" and all(result.get(key) is False for key in ("model_started", "graph_started", "task_continuation_verified"))
    if candidate == "openai_agents_sdk":
        return (result.get("status") == "validated" and result.get("runner_started") is False and result.get("model_calls") == 0
                and result.get("http_discovery_requests") == 0 and result.get("model_client_request_attempts") == [] and result.get("task_continuation_verified") is False)
    if candidate == "deepseek_harness":
        snapshot = read(directory / "session-before-followup.json", directory)
        return (result.get("status") == "session_restored" and result.get("stop_reason") == "resume_inspection_only"
                and result.get("session_action") == "resume" and result.get("followup_submitted") is False
                and result.get("native_result", {}).get("inspectionOnly") is True
                and snapshot.get("agent_status") == "idle" and snapshot.get("inbox", {}).get("has_pending") is False)
    return False


def grade(args):
    _, paths, scope, manifest = context(args)
    probe = PROBE_ROOT
    baseline = read(probe / "baseline.json", probe)
    orchestration = read(probe / "orchestration.json", probe)
    checks, errors = {}, []
    checks["same_probe_and_original_scope"] = (baseline.get("probe_id") == args.probe_id == orchestration.get("probe_id")
        and baseline.get("candidate") == manifest["candidate"] == orchestration.get("candidate") and baseline.get("scope") == scope)
    try:
        original, _ = projection(paths.run / "candidate", "business-state.json", scope)
        reconstructed, native = projection(probe / "candidate", "business-state-reconnected.json", scope)
        state = reconstructed["business_state"]
        checks["same_complete_business_state"] = state == original["business_state"] == baseline["business_state"]
        receipt_sources(state, paths.run / "candidate", scope)
        binding = native_binding(manifest["candidate"], reconstructed, native, state, paths, scope)
        save(probe / "native-source-check.json", binding)
        checks["native_source_hash_pointer_and_actual_state"] = True
        result = read(probe / "candidate/result.json", probe / "candidate")
        checks["inspection_without_task_submission"] = no_task_execution(manifest["candidate"], result, probe / "candidate")
    except (OSError, ValueError, KeyError, IndexError, TypeError, sqlite3.Error) as error:
        errors.append(type(error).__name__ + ": " + str(error))
        checks["native_source_hash_pointer_and_actual_state"] = False
    now = baseline_files(paths)
    for key in ("budget", "http_audit", "original_candidate", "workspace"):
        checks[key + "_unchanged"] = now[key] == baseline["files"][key]
    checks["source_manifest_unchanged"] = hashlib.sha256(bytes_at(paths.run / "manifest.json", paths.run)).hexdigest() == baseline["source_manifest_sha256"]
    difference = state_difference(baseline["files"]["state"], paths)
    save(probe / "native-state-difference.json", difference)
    if manifest["candidate"] == "deepseek_harness":
        original_path = baseline["native_binding"]["path"]
        changed = next((row for row in difference["changed"] if row["path"] == original_path), None)
        checks["native_session_original_prefix_retained"] = original_path not in difference["removed"] and (changed is None or changed["original_prefix_preserved"])
        try:
            checks["native_session_appends_only_load_boundary"] = dsh_load_boundary_only(difference, original_path, paths)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            checks["native_session_appends_only_load_boundary"] = False
            errors.append(type(error).__name__ + ": " + str(error))
    else:
        checks["native_state_read_only_and_unchanged"] = now["state"] == baseline["files"]["state"]
    old, new = orchestration.get("original_worker", {}), orchestration.get("new_worker", {})
    checks["original_worker_matches_source_manifest"] = old in manifest.get("containers", [])
    checks["old_worker_deleted_by_actual_docker_inspection"] = all(orchestration.get(key) == {"id": True, "name": True} for key in ("original_worker_absent_before", "original_worker_absent_after"))
    checks["new_worker_same_image_new_container"] = bool(new.get("id")) and new.get("id") != old.get("id") and new.get("image_id") == old.get("image_id")
    checks["worker_resource_and_read_only_root_limits"] = new.get("memory_limit") == 2 ** 31 and new.get("cpu_nano") == 10 ** 9 and new.get("read_only") is True and bool(new.get("tmpfs"))
    mounted = {row["Destination"]: row for row in new.get("mounts", [])}
    original_mounted = {row["Destination"]: row for row in old.get("mounts", [])}
    checks["same_native_state_and_workspace_mounts"] = all(mounted.get(key, {}).get("Source") == original_mounted.get(key, {}).get("Source") for key in ("/state", "/workspace"))
    checks["mount_write_permissions"] = (mounted.get("/state", {}).get("RW") is (manifest["candidate"] == "deepseek_harness")
        and mounted.get("/workspace", {}).get("RW") is False and mounted.get("/evidence", {}).get("RW") is True)
    checks["fresh_candidate_evidence"] = mounted.get("/evidence", {}).get("Source") != original_mounted.get("/evidence", {}).get("Source") and bool(mounted.get("/evidence", {}).get("Source"))
    checks["no_proxy_or_upstream_credential"] = orchestration.get("model_proxy_started") is False and orchestration.get("real_model_credential_injected") is False and new.get("credential_env_present") is False
    checks["no_new_business_tool_calls"] = not (probe / "candidate/http-tool-events.jsonl").exists() or bytes_at(probe / "candidate/http-tool-events.jsonl", probe / "candidate").strip() == b""
    checks["candidate_native_inspection_exited_normally"] = orchestration.get("candidate_exit_code") == 0 and new.get("state", {}).get("ExitCode") == 0 and new.get("state", {}).get("Running") is False
    checks["no_new_task_resume_claim"] = orchestration.get("task_continuation_claim") is False
    checks["isolated_inspection_network"] = (new.get("network_mode") == "none" if manifest["candidate"] != "deepseek_harness" else
        new.get("network_mode") == orchestration.get("network", {}).get("name") and orchestration.get("network", {}).get("internal") is True)
    verdict = {"probe_id": args.probe_id, "candidate": manifest["candidate"], "source_run_id": manifest["run_id"],
        "scope": scope, "passed": all(checks.values()) and not errors and not orchestration.get("errors"), "checks": checks,
        "errors": errors + orchestration.get("errors", []), "state_was_read_only": manifest["candidate"] != "deepseek_harness",
        "claim": "same business state read/reconstructed in a different container; task continuation was not tested",
        "environment": environment()}
    save(probe / "verdict.json", verdict)
    print(json.dumps({key: verdict[key] for key in ("probe_id", "candidate", "passed", "claim")}))
    return 0 if verdict["passed"] else 1


def wait_services():
    import httpx
    for service, port in (("fixtures", 8080), ("tools", 8081)):
        for _ in range(30):
            try:
                reply = httpx.get(f"http://{service}:{port}/health", timeout=1, trust_env=False)
                reply.raise_for_status()
                break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            raise RuntimeError("Original setup service did not become ready: " + service)
    return 0


def main():
    environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "grade", "wait-services"))
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--probe-id", required=True, type=identifier)
    args = parser.parse_args()
    return wait_services() if args.action == "wait-services" else prepare(args) if args.action == "prepare" else grade(args)


if __name__ == "__main__":
    raise SystemExit(main())
