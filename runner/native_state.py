"""仅在 bench 中验收替换容器内的原生状态检查。

此处检查冷读取和反序列化，不判定任务暂停/恢复。
旧回执引用有意解析到原始证据，因为替换容器不得重新查询业务工具。"""
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
        raise ValueError("原生证据路径超出声明挂载")
    current = root
    if current.is_symlink():
        raise ValueError("原生证据不能跟随符号链接")
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("原生证据不能跟随符号链接")
    return path


def bytes_at(path, root):
    path = safe(path, root)
    if not path.is_file():
        raise ValueError("原生证据必须是普通文件")
    return path.read_bytes()


def read(path, root):
    value = json.loads(bytes_at(path, root))
    if not isinstance(value, dict):
        raise ValueError("预期为证据对象")
    return value


def inventory(directory):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("预期为原始挂载目录")
    result = {}
    def visit(parent):
        # 原生运行时可能在状态中保留链接。只盘点链接
        # 本身，不遍历它，也不读取其指向的文件或目录。
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
    """仅对外部已停止、只读的存储允许 immutable 读取。

    immutable=1 会忽略 WAL、日志和锁。不能借此掩盖可能已提交的事务：
    保守拒绝非空 WAL 或回滚日志，并保留前后全部伴随文件身份。
    调用方必须独立保证整个检查期间没有写入者。"""
    database = safe(database, root)
    result = {"directory_entries": sorted(path.name for path in database.parent.iterdir()), "files": {}}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database.with_name(database.name + suffix)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if not suffix:
                raise ValueError("缺少原始原生 SQLite 数据库") from None
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("SQLite 数据库或伴随文件必须是普通文件，不能是符号链接")
        if suffix in {"-wal", "-journal"} and metadata.st_size:
            raise ValueError("非空 SQLite WAL 或日志需要单独的一致性快照；已拒绝不可变冷读取")
        data = bytes_at(path, root)
        result["files"][path.name] = {"bytes": len(data), "mode": stat.S_IMODE(metadata.st_mode),
            "sha256": hashlib.sha256(data).hexdigest()}
    return result


