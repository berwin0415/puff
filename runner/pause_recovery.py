"""Docker bench I22：原任务暂停、删除、空闲和替换。

校准及各条真实链记录在 pause-recovery.md。宿主机负责 Docker；
此模块负责控制和独立证据检查，不实例化 Agent 或调用模型。
所有运行时代码必须放在固定 bench 镜像中；候选框架可写标签不能证明暂停安全。"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from runner.bench import environment, identifier
from runner.config import load_config
from runner.dsh_goal import change_projection, fold_durable_goal, matches_live_view
from runner import detailed
from runner.experiment import ledger_request
from runner.grading import audited_calls, grade_tools, scoped_ledger


CANDIDATES = ("deepagents_langgraph", "deepseek_harness")
ACTIONS = ("prepare", "bind-start", "check-paused", "wait-monitor", "bind-resume", "grade")
HEX = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
LIMIT = 300
WAIT = 600
CAP = 20
MEMORY = 6 * 1024**3
CPU = 3_000_000_000
MAX_FILE = 64 * 1024**2
HELPER = Path("/app/adapters/deepagents_langgraph/pause_recovery.py")


def require(value, code):
    if not value:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(canonical(value))


def pairs(values):
    result = {}
    for key, value in values:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def regular(path, limit=MAX_FILE):
    """读取有大小限制且稳定的普通文件，同时检查祖先目录和链接。"""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "absolute_path_required")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_evidence")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_size <= limit, "bounded_regular_file_required")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        require(len(data) <= limit and (before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_ino, after.st_size, after.st_mtime_ns), "evidence_changed_during_read")
        return data


def read(path):
    value = decode(regular(path))
    require(isinstance(value, dict), "json_object_required")
    return value


def create(path, value):
    """独占输出：失败链不能悄悄变成一条新链。"""
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), "linked_output")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def jsonl(path):
    data = regular(path)
    require(not data or data.endswith(b"\n"), "partial_jsonl_line")
    result = []
    for number, line in enumerate(data.splitlines(keepends=True), 1):
        value = decode(line)
        require(isinstance(value, dict), "jsonl_object_required")
        result.append((number, line, value))
    return result


def instant(value):
    require(isinstance(value, str), "timestamp_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timezone_required")
    return parsed.timestamp()


def now():
    return datetime.now(timezone.utc).isoformat()


def number(value, *, minimum=0):
    return type(value) in (float, int) and math.isfinite(value) and value >= minimum


def reference(root, ref, *, required_hash=False):
    require(isinstance(ref, dict), "source_reference_required")
    name = ref.get("path")
    require(isinstance(name, str), "source_path_required")
    path = Path(name.removeprefix("/evidence/"))
    require(not path.is_absolute() and ".." not in path.parts and path.suffix == ".jsonl",
            "source_path_invalid")
    require(type(ref.get("line")) is int and ref["line"] >= 1, "source_line_invalid")
    rows = jsonl(root / path)
    require(ref["line"] <= len(rows), "source_line_missing")
    line, encoded, value = rows[ref["line"] - 1]
    actual = sha(encoded)
    if required_hash or "line_sha256" in ref:
        require(ref.get("line_sha256") == actual, "source_line_hash_mismatch")
    return value, {"path": path.as_posix(), "line": line, "line_sha256": actual}


def inventory(state, workspace):
    """独立盘点 DSH 整棵目录树，包含空目录。"""
    files, directories, total = [], [], 0
    for label, root in (("state", state), ("workspace", workspace)):
        root = Path(root)
        require(root.is_dir() and not root.is_symlink(), "inventory_root_invalid")
        def walk(directory):
            nonlocal total
            directories.append({"root": label, "path": directory.relative_to(root).as_posix().replace(".", "", 1)
                                if directory == root else directory.relative_to(root).as_posix()})
            for child in sorted(directory.iterdir(), key=lambda item: item.name):
                info = child.lstat()
                if stat.S_ISDIR(info.st_mode):
                    walk(child)
                else:
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "special_or_linked_native_file")
                    data = regular(child)
                    total += len(data)
                    require(len(files) < 4096 and total <= 256 * 1024**2, "native_inventory_too_large")
                    files.append({"root": label, "path": child.relative_to(root).as_posix(),
                                  "bytes": len(data), "sha256": sha(data)})
        walk(root)
    return {"schema": "harness-lab-native-state-index/1", "files": files, "directories": directories}


def da_files(root, *, state=False):
    result = {}
    require(root.is_dir() and not root.is_symlink(), "native_root_invalid")
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode), "linked_native_state")
        if stat.S_ISDIR(info.st_mode):
            continue
        relative = path.relative_to(root).as_posix()
        if state and relative in {".i22-chain.lock", "pause-index.json"}:
            continue
        data = regular(path)
        result[relative] = {"bytes": len(data), "mode": stat.S_IMODE(info.st_mode), "sha256": sha(data)}
    require(not state or "checkpoints.sqlite" in result, "native_sqlite_missing")
    return result


def context(args):
    config = load_config().for_environment("container")
    paths = config.paths_for("pause-recovery", args.chain_id, args.candidate,
                             "alice", "main", args.chain_id, case="tools", date=args.date)
    scope = {"experiment_id": "pause-recovery", "variant": args.chain_id,
             "user_id": "alice", "session_id": "main"}
    return config, paths, scope


def pinned(args, paths, scope):
    pins = read(paths.run / "trusted/pins.json")
    require(pins.get("schema") == "harness-lab-pause-pins/1"
            and pins.get("candidate") == args.candidate and pins.get("chain_id") == args.chain_id
            and pins.get("scope") == scope and pins.get("seed") == args.seed
            and pins.get("date") == args.date, "frozen_identity_mismatch")
    require(pins.get("variant") in {"requested-pause", "uninterrupted"}, "unknown_lifecycle_variant")
    for name in ("source_export_sha256", "adapter_export_sha256"):
        require(isinstance(pins.get(name), str) and HEX.fullmatch(pins[name]), "invalid_material_pin")
    for name in ("candidate_image_id", "bench_image_id"):
        require(isinstance(pins.get(name), str) and IMAGE.fullmatch(pins[name]), "invalid_image_pin")
    require(isinstance(pins.get("model_name"), str) and pins["model_name"], "model_pin_required")
    require(pins.get("max_goal_rounds") == CAP and pins.get("model_call_cap") == CAP
            and pins.get("active_limit_seconds") == LIMIT and pins.get("pause_wait_seconds") == WAIT,
            "fixed_budget_pin_mismatch")
    require(isinstance(pins.get("worker_names"), dict)
            and set(pins["worker_names"]) == {"worker-a", "worker-b"}
            and len(set(pins["worker_names"].values())) == 2, "worker_name_pins_required")
    require(read(paths.run / "chain.json")["input_sha256"] == sha(regular(paths.run / "input.json")),
            "original_task_input_changed")
    return pins


def source_on_host(config, path):
    return str(config.host_data_root / path.relative_to(config.container_data_root))


def host_record(args, config, paths, scope, pins, attempt, *, paused):
    host = read(paths.run / "trusted" / (attempt + ".json"))
    require(all(host.get(key) == value for key, value in {
        "schema": "harness-lab-pause-host/1", "candidate": args.candidate,
        "chain_id": args.chain_id, "scope": scope, "attempt_id": attempt}.items()), "host_identity_mismatch")
    require(host.get("errors") == [] and type(host.get("process_exit")) is int and host["process_exit"] == 0
            and type(host.get("attach_exit")) is int and host["attach_exit"] == 0 and host.get("host_timeout", False) is False
            and host.get("trigger_error") is None, "host_execution_error")
    before, after = host.get("before_start"), host.get("after_exit")
    require(isinstance(before, dict) and isinstance(after, dict), "host_snapshots_missing")
    require(HEX.fullmatch(str(before.get("id", ""))) and before.get("id") == after.get("id"), "created_worker_id_mismatch")
    for snapshot in (before, after):
        require(snapshot.get("name", "").removeprefix("/") == pins["worker_names"][attempt], "worker_name_mismatch")
        require(snapshot.get("image_id") == pins["candidate_image_id"]
                and snapshot.get("image_tag") in {pins["candidate_image_id"], pins["candidate_image_tag"]},
                "worker_image_mismatch")
        require(snapshot.get("memory_limit") == 2 * 1024**3 and snapshot.get("cpu_nano") == 1_000_000_000,
                "worker_resource_cap_mismatch")
        mounts = snapshot.get("mounts")
        require(isinstance(mounts, list) and all(isinstance(row, dict) for row in mounts), "worker_mounts_missing")
        by_dest = {row.get("Destination"): row for row in mounts}
        require(len(by_dest) == len(mounts), "duplicate_mount_destination")
        expected = {"/state": (paths.state, True), "/workspace": (paths.workspace, True),
                    "/evidence": (paths.run / "attempts" / attempt / "candidate", True),
                    "/config/request.json": (paths.fixture / "attempts" / attempt / "request.json", False),
                    "/config/lifecycle.json": (paths.fixture / "attempts" / attempt / "lifecycle.json", False),
                    "/config/scope.json": (paths.fixture / "scope.json", False),
                    "/config/scope.token": (paths.fixture / "scope.token", False)}
        token_dest = "/config/model-run.token" if args.candidate == "deepagents_langgraph" else "/budget/run.token"
        expected[token_dest] = (paths.budget / "run.token", False)
        if args.candidate == "deepseek_harness":
            expected["/config/lifecycle-state.json"] = (paths.fixture / "attempts" / attempt / "lifecycle-state.json", False)
            if attempt == "worker-b":
                expected["/config/lifecycle-pause.json"] = (paths.fixture / "attempts" / attempt / "lifecycle-pause.json", False)
        require(set(by_dest) == set(expected), "unexpected_worker_mount")
        for destination, (path, writable) in expected.items():
            require(by_dest[destination].get("Source") == source_on_host(config, path)
                    and by_dest[destination].get("RW") is writable, "worker_mount_binding_mismatch")
    require(before["state"].get("Status") == "created" and before["state"].get("Running") is False,
            "worker_not_observed_before_start")
    state = after["state"]
    require(state.get("Status") == "exited" and type(state.get("Pid")) is int and state["Pid"] == 0
            and type(state.get("ExitCode")) is int and state["ExitCode"] == 0
            and all(state.get(key) is False for key in ("OOMKilled", "Dead", "Running", "Paused", "Restarting"))
            and state.get("Error") == "", "worker_not_healthy_exited")
    active = host.get("active_seconds")
    require(number(active, minimum=0.001) and active <= LIMIT, "invalid_host_active_duration")
    wall = instant(host["finished_at"]) - instant(host["started_at"])
    require(wall >= 0 and abs(wall - active) <= 2, "host_clock_duration_mismatch")
    require(before["image_tag"] == after["image_tag"], "worker_image_reference_changed")
    if paused:
        require(host.get("deleted", {}).get("id_absent") is True
                and host.get("deleted", {}).get("name_absent") is True, "old_worker_not_deleted")
        require(instant(host["deleted"]["observed_at"]) >= instant(host["finished_at"]), "deletion_precedes_exit")
        trigger = host.get("pause_request")
        require(isinstance(trigger, dict) and trigger.get("signal") == "SIGUSR2"
                and trigger.get("container_id") == before["id"]
                and type(trigger.get("signal_exit_code")) is int and trigger["signal_exit_code"] == 0,
                "actual_worker_pause_signal_missing")
        require(instant(host["started_at"]) <= instant(trigger["sent_at"]) <= instant(host["finished_at"]),
                "pause_signal_outside_active_attempt")
        validate_trigger(paths.run, scope, args.candidate, trigger)
    else:
        require(host.get("pause_request") is None, "unexpected_pause_request")
    return host


def observed_prefix(run, ref, *, empty=False):
    records = jsonl(run / ref['path'])
    observed = ref.get('observed_through_line')
    require(type(observed) is int and (0 if empty else ref['line']) <= observed <= len(records),
            'trigger_observed_native_prefix_missing')
    prefix = records[:observed]
    require(sha(b''.join(encoded for _, encoded, _ in prefix)) == ref.get('observed_prefix_sha256'),
            'trigger_observed_native_prefix_changed')
    return prefix


def validate_handler_observation(run, candidate, trigger, request_ref):
    """在控制器时钟上比较文件因果关系，不使用宿主机/VM UTC。"""
    if trigger.get('observation_clock') != 'host_monotonic_prefix/1':
        return False
    ref = trigger.get('control_source', {})
    filename = 'control-events.jsonl' if candidate == 'deepagents_langgraph' else 'pause-events.jsonl'
    require(ref.get('path') == 'attempts/worker-a/candidate/' + filename,
            'trigger_control_source_substitution')
    prefix = observed_prefix(run, ref, empty=True)
    require(not any(row.get('phase') == 'requested' for _, _, row in prefix),
            'pause_handler_already_requested_before_signal')
    require(request_ref.get('path') == filename and request_ref.get('line', 0) > len(prefix),
            'native_pause_request_not_after_host_observation')
    return True


def validate_trigger(run, scope, candidate, trigger):
    require(trigger.get("activity_source", {}).get("path") == "attempts/worker-a/candidate/raw-events.jsonl"
            and trigger.get("write_source", {}).get("path") == "tool-events.jsonl", "trigger_source_path_substitution")
    activity, _ = reference(run, trigger["activity_source"], required_hash=True)
    write, _ = reference(run, trigger["write_source"], required_hash=True)
    require(write.get("scope") == scope, "trigger_scope_mismatch")
    if candidate == "deepagents_langgraph":
        require(activity.get("scope") == scope, "trigger_native_scope_mismatch")
    else:
        header = read(run / "attempts/worker-a/candidate/session-before-followup.json")["header"]
        require(header.get("id") == scope["session_id"] and header.get("cwd") == "/workspace"
                and activity.get("params", {}).get("sessionId") == header["id"], "trigger_native_session_mismatch")
    require(write.get("tool") == "write" and write.get("phase") == "finish" and write.get("status") == 200
            and isinstance(write.get("result", {}).get("receipt"), dict), "pause_before_confirmed_business_write")
    sent = instant(trigger["sent_at"])
    causal = trigger.get('observation_clock') == 'host_monotonic_prefix/1'
    require(trigger.get('observation_clock') in (None, 'host_monotonic_prefix/1'), 'unknown_trigger_observation_clock')
    if causal:
        times = [trigger.get(key) for key in ('observation_started_monotonic', 'observation_finished_monotonic',
                                              'signal_sent_monotonic', 'signal_acknowledged_monotonic')]
        require(all(number(value) for value in times) and times == sorted(times)
                and times[2] - times[0] <= 5 and times[3] - times[2] <= 10,
                'trigger_host_observation_order_invalid')
        observed_prefix(run, trigger['write_source'])
        instant(write['time'])  # 保留原生时间戳，它属于不同的时钟。
        control = trigger.get('control_source', {})
        name = 'control-events.jsonl' if candidate == 'deepagents_langgraph' else 'pause-events.jsonl'
        require(control.get('path') == 'attempts/worker-a/candidate/' + name, 'trigger_control_source_substitution')
        require(not any(row.get('phase') == 'requested' for _, _, row in observed_prefix(run, control, empty=True)),
                'pause_handler_already_requested_before_signal')
    else:
        require(instant(write["time"]) <= sent, "trigger_future_write_source")
    if candidate == "deepagents_langgraph" and not causal:
        require(instant(activity["time"]) <= sent, "trigger_future_native_source")
    active = {}
    raw_path = run / trigger["activity_source"]["path"]
    raw = jsonl(raw_path)
    observed = trigger["activity_source"].get("observed_through_line")
    require(type(observed) is int and trigger["activity_source"]["line"] <= observed <= len(raw),
            "trigger_observed_native_prefix_missing")
    prefix = raw[:observed]
    require(sha(b"".join(encoded for _, encoded, _ in prefix))
            == trigger["activity_source"].get("observed_prefix_sha256"), "trigger_observed_native_prefix_changed")
    for line, _, row in prefix:
        if candidate == "deepagents_langgraph":
            require(row.get("scope") == scope and (causal or instant(row["time"]) <= sent), "trigger_native_prefix_scope_or_time")
            event = row.get("data", {})
            if row.get("stream_mode") == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
                if "input" in event and "result" not in event:
                    active[event["id"]] = (line, event.get("name"))
                elif any(k in event for k in ("result", "error", "interrupts")):
                    active.pop(event["id"], None)
            if row.get("type") == "native_graph_drained":
                active.clear()
        else:
            require(row.get("params", {}).get("sessionId") == scope["session_id"], "trigger_foreign_native_session")
            event = row.get("params", {}).get("event", {})
            if event.get("type") == "turn/start":
                active = {"turn": (line, "turn")}
            elif event.get("type") == "turn/end":
                active.clear()
    require(any(line == trigger["activity_source"]["line"] and name in {"model", "tools", "turn"}
                for line, name in active.values()), "pause_was_not_during_actual_native_activity")


def budget_snapshot(budget, *, allow_empty=False):
    token_hash = sha(regular(budget / "run.token").strip())
    database = budget / "calls.sqlite"
    regular(database)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        binding = db.execute("SELECT id,token_hash,cap FROM budget ORDER BY id").fetchall()
        rows = [list(row) for row in db.execute("SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id").fetchall()]
    require(binding == [(1, token_hash, CAP)], "original_model_budget_binding_mismatch")
    require((bool(rows) or allow_empty) and len(rows) <= CAP and all(row[0] == i for i, row in enumerate(rows, 1)),
            "model_call_reservation_history_invalid")
    require(all(row[3] == "forwarded" and type(row[4]) is int and 200 <= row[4] < 300 for row in rows),
            "inflight_or_unknown_model_result")
    return {"token_sha256": token_hash, "cap": CAP, "count": len(rows),
            "rows_sha256": digest(rows), "rows": rows}


def audit_snapshot(path, scope):
    rows = jsonl(path)
    complete, calls = audited_calls([r for _, _, r in rows], scope)
    require(complete and all(finish.get("status") == 200 for _, finish in calls), "unsafe_or_incomplete_trusted_tool_audit")
    return {"bytes": sum(len(line) for _, line, _ in rows), "lines": len(rows),
            "sha256": sha(b"".join(line for _, line, _ in rows)),
            "completed_write_ids": sorted({start["arguments"]["operation_id"] for start, finish in calls
                                           if start["tool"] == "write" and finish["status"] == 200})}


def closed_snapshot(paths):
    return inventory(paths.state, paths.workspace)


def clean_da_snapshot(value, scope, checkpoint):
    require(isinstance(value, dict), "native_checkpoint_projection_missing")
    config = value.get("config", {}).get("configurable", {})
    tasks = value.get("tasks")
    # 固定 StateSnapshot 投影将 PregelTask NamedTuple 序列化为 JSON 数组：
    # id、name、path、error、interrupts、state、result。
    # JSON 自带元组处理先于序列化器 default 钩子执行。
    def clean_task(task):
        return (isinstance(task, list) and len(task) == 7
            and isinstance(task[0], str) and bool(task[0])
            and isinstance(task[1], str) and task[1] in value.get("next", [])
            and isinstance(task[2], list) and bool(task[2])
            and task[3] is None and task[4] == [])
    require(config.get("thread_id") == scope["session_id"] and config.get("checkpoint_ns", "") == ""
            and config.get("checkpoint_id") == checkpoint and bool(value.get("next"))
            and isinstance(tasks, list) and bool(tasks) and not value.get("interrupts")
            and all(clean_task(task) for task in tasks),
            "native_checkpoint_not_clean_unfinished")


def validate_da_pause(paths, scope, pins, host):
    candidate = paths.run / "attempts/worker-a/candidate"
    index_path = paths.state / "pause-index.json"
    index = read(index_path)
    result = read(candidate / "result.json")
    require(result.get("status") == "paused" and result.get("native_status") == "drained"
            and result.get("stop_reason") == "native_safe_pause" and not result.get("error"), "native_da_pause_result_missing")
    require(index.get("schema") == "harness-lab-pause-index/1" and index.get("candidate") == pins["candidate"]
            and index.get("chain_id") == pins["chain_id"] and index.get("attempt_id") == "worker-a"
            and index.get("scope") == scope and index.get("session_id") == scope["session_id"]
            and index.get("checkpoint_ns") == "", "native_da_index_identity_mismatch")
    binding = read(paths.fixture / "attempts/worker-a/lifecycle.json")
    for key in ("source_export_sha256", "adapter_export_sha256", "runtime_sha256"):
        require(index.get(key) == binding[key], "native_da_runtime_binding_mismatch")
    for field in ("exact_snapshot", "head_snapshot"):
        require(digest(index.get(field)) == index.get(field + "_sha256"), "native_da_snapshot_hash_mismatch")
        clean_da_snapshot(index[field], scope, index.get("checkpoint_id"))
    for field, root, is_state in (("state_files", paths.state, True), ("workspace_files", paths.workspace, False)):
        actual = da_files(root, state=is_state)
        require(index.get(field) == actual and index.get(field + "_sha256") == digest(actual), "native_da_closed_files_mismatch")
    require(all(index.get(key) is True for key in ("saver_closed", "tools_closed", "safe_pause"))
            and all(type(index.get(key)) is int and index[key] == 0 for key in (
                "observed_active_graph_tasks", "observed_active_http_calls", "observed_http_errors_or_unknown_results")),
            "native_da_close_observation_missing")
    require(number(index.get("total_active_seconds")) and index["total_active_seconds"] <= host["active_seconds"] + 0.5,
            "native_da_duration_exceeds_host")
    observation = index.get("native_pause_observation")
    require(isinstance(observation, dict) and observation.get("phase") == "native_drained_observed"
            and observation.get("action") == "pause" and observation.get("reason") == "experiment_pause"
            and observation.get("pause_boundary_observed") is True
            and observation.get("observed_active_tasks") == [] and observation.get("immediate_cancel") is False,
            "native_da_drain_observation_missing")
    native, native_ref = reference(candidate, observation.get("source"))
    request, request_ref = reference(candidate, observation.get("request_source"))
    require(native.get("type") == "native_graph_drained" and native.get("reason") == "experiment_pause"
            and native.get("exception_type", "").endswith(".GraphDrained") and native.get("drain_requested") is True,
            "actual_native_graphdrained_missing")
    require(request.get("phase") == "requested" and request.get("admitted") is True
            and request.get("active_native_task_observed") is True and request.get("signal") == "SIGUSR2",
            "actual_native_drain_request_missing")
    causal = validate_handler_observation(paths.run, pins['candidate'], host['pause_request'], request_ref)
    require((causal or instant(request["time"]) >= instant(host["pause_request"]["sent_at"]))
            and instant(native["time"]) >= instant(request["time"]), "native_pause_order_invalid")
    controls = [row for _, _, row in jsonl(candidate / "control-events.jsonl")]
    require(any(row == observation for row in controls), "native_da_index_not_backed_by_control_log")
    api = [row for row in controls if row.get("phase") == "native_api_returned" and row.get("action") == "pause"]
    require(len(api) == 1 and api[0].get("native_api") == "RunControl.request_drain"
            and api[0].get("drain_reason") == "experiment_pause" and api[0].get("drain_requested") is True,
            "public_native_drain_api_missing")
    api_native, _ = reference(candidate, api[0]["source"])
    require(api_native.get("type") == "native_control_api_returned" and api_native.get("arguments") == {"reason": "experiment_pause"},
            "native_drain_api_source_invalid")
    active = {}
    drained_count = 0
    for _, _, row in jsonl(candidate / "raw-events.jsonl"):
        require(row.get("scope") == scope, "foreign_native_scope")
        event = row.get("data", {})
        if row.get("stream_mode") == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
            if "input" in event and "result" not in event:
                require(event["id"] not in active, "duplicate_native_task_start")
                active[event["id"]] = event
            elif any(key in event for key in ("result", "error", "interrupts")):
                require(event["id"] in active and not event.get("error") and not event.get("interrupts"), "native_task_result_invalid")
                active.pop(event["id"])
        if row.get("type") == "native_graph_drained":
            require(not active and row == native, "native_drain_with_active_or_unmatched_tasks")
            drained_count += 1
    require(not active and drained_count == 1, "native_task_stream_not_quiescent")
    return {"kind": "langgraph-original-checkpoint", "index_sha256": sha(regular(index_path)),
            "index": index, "sources": {"request": request_ref, "native_drained": native_ref}}


def native_dsh(row):
    require(row.get("method") == "session.event" and isinstance(row.get("params"), dict), "native_session_event_required")
    return row["params"].get("event", {})


def expand_dsh_provenance(event):
    """仅复现文档规定的无损 JSONL sourceEventSeqs 编码。"""
    if "sourceEventSeqs" not in event:
        return event
    stored, decoded, had_range = event["sourceEventSeqs"], [], False
    require(isinstance(stored, list), "dsh_native_provenance_array_required")
    for entry in stored:
        if type(entry) is int:
            require(0 <= entry <= 2**53 - 1 and len(decoded) < event["seq"], "dsh_native_provenance_invalid")
            decoded.append(entry)
        else:
            require(isinstance(entry, list) and len(entry) == 2 and all(type(value) is int for value in entry)
                    and 0 <= entry[0] <= entry[1] <= 2**53 - 1
                    and entry[1] - entry[0] + 1 <= event["seq"] - len(decoded), "dsh_native_provenance_range_invalid")
            decoded.extend(range(entry[0], entry[1] + 1))
            had_range = True
    require(not had_range or all(a < b for a, b in zip(decoded, decoded[1:])), "dsh_native_provenance_range_order")
    return {**event, "sourceEventSeqs": decoded}


def dsh_storage(paths, candidate, scope, *, stage="after-flush"):
    """读取此变体配置的原始明文、未打包 JSONL。

    仅返回 header/Goal/end 投影，不打印 Session 内容，也不放入公开报告。
    完整物理哈希仍绑定受保护索引；此检查独立于复制的 JSON 视图。"""
    artifact = read(candidate / ("session-artifact-" + stage + ".json"))
    location = Path(artifact.get("native_location", {}).get("path", ""))
    require(location.is_absolute() and location.is_relative_to("/state") and ".." not in location.parts
            and location.suffix == ".jsonl", "dsh_public_native_location_invalid")
    physical = paths.state / location.relative_to("/state")
    data = regular(physical)
    require(sha(data) == artifact.get("physical_sha256") and artifact.get("scope") == scope,
            "dsh_raw_session_physical_hash_mismatch")
    records = [row for _, _, row in jsonl(physical)]
    require(records and records[0].get("type") == "session" and records[0].get("id") == scope["session_id"]
            and records[0].get("cwd") == "/workspace", "dsh_original_native_header_invalid")
    stored_events = records[1:]
    require(all(type(row.get("seq")) is int and row["seq"] == index for index, row in enumerate(stored_events)),
            "dsh_native_physical_seq_gap_or_packed_storage")
    events = [expand_dsh_provenance(event) for event in stored_events]
    changes = [row for row in events if row.get("type") == "goal/change"]
    ends = [row for row in events if row.get("type") == "turn/end"]
    require(changes and ends, "dsh_native_physical_goal_or_turn_end_missing")
    goal = fold_durable_goal(events)
    require(goal is not None, "dsh_native_current_goal_missing")
    return {"artifact": artifact, "goal": goal,
            "goal_change_events": [{"seq": event["seq"], "data": event["data"]} for event in changes],
            "goal_change": changes[-1], "turn_end": ends[-1], "next_seq": len(events),
            "persisted_events_sha256": digest(events)}


def validate_dsh_pause(paths, scope, pins, host):
    candidate = paths.run / "attempts/worker-a/candidate"
    index = read(candidate / "lifecycle-pause-candidate.json")
    require(index.get("schema") == "harness-lab-goal-pause/1" and index.get("candidate") == pins["candidate"]
            and index.get("chain_id") == pins["chain_id"] and index.get("scope") == scope
            and index.get("session_id") == scope["session_id"], "native_dsh_index_identity_mismatch")
    goal, session = index.get("goal", {}), index.get("session", {})
    original = read(paths.fixture / "candidate-request.json")
    require(goal.get("phase") == "paused" and goal.get("activation") == "disarmed"
            and goal.get("objective") == original["prompt"] and goal.get("maxGoalRounds") == CAP
            and type(goal.get("roundsStarted")) is int and 1 <= goal["roundsStarted"] < CAP
            and type(goal.get("revision")) is int and isinstance(goal.get("id"), str) and goal["id"],
            "native_goal_not_original_paused_unfinished")
    require(session.get("id") == scope["session_id"] and session.get("cwd") == "/workspace"
            and type(session.get("next_seq")) is int, "native_session_identity_invalid")
    source = index.get("source", {})
    request, request_ref = reference(candidate, source.get("pause_request"))
    changed, goal_ref = reference(candidate, source.get("goal_change"))
    ended, end_ref = reference(candidate, source.get("native_turn_end"))
    change, end = native_dsh(changed), native_dsh(ended)
    require(request.get("phase") == "requested" and request.get("native_status") == "running"
            and request.get("scope") == scope and request.get("attempt_id") == "worker-a", "dsh_pause_request_missing")
    require(change.get("type") == "goal/change" and change.get("data", {}).get("operation") == "pause"
            and matches_live_view(goal, change_projection(change["data"]), "disarmed"), "actual_native_goal_pause_missing")
    require(end.get("type") == "turn/end" and end.get("data", {}).get("reason") == {"kind": "blocked"}
            and end["data"].get("turn") == source.get("native_turn") == request.get("turn")
            and end.get("seq") == source.get("native_turn_end_seq")
            and goal_ref["line"] < end_ref["line"], "actual_native_blocked_turn_end_missing")
    causal = validate_handler_observation(paths.run, pins['candidate'], host['pause_request'], request_ref)
    require(causal or instant(request["time"]) >= instant(host["pause_request"]["sent_at"]), "dsh_pause_precedes_signal")
    pause_events = [row for _, _, row in jsonl(candidate / "pause-events.jsonl")]
    require(all(row.get("scope") == scope and row.get("chain_id") == pins["chain_id"] for row in pause_events), "foreign_dsh_pause_scope")
    require(not any(row.get("phase") in {"not_requested", "not_paused", "control_error"} for row in pause_events), "dsh_pause_was_refused")
    accepted = [row for row in pause_events if row.get("phase") == "public_goal_pause_returned"]
    idle = [row for row in pause_events if row.get("phase") == "native_idle_and_flush_observed"]
    close = [row for row in pause_events if row.get("phase") == "native_normal_close_observed"]
    require(len(accepted) == len(idle) == len(close) == 1, "dsh_pause_close_events_missing")
    require(accepted[0].get("native_api") == "goals.pause" and accepted[0].get("original_claimed_count") == 0
            and accepted[0].get("inbox_pending") is False and accepted[0].get("goal") == goal,
            "dsh_pause_not_at_empty_original_claim")
    require(idle[0].get("native_status") == "idle" and idle[0].get("native_inbox_pending") is False
            and idle[0].get("in_flight_models") == idle[0].get("in_flight_tools") == 0
            and source.get("flush") == "await_live_sessions_flush_after_native_turn_end_and_whenIdle"
            and source.get("close") == "handle_dispose_private_scopes_and_runtime_shutdown_all_resolved", "dsh_not_idle_flushed_and_closed")
    persisted = dsh_storage(paths, candidate, scope)
    require(persisted["artifact"]["physical_sha256"] == session.get("artifact_sha256")
            and matches_live_view(goal, persisted["goal"], "disarmed") and persisted["next_seq"] == session["next_seq"]
            and persisted["persisted_events_sha256"] == session.get("persisted_events_sha256")
            and persisted["goal_change"]["data"] == change["data"]
            and persisted["goal_change"]["seq"] == change["seq"]
            and persisted["turn_end"]["data"] == end["data"] and persisted["turn_end"]["seq"] == end["seq"],
            "dsh_paused_projection_not_original_physical_goal_and_turn")
    require(read(candidate / "native-state-index-after-close.json") == closed_snapshot(paths), "dsh_closed_inventory_mismatch")
    result = read(candidate / "result.json")
    require(result.get("status") == "paused" and result.get("pause_confirmed") is False
            and result.get("stop_reason") == "blocked" and not result.get("error"), "dsh_normal_pause_result_missing")
    events = [native_dsh(row) for _, _, row in jsonl(candidate / "raw-events.jsonl") if row.get("method") == "session.event"]
    endings = [event for event in events if event.get("type") == "turn/end"]
    require(endings and endings[-1].get("seq") == end.get("seq")
            and not any(event.get("seq", -1) > end.get("seq", -1)
                        and event.get("type") == "turn/start" for event in events),
            "dsh_native_work_after_pause_boundary")
    return {"kind": "dsh-original-session-goal", "index_sha256": sha(regular(candidate / "lifecycle-pause-candidate.json")),
            "index": index, "sources": {"request": request_ref, "goal_change": goal_ref, "turn_end": end_ref}}


def prepare(args, config, paths, scope):
    detailed.prepare(argparse.Namespace(candidate=args.candidate, run_id=args.chain_id, case="tools",
                                       experiment_id="pause-recovery", seed=args.seed, date=args.date))
    (paths.run / "trusted").mkdir()
    for attempt in ("worker-a", "worker-b"):
        (paths.run / "attempts" / attempt / "candidate").mkdir(parents=True)
        (paths.fixture / "attempts" / attempt).mkdir(parents=True)
    create(paths.run / "chain.json", {"schema": "harness-lab-pause-chain/1", "specialty": "pause-recovery",
            "candidate": args.candidate, "chain_id": args.chain_id, "scope": scope, "seed": args.seed,
            "date": args.date, "input_sha256": sha(regular(paths.run / "input.json")),
            "original_request_sha256": sha(regular(paths.fixture / "candidate-request.json"))})


def trusted_tools(paths):
    import httpx
    token = regular(paths.fixture / "scope.token").decode().strip()
    with httpx.Client(timeout=10, trust_env=False) as client:
        reply = client.get("http://tools:8081/tools", headers={"Authorization": "Bearer " + token})
        reply.raise_for_status()
        value = decode(reply.content)
    require(isinstance(value, dict) and set(value) == {"tools"} and isinstance(value["tools"], list), "trusted_tool_surface_invalid")
    return value["tools"]


def da_runtime(paths, pins, request):
    data = regular(HELPER)
    require(sha(data) == pins.get("adapter_helper_sha256"), "image_bundled_helper_hash_mismatch")
    spec = importlib.util.spec_from_file_location("_i22_pinned_stdlib_identity", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper.runtime_identity_sha256(model_name=pins["model_name"], max_output_tokens=request["max_output_tokens"],
        max_retries=request["max_retries"], candidate_options=request["candidate_options"],
        tool_definitions=decode(regular(paths.run / "trusted/http-tool-surface.json"))['tools'])


def dsh_contract(paths, pins, scope):
    path = paths.run / "trusted/tool-surface.json"
    require(sha(regular(path)) == pins.get("tool_surface_sha256"), "frozen_native_surface_hash_mismatch")
    surface = read(path)
    host = read(paths.run / "trusted/tool-surface-host.json")
    require(host.get("schema") == "harness-lab-goal-surface-host/1"
            and host.get("candidate_image_id") == pins["candidate_image_id"]
            and host.get("source_export_sha256") == pins["source_export_sha256"]
            and host.get("adapter_export_sha256") == pins["adapter_export_sha256"]
            and host.get("surface_sha256") == sha(regular(path))
            and host.get("process_exit") == 0 and host.get("oom_killed") is False
            and host.get("model_call_count_before") == host.get("model_call_count_after") == 0
            and host.get("real_model_token_mounted") is False, "native_surface_not_independently_calibrated")
    require(surface.get("schema") == "harness-lab-goal-tool-surface/1" and surface.get("scope") == scope
            and surface.get("declared_image_id") == pins["candidate_image_id"]
            and surface.get("model_dispatch_attempts") == 0, "native_surface_calibration_identity_mismatch")
    for name in ("source_export_sha256", "adapter_export_sha256"):
        require(surface.get("provenance", {}).get(name) == pins[name], "native_surface_material_mismatch")
    schemas = surface.get("schemas")
    require(isinstance(schemas, list) and schemas and all(isinstance(row, dict)
            and set(row) == {"name", "description", "parameters"} and isinstance(row["name"], str)
            and isinstance(row["description"], str) and isinstance(row["parameters"], dict) for row in schemas),
            "native_surface_schema_shape_invalid")
    require(len({row["name"] for row in schemas}) == len(schemas), "native_tool_name_collision")
    contract = digest(sorted(schemas, key=lambda row: row["name"]))
    require(surface.get("contract_sha256") == contract, "native_surface_contract_mismatch")
    return contract


def bind(paths, scope, pins, attempt, *, used, native=None):
    original = read(paths.fixture / "candidate-request.json")
    require(sha(regular(paths.fixture / "candidate-request.json")) == read(paths.run / "chain.json")["original_request_sha256"],
            "original_candidate_request_changed")
    request = dict(original)
    mode = "new" if attempt == "worker-a" else "resume"
    remaining = math.floor(LIMIT - used)
    require(remaining >= 1, "cumulative_active_budget_exhausted")
    request["timeout_seconds"] = remaining
    request["lifecycle"] = {"mode": mode, "chain_id": pins["chain_id"], "attempt_id": attempt,
                            "binding_file": "/config/lifecycle.json"}
    if mode == "resume":
        request.pop("prompt")
    base = {"schema": "harness-lab-lifecycle/1", "candidate": pins["candidate"], "mode": mode,
            "chain_id": pins["chain_id"], "attempt_id": attempt, "scope": scope, "session_id": scope["session_id"]}
    target = paths.fixture / "attempts" / attempt
    if pins["candidate"] == "deepagents_langgraph":
        recovery = None if native is None else {"attempt_id": "worker-a", "pause_index_sha256": native["index_sha256"],
            **{key: native["index"][key] for key in ("checkpoint_id", "exact_snapshot_sha256", "head_snapshot_sha256",
                                                    "state_files_sha256", "workspace_files_sha256")}}
        binding = {**base, "checkpoint_ns": "", "source_export_sha256": pins["source_export_sha256"],
            "adapter_export_sha256": pins["adapter_export_sha256"], "runtime_sha256": da_runtime(paths, pins, original),
            "budget": {"active_limit_seconds": LIMIT, "used_active_seconds": used, "remaining_seconds": remaining},
            "recovery": recovery}
    else:
        state_index = closed_snapshot(paths)
        create(target / "lifecycle-state.json", state_index)
        config = {"provider": "deepseek-official", "base_url": "http://model-proxy:8082/v1",
                  "model": pins["model_name"], "max_output_tokens": original["max_output_tokens"],
                  "max_retries": original["max_retries"], "thinking": "disabled", "reasoning_effort": "off"}
        tool_config = {"url": original["tools_url"], "scope": scope,
                       "token_sha256": sha(regular(paths.fixture / "scope.token").strip())}
        binding = {**base, "variant": "goal-runtime", "materials": {key: pins[key] for key in ("source_export_sha256", "adapter_export_sha256")},
            "image": {"original_image_id": pins["candidate_image_id"]},
            "model": {"config_sha256": digest(config), "run_token_sha256": sha(regular(paths.budget / "run.token").strip()),
                      "budget_id": pins["chain_id"], "max_model_calls": CAP},
            "tools": {"config_sha256": digest(tool_config), "contract_sha256": dsh_contract(paths, pins, scope)},
            "state": {"index_file": "/config/lifecycle-state.json", "index_sha256": sha(regular(target / "lifecycle-state.json"))},
            "goal": {"max_goal_rounds": CAP}, "active_budget": {"limit_seconds": LIMIT, "used_seconds": used, "remaining_seconds": remaining}}
        if native is not None:
            create(target / "lifecycle-pause.json", native["index"])
            binding["resume"] = {"pause_index_file": "/config/lifecycle-pause.json",
                                 "pause_index_sha256": sha(regular(target / "lifecycle-pause.json"))}
    create(target / "lifecycle.json", binding)
    create(target / "request.json", request)
    return {"attempt_id": attempt, "remaining_seconds": remaining,
            "request_sha256": sha(regular(target / "request.json")), "binding_sha256": sha(regular(target / "lifecycle.json")),
            "scope_sha256": sha(regular(paths.fixture / "scope.json")),
            "scope_token_sha256": sha(regular(paths.fixture / "scope.token").strip()),
            "model_token_sha256": sha(regular(paths.budget / "run.token").strip())}


def control_integrity(paths, attempt):
    acceptance = read(paths.run / ("start-binding.json" if attempt == "worker-a" else "resume-binding.json"))
    target = paths.fixture / "attempts" / attempt
    require(acceptance.get("attempt_id") == attempt
            and acceptance.get("request_sha256") == sha(regular(target / "request.json"))
            and acceptance.get("binding_sha256") == sha(regular(target / "lifecycle.json"))
            and acceptance.get("scope_sha256") == sha(regular(paths.fixture / "scope.json"))
            and acceptance.get("scope_token_sha256") == sha(regular(paths.fixture / "scope.token").strip())
            and acceptance.get("model_token_sha256") == sha(regular(paths.budget / "run.token").strip()),
            "protected_original_controls_or_tokens_changed")
    return acceptance


def bind_start(args, config, paths, scope):
    pins = pinned(args, paths, scope)
    require(not list(paths.state.iterdir()), "original_state_not_empty")
    require(not list((paths.run / "attempts/worker-a/candidate").iterdir()), "attempt_a_output_not_empty")
    create(paths.run / "trusted/http-tool-surface.json", {"tools": trusted_tools(paths)})
    require(budget_snapshot(paths.budget, allow_empty=True)["count"] == 0, "model_calls_before_original_task")
    binding = bind(paths, scope, pins, "worker-a", used=0)
    create(paths.run / "start-binding.json", {"schema": "harness-lab-start-binding/1", "pins_sha256": sha(regular(paths.run / "trusted/pins.json")), **binding})


def check_paused(args, config, paths, scope):
    pins = pinned(args, paths, scope)
    require(pins["variant"] == "requested-pause", "pause_action_in_uninterrupted_chain")
    host = host_record(args, config, paths, scope, pins, "worker-a", paused=True)
    control_integrity(paths, "worker-a")
    native = (validate_da_pause if args.candidate == "deepagents_langgraph" else validate_dsh_pause)(paths, scope, pins, host)
    audit = audit_snapshot(paths.run / "tool-events.jsonl", scope)
    require(audit["completed_write_ids"], "no_business_write_before_pause")
    budget = budget_snapshot(paths.budget)
    ledger = ledger_request(paths, "GET", "/admin/ledger", params={"experiment_id": "pause-recovery", "variant": args.chain_id})
    require(scoped_ledger(ledger, scope) and ledger["write_count"] >= 1, "pause_ledger_invalid")
    create(paths.run / "trusted/pause-ledger.json", ledger)
    create(paths.run / "pause-validated.json", {"schema": "harness-lab-pause-validated/1", "passed": True,
        "candidate": args.candidate, "chain_id": args.chain_id, "scope": scope, "validated_at": now(),
        "pins_sha256": sha(regular(paths.run / "trusted/pins.json")),
        "host_a_sha256": sha(regular(paths.run / "trusted/worker-a.json")), "native": native,
        "closed_inventory": closed_snapshot(paths), "budget": budget, "audit": audit,
        "ledger_sha256": sha(regular(paths.run / "trusted/pause-ledger.json")), "used_active_seconds": host["active_seconds"]})


def monitoring_projection(paths, scope):
    budget = budget_snapshot(paths.budget)
    audit = audit_snapshot(paths.run / "tool-events.jsonl", scope)
    return {"model_calls": budget["count"], "model_rows_sha256": budget["rows_sha256"],
            "model_token_sha256": budget["token_sha256"], "model_cap": budget["cap"],
            "tool_lines": audit["lines"], "tool_bytes": audit["bytes"], "tool_sha256": audit["sha256"]}


def wait_monitor(args, config, paths, scope):
    # 此动作特意不需要 Fixture 或原生状态挂载。
    paused = read(paths.run / "pause-validated.json")
    require(paused.get("passed") is True and paused.get("candidate") == args.candidate
            and paused.get("chain_id") == args.chain_id and paused.get("scope") == scope, "unvalidated_pause")
    baseline = {"model_calls": paused["budget"]["count"], "model_rows_sha256": paused["budget"]["rows_sha256"],
                "model_token_sha256": paused["budget"]["token_sha256"], "model_cap": CAP,
                "tool_lines": paused["audit"]["lines"], "tool_bytes": paused["audit"]["bytes"], "tool_sha256": paused["audit"]["sha256"]}
    target = paths.run / "wait-budget-samples.jsonl"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    started, tick, count, last_elapsed = now(), time.monotonic(), 0, None
    failure = None
    with os.fdopen(descriptor, "wb") as stream:
        try:
            while True:
                elapsed = time.monotonic() - tick
                require(last_elapsed is None or elapsed - last_elapsed <= 5, "monitor_sampling_gap")
                projection = monitoring_projection(paths, scope)
                require(projection == baseline, "paused_budget_or_tool_activity_changed")
                row = {"at": now(), "elapsed_seconds": elapsed, **projection}
                stream.write(canonical(row) + b"\n")
                stream.flush()
                count += 1
                last_elapsed = elapsed
                if elapsed >= WAIT:
                    break
                time.sleep(min(2, WAIT - elapsed))
        except Exception as error:
            failure = type(error).__name__ + ":" + str(error)
        finally:
            os.fsync(stream.fileno())
    summary = {"schema": "harness-lab-pause-wait-budget/1", "candidate": args.candidate,
               "chain_id": args.chain_id, "started_at": started, "finished_at": now(),
               "elapsed_seconds": time.monotonic() - tick, "samples": count,
               "samples_sha256": sha(regular(target)), "baseline": baseline, "passed": failure is None, "error": failure}
    create(paths.run / "wait-budget-summary.json", summary)
    require(summary["passed"], "pause_wait_monitor_failed")


def validate_proxy_replacement(row, image, budget_source):
    before, stopped, created, after = (row.get(key) for key in ('before', 'old_after_stop', 'replacement_created', 'replacement'))
    require(all(isinstance(value, dict) for value in (before, stopped, created, after)), 'proxy_snapshots_missing')
    expected_mounts = [{'Source': budget_source, 'Destination': '/budget', 'RW': True}]
    for value in (before, stopped, created, after):
        require(value.get('image_id') == image and value.get('mounts') == expected_mounts
                and value.get('memory_limit') == 256 * 1024**2 and value.get('cpu_nano') == 250000000,
                'proxy_image_budget_mount_or_caps_changed')
    require(before.get('id') == stopped.get('id') == row.get('before_id')
            and created.get('id') == after.get('id') == row.get('after_id')
            and row['before_id'] != row['after_id'], 'proxy_snapshot_identity_changed')
    state = stopped.get('state', {})
    require(before.get('state', {}).get('Running') is True and created.get('state', {}).get('Status') == 'created'
            and after.get('state', {}).get('Running') is True and after.get('name') == '/harness-lab-model-proxy-1'
            and state.get('Status') == 'exited' and state.get('Pid') == 0 and state.get('Running') is False
            and state.get('OOMKilled') is False and state.get('Error') == '' and state.get('ExitCode') in (0, 137, 143)
            and row.get('old_id_absent') is True, 'proxy_replacement_lifecycle_invalid')
    command = ['-m', 'execution.model_proxy', '--database', '/budget/calls.sqlite',
               '--run-token-file', '/budget/run.token', '--cap', '20', '--port', '8082']
    require(HEX.fullmatch(str(row.get('environment_sha256', '')))
            and row.get('environment_sha256') == row.get('replacement_environment_sha256')
            and row.get('command') == row.get('replacement_command') == command,
            'proxy_environment_or_budget_command_changed')
    require(number(row.get('sampling_lock_seconds')) and row['sampling_lock_seconds'] <= 5
            and row.get('transition') == 'replacement_ready_before_old_proxy_removed; same_budget_no_worker',
            'proxy_transition_observation_invalid')


def validate_wait(paths, pins, paused):
    host = read(paths.run / "trusted/wait.json")
    require(host.get("schema") == "harness-lab-pause-wait-host/1" and host.get("candidate") == pins["candidate"]
            and host.get("chain_id") == pins["chain_id"] and host.get("errors") == [], "wait_host_identity_or_error")
    a = read(paths.run / "trusted/worker-a.json")
    require(host.get("worker_a_deleted") == {"id": a["after_exit"]["id"], "id_absent": True, "name_absent": True}, "wait_old_worker_not_absent")
    require(instant(host["started_at"]) >= instant(a["deleted"]["observed_at"])
            and instant(host["finished_at"]) - instant(host["started_at"]) >= WAIT, "wait_interval_too_short")
    require(host.get("resources_file") == "trusted/wait-resource-samples.jsonl"
            and host.get("budget_samples_file") == "wait-budget-samples.jsonl"
            and host.get("budget_summary_file") == "wait-budget-summary.json", "wait_path_substitution")
    resource_rows = [row for _, _, row in jsonl(paths.run / host["resources_file"])]
    require(len(resource_rows) >= 121, "wait_resource_samples_missing")
    last, start = None, resource_rows[0]["elapsed_seconds"]
    for row in resource_rows:
        elapsed, containers = row.get("elapsed_seconds"), row.get("containers")
        require(number(elapsed) and (last is None or 0 < elapsed - last <= 5), "wait_resource_sampling_gap")
        require(row.get("error") is None and row.get("worker_ids") == [] and isinstance(containers, list) and containers,
                "worker_or_sampling_error_during_wait")
        require(len({c.get("id") for c in containers}) == len(containers)
                and all(isinstance(c, dict) and HEX.fullmatch(str(c.get("id", "")))
                        and c.get("role") in {"bench", "shared_service"}
                        and type(c.get("memory_limit")) is int and c["memory_limit"] > 0
                        and type(c.get("cpu_nano")) is int and c["cpu_nano"] > 0
                        and all(key in c for key in ("CPUPerc", "MemUsage", "MemPerc", "PIDs")) for c in containers),
                "wait_container_caps_or_stats_missing")
        require(sum(c["memory_limit"] for c in containers) <= MEMORY and sum(c["cpu_nano"] for c in containers) <= CPU,
                "wait_total_resource_cap_exceeded")
        last = elapsed
    require(last - start >= WAIT, "wait_resource_samples_do_not_cover_600_seconds")
    summary = read(paths.run / "wait-budget-summary.json")
    require(summary.get("schema") == "harness-lab-pause-wait-budget/1" and summary.get("passed") is True
            and summary.get("candidate") == pins["candidate"] and summary.get("chain_id") == pins["chain_id"]
            and number(summary.get("elapsed_seconds"), minimum=WAIT)
            and summary.get("samples_sha256") == sha(regular(paths.run / "wait-budget-samples.jsonl")), "wait_budget_monitor_invalid")
    monitor_rows = [row for _, _, row in jsonl(paths.run / "wait-budget-samples.jsonl")]
    require(len(monitor_rows) == summary.get("samples") and len(monitor_rows) >= 121, "wait_budget_sample_count_mismatch")
    previous = None
    for row in monitor_rows:
        elapsed = row.get("elapsed_seconds")
        require(number(elapsed) and (previous is None or 0 < elapsed - previous <= 5), "wait_budget_sampling_gap")
        require({k: row.get(k) for k in summary["baseline"]} == summary["baseline"], "wait_budget_activity_changed")
        previous = elapsed
    require(monitor_rows[-1]["elapsed_seconds"] - monitor_rows[0]["elapsed_seconds"] >= WAIT - 0.05,
            "wait_budget_samples_do_not_cover_interval")
    expected = {"model_calls": paused["budget"]["count"], "model_rows_sha256": paused["budget"]["rows_sha256"],
                "model_token_sha256": paused["budget"]["token_sha256"], "model_cap": CAP,
                "tool_lines": paused["audit"]["lines"], "tool_bytes": paused["audit"]["bytes"], "tool_sha256": paused["audit"]["sha256"]}
    require(summary["baseline"] == expected, "wait_baseline_not_original_pause")
    require(instant(summary["started_at"]) >= instant(host["started_at"])
            and instant(summary["finished_at"]) <= instant(host["finished_at"]), "monitor_outside_host_wait_interval")
    require(isinstance(host.get("proxy_restarts"), list), "restart_observation_missing")
    for row in host["proxy_restarts"]:
        require(HEX.fullmatch(str(row.get("before_id", ""))) and HEX.fullmatch(str(row.get("after_id", "")))
                and row["before_id"] != row["after_id"]
                and instant(host["started_at"]) <= instant(row["at"]) <= instant(host["finished_at"]), "proxy_restart_identity_invalid")
        token_destination = '/config/model-run.token' if pins['candidate'] == 'deepagents_langgraph' else '/budget/run.token'
        token_mount = next(m for m in a['before_start']['mounts'] if m['Destination'] == token_destination)
        image = read(paths.run / 'trusted/service-images.json')['model-proxy']
        validate_proxy_replacement(row, image, str(Path(token_mount['Source']).parent))
    return host


def bind_resume(args, config, paths, scope):
    pins = pinned(args, paths, scope)
    require(pins["variant"] == "requested-pause", "resume_action_in_uninterrupted_chain")
    paused = read(paths.run / "pause-validated.json")
    require(paused.get("passed") is True and paused.get("pins_sha256") == sha(regular(paths.run / "trusted/pins.json"))
            and paused.get("host_a_sha256") == sha(regular(paths.run / "trusted/worker-a.json")), "accepted_pause_evidence_changed")
    validate_wait(paths, pins, paused)
    control_integrity(paths, "worker-a")
    require(closed_snapshot(paths) == paused["closed_inventory"], "native_state_or_workspace_changed_during_wait")
    require(budget_snapshot(paths.budget) == paused["budget"]
            and audit_snapshot(paths.run / "tool-events.jsonl", scope) == paused["audit"], "budget_or_tool_activity_changed_before_resume")
    require(not list((paths.run / "attempts/worker-b/candidate").iterdir()), "attempt_b_output_not_empty")
    control = bind(paths, scope, pins, "worker-b", used=paused["used_active_seconds"], native=paused["native"])
    create(paths.run / "resume-binding.json", {"schema": "harness-lab-resume-binding/1", "candidate": args.candidate,
        "chain_id": args.chain_id, "scope": scope, "new_prompt_submitted": False,
        "pause_acceptance_sha256": sha(regular(paths.run / "pause-validated.json")), **control})


def clean_da_final_snapshot(snapshot):
    # 完成的固定 StateSnapshot 不应再有 next 任务。PregelTask
    # 序列化为七元素数组，绝不能当成字典；
    # 任何残留任务都算未完成，即使其 error 字段为空。
    require(isinstance(snapshot, dict) and snapshot.get("next") == []
            and snapshot.get("interrupts") == [] and snapshot.get("tasks") == [],
            "native_da_final_checkpoint_unfinished")


def restored_dsh_header(original, restored):
    # 固定 session-persistence-jsonl/src/format.ts 的 toHeaderLine 写入
    # delegationDepth ?? 0，fromHeaderLine 读取这个具体数值。
    # 其他所有头字段必须相同。比较内存中的 A 视图
    # 与 B 经官方持久化后端重新加载的会话时，不能忽略头信息，
    # 也不能接受任意元数据变化。
    require(isinstance(original, dict) and isinstance(restored, dict),
            "native_dsh_restored_header_invalid")
    depth = original.get("delegationDepth", 0)
    require(type(depth) is int and 0 <= depth <= 2**53 - 1
            and digest(restored) == digest({**original, "delegationDepth": depth}),
            "native_dsh_restored_header_invalid")
    return True


def final_native(paths, scope, pins, attempt, paused=None):
    directory = paths.run / "attempts" / attempt / "candidate"
    result = read(directory / "result.json")
    native_detail = {}
    require(result.get("status") == "completed" and result.get("native_status") == "completed"
            and not result.get("error") and not result.get("error_message"), "original_task_not_natively_completed")
    if pins["candidate"] == "deepagents_langgraph":
        terminal = detailed.native_terminal(replace(paths, run=directory.parent))
        require(terminal["normal_end"], "native_da_terminal_not_normal")
        snapshot = read(directory / "state-snapshot.json")
        clean_da_final_snapshot(snapshot)
        if paused is not None:
            resumed = read(directory / "native-resume-binding.json")
            require(resumed.get("scope") == scope and resumed.get("chain_id") == pins["chain_id"]
                    and resumed.get("attempt_id") == "worker-b" and resumed.get("public_api") == "graph.aget_state"
                    and resumed.get("checkpoint_id") == paused["native"]["index"]["checkpoint_id"]
                    and resumed.get("exact_and_head_verified") is True and resumed.get("execution_input") is None
                    and resumed.get("execution_checkpoint_id_present") is False, "original_native_checkpoint_not_resumed")
    else:
        raw = [native_dsh(row) for _, _, row in jsonl(directory / "raw-events.jsonl") if row.get("method") == "session.event"]
        endings = [event for event in raw if event.get("type") == "turn/end"]
        require(endings and endings[-1].get("data", {}).get("reason") == {"kind": "completed"},
                "native_dsh_completed_turn_end_missing")
        changes = [row for row in raw if row.get("type") == "goal/change"]
        persisted = dsh_storage(paths, directory, scope)
        goal = persisted["goal"]
        require(goal.get("phase") == "complete" and goal.get("maxGoalRounds") == CAP
                and type(goal.get("roundsStarted")) is int and 1 <= goal["roundsStarted"] <= CAP
                and persisted["goal_change"]["data"]["operation"] == "complete", "native_original_goal_not_complete")
        # Worker B 只发出运行中事件。先重放完整物理会话，
        # 再绑定运行中后缀；不能把 B 后缀当成全新 Goal 折叠。
        require(changes and {"seq": changes[-1].get("seq"), "data": changes[-1].get("data")}
                == persisted["goal_change_events"][-1]
                and all({"seq": event.get("seq"), "data": event.get("data")} in persisted["goal_change_events"] for event in changes)
                and persisted["turn_end"]["data"] == endings[-1]["data"]
                and persisted["turn_end"]["seq"] == endings[-1]["seq"], "final_goal_not_original_physical_session")
        # 激活状态绝不存入 goal/change 或 foldGoal。公共
        # 运行中视图必须与持久化折叠相符；官方 goals.complete
        # 会解除该运行时的激活。这是有源码关联的运行中证据，
        # 不表示持久化 phase 能重建进程激活状态。
        outcome = result.get("native_result", {}).get("lifecycleOutcome", {})
        require(outcome.get("kind") == "completed" and matches_live_view(outcome.get("goal"), goal, "disarmed"),
                "native_live_goal_view_not_complete_disarmed")
        native_detail = {"goal_evidence": {"durable": "independent_physical_session_strict_fold",
            "activation": "public_live_view_and_pinned_goals.complete_disarm_semantics", "activation_persisted": False}}
        require(goal.get("objective") == read(paths.fixture / "candidate-request.json")["prompt"], "goal_objective_changed")
        if paused is not None:
            original = paused["native"]["index"]
            require(goal.get("id") == original["goal"]["id"], "new_goal_substituted_for_original")
            before = read(directory / "session-artifact-before-resume.json")
            require(before.get("physical_sha256") == original["session"]["artifact_sha256"], "original_raw_session_not_restored")
            restored = read(directory / "session-before-followup.json")
            original_view = read(paths.run / "attempts/worker-a/candidate/session-after-idle.json")
            restored_dsh_header(original_view.get("header"), restored.get("header"))
            restored_events = restored.get("events", [])
            # 原生 Session 构造器接受恢复种子时，可合法追加一个
            # 标记；它不是重放的用户输入。
            require(restored.get("first_live_seq") == original["session"]["next_seq"]
                    and restored.get("next_seq") == original["session"]["next_seq"] + 1
                    and restored_events[:-1] == original_view.get("events") and restored_events
                    and digest(restored_events[:-1]) == original["session"]["persisted_events_sha256"]
                    and restored_events[-1].get("type") == "session/end-seed"
                    and restored_events[-1].get("seq") == original["session"]["next_seq"]
                    and restored_events[-1].get("data") == {}, "native_restored_seed_prefix_or_marker_invalid")
            events = [row for _, _, row in jsonl(directory / "pause-events.jsonl")]
            resumes = [row for row in events if row.get("phase") == "native_goal_resume_returned"]
            require(len(resumes) == 1 and resumes[0].get("native_api") == "goals.resume"
                    and resumes[0].get("new_prompt_submitted") is False and resumes[0].get("new_followup_submitted") is False
                    and resumes[0].get("goal", {}).get("id") == original["goal"]["id"]
                    and not any(row.get("phase") == "native_goal_create_returned" for row in events),
                    "original_goal_not_resumed_without_new_input")
    return {"status": result["status"], "native_status": result["native_status"], "stop_reason": result.get("stop_reason"), **native_detail}


def grade(args, config, paths, scope, *, review_output=None):
    """保留失败；异常转为失败判定，不能变成新任务。"""
    output = paths.run
    if review_output is not None:
        output = Path(review_output)
        require(output.is_relative_to(paths.run / "reviews") and output.is_dir()
                and not output.is_symlink() and not list(output.iterdir()), "fresh_review_output_required")
    checks, metrics = [], {}
    def check(name, callback):
        try:
            value = callback()
            checks.append({"name": name, "passed": True, "detail": "verified"})
            return value
        except Exception as error:
            checks.append({"name": name, "passed": False, "detail": type(error).__name__ + ":" + str(error)[:160]})
            return None
    pins = check("frozen_chain_identity", lambda: pinned(args, paths, scope))
    if pins is not None:
        pause = pins["variant"] == "requested-pause"
        a = check("worker_a_original_process", lambda: host_record(args, config, paths, scope, pins, "worker-a", paused=pause))
        check("worker_a_protected_controls", lambda: control_integrity(paths, "worker-a"))
        paused = check("accepted_original_pause", lambda: read(paths.run / "pause-validated.json")) if pause else None
        if pause and paused is not None:
            check("pause_acceptance_binding", lambda: require(paused.get("passed") is True
                and paused.get("pins_sha256") == sha(regular(paths.run / "trusted/pins.json"))
                and paused.get("host_a_sha256") == sha(regular(paths.run / "trusted/worker-a.json")), "pause_acceptance_binding_changed"))
            check("worker_free_600_second_wait", lambda: validate_wait(paths, pins, paused))
            b = check("replacement_worker_process", lambda: host_record(args, config, paths, scope, pins, "worker-b", paused=False))
            check("worker_b_protected_controls", lambda: control_integrity(paths, "worker-b"))
            if a is not None and b is not None:
                check("different_worker_same_image_cumulative_active_budget", lambda: require(
                    a["before_start"]["id"] != b["before_start"]["id"] and a["active_seconds"] + b["active_seconds"] <= LIMIT,
                    "worker_replacement_or_cumulative_active_budget_invalid"))
                metrics["cumulative_active_seconds"] = a["active_seconds"] + b["active_seconds"]
        attempt = "worker-b" if pause else "worker-a"
        request = check("original_resume_control", lambda: read(paths.fixture / "attempts" / attempt / "request.json"))
        if pause and request is not None:
            check("no_reissued_prompt_or_followup", lambda: require("prompt" not in request
                and request.get("lifecycle", {}).get("mode") == "resume"
                and "followup" not in request.get("candidate_options", {}), "resume_new_input_forbidden"))
        check("original_task_native_completion", lambda: final_native(paths, scope, pins, attempt, paused))
        final_budget = check("same_cumulative_model_budget", lambda: budget_snapshot(paths.budget))
        if final_budget is not None:
            metrics["reserved_model_calls"] = final_budget["count"]
            if pause and paused is not None:
                check("original_model_reservation_prefix", lambda: require(
                    final_budget["token_sha256"] == paused["budget"]["token_sha256"]
                    and final_budget["rows"][:paused["budget"]["count"]] == paused["budget"]["rows"], "model_budget_reset_or_prefix_changed"))
        audit = check("trusted_tools_final_audit", lambda: audit_snapshot(paths.run / "tool-events.jsonl", scope))
        if pause and paused is not None and audit is not None:
            def no_replay():
                data = regular(paths.run / "tool-events.jsonl")
                saved = paused["audit"]
                require(sha(data[:saved["bytes"]]) == saved["sha256"], "trusted_tool_prefix_changed")
                tail = [decode(line) for line in data[saved["bytes"]:].splitlines()]
                require(not any(row.get("tool") == "write" and row.get("phase") == "start"
                    and row.get("arguments", {}).get("operation_id") in saved["completed_write_ids"] for row in tail),
                    "completed_business_write_reissued_after_resume")
            check("completed_write_not_reissued_even_if_idempotent", no_replay)
        def business():
            if review_output is None:
                ledger = ledger_request(paths, "GET", "/admin/ledger", params={"experiment_id": "pause-recovery", "variant": args.chain_id})
                create(paths.run / "trusted/final-ledger.json", ledger)
            else:
                # 离线复核只读取冻结的原始账本，不能
                # 联系服务、修改原始证据或执行任务。
                ledger = read(paths.run / "trusted/final-ledger.json")
            rows = [row for _, _, row in jsonl(paths.run / "tool-events.jsonl")]
            task, _ = detailed.task_definition("tools")
            result = grade_tools(args.seed, scope, paths.workspace, ledger, rows, task["workspace_inputs"]["request.json"])
            create(output / "tools-verdict.json", result)
            require(result["passed"], "independent_tools_business_grade_failed")
            metrics.update(result.get("metrics", {}))
            return result
        check("fixed_tools_independent_business_result", business)
    verdict = {"schema": "harness-lab-pause-verdict/1", "candidate": args.candidate,
               "chain_id": args.chain_id, "seed": args.seed, "passed": bool(checks) and all(row["passed"] for row in checks),
               "checks": checks, "metrics": metrics,
               "scope_of_claim": "仅覆盖一条本地 Docker 链；三种子比较及共享 Worker 成本须单独评估。"}
    create(output / "verdict.json", verdict)
    return verdict["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--candidate", choices=CANDIDATES, required=True)
    parser.add_argument("--chain-id", type=identifier, required=True)
    parser.add_argument("--seed", type=int, choices=(101, 202, 303, 404, 505), required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--host-record", type=Path)
    args = parser.parse_args()
    env = environment()
    require(env["model_credentials_injected"] is False, "bench_must_not_receive_model_configuration_or_credentials")
    config, paths, scope = context(args)
    expected_host = {"bind-start": "pins.json", "check-paused": "worker-a.json",
                     "bind-resume": "wait.json"}.get(args.action)
    if args.action == "grade":
        variant = read(paths.run / "trusted/pins.json").get("variant")
        expected_host = "worker-b.json" if variant == "requested-pause" else "worker-a.json"
    if args.host_record is not None:
        require(expected_host is not None and args.host_record == paths.run / "trusted" / expected_host,
                "host_record_path_substitution")
    passed = True
    if args.action == "grade":
        passed = grade(args, config, paths, scope)
    else:
        globals()[args.action.replace("-", "_")](args, config, paths, scope)
    print(json.dumps({"action": args.action, "candidate": args.candidate, "chain_id": args.chain_id,
                      "passed": passed, "output": str(paths.run)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
