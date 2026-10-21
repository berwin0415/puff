"""I25 DA cost-flow preparation/observation/grading, only inside pinned Docker.

This is trusted host orchestration, not a product authentication experiment.
The measured observer occupies the reserved 256 MiB / .25 CPU backend slot;
it is not a simulated product backend and cannot establish I24 isolation.
No failed business task is replayed. The fault phase deliberately leaves A's
original store and budget untouched for independent recovery investigation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from runner.instance_cost import (CAPS, CPU, MEMORY, canonical, exclusive, make_plan,
    prepare_idle_materials, read, sha, verify_native_projection, rotation_users, INACTIVE_WAKE_KIND)
from runner.metrics import summarize
from runner.resource_budget import allocation_verdict


SCHEMA = "harness-lab-instance-cost-flow/1"
KINDS = ("baseline", "active", "wake", "fault", INACTIVE_WAKE_KIND)
CONTROL_NAME = re.compile(r"control-([0-9]{12})\.json\Z")
PROBE_NONCE = re.compile(r"[0-9a-f]{32}\Z")


class ObservationError(ValueError):
    """Fixed reason with source coordinates, never event contents."""

    def __init__(self, code, source, line=None):
        super().__init__(code)
        self.code, self.source, self.line = code, str(source), line


def observer_error_record(error, *, phase, stage, task=None):
    if isinstance(error, ObservationError):
        code = error.code
    elif isinstance(error, json.JSONDecodeError):
        code = "observer_json_invalid"
    elif isinstance(error, OSError):
        code = "observer_source_io_failed"
    elif isinstance(error, KeyError):
        code = "observer_required_field_missing"
    elif isinstance(error, TypeError):
        code = "observer_source_type_invalid"
    else:
        reason = error.args[0] if error.args and isinstance(error.args[0], str) else None
        code = {"Linked cost-flow evidence": "observer_linked_source",
            "Torn final evidence line": "observer_torn_final_source",
            "Linked instance-cost evidence is forbidden": "observer_linked_control",
            "Instance-cost evidence must be a bounded regular file": "observer_control_not_bounded_regular",
            "Linked instance-cost material is forbidden": "observer_linked_output"}.get(reason, "observer_value_error")
    record = {"time": now(), "phase": phase, "stage": stage, "error_type": type(error).__name__, "reason_code": code}
    if task is not None:
        record["task_id"] = task["task_id"]
        record["owner"] = task["owner"]
        record["kind"] = task["kind"]
    if isinstance(error, ObservationError):
        record.update(source=error.source, line=error.line)
    elif isinstance(error, json.JSONDecodeError):
        record.update(json_line=error.lineno, json_column=error.colno)
    return record


def now():
    return datetime.now(timezone.utc).isoformat()


def append(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def rows(path, *, partial=False):
    path = Path(path)
    if not path.exists():
        return []
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Linked cost-flow evidence")
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        if not partial:
            raise ValueError("Torn final evidence line")
        data = data[:data.rfind(b"\n") + 1]
    decoded = []
    for number, line in enumerate(data.splitlines(), 1):
        try:
            decoded.append(json.loads(line))
        except json.JSONDecodeError:
            raise ObservationError("source_json_invalid", path, number) from None
    return decoded


def text(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value + "\n")


def observer_control(path):
    """Read one bounded, immutable host control snapshot from one descriptor."""
    path = Path(path)
    matched = CONTROL_NAME.fullmatch(path.name)
    if (matched is None or path.parent.name != "controls" or path.parent.parent.name != "observer" or any(
            parent.is_symlink() for parent in (path, *path.parents))):
        raise ObservationError("observer_control_path_invalid", path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ObservationError("observer_control_descriptor_invalid", path)
        data = stream.read(4097)
    if len(data) > 4096:
        raise ObservationError("observer_control_size_invalid", path)
    value = json.loads(data)
    if (not isinstance(value, dict) or set(value) not in (
            {"sequence", "phase", "kind"}, {"sequence", "phase", "kind", "probe_nonce"})
            or type(value.get("sequence")) is not int or value["sequence"] < 1
            or value["sequence"] != int(matched[1])
            or not isinstance(value.get("phase"), str) or not value["phase"]
            or value.get("kind") not in (*KINDS, None)):
        raise ObservationError("observer_control_snapshot_invalid", path)
    if "probe_nonce" in value and (not isinstance(value["probe_nonce"], str)
            or PROBE_NONCE.fullmatch(value["probe_nonce"]) is None):
        raise ObservationError("observer_probe_nonce_invalid", path)
    return value


def publish_reply(path, value):
    """Publish the whole nonce reply before the host can observe its name."""
    path = Path(path)
    temporary = path.with_name(path.name + ".pending")
    exclusive(temporary, value)
    if path.exists():
        raise ObservationError("observer_reply_already_exists", path)
    os.rename(temporary, path)


def make_flow_plan(*, services_image, seed, baseline_only=False, **kwargs):
    from runner.instance_cost import IMAGE
    if not IMAGE.fullmatch(services_image) or seed not in {101, 202, 303}:
        raise ValueError("Pinned services image and frozen formal seed required")
    plan = make_plan(**kwargs)
    rotated = rotation_users(plan)
    if baseline_only and rotated:
        raise ValueError("Inactive-user rotation is a separate full-flow variant, not a baseline")
    plan.update(flow_schema=SCHEMA, services_image_id=services_image, seed=seed,
        baseline_only=baseline_only, business_tasks_bound=8 + len(rotated),
        business_tasks_planned_to_execute=1 if baseline_only else 6 + len(rotated),
        implemented_subset="two actual tasks, 60-second recycle, fresh-task wake and independent single-Worker fault impact",
        orchestration_trust="trusted host; no product identity/control/isolation claim",
        observer_policy="actual observer replaces the reserved backend slot; product-backend overhead remains unmeasured",
        subset_service_policy="shared Fixture/observer; owner-specific tools/proxy only during a wave; identical policy for both strategies",
        full_network_policy="hl-ic-<trial>-user-001/-002; isolated owner tools/model-proxy aliases; no browser",
        allocation_role_alias={"observer": "backend"},
        formal_i25_eligible=False, i25_complete=False,
        remaining_work=["resident entry I24 integration and actual product-backend overhead",
            "three-seed paired measurements after same-entry/same-quota baseline",
            "fault A native recovery and I23 uncertain-write adjudication",
            "DeepSeek Harness cost-flow entry and measurements"])
    if rotated:
        plan.update(implemented_subset=plan["implemented_subset"] + "; fresh-task cold wake of fixed previously inactive users 003/004",
            full_network_policy="hl-ic-<trial>-user-001/-002/-003/-004; one owner per tools/proxy/Worker, at most two active; no browser",
            sampling_variant="registered-lifecycle")
    return plan


def owners_for_kind(plan, kind):
    return rotation_users(plan) if kind == INACTIVE_WAKE_KIND else list(plan["active_users"])


def admin(root, method, route, **kwargs):
    import httpx
    token = (root / "fixture/.admin-token").read_text().strip()
    with httpx.Client(timeout=10, trust_env=False) as client:
        result = client.request(method, "http://fixtures:8080" + route,
            headers={"Authorization": "Bearer " + token}, **kwargs)
        result.raise_for_status()
        return result.json()


def prepare(root):
    """No candidate imports; only requirements and actual Fixture bindings."""
    from runner.grading import prepare_tools_inputs, same_scope
    root = Path(root)
    plan = read(root / "protected/plan.json")
    prepare_idle_materials(root)
    user_record = read(root / "protected/users.json")
    tasks = []
    for user in user_record["users"]:
        owner = user["owner"]
        home = root / "users" / owner
        binding = read(home / "config/resident.json")
        binding["limits"].update(max_output_tokens=4096, max_retries=2)
        # Histories and business sessions remain in one immutable experiment.
        binding["history"]["scope"]["experiment_id"] = "instance-cost"
        user["scope"] = binding["history"]["scope"]
        for kind in KINDS:
            if owner not in owners_for_kind(plan, kind):
                continue
            task_id = kind + "-" + owner
            scope = {"experiment_id": "instance-cost", "variant": plan["trial_id"] + "-" + task_id,
                "user_id": owner, "session_id": task_id}
            for part in ("state", "workspace", "evidence", "budgets", "trusted"):
                (home / part / kind).mkdir(parents=True, exist_ok=False)
            setup = admin(root, "POST", "/admin/scenarios", json={"experiment_id": scope["experiment_id"],
                "variant": scope["variant"], "seed": plan["seed"],
                "users": [{"user_id": owner, "session_id": scope["session_id"]}], "write_mode": "idempotent"})
            actual = [row for row in setup["scopes"] if same_scope(row, scope)]
            if len(actual) != 1:
                raise ValueError("Fixture did not create the exact frozen scope")
            tool_token, model_token = actual[0]["token"], secrets.token_urlsafe(32)
            text(home / "config" / (kind + "-tools.token"), tool_token)
            text(home / "config" / (kind + "-model.token"), model_token)
            text(home / "budgets" / kind / "run.token", model_token)
            public = prepare_tools_inputs(home / "workspace" / kind, scope)
            text(home / "workspace" / kind / "task-marker.txt", task_id + ":" + scope["variant"])
            prompt = "本任务可信作用域为 " + json.dumps(scope, ensure_ascii=False) + "。\n" + public["prompt"]
            text(home / "config" / (kind + ".prompt.txt"), prompt)
            exclusive(home / "config" / (kind + "-tool-binding.json"), {"fixture_url": "http://fixtures:8080",
                "fixture_token": tool_token, "scope": scope, "workspace": "/workspace",
                "events_path": "/evidence/tool-events.jsonl"})
            binding["tasks"].append({"task_id": task_id, "scope": scope, "state_dir": "/state/" + kind,
                "workspace": "/workspace/" + kind, "output_dir": "/evidence/" + kind,
                "tool_token_file": "/config/" + kind + "-tools.token",
                "model_token_file": "/config/" + kind + "-model.token",
                "tools_schema_file": "/config/tool-schema.json", "prompt_file": "/config/" + kind + ".prompt.txt"})
            tasks.append({"task_id": task_id, "owner": owner, "kind": kind, "scope": scope,
                "model_token_sha256": sha(model_token.encode()), "tool_token_sha256": sha(tool_token.encode()),
                "prompt_sha256": sha((home / "config" / (kind + ".prompt.txt")).read_bytes())})
        # Preparation only, before any candidate starts. Preserve old material.
        os.rename(home / "config/resident.json", home / "config/pre-flow-resident.json")
        exclusive(home / "config/resident.json", binding)
        user["binding_sha256"] = sha((home / "config/resident.json").read_bytes())
    os.rename(root / "protected/users.json", root / "protected/pre-flow-users.json")
    exclusive(root / "protected/users.json", user_record)
    exclusive(root / "protected/tasks.json", tasks)


def native_activity(root, task):
    """Read actual public task events and trusted tools in the Docker VM clock."""
    home = Path(root) / "users" / task["owner"]
    raw_path = home / "evidence" / task["kind"] / "raw-events.jsonl"
    tool_path = home / "trusted" / task["kind"] / "tool-events.jsonl"
    raw, tools = rows(raw_path, partial=True), rows(tool_path, partial=True)
    current, intervals = {}, []
    for number, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise ObservationError("raw_event_not_object", raw_path, number)
        if row.get("scope") != task["scope"]:
            raise ObservationError("raw_scope_mismatch", raw_path, number)
        if row.get("task_id") != task["task_id"]:
            raise ObservationError("raw_task_identity_mismatch", raw_path, number)
        event = row.get("data")
        if row.get("stream_mode") != "tasks" or not isinstance(event, dict) or not isinstance(event.get("id"), str):
            continue
        if "input" in event and "result" not in event:
            if event["id"] in current:
                raise ObservationError("native_task_start_duplicate", raw_path, number)
            current[event["id"]] = {"line": number, "name": event.get("name"), "start": row["time"]}
        elif any(key in event for key in ("result", "error", "interrupts")):
            opened = current.pop(event["id"], None)
            if opened is None:
                raise ObservationError("native_task_finish_without_start", raw_path, number)
            if opened is not None and opened["name"] in {"model", "tools"} and not event.get("error"):
                intervals.append([opened["start"], row["time"]])
    for number, row in enumerate(tools, 1):
        if not isinstance(row, dict):
            raise ObservationError("trusted_tool_event_not_object", tool_path, number)
        if row.get("scope") != task["scope"]:
            raise ObservationError("trusted_tool_scope_mismatch", tool_path, number)
    live = {key: value for key, value in current.items() if value["name"] in {"model", "tools"}}
    started_tools = {row.get("call_id"): row for row in tools if row.get("phase") == "start"}
    completed = [row["time"] for row in tools if row.get("phase") == "finish" and row.get("status") == 200
        and row.get("call_id") in started_tools and row.get("tool") == started_tools[row["call_id"]].get("tool")]
    return {"task_id": task["task_id"], "scope": task["scope"], "native_prefix_lines": len(raw),
        "tool_prefix_lines": len(tools), "live": live, "intervals": intervals,
        "successful_tool_times": completed, "source_paths": [str(raw_path), str(tool_path)]}


def observe(root):
    """Measured observer process; no candidate SDK, model, control or answers."""
    root = Path(root)
    tasks = read(root / "protected/tasks.json")
    target = root / "protected/observer"
    target.mkdir(parents=True, exist_ok=True)
    control, sequence, seen_probes = None, 0, set()
    while not (target / "stop.json").exists():
        phase, selected_task, stage = control, None, "read_control"
        try:
            # Apply only the next immutable sequence; an absent predecessor is
            # never skipped. The host's existing probe deadline remains binding.
            next_path = target / "controls" / f"control-{sequence + 1:012d}.json"
            new_control = next_path.exists()
            if new_control:
                snapshot = observer_control(next_path)
                nonce = snapshot.get("probe_nonce")
                if nonce is not None and nonce in seen_probes:
                    raise ObservationError("observer_probe_nonce_reused", next_path)
                control, sequence = snapshot, snapshot["sequence"]
                if nonce is not None:
                    seen_probes.add(nonce)
            phase = control
            if phase is None:
                time.sleep(.25)
                continue
            selected = [task for task in tasks if task["kind"] == phase.get("kind")]
            observed = []
            stage = "native_activity"
            for selected_task in selected:
                observed.append(native_activity(root, selected_task))
            selected_task = None
            record = {"time": now(), "phase": phase["phase"], "kind": phase["kind"],
                "control_sequence": sequence, "tasks": observed,
                "clock_domain": "same_Docker_VM_UTC", "model_or_tool_action_requested": False}
            if new_control and "probe_nonce" in phase:
                nonce = phase["probe_nonce"]
                record["probe_nonce"] = nonce
                stage = "write_reply"
                publish_reply(target / "replies" / (nonce + ".json"), record)
            stage = "append_live"
            append(target / "live.jsonl", record)
            # A trigger is evidence of an actually observed window, never a
            # guarantee the tasks remain live when the host receives it.
            trigger = target / "kill-ready.json"
            if (phase.get("kind") == "fault" and not trigger.exists() and len(observed) == 2
                    and all(item["live"] and item["successful_tool_times"] for item in observed)):
                stage = "write_fault_trigger"
                exclusive(trigger, record)
        except (OSError, ValueError, KeyError, TypeError) as error:
            append(target / "errors.jsonl", observer_error_record(error,
                phase=phase.get("phase", "unread") if isinstance(phase, dict) else "unread",
                stage=stage, task=selected_task))
        time.sleep(.25)


def budget(root, task):
    home = Path(root) / "users" / task["owner"]
    directory = home / "budgets" / task["kind"]
    token_hash = sha((directory / "run.token").read_bytes().strip())
    with sqlite3.connect((directory / "calls.sqlite").as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        binding = db.execute("SELECT id,token_hash,cap FROM budget").fetchall()
        calls = [list(row) for row in db.execute("SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id")]
    valid = (binding == [(1, task["model_token_sha256"], 20)] and token_hash == task["model_token_sha256"]
        and len(calls) <= 20 and [row[0] for row in calls] == list(range(1, len(calls) + 1)))
    return {"binding_valid": valid, "token_sha256": token_hash, "cap": 20, "calls": calls,
        "count": len(calls), "all_forwarded_2xx": bool(calls) and all(row[3] == "forwarded"
            and type(row[4]) is int and 200 <= row[4] < 300 for row in calls)}


def collect(root):
    """After Workers exit; snapshot the actual Fixture and original budgets."""
    root = Path(root)
    tasks = read(root / "protected/tasks.json")
    results = {}
    for task in tasks:
        home = root / "users" / task["owner"]
        if not (home / "budgets" / task["kind"] / "calls.sqlite").exists():
            continue
        results[task["task_id"]] = {"scope": task["scope"], "budget": budget(root, task),
            "ledger": admin(root, "GET", "/admin/ledger", params={"experiment_id": task["scope"]["experiment_id"],
                "variant": task["scope"]["variant"]})}
    exclusive(root / "protected/final-task-observations.json", results)
    disk = {}
    for category in ("state", "workspace", "budgets"):
        files = [path for user in (root / "users").iterdir() for path in (user / category).rglob("*") if path.is_file()]
        if any(path.is_symlink() for path in files):
            raise ValueError("Linked cost-flow persistent data")
        disk[category] = {"files": len(files), "logical_bytes": sum(path.stat().st_size for path in files),
            "kind": "measured_file_lengths_not_host_storage_provider_billing"}
    exclusive(root / "protected/persistent-disk.json", disk)


def effective_overlap(root, tasks, samples, observer):
    """Require paired native intervals, both actual tool progress and 2 samples.

    Sample bracketing uses two observer entries from the same Docker clock.
    macOS monotonic timestamps are used only to prove those observations bracket
    the resource operation; they are not compared with a container UTC time.
    """
    from runner.pause_recovery import instant
    activity = [native_activity(root, task) for task in tasks]
    intersections = [(max(instant(a), instant(c)), min(instant(b), instant(d)))
        for a, b in activity[0]["intervals"] for c, d in activity[1]["intervals"]
        if max(instant(a), instant(c)) < min(instant(b), instant(d))]
    progress = bool(intersections) and all(any(start <= instant(value) <= end
        for start, end in intersections for value in item["successful_tool_times"]) for item in activity)
    running_overlap = None
    if all(item["intervals"] for item in activity):
        start = max(min(instant(a) for a, _ in item["intervals"]) for item in activity)
        end = min(max(instant(b) for _, b in item["intervals"]) for item in activity)
        if start < end:
            running_overlap = (start, end)
    bracketed = []
    original_rows = {sha(canonical(row)) for row in observer}
    for sample in samples:
        before, after = sample.get("observer_before"), sample.get("observer_after")
        if not before or not after or before.get("phase") != sample.get("phase") or after.get("phase") != sample.get("phase"):
            continue
        lo, hi = instant(before["time"]), instant(after["time"])
        if (sha(canonical(before)) in original_rows and sha(canonical(after)) in original_rows
                and isinstance(before.get("probe_nonce"), str) and isinstance(after.get("probe_nonce"), str)
                and before["probe_nonce"] != after["probe_nonce"]
                and sample["host_before_observer_monotonic"] <= sample["host_started_monotonic"]
                <= sample["host_finished_monotonic"] <= sample["host_after_observer_monotonic"]
                and running_overlap is not None and running_overlap[0] <= lo <= hi <= running_overlap[1]):
            bracketed.append(sample["host_started_monotonic"])
    return {"passed": progress and len(set(bracketed)) >= 2, "actual_native_intersections": len(intersections),
        "both_tasks_effective_tool_progress": progress, "samples_inside_native_overlap": len(set(bracketed)),
        "sample_window": "intersection of actual native execution lifetimes; effective tool progress separately requires native model/tool interval intersections",
        "source_observer_rows": len(observer), "clock_policy": "Docker UTC interval brackets; no host/container UTC comparison"}


def grade_task(root, plan, task, observations):
    from runner.grading import grade_tools, audited_calls
    home = Path(root) / "users" / task["owner"]
    trusted = observations[task["task_id"]]
    tools = rows(home / "trusted" / task["kind"] / "tool-events.jsonl")
    result = read(home / "evidence" / task["kind"] / "result.json")
    business = grade_tools(plan["seed"], task["scope"], home / "workspace" / task["kind"], trusted["ledger"], tools)
    projection = result.get("native_state", {})
    source = projection.get("source", {})
    name = source.get("path")
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("native-state-"):
        raise ValueError("Task native completion lacks original projection")
    path = home / "evidence" / task["kind"] / name
    native = read(path)
    normal = (source.get("public_api") == "CompiledStateGraph.aget_state" and source.get("sha256") == sha(path.read_bytes())
        and projection.get("scope") == task["scope"] and result.get("scope") == task["scope"]
        and native.get("config", {}).get("configurable", {}).get("thread_id") == task["scope"]["session_id"]
        and not native.get("next") and not native.get("tasks") and not native.get("interrupts")
        and result.get("native_status") == "completed" and result.get("finish_reason") == "stop"
        and result.get("status") == "completed" and 0 < result.get("elapsed_seconds", 301) <= 300)
    actual_marker = (home / "workspace" / task["kind"] / "task-marker.txt").read_text()
    b = trusted["budget"]
    valid, pairs = audited_calls(tools, task["scope"])
    finished_writes = [start["arguments"].get("operation_id") for start, finish in pairs
        if start["tool"] == "write" and finish["status"] == 200]
    passed = (business["passed"] and normal and b["binding_valid"] and b["all_forwarded_2xx"]
        and actual_marker == task["task_id"] + ":" + task["scope"]["variant"] + "\n"
        and valid and len(finished_writes) == len(set(finished_writes)))
    return {"passed": passed, "business": business, "native_normal_completion": normal, "budget": b,
        "original_business_checkpoint": projection, "safe_replay_requested": False}


def verify_business_read(root, plan, item, task_timings):
    """Compare actual completed business checkpoint bytes, not a new sentinel."""
    if item.get("read_succeeded") is not True:
        return False
    original, restored = item["original"], item["restored"]
    kind = item.get("kind", "active")
    home = Path(root) / "users" / item["owner"] / "evidence" / kind
    values = []
    for summary in (original, restored):
        source = summary.get("source", {})
        name = source.get("path")
        if (not isinstance(name, str) or Path(name).name != name or not name.startswith("native-state-")
                or source.get("public_api") != "CompiledStateGraph.aget_state"):
            return False
        data = (home / name).read_bytes()
        if sha(data) != source.get("sha256"):
            return False
        value = read(home / name)
        if (not value.get("values", {}).get("messages") or value.get("next") or value.get("tasks") or value.get("interrupts")
                or value.get("config", {}).get("configurable", {}).get("checkpoint_id") != summary.get("checkpoint_id")):
            return False
        values.append(value)
    before = [row for row in task_timings if row["kind"] == kind and row["owner"] == item["owner"]]
    if len(before) != 1 or values[0] != values[1] or original.get("scope") != restored.get("scope"):
        return False
    if plan["strategy"] == "resident":
        return (item["mode"] == "same_retained_objects" and item["worker_after_id"] == before[0]["worker_id"]
            and original.get("objects") == restored.get("objects"))
    return (item["mode"] == "cold_same_store" and item["worker_after_id"] != before[0]["worker_id"]
        and original.get("checkpoint_id") == restored.get("checkpoint_id"))


def verify_rotation_materials(root, plan, tasks, users):
    """Independently bind the new wave to separate frozen owner/task material."""
    root = Path(root)
    expected = {(user["owner"], kind) for user in users for kind in KINDS
        if user["owner"] in owners_for_kind(plan, kind)}
    actual = [(task["owner"], task["kind"]) for task in tasks]
    if len(actual) != len(expected) or set(actual) != expected or len(users) != plan["users"]:
        return False
    credentials, mutable_paths, scopes = [], [], []
    for user in users:
        home = root / "users" / user["owner"]
        binding_path = home / "config/resident.json"
        binding = read(binding_path)
        if (sha(binding_path.read_bytes()) != user["binding_sha256"] or binding.get("owner") != user["owner"]
                or binding["history"]["scope"] != user["scope"]):
            return False
        for filename in ("tools.token", "model.token", "principal.token"):
            credentials.append(sha((home / "config" / filename).read_bytes().strip()))
        owned = [task for task in tasks if task["owner"] == user["owner"]]
        if {item["task_id"] for item in binding["tasks"]} != {task["task_id"] for task in owned} or len(binding["tasks"]) != len(owned):
            return False
        for task in owned:
            kind, owner = task["kind"], task["owner"]
            task_id = kind + "-" + owner
            scope = {"experiment_id": "instance-cost", "variant": plan["trial_id"] + "-" + task_id,
                "user_id": owner, "session_id": task_id}
            selected = [item for item in binding["tasks"] if item["task_id"] == task_id]
            if task.get("task_id") != task_id or task.get("scope") != scope or len(selected) != 1:
                return False
            item = selected[0]
            if (item != {"task_id": task_id, "scope": scope, "state_dir": "/state/" + kind,
                    "workspace": "/workspace/" + kind, "output_dir": "/evidence/" + kind,
                    "tool_token_file": "/config/" + kind + "-tools.token",
                    "model_token_file": "/config/" + kind + "-model.token",
                    "tools_schema_file": "/config/tool-schema.json", "prompt_file": "/config/" + kind + ".prompt.txt"}):
                return False
            tool_hash = sha((home / "config" / (kind + "-tools.token")).read_bytes().strip())
            model_hash = sha((home / "config" / (kind + "-model.token")).read_bytes().strip())
            tools = read(home / "config" / (kind + "-tool-binding.json"))
            if (tool_hash != task["tool_token_sha256"] or model_hash != task["model_token_sha256"]
                    or sha((home / "budgets" / kind / "run.token").read_bytes().strip()) != model_hash
                    or tools.get("scope") != scope or sha(tools["fixture_token"].encode()) != tool_hash
                    or tools.get("fixture_url") != "http://fixtures:8080"
                    or tools.get("workspace") != "/workspace" or tools.get("events_path") != "/evidence/tool-events.jsonl"
                    or sha((home / "config" / (kind + ".prompt.txt")).read_bytes()) != task["prompt_sha256"]):
                return False
            credentials.extend((tool_hash, model_hash))
            mutable_paths.extend(str(home / category / kind) for category in ("state", "workspace", "evidence", "budgets", "trusted"))
            scopes.append(canonical(scope))
    return (len(set(credentials)) == len(credentials) and len(set(scopes)) == len(tasks)
        and len(set(mutable_paths)) == len(mutable_paths))


def verify_rotation_history(root, user, records, timing, prepared):
    """Same original checkpoint before/after a new task in a new cold Worker."""
    selected = [item for item in records if item.get("owner") == user["owner"]]
    if len(selected) != 2 or {item.get("stage") for item in selected} != {"before-task", "after-task"}:
        return False
    original = prepared["preparation_result"]
    original_source = verify_native_projection(root, user, original)
    original_value = read(Path(root) / original_source["path"])
    for item in selected:
        if (item.get("read_succeeded") is not True or item.get("task_id") != user["history_task_id"]
                or item.get("worker_id") != timing["worker_id"]
                or item["worker_id"] == prepared["after_exit"]["id"]
                or item["started_monotonic"] > item["finished_monotonic"]):
            return False
        source = verify_native_projection(root, user, item["restored"])
        if (read(Path(root) / source["path"]) != original_value
                or source["native_checkpoint_id"] != original_source["native_checkpoint_id"]):
            return False
    before = next(item for item in selected if item["stage"] == "before-task")
    after = next(item for item in selected if item["stage"] == "after-task")
    return (before["finished_monotonic"] <= timing["command_submitted_monotonic"]
        <= timing["response_observed_monotonic"] <= after["started_monotonic"])


def rotation_sampling_check(samples, status):
    """All new-variant phases, including startup/control/closing, with 5s edges."""
    try:
        if (status.get("completed") is not True or type(status.get("samples_written")) is not int
                or status["samples_written"] != len(samples) or len(samples) < 2
                or any("error" in row for row in samples)
                or [row.get("sample_sequence") for row in samples] != list(range(1, len(samples) + 1))
                or any(type(row.get("sample_sequence")) is not int for row in samples)):
            return False
        bounds = [status["started_monotonic"], status["finished_monotonic"]]
        if not all(type(value) in (int, float) and math.isfinite(value) for value in bounds):
            return False
        previous_after, previous_before = bounds[0], None
        for row in samples:
            before, started, finished, after = [row[key] for key in (
                "host_before_observer_monotonic", "host_started_monotonic", "host_finished_monotonic", "host_after_observer_monotonic")]
            if (not all(type(value) in (int, float) and math.isfinite(value) for value in (before, started, finished, after))
                    or not 0 <= previous_after <= before <= started <= finished <= after <= status["finished_monotonic"]
                    or after - before > 5 or (previous_before is None and before - status["started_monotonic"] > 5)
                    or (previous_before is not None and before - previous_before > 5)):
                return False
            previous_before, previous_after = before, after
        return status["finished_monotonic"] - previous_after <= 5
    except (KeyError, TypeError, AttributeError, OverflowError):
        return False


def rotation_checks(root, plan, tasks, users, host, samples):
    """Additional opt-in checks; legacy two-user verdicts are unchanged."""
    owners = rotation_users(plan)
    checks = {name: False for name in ("inactive_wave_frozen_independent_materials",
        "inactive_wave_follows_original_pair_without_prior_business", "inactive_original_histories_survived_cold_wake",
        "inactive_wave_exact_owner_workers", "inactive_business_state_survived_recycle",
        "rotation_all_resource_samples_within_five_seconds")}
    checks["rotation_all_resource_samples_within_five_seconds"] = rotation_sampling_check(samples, host.get("sampling_status", {}))
    try:
        checks["inactive_wave_frozen_independent_materials"] = verify_rotation_materials(root, plan, tasks, users)
        timings = [row for row in host["task_timings"] if row["kind"] == INACTIVE_WAKE_KIND]
        prior = [row for row in host["task_timings"] if row["kind"] in {"active", "wake"}]
        start = host["rotation_start"]
        prior_ids = {row["kind"] + "-" + row["owner"] for row in prior}
        fixed_prior = {kind + "-" + owner for kind in ("active", "wake") for owner in plan["active_users"]}
        fresh = start["new_task_materials"]
        checks["inactive_wave_follows_original_pair_without_prior_business"] = (
            len(prior) == 4 and prior_ids == fixed_prior and len(start["previous_task_ids"]) == 4
            and set(start["previous_task_ids"]) == fixed_prior and len(timings) == 2
            and {row["owner"] for row in timings} == set(owners)
            and all(row["response_observed_monotonic"] <= start["started_monotonic"] for row in prior)
            and (plan["strategy"] != "on-demand" or (start["worker_ids_before"] == []
                and all(row["role"] != "worker" for row in start["observed_before"])))
            and len(fresh) == 2 and {row["owner"] for row in fresh} == set(owners)
            and all(row["empty_execution_directories"] is True and row["budget_contains_only_new_token"] is True for row in fresh)
            and all({row["task_id"] for row in timing["docker_request_observation"]["tasks"]}
                == {INACTIVE_WAKE_KIND + "-" + owner for owner in owners}
                and all(row["native_prefix_lines"] == row["tool_prefix_lines"] == 0 and not row["live"]
                    for row in timing["docker_request_observation"]["tasks"]) for timing in timings))
        checks["inactive_original_histories_survived_cold_wake"] = (
            len(host["rotation_history_reads"]) == 4 and len(timings) == 2
            and all(verify_rotation_history(root, user, host["rotation_history_reads"],
                next(row for row in timings if row["owner"] == user["owner"]),
                read(Path(root) / "protected" / ("prepared-" + user["owner"] + ".json")))
                for user in users if user["owner"] in owners))
        checks["inactive_wave_exact_owner_workers"] = len(timings) == 2 and len({row["worker_id"] for row in timings}) == 2
        original_ids = {row["worker_id"] for row in prior}
        for timing in timings:
            owner, identity = timing["owner"], timing["worker_id"]
            created = read(Path(root) / "protected" / ("hl-ic-" + plan["trial_id"] + "-" + INACTIVE_WAKE_KIND + "-start-" + owner + "-created.json"))
            checks["inactive_wave_exact_owner_workers"] &= (
                identity not in original_ids and created["id"] == identity
                and created["network_mode"] == "hl-ic-" + plan["trial_id"] + "-" + owner
                and created["image_id"] == plan["candidate_image_id"]
                and created["memory_limit"] == CAPS["worker"][0] and created["cpu_nano"] == CAPS["worker"][1]
                and {row["Destination"] for row in created["mounts"]} == {"/config", "/state", "/workspace", "/evidence"}
                and len(created["mounts"]) == 4
                and all(row["Source"].endswith("/users/" + owner + row["Destination"])
                    and row["RW"] is (row["Destination"] != "/config") for row in created["mounts"]))
            for sample in (row for row in samples if row.get("phase") == INACTIVE_WAKE_KIND and "error" not in row):
                checks["inactive_wave_exact_owner_workers"] &= {row["id"] for row in sample["docker_observations"]
                    if row["role"] == "worker"} == {row["worker_id"] for row in timings}
        reads = [item for item in host["cold_reads"] if item.get("kind") == INACTIVE_WAKE_KIND]
        checks["inactive_business_state_survived_recycle"] = (len(reads) == 2
            and {item["owner"] for item in reads} == set(owners)
            and all(verify_business_read(root, plan, item, host["task_timings"]) for item in reads))
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        # An incomplete new-wave proof is a failed check, never a reason to
        # replay a failed business task or replace evidence with a new history.
        pass
    return checks


def timing_metrics(root, task, record):
    """All derived latencies use the same actual Docker UTC clock domain."""
    from runner.pause_recovery import instant
    activity = native_activity(root, task)
    request, ready = record["docker_request_observation"], record["docker_ready_observation"]
    actions = [instant(a) for a, _ in activity["intervals"]] + [instant(value) for value in activity["successful_tool_times"]]
    if not actions:
        return {"first_effective_action_seconds": None, "reason": "no_actual_native_action"}
    start, available, first = instant(request["time"]), instant(ready["time"]), min(actions)
    return {"trusted_request_to_ready_seconds": available - start,
        "trusted_request_to_first_effective_action_seconds": first - start,
        "worker_ready_to_first_effective_action_seconds": first - available,
        "queue_latency_seconds": None, "queue_policy": "no_product_scheduler_in_this_entry",
        "clock_domain": "same_Docker_VM_UTC; request/ready are protected observer markers",
        "not_product_end_to_end_latency": True}


def progress_after_worker_exit(fault, activity):
    """Use the actual Docker exit, not later inspection/probe arrival time.

    Native task events and Docker State.FinishedAt use the same Docker VM UTC
    clock. Missing/invalid exit evidence is unverified; no time is fabricated.
    """
    from runner.pause_recovery import instant
    try:
        observed = fault["after_kill"]
        state = observed["state"]
        if (fault.get("injected") is not True or observed.get("id") != fault.get("victim_id")
                or state.get("Status") != "exited" or state.get("Running") is not False
                or type(state.get("Pid")) is not int or state["Pid"] != 0
                or state.get("ExitCode") != 137 or state.get("OOMKilled") is not False
                or any(state.get(field) is not False for field in ("Dead", "Paused", "Restarting"))
                or state.get("Error") != ""):
            return {"passed": False, "reason": "actual_victim_exit_unverified"}
        finished = state.get("FinishedAt")
        boundary = instant(finished)
        started = instant(state.get("StartedAt"))
        if boundary <= 0 or started <= 0 or boundary < started:
            return {"passed": False, "reason": "actual_victim_exit_timestamp_invalid"}
        progressed = any(instant(end) > boundary for _, end in activity["intervals"])
        return {"passed": progressed, "exit_boundary": finished,
            "source": "after_kill.state.FinishedAt", "source_container_id": observed["id"],
            "clock_domain": "same_Docker_VM_UTC", "later_probe_time_used_as_exit": False,
            "reason": "actual_native_progress_after_exit" if progressed else "no_verified_native_progress_after_exit"}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"passed": False, "reason": "actual_victim_exit_or_native_time_unverified"}


def grade(root):
    root = Path(root)
    plan = read(root / "protected/plan.json")
    tasks = read(root / "protected/tasks.json")
    observations = read(root / "protected/final-task-observations.json")
    host = read(root / "protected/flow-host.json")
    samples = rows(root / "protected/flow-resource-samples.jsonl")
    observer = rows(root / "protected/observer/live.jsonl")
    checks, task_results, overlap = {}, {}, {}
    users = read(root / "protected/users.json")["users"]
    preparations = []
    for user in users:
        try:
            prepared = read(root / "protected" / ("prepared-" + user["owner"] + ".json"))
            verify_native_projection(root, user, prepared["preparation_result"])
            preparations.append(prepared["healthy_closed"] is True and prepared["preparation_result"]["agent_task_completed"] is False)
        except (OSError, ValueError, KeyError):
            preparations.append(False)
    checks["all_original_user_histories_real_and_independent"] = len(users) == plan["users"] and all(preparations)
    rotated = rotation_users(plan)
    lifecycle_registry = None
    if rotated:
        try:
            from runner.instance_cost_lifecycle import VARIANT, validate_registry
            registry_source = host["lifecycle_registry"]
            registry_path = root / "protected/rotation-lifecycle-registry.json"
            if (plan.get("sampling_variant") != VARIANT
                    or registry_source.get("path") != "protected/rotation-lifecycle-registry.json"
                    or registry_source.get("sha256") != sha(registry_path.read_bytes())):
                raise ValueError("lifecycle_registry_source_changed")
            lifecycle_registry = read(registry_path)
            checks["rotation_exact_lifecycle_registry"] = validate_registry(lifecycle_registry, plan,
                root=root, fault=host.get("fault"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            checks["rotation_exact_lifecycle_registry"] = False
    business_kinds = ("active", "wake", INACTIVE_WAKE_KIND) if rotated else ("active", "wake")
    expected = [task for task in tasks if task["kind"] == "baseline" and task["owner"] == "user-001"] if plan["baseline_only"] else [task for task in tasks if task["kind"] in business_kinds or task["task_id"] == "fault-user-002"]
    for task in expected:
        try:
            task_results[task["task_id"]] = grade_task(root, plan, task, observations)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            task_results[task["task_id"]] = {"passed": False, "error_type": type(error).__name__}
    checks["all_actual_business_tasks_passed"] = bool(task_results) and all(row["passed"] for row in task_results.values())
    checks["no_observer_errors"] = not rows(root / "protected/observer/errors.jsonl")
    checks["no_resource_sampling_errors"] = bool(samples) and not any("error" in sample for sample in samples)
    allocation_checks = []
    identity_checks = []
    for sample in samples:
        if "error" in sample:
            continue
        active_ids = [task["task_id"] for task in tasks if task["kind"] == sample.get("phase")
            and (sample.get("phase") != "baseline" or task["owner"] == "user-001")]
        allocation_checks.append(allocation_verdict(sample["docker_observations"], sample["planned_inventory"],
            memory_bytes=MEMORY, cpu_nano=CPU, active_task_ids=active_ids)["passed"])
        pins = {row["id"]: row for row in sample["planned_inventory"]}
        expected_stats = {row["id"] for row in sample["docker_observations"] if row["state"]["Running"]}
        if rotated:
            try:
                from runner.instance_cost_lifecycle import validate_sample
                exact_resources = lifecycle_registry is not None and validate_sample(sample, lifecycle_registry, plan)
                after = sample["docker_after_observations"]
                after_pins = [{"id": row["id"], "image_id": lifecycle_registry["registrations"][row["id"]]["created"]["image_id"],
                    "memory_limit": lifecycle_registry["registrations"][row["id"]]["created"]["memory_limit"],
                    "cpu_nano": lifecycle_registry["registrations"][row["id"]]["created"]["cpu_nano"],
                    "running": row["state"]["Running"]} for row in after]
                allocation_checks.append(allocation_verdict(after, after_pins, memory_bytes=MEMORY,
                    cpu_nano=CPU, active_task_ids=active_ids)["passed"])
            except (ValueError, KeyError, TypeError, AttributeError):
                exact_resources = False
        else:
            exact_resources = ({row["Container"] for row in sample["containers"]} == expected_stats
                and {row["container_id"] for row in sample["process_observations"]} == expected_stats)
        identity_checks.append(exact_resources and all(row["image_id"] == pins[row["id"]]["image_id"]
                == plan["candidate_image_id" if row["role"] == "worker" else "bench_image_id" if row["role"] in {"observer", "bench"} else "services_image_id"]
                and row["state"].get("OOMKilled") is False
                and not row["state"].get("Dead")
                and (row["role"] == "model-proxy" or not {"MODEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"}.intersection(row["environment_names"]))
                for row in sample["docker_observations"]))
    checks["all_measured_allocations_passed"] = bool(allocation_checks) and all(allocation_checks)
    checks["every_exact_worker_and_service_observed_without_OOM"] = bool(identity_checks) and all(identity_checks)
    checks["normal_closes_healthy"] = bool(host["normal_closes"]) and all(row["healthy_closed"] for row in host["normal_closes"])
    if not plan["baseline_only"]:
        for kind in business_kinds:
            selected = [task for task in tasks if task["kind"] == kind]
            overlap[kind] = effective_overlap(root, selected, [row for row in samples if row.get("phase") == kind], observer)
            active_samples = [row for row in samples if row.get("phase") == kind and "error" not in row]
            checks[kind + "_complete_eight_container_inventory"] = bool(active_samples) and all(
                sorted(item["role"] for item in sample["docker_observations"]) ==
                ["fixtures", "model-proxy", "model-proxy", "observer", "tools", "tools", "worker", "worker"]
                for sample in active_samples)
        checks["two_true_active_tasks_and_two_raw_overlap_samples"] = all(row["passed"] for row in overlap.values())
        for phase in ("initial-idle", "recycled-idle"):
            timing = host["idle_phases"][phase]
            chosen = [sample for sample in samples if sample.get("phase") == phase and "error" not in sample]
            workers = plan["users"] if plan["strategy"] == "resident" else 0
            if rotated:
                from runner.instance_cost_lifecycle import resource_complete
                checks[phase + "_rotation_resources_complete"] = bool(chosen) and all(resource_complete(sample) for sample in chosen)
            checks[phase + "_60s_actual_inventory"] = (timing["end"] - timing["start"] >= 60 and len(chosen) >= 2
                and all(sum(row.get("role") == "worker" and row["state"].get("Running") is True for row in sample["docker_observations"]) == workers for sample in chosen)
                and all(sorted(row["role"] for row in sample["docker_observations"]) == ["fixtures", "observer"] + ["worker"] * workers for sample in chosen)
                and chosen[0]["host_started_monotonic"] <= timing["start"] + 4
                and chosen[-1]["host_finished_monotonic"] >= timing["end"] - 4
                and all(0 <= right["host_started_monotonic"] - left["host_started_monotonic"] <= 4 for left, right in zip(chosen, chosen[1:])))
        fault = host["fault"]
        checks["only_one_exact_worker_fault_and_B_progress"] = (fault.get("injected") is True
            and len(fault.get("victim_id", "")) == 64 and fault.get("victim_owner") == "user-001"
            and fault.get("after_kill", {}).get("state", {}).get("ExitCode") == 137
            and fault.get("after_kill", {}).get("state", {}).get("OOMKilled") is False
            and fault.get("after_kill", {}).get("id") == fault.get("victim_id")
            and fault.get("unaffected_id") != fault.get("victim_id")
            and task_results.get("fault-user-002", {}).get("passed") is True)
        if fault.get("injected") is True:
            task_b = next(task for task in tasks if task["task_id"] == "fault-user-002")
            activity_b = native_activity(root, task_b)
            exit_progress = progress_after_worker_exit(fault, activity_b)
            checks["unaffected_B_native_progress_after_actual_kill"] = exit_progress["passed"]
            fault["unaffected_native_progress_evidence"] = exit_progress
            task_a = next(task for task in tasks if task["task_id"] == "fault-user-001")
            checks["A_original_budget_retained_without_replay"] = (fault.get("recovery_attempted") is False
                and observations[task_a["task_id"]]["budget"]["binding_valid"] is True)
            fault_bindings = {row["owner"]: row["worker_id"] for row in host["task_timings"] if row["kind"] == "fault"}
            checks["fault_target_bound_to_original_real_tasks"] = (fault_bindings.get("user-001") == fault.get("victim_id")
                and fault_bindings.get("user-002") == fault.get("unaffected_id")
                and not (root / "users/user-001/evidence/fault/result.json").exists())
        # The observer trigger proves actual live activities, independent of a
        # Future merely being queued on the host.
        try:
            trigger = read(root / "protected/observer/kill-ready.json")
            checks["fault_had_two_real_active_tasks"] = (len(trigger["tasks"]) == 2
                and all(row["live"] and row["successful_tool_times"] for row in trigger["tasks"]))
        except (OSError, ValueError, KeyError):
            checks["fault_had_two_real_active_tasks"] = False
        checks["completed_business_state_survived_recycle"] = (len(host.get("cold_reads", [])) == 2 + len(rotated)
            and all(verify_business_read(root, plan, item, host["task_timings"]) for item in host["cold_reads"]))
        if rotated:
            checks.update(rotation_checks(root, plan, tasks, users, host, samples))
            checks["rotation_sampling_status_preserved"] = read(root / "protected/rotation-sampler-status.json") == host.get("sampling_status")
    checks["no_leftover_containers"] = read(root / "protected/final-containers.json") == []
    if rotated:
        from runner.instance_cost_lifecycle import rotation_phase_metrics
        metrics = {phase: rotation_phase_metrics([row for row in samples if row.get("phase") == phase])
            for phase in sorted({row.get("phase", "unknown") for row in samples})}
    else:
        metrics = {phase: summarize([row for row in samples if row.get("phase") == phase and "error" not in row])
            for phase in sorted({row.get("phase", "unknown") for row in samples})}
        for phase, metric in metrics.items():
            counts = [sum(row["process_count"] for row in sample.get("process_observations", []))
                for sample in samples if sample.get("phase") == phase and "error" not in sample]
            metric["actual_process_count"] = {"mean": sum(counts) / len(counts), "peak": max(counts)} if counts else None
    result = {"schema": SCHEMA, "candidate": "deepagents_langgraph", "trial_id": plan["trial_id"],
        "pair_id": plan["pair_id"], "strategy": plan["strategy"], "seed": plan["seed"], "users": plan["users"],
        "pins": {key: plan[key] for key in ("candidate_image_id", "bench_image_id", "services_image_id", "model_name")},
        "cost_subset_passed": all(checks.values()), "checks": checks, "tasks": task_results, "overlap": overlap,
        "metrics": metrics, "persistent_disk": read(root / "protected/persistent-disk.json"),
        "wake_latency": {task["task_id"]: timing_metrics(root, task, record) for task in expected
            for record in host.get("task_timings", []) if record["kind"] == task["kind"] and record["owner"] == task["owner"]},
        "fault": host.get("fault"),
        "model_calls_by_task": {key: value["budget"]["count"] for key, value in observations.items()},
        "fault_worker_failures_observed": 1 if host.get("fault", {}).get("injected") else 0,
        "unavailable_task_results_after_fault": 1 if checks.get("fault_target_bound_to_original_real_tasks") else None,
        "i25_complete": False, "i24_isolation_verified_by_this_entry": False, "product_backend_measured": False,
        "savings_percent": None, "native_fault_recovery": "not_attempted; original state and model budget retained",
        "remaining_work": plan["remaining_work"], "calibration_or_execution_implied_by_source": False}
    if rotated:
        result.update(flow_variant=plan["flow_variant"], inactive_user_rotation=plan["inactive_user_rotation"])
    exclusive(root / "protected" / ("baseline-verdict.json" if plan["baseline_only"] else "flow-verdict.json"), result)
    return result


def main():
    from runner.bench import environment
    environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "observe", "collect", "grade"))
    parser.add_argument("--root", default="/trial")
    args = parser.parse_args()
    if args.action == "grade":
        result = grade(args.root)
        print(json.dumps({"cost_subset_passed": result["cost_subset_passed"], "i25_complete": False}))
        return 0 if result["cost_subset_passed"] else 1
    {"prepare": prepare, "observe": observe, "collect": collect}[args.action](Path(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