def pointer(value, pointer_):
    if not isinstance(pointer_, str) or not pointer_.startswith("/"):
        raise ValueError("原生来源必须包含 JSON 指针")
    for part in pointer_[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def projection(directory, filename, scope):
    record = read(directory / filename, directory)
    source, state = record.get("source"), record.get("business_state")
    if not same_scope(record.get("scope", {}), scope) or not isinstance(source, dict) or not isinstance(state, dict):
        raise ValueError("业务投影没有作用域内的原生来源")
    name = source.get("path")
    if not isinstance(name, str):
        raise ValueError("缺少原生来源路径")
    if name.startswith("/state/"):
        path, root = Path(name), Path("/state")
    else:
        relative = Path(name.removeprefix("/evidence/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("原生证据来源路径无效")
        path, root = directory / relative, directory
    data = bytes_at(path, root)
    if hashlib.sha256(data).hexdigest() != source.get("sha256"):
        raise ValueError("原生来源字节哈希不匹配")
    if "line" in source:
        line = source["line"]
        if type(line) is not int or line < 1:
            raise ValueError("原生来源行无效")
        native = json.loads(data.splitlines()[line - 1])
    else:
        native = json.loads(data)
    if pointer(native, source.get("json_pointer")) != state:
        raise ValueError("业务字段与留存原生来源不一致")
    if state.get("schema_version") != 1 or not isinstance(state.get("operations"), dict) or state.get("receipt_confirmed") is not True:
        raise ValueError("原始扩展任务必须具有已确认的原生业务字段")
    return record, native


def receipt_sources(state, directory, scope):
    audit = [json.loads(line) for line in bytes_at(directory.parent / "tool-events.jsonl", directory.parent).splitlines()]
    audit_ok, pairs = audited_calls(audit, scope)
    if not audit_ok or not state["operations"]:
        raise ValueError("原始独立工具审计不完整")
    for operation, entry in state["operations"].items():
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("path") != "http-tool-events.jsonl" or type(source.get("line")) is not int or source["line"] < 1:
            raise ValueError("确认状态必须引用真实原始回执完成记录")
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
            raise ValueError("原生确认与独立审计的真实回执不匹配")


def context(args):
    config = load_config().for_environment("container")
    run = safe(args.run_dir, config.data_root / "runs")
    manifest = read(run / "manifest.json", run)
    candidate = manifest.get("candidate")
    if candidate not in SUPPORTED or manifest.get("case") != "extension" or manifest.get("candidate_exit_code") != 0 or manifest.get("grade_exit_code") != 0:
        raise ValueError("原始扩展运行必须已完成且通过")
    input_ = read(run / "input.json", run)
    scope = input_.get("scope", {})
    expected_scope = {"experiment_id": manifest["experiment_id"], "variant": manifest["run_id"], "user_id": "alice", "session_id": "main"}
    if scope != expected_scope:
        raise ValueError("原始运行作用域与清单不一致")
    paths = config.paths_for(manifest["experiment_id"], manifest["run_id"], candidate, "alice", "main",
        manifest["run_id"], case="extension", date=run.parent.parent.parent.name)
    if run != paths.run:
        raise ValueError("原始证据路径与当前实验布局不一致")
    return config, paths, scope, manifest


def state_path(name, paths):
    if not isinstance(name, str) or not name.startswith("/state/"):
        raise ValueError("原生来源必须位于原始状态挂载内")
    return safe(paths.state / Path(name).relative_to("/state"), paths.state)


def dsh_metadata_binding(record, event, state, rows, paths, scope):
    """将真实调度器结果与原生调用及实际回执绑定。

    工具正文提供规范输出，官方 presentationMeta 投影保存其状态，
    官方调度器负责工具/结果归属。适配器快照或任意类似元数据的对象不足以证明。"""
    if (not isinstance(event.get("data"), dict) or not isinstance(event["data"].get("meta"), dict)
            or type(event.get("seq")) is not int or not all(isinstance(row, dict) for row in rows)):
        raise ValueError("DSH 元数据来源不是原生事件对象")
    meta = event["data"]["meta"]
    if (record.get("source", {}).get("json_pointer") != "/data/meta/business_state"
            or meta.get("kind") != "harness-lab/business-state/v1" or meta.get("business_state") != state
            or state.get("session_id") != scope["session_id"] or state.get("scope") != scope
            or record.get("native_origin", {}).get("session_id") != scope["session_id"]
            or type(record["native_origin"].get("event_seq")) is not int
            or record["native_origin"]["event_seq"] != event["seq"]):
        raise ValueError("DSH 必需业务状态缺少精确原生元数据身份或指针")
    operation = state.get("operation_id")
    if operation not in state["operations"] or state.get("receipt_confirmed") is not True:
        raise ValueError("DSH 元数据没有当前已确认操作")
    current_result = None
    audit_path = paths.run / "candidate/http-tool-events.jsonl"
    audit = [json.loads(line) for line in bytes_at(audit_path, paths.run / "candidate").splitlines()]
    for operation_id, entry in state["operations"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), dict):
            raise ValueError("DSH 元数据操作不是完整回执观测")
        source = entry.get("source", {})
        line = source.get("line")
        if source.get("path") != "http-tool-events.jsonl" or type(line) is not int or line < 1:
            raise ValueError("DSH 元数据回执缺少原始 HTTP 观测")
        finish = audit[line - 1]
        if (not isinstance(finish, dict) or not isinstance(finish.get("result"), dict)
                or not isinstance(finish.get("arguments"), dict)):
            raise ValueError("DSH 回执来源不是真实 HTTP 完成对象")
        call_id = finish.get("call_id")
        actual = finish.get("result", {}).get("result")
        if (not isinstance(call_id, str) or not call_id or finish.get("scope") != scope
                or finish.get("tool") != "receipt" or finish.get("exposed_tool") != "query_order_status"
                or finish.get("phase") != "finish" or finish.get("status") != 200
                or finish.get("arguments", {}).get("operation_id") != operation_id
                or not isinstance(actual, dict) or actual.get("operation_id") != operation_id
                or entry.get("operation_id") != operation_id or entry.get("receipt_confirmed") is not True
                or actual.get("write_count") != entry.get("write_count") or actual.get("receipts") != entry.get("receipts")):
            raise ValueError("DSH 元数据操作与真实回执来源不一致")
        results = [row for row in rows if row.get("type") == "tool/result"
                   and isinstance(row.get("data"), dict) and isinstance(row["data"].get("message"), dict)
                   and isinstance(row["data"]["message"].get("source"), dict)
                   and row["data"]["message"]["source"].get("callId") == call_id]
        if len(results) != 1:
            raise ValueError("DSH 回执观测必须恰好包含一个原生工具结果")
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
            raise ValueError("DSH 原生回执结果身份或结果状态无效")
        content = blocks[0].get("content", [])
        if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str) or json.loads(content[0]["text"]) != actual):
            raise ValueError("DSH 原生回执内容与真实 HTTP 结果不一致")
        calls = [row for row in rows if row.get("seq") == refs[0]]
        if len(calls) != 1:
            raise ValueError("DSH 原生回执结果没有唯一来源调用")
        call = calls[0]
        call_data = call.get("data", {})
        if not isinstance(call_data, dict):
            raise ValueError("DSH 原生回执调用缺少参数对象")
        if (call.get("type") != "tool/call" or call["seq"] >= result["seq"]
                or call_data.get("name") != "query_order_status" or call_data.get("callId") != call_id
                or call_data.get("turn") != data.get("turn") or call_data.get("step") != data.get("step")
                or json.loads(call_data.get("arguments", "null")) != finish.get("arguments")):
            raise ValueError("DSH 原生回执不是真实别名调用及操作")
        if operation_id == operation:
            current_result = result
    if current_result != event:
        raise ValueError("DSH 元数据记录不是当前操作的真实回执结果")
    for row in rows:
        if type(row.get("seq")) is not int or row["seq"] <= event["seq"]:
            continue
        row_data = row.get("data", {})
        if not isinstance(row_data, dict):
            raise ValueError("DSH 原生事件没有 data 对象")
        later_meta = row_data.get("meta")
        if row.get("type") == "tool/result" and isinstance(later_meta, dict) and later_meta.get("kind") == meta["kind"]:
            raise ValueError("DSH 来源遗漏了后续原生业务元数据结果")
        if row.get("type") == "tool/call" and row_data.get("name") == "write":
            args = json.loads(row_data.get("arguments", "null"))
            if isinstance(args, dict) and args.get("operation_id") in state["operations"]:
                raise ValueError("DSH 来源早于未确认原生写入")


def native_binding(candidate, record, native, state, paths, scope):
    """将报告的 API 来源与实际原始状态文件交叉核对。"""
    if candidate == "deepseek_harness":
        origin = record.get("native_origin", {})
        path = state_path(origin.get("path"), paths)
        data = bytes_at(path, paths.state)
        if hashlib.sha256(data).hexdigest() != origin.get("physical_sha256"):
            raise ValueError("原生 DSH 会话物理哈希与报告来源不一致")
        line = origin.get("line")
        if type(line) is not int or line < 1:
            raise ValueError("原生 DSH 记录没有精确原始行")
        rows = [json.loads(row) for row in data.splitlines()]
        event = rows[line - 1]
        if event != native:
            raise ValueError("DSH 公共投影与真实原始会话记录不一致")
        if event.get("type") == "harness-lab/business-state":
            # 将早期自定义事件变体保留为证据，包括
            # 真实固定上游拒绝重载的结果。绝不改写旧日志。
            if event.get("data") != state or record.get("source", {}).get("json_pointer") != "/data":
                raise ValueError("DSH 旧自定义事件与留存状态不一致")
        elif event.get("type") == "tool/result":
            dsh_metadata_binding(record, event, state, rows, paths, scope)
        else:
            raise ValueError("DSH 必需业务状态不是准入原生来源")
        return {"path": str(path.relative_to(paths.state)), "native_session_id": origin.get("session_id"), "event_seq": event.get("seq")}
    if candidate == "openai_agents_sdk":
        path = paths.state / "run-state.json"
        raw = bytes_at(path, paths.state)
        actual = json.loads(raw)
        if pointer(actual, "/context/context/business_state") != state or actual != native:
            raise ValueError("SDK 重建与真实原始原生 RunState 不一致")
        context_ = actual.get("context", {}).get("context", {})
        if context_.get("scope") != scope or context_.get("session_id") != scope["session_id"]:
            raise ValueError("SDK 原生 RunState 的原始身份不同")
        return {"path": "run-state.json", "sha256": hashlib.sha256(raw).hexdigest()}
    if candidate == "deepagents_langgraph":
        import ormsgpack

        path = paths.state / "checkpoints.sqlite"
        before = cold_sqlite_inventory(path, paths.state)
        digest = before["files"][path.name]["sha256"]
        configurable = native.get("config", {}).get("configurable", {})
        checkpoint_id = configurable.get("checkpoint_id") or record.get("checkpoint_id")
        if configurable.get("thread_id") != scope["session_id"] or configurable.get("checkpoint_ns", "") != "":
            raise ValueError("公共图来源的原始线程或命名空间不同")
        # 正常关闭的 WAL 模式数据库可能保留 WAL 头而没有 shm。
        # immutable 避免在严格只读冷挂载上创建 shm；
        # 上方显式伴随文件门槛禁止静默忽略 WAL。
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            row = db.execute("SELECT checkpoint_id,type,checkpoint FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1", (scope["session_id"],)).fetchone()
        after = cold_sqlite_inventory(path, paths.state)
        if before != after:
            raise ValueError("原生 SQLite 数据库或伴随文件清单在冷检查期间变化")
        if row is None or row[0] != checkpoint_id:
            raise ValueError("公共图检查点未标识原始数据库当前线程检查点")
        # 固定 SqliteSaver 3.1.2 将 JsonPlusSerializer 4.3.0 的类型化
        # 检查点直接存入此行，准入格式为 msgpack。
        # 使用已锁定的标准解码器，对无关消息扩展
        # 保持不透明；不导入或重建候选框架类，也不将
        # 适配器导出快照当成数据库内值。
        if row[1] != "msgpack" or not isinstance(row[2], bytes):
            raise ValueError("原生图检查点的存储序列化格式不受支持")
        actual = ormsgpack.unpackb(row[2], ext_hook=ormsgpack.Ext, option=ormsgpack.OPT_NON_STR_KEYS)
        if (not isinstance(actual, dict) or actual.get("id") != checkpoint_id
                or not isinstance(actual.get("channel_values"), dict)
                or actual["channel_values"].get("business_state") != state):
            raise ValueError("图投影与原生检查点数据库真实业务值不一致")
        if "database_sha256_after" in record and not (record.get("database_sha256_before") == record["database_sha256_after"] == digest):
            raise ValueError("图检查点数据库哈希在检查期间变化")
        return {"path": "checkpoints.sqlite", "sha256": digest, "checkpoint_id": checkpoint_id,
                "sqlite_files_before": before, "sqlite_files_after": after,
                "business_values_read_by": "bench 独立解码真实 SQLite 检查点 msgpack；无关扩展保持不透明"}
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
            raise ValueError("OpenClaw API 快照未恰好匹配一条真实原生会话记录")
        return matches[0]
    raise ValueError("不支持的原生状态读取器")


def baseline_files(paths):
    if not (paths.budget / "calls.sqlite").is_file() or not (paths.run / "tool-events.jsonl").is_file():
        raise ValueError("必须提供原始预算数据库和独立 HTTP 审计")
    return {"budget": inventory(paths.budget), "http_audit": hashlib.sha256(bytes_at(paths.run / "tool-events.jsonl", paths.run)).hexdigest(),
            "original_candidate": inventory(paths.run / "candidate"), "state": inventory(paths.state), "workspace": inventory(paths.workspace)}


def prepare(args):
    config, paths, scope, manifest = context(args)
    probe = PROBE_ROOT
    if any(probe.iterdir()):
        raise ValueError("原生检查必须使用全新独立证据")
    original, native = projection(paths.run / "candidate", "business-state.json", scope)
    state = original["business_state"]
    receipt_sources(state, paths.run / "candidate", scope)
    binding = native_binding(manifest["candidate"], original, native, state, paths, scope)
    request = read(paths.fixture / "candidate-request.json", paths.fixture)
    if request.get("run_id") != manifest["run_id"] or request.get("session_id") != scope["session_id"] or request.get("candidate_options", {}).get("case") != "extension":
        raise ValueError("原始可信请求与来源运行不匹配")
    if any(request.get(key) != value for key, value in {"workspace": "/workspace", "state_dir": "/state", "output_dir": "/evidence", "tools_url": "http://tools:8081"}.items()):
        raise ValueError("原始请求改变了准入挂载或服务路径")
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
    """仅允许固定 Session 构造器的唯一原生加载标记。"""
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
        "claim": "在不同容器中读取或重建同一业务状态；未验证任务继续执行",
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
            raise RuntimeError("原始初始化服务未就绪：" + service)
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
