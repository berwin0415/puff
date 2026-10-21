"""DSH-only I24 evidence adapters; no candidate imports, repair or new prompts."""
from __future__ import annotations

from pathlib import Path
import time

from execution import dsh_materials as d
from runner import pause_recovery as p
from runner.config import load_config
from runner.grading import audited_calls, grade_tools, scoped_ledger


def environment(model, image):
    p.require(isinstance(image, str) and d.IMAGE.fullmatch(image), "dsh_actual_image_pin_required")
    return {"MODEL_API_BASE_URL": "http://model-proxy:8082/v1", "MODEL_NAME": model,
        "MODEL_RUN_TOKEN_FILE": d.TOKEN, "HARNESS_LIFECYCLE_IMAGE_ID": image,
        "TSX_TSCONFIG_PATH": d.CWD + "/tsconfig.json", "DSH_TELEMETRY_DISABLED": "1"}


def assert_native_scope(task, attempt="worker-a"):
    root = task.paths.run / "attempts" / attempt / "candidate"
    entry = p.read(root / "entrypoint.json")
    header = p.read(root / "session-before-followup.json")["header"]
    p.require(entry.get("scope") == task.scope and entry.get("run_id") == task.scope["variant"]
        and entry.get("session_id") == header.get("id") == task.scope["session_id"]
        and header.get("cwd") == "/workspace" and entry.get("variant") == "goal-runtime", "dsh_native_scope_changed")


def event_scope(task, row):
    return (row.get("method") == "session.event" and row.get("params", {}).get("sessionId") == task.scope["session_id"]
            and isinstance(row.get("params", {}).get("event"), dict))


def live_turn(task, rows):
    current = None
    last_seq = -1
    for line, encoded, row in rows:
        p.require(event_scope(task, row), "dsh_foreign_native_event")
        event = p.native_dsh(row)
        p.require(type(event.get("seq")) is int and event["seq"] > last_seq
            and type(event.get("time")) is int and event["time"] >= 0, "dsh_event_sequence_or_clock_changed")
        last_seq = event["seq"]
        if event.get("type") == "turn/start":
            p.require(current is None and type(event.get("data", {}).get("turn")) is int, "dsh_overlapping_native_turn")
            current = (line, encoded, event)
        elif event.get("type") == "turn/end":
            p.require(current is not None and current[2]["data"]["turn"] == event.get("data", {}).get("turn"),
                      "dsh_unpaired_native_turn")
            current = None
    return current


def activity(task, *, require_write=True):
    from runner.multi_user import prefix
    started = time.monotonic()
    assert_native_scope(task)
    root = task.paths.run
    raw, raw_ref = prefix(root / "attempts/worker-a/candidate/raw-events.jsonl", "attempts/worker-a/candidate/raw-events.jsonl")
    tools, tools_ref = prefix(root / "tool-events.jsonl", "tool-events.jsonl", empty=True)
    name = "pause-events.jsonl" if task.kind == "main" else "control-events.jsonl"
    relative = "attempts/worker-a/candidate/" + name
    controls, control_ref = prefix(root / relative, relative, empty=True)
    current = live_turn(task, raw)
    p.require(all(row.get("scope") == task.scope for _, _, row in tools), "dsh_foreign_tool_observation")
    writes = [(line, encoded) for line, encoded, row in tools if row.get("tool") == "write"
        and row.get("phase") == "finish" and row.get("status") == 200 and isinstance(row.get("result", {}).get("receipt"), dict)]
    p.require(current is not None and (writes or not require_write), "no_eligible_native_activity")
    p.require(not any(row.get("phase") == "requested" for _, _, row in controls), "native_control_already_requested")
    line, encoded, _ = current
    value = {"observation_clock": "host_monotonic_prefix/1", "collector_clock_domain": "Docker_VM_monotonic",
        "observation_started_monotonic": started, "observation_finished_monotonic": time.monotonic(),
        "activity_source": {**raw_ref, "line": line, "line_sha256": p.sha(encoded)}, "control_source": control_ref,
        "tools_source": tools_ref, "native_prefix_lines": len(raw),
        "successful_tools": sum(row.get("phase") == "finish" and row.get("status") == 200 for _, _, row in tools)}
    if writes:
        line, encoded = writes[-1]
        value["write_source"] = {**tools_ref, "line": line, "line_sha256": p.sha(encoded)}
    return value


def validate_overlap(tasks, record):
    intervals, finishes = {}, {}
    p.require(len(tasks) == 2 and all(task.candidate == d.CANDIDATE for task in tasks), "dsh_two_tasks_required")
    for task in tasks:
        assert_native_scope(task)
        for point in ("before", "after"):
            sample = record[point]["tasks"][task.task_id]
            rows = p.observed_prefix(task.paths.run, sample["activity_source"])
            current = live_turn(task, rows)
            p.reference(task.paths.run, sample["activity_source"], required_hash=True)
            p.require(current and current[0] == sample["activity_source"]["line"], "dsh_overlap_not_live_turn")
            tools = p.observed_prefix(task.paths.run, sample["tools_source"], empty=True)
            p.require(all(row.get("scope") == task.scope for _, _, row in tools)
                and len(rows) == sample["native_prefix_lines"]
                and sum(row.get("phase") == "finish" and row.get("status") == 200 for _, _, row in tools)
                == sample["successful_tools"], "dsh_overlap_projection_changed")
        before, after = (record[point]["tasks"][task.task_id] for point in ("before", "after"))
        p.require(after["native_prefix_lines"] > before["native_prefix_lines"]
            and after["successful_tools"] > before["successful_tools"], "overlap_missing_effective_progress")
        rows = p.jsonl(task.paths.run / "attempts/worker-a/candidate/raw-events.jsonl")
        live_turn(task, rows)
        opened, pairs = None, []
        for _, _, row in rows:
            event = p.native_dsh(row)
            if event["type"] == "turn/start":
                opened = event["time"] / 1000
            elif event["type"] == "turn/end":
                p.require(opened is not None and opened <= event["time"] / 1000, "dsh_turn_clock_reversed")
                pairs.append((opened, event["time"] / 1000))
                opened = None
        intervals[task.task_id] = pairs
        finishes[task.task_id] = [p.instant(row["time"]) for _, _, row in p.jsonl(task.paths.run / "tool-events.jsonl")
            if row.get("scope") == task.scope and row.get("phase") == "finish" and row.get("status") == 200]
    first, second = tasks
    overlap = [(max(a, c), min(b, e)) for a, b in intervals[first.task_id] for c, e in intervals[second.task_id]
               if max(a, c) < min(b, e)]
    p.require(overlap and all(any(start <= finish <= end for start, end in overlap for finish in finishes[task.task_id])
        for task in tasks), "tool_progress_not_within_actual_native_overlap")
    return {"native_clock_domain": "same_Docker_VM_UTC", "overlapping_native_turns": len(overlap),
        "both_tasks_have_actual_tool_success_within_overlap": True}


def baseline_binding(task, pins, result):
    assert_native_scope(task)
    integrity = p.control_integrity(task.paths, "worker-a")
    original = p.read(task.paths.fixture / "candidate-request.json")
    target = task.paths.fixture / "attempts/worker-a"
    request, binding = p.read(target / "request.json"), p.read(target / "lifecycle.json")
    p.require(p.sha(p.regular(task.paths.fixture / "candidate-request.json"))
        == p.read(task.paths.run / "chain.json")["original_request_sha256"], "dsh_baseline_original_request_changed")
    expected = {**original, "timeout_seconds": 300, "lifecycle": {"mode": "new", "chain_id": task.scope["variant"],
        "attempt_id": "worker-a", "binding_file": "/config/lifecycle.json"}}
    p.require(request == expected and integrity["remaining_seconds"] == 300, "dsh_baseline_request_changed")
    # The pre-start inventory is now intentionally historical; the actual
    # Worker checked it before launch, while final_native checks final storage.
    spec_env = environment(pins["model_name"], pins["worker_image_id"])
    d.verify_binding(task, request, binding, config_dir=target, environment=spec_env, verify_files=False)
    entry_raw = p.regular(task.paths.run / "attempts/worker-a/candidate/entrypoint.json")
    entry = p.decode(entry_raw)
    p.require(entry.get("runtime_api") == "official runProfile + agents.create/agents.resume({setup})"
        and entry.get("session_action") == "create" and entry.get("resume_input") is None
        and isinstance(entry.get("provenance"), dict)
        and all(entry["provenance"].get(key) == value for key, value in binding["materials"].items())
        and all(binding["materials"][key] == pins[key] for key in ("source_export_sha256", "adapter_export_sha256")),
        "dsh_baseline_entrypoint_changed")
    # The frozen DSH result has session_id/lifecycle, not DA's run_id/scope;
    # complete scope and source identity come from the original entry artifact.
    p.require(result.get("session_id") == task.scope["session_id"] and p.number(result.get("elapsed_seconds"))
        and 0 < result["elapsed_seconds"] <= 300, "dsh_baseline_result_identity_or_budget_changed")
    p.require(all(result.get("lifecycle", {}).get(key) == value for key, value in request["lifecycle"].items()),
        "dsh_baseline_lifecycle_changed")
    p.dsh_contract(task.paths, pins, task.scope)
    return {"entrypoint_source": {"path": "entrypoint.json", "sha256": p.sha(entry_raw)},
        **{key: integrity[key] for key in ("request_sha256", "binding_sha256", "scope_sha256")}}


def cancel_native(task, trigger, tools, actual_ledger, *, seed, global_paths):
    from runner.multi_user import validate_http_source
    root = task.paths.run / "attempts/worker-a/candidate"
    assert_native_scope(task)
    raw = p.jsonl(root / "raw-events.jsonl")
    live_turn(task, raw)
    activity_rows = p.observed_prefix(task.paths.run, trigger["activity_source"])
    current = live_turn(task, activity_rows)
    p.require(current and current[0] == trigger["activity_source"]["line"], "dsh_cancel_not_during_live_turn")
    p.reference(task.paths.run, trigger["activity_source"], required_hash=True)
    controls = p.jsonl(root / "control-events.jsonl")
    p.require(all(row.get("scope") == task.scope for _, _, row in controls), "dsh_foreign_cancel_control")
    requested = [(line, encoded, row) for line, encoded, row in controls if row.get("phase") == "requested"]
    p.require(len(requested) == 1, "dsh_single_cancel_request_required")
    line, encoded, request = requested[0]
    p.require(request.get("signal") == "SIGUSR1" and request.get("action") == "cancel"
        and request.get("native_api") == "Agent.cancel" and request.get("native_status_before") == "running"
        and request.get("active_input_submitted") is True and request.get("turn") == current[2]["data"]["turn"]
        and request.get("cause") == {"kind": "hook", "reason": "experiment_cancel"}, "dsh_cancel_request_mismatch")
    request_id = request["request_id"]
    api_rows = [(number, row) for number, _, row in controls
        if row.get("phase") == "native_api_returned" and row.get("request_id") == request_id]
    p.require(len(api_rows) == 1, "dsh_cancel_api_source_missing")
    api_line, api = api_rows[0]
    p.require(line < api_line and api.get("api_executed") is True
        and all(api.get(key) == request.get(key) for key in ("native_api", "signal", "action", "turn", "cause"))
        and api.get("request_source") == {"path": "control-events.jsonl", "line": line}, "dsh_cancel_api_source_missing")
    moments = [trigger.get(key) for key in ("observation_started_monotonic", "observation_finished_monotonic",
        "signal_sent_monotonic", "signal_acknowledged_monotonic")]
    p.require(all(p.number(value) for value in moments) and moments == sorted(moments)
        and moments[2] - moments[0] <= 5 and moments[3] - moments[2] <= 10, "dsh_cancel_control_order_changed")
    prefix_rows = p.observed_prefix(task.paths.run, trigger["control_source"], empty=True)
    p.require(trigger["control_source"].get("path") == "attempts/worker-a/candidate/control-events.jsonl"
        and not any(row.get("phase") == "requested" for _, _, row in prefix_rows)
        and line > len(prefix_rows), "dsh_cancel_precedes_signal")
    complete, calls = audited_calls(tools, task.scope)
    partial = grade_tools(seed, task.scope, task.paths.workspace, actual_ledger, tools)
    audit_checks = {"tool_scope", "tool_audit_complete", "ledger_scope_and_counts", "business_audit_complete", "no_sensitive_effect"}
    effects_accounted = (complete and scoped_ledger(actual_ledger, task.scope)
        and all(check["passed"] for check in partial["checks"] if check["name"] in audit_checks))
    endings = [(number, p.native_dsh(row)) for number, _, row in raw if p.native_dsh(row).get("type") == "turn/end"
        and p.native_dsh(row).get("data", {}).get("turn") == request["turn"]]
    idle = [(number, row) for number, _, row in controls
        if row.get("phase") == "native_idle_observed" and row.get("request_id") == request_id]
    reason = {"kind": "aborted", "reason": {"kind": "hook", "reason": "experiment_cancel"}}
    confirmed = (len(endings) == 1 and endings[0][1].get("data", {}).get("reason") == reason
        and len(idle) == 1 and idle[0][1].get("cancel_confirmed") is True
        and idle[0][1].get("native_status_after") == "idle" and idle[0][1].get("native_inbox_pending") is False)
    if confirmed:
        idle_line, idle_row = idle[0]
        p.require(api_line < idle_line and idle_row.get("turn") == request["turn"]
            and idle_row.get("native_turn_end_reason") == reason
            and idle_row.get("source") == {"path": "raw-events.jsonl", "line": endings[0][0]}
            and idle_row.get("request_source") == {"path": "control-events.jsonl", "line": line}
            and idle_row.get("api_source") == {"path": "control-events.jsonl", "line": api_line},
            "dsh_cancel_idle_original_source_missing")
        storage = p.dsh_storage(task.paths, root, task.scope)
        p.require(storage["turn_end"] == endings[0][1], "dsh_cancel_terminal_not_in_physical_session")
        confirmation = p.instant(idle_row["time"])
        p.require(not any(start.get("tool") in {"write", "write_file", "sensitive_export"}
            and p.instant(start["time"]) > confirmation for start, _ in calls), "write_dispatched_after_native_cancel_confirmation")
        p.require(not any(p.native_dsh(row).get("type") == "turn/start"
            and p.native_dsh(row)["time"] / 1000 > confirmation for _, _, row in raw), "dsh_work_after_cancel_confirmation")
    closed = p.read(task.paths.run / "trusted/closed-observation.json")["http"]
    validate_http_source(global_paths, task.kind, closed)
    # The common grade validates the product control bytes and fixed Worker
    # journal. Its protected close record retains the observed child exit.
    process = closed.get("body", {}).get("process_observation", {})
    p.require(closed.get("task_id") == task.task_id and closed.get("principal") == task.owner
        and closed.get("status") == 200 and closed.get("body", {}).get("scope") == task.scope
        and process.get("availability") == "observed" and process.get("process_status") == "exited"
        and type(process.get("exit_code")) is int and process["exit_code"] in (0, 1), "dsh_cancel_actual_exit_missing")
    return {"passed": bool(confirmed and effects_accounted), "native_cancel_confirmed": bool(confirmed),
        "effects_accounted": bool(effects_accounted), "actual_child_exit_code": process["exit_code"],
        "api_returned": True, "native_request_id": request_id, "product_request_id": trigger["product_http"]["request_id"],
        "request_id_transport": "single_control_per_fixed_attempt_POSIX_signal_without_UUID",
        "reason": "matching_aborted_and_idle" if confirmed else "missing_or_mismatched_native_turn_end_and_idle",
        "native_request": {"path": "control-events.jsonl", "line": line, "line_sha256": p.sha(encoded)},
        "semantics": d.SEMANTICS["cancel"], "inflight_tool_write_stop_point": "not_separately_verified",
        "post_confirmation_effects": "checked" if confirmed else "unverified_without_native_confirmation"}


def prepare_surface(task):
    target = task.paths.fixture / "surface"
    target.mkdir()
    (target / "workspace").mkdir()
    (task.paths.run / "surface-calibration").mkdir()
    p.require(p.budget_snapshot(task.paths.budget, allow_empty=True)["count"] == 0, "dsh_surface_model_calls_before_calibration")
    p.create(target / "request.json", {"session_id": task.scope["session_id"], "workspace": "/workspace", "output_dir": "/evidence",
        "tools_url": "http://tools:8081", "tool_token_file": "/config/scope.token", "trusted_scope_file": "/config/scope.json",
        "max_output_tokens": 4096, "max_retries": 2})


def accept_surface(task):
    pins = p.read(task.paths.run / "trusted/pins.json")
    record = p.read(task.paths.run / "trusted/surface-container.json")
    before, after = record["before_start"], record["after_exit"]
    p.require(before["id"] == after["id"] and before["image_id"] == after["image_id"] == pins["candidate_image_id"]
        and before["state"]["Status"] == "created" and after["state"]["Status"] == "exited"
        and before["state"]["Running"] is False and after["state"]["Running"] is False
        and type(after["state"]["ExitCode"]) is int and after["state"]["ExitCode"] == 0 and after["state"]["OOMKilled"] is False
        and after["state"].get("Error") == "" and record.get("deleted", {}).get("id_absent") is True
        and record["deleted"].get("id") == before["id"], "dsh_surface_container_not_verified")
    config = load_config()
    host = lambda path: str(config.host_data_root / path.relative_to(config.container_data_root))
    target = task.paths.fixture / "surface"
    expected = {"/config/surface.json": (host(target / "request.json"), False),
        "/config/scope.json": (host(task.paths.fixture / "scope.json"), False),
        "/config/scope.token": (host(task.paths.fixture / "scope.token"), False),
        "/workspace": (host(target / "workspace"), True),
        "/evidence": (host(task.paths.run / "surface-calibration"), True)}
    for observation in (before, after):
        mounts = observation["mounts"]
        p.require(len(mounts) == len(expected) and len({item["Destination"] for item in mounts}) == len(mounts)
            and {item["Destination"]: (item["Source"], item["RW"]) for item in mounts} == expected,
            "dsh_surface_exact_mounts_changed")
        p.require(observation.get("role") == "worker" and observation.get("init_enabled") is True
            and observation.get("memory_limit") == 2 * 1024**3 and observation.get("cpu_nano") == 750000000
            and len(observation.get("networks", {})) == 1
            and all(observation["state"].get(key) is False for key in ("OOMKilled", "Dead", "Paused", "Restarting")),
            "dsh_surface_caps_or_runtime_changed")
    network = record.get("network_binding", {})
    trial, network_id = network.get("trial_id"), network.get("id")
    p.require(isinstance(trial, str) and trial and isinstance(network_id, str) and d.HEX.fullmatch(network_id)
        and task.scope["variant"] == trial + "-" + task.kind + "-" + task.letter
        and before.get("trial_id") == after.get("trial_id") == trial
        and network == {"id": network_id, "name": "harness-lab-" + trial + "-" + task.letter,
            "trial_id": trial, "aliases": ["surface-" + task.letter]}, "dsh_surface_network_binding_changed")
    for observation, allowed_ids in ((before, {"", network_id}), (after, {network_id})):
        observed = observation["networks"]
        p.require(set(observed) == {network["name"]}
            and observed[network["name"]].get("network_id") in allowed_ids
            and observed[network["name"]].get("aliases") == network["aliases"], "dsh_surface_network_changed")
    p.require(not any(mount["Destination"] in {"/budget", "/budget/run.token", "/config/model-run.token"}
        for mount in before["mounts"]) and "MODEL_API_KEY" not in before["environment_names"], "dsh_surface_token_mounted")
    p.require(p.budget_snapshot(task.paths.budget, allow_empty=True)["count"] == 0, "dsh_surface_model_calls_after_calibration")
    raw = p.regular(task.paths.run / "surface-calibration/goal-tool-surface.json")
    surface = p.decode(raw)
    p.require(len(surface.get("schemas", [])) == 11, "dsh_frozen_eleven_tool_surface_changed")
    with (task.paths.run / "trusted/tool-surface.json").open("xb") as stream:
        stream.write(raw)
    p.create(task.paths.run / "trusted/tool-surface-host.json", {"schema": "harness-lab-goal-surface-host/1",
        "candidate_image_id": pins["candidate_image_id"], "source_export_sha256": pins["source_export_sha256"],
        "adapter_export_sha256": pins["adapter_export_sha256"], "surface_sha256": p.sha(raw), "process_exit": 0,
        "oom_killed": False, "model_call_count_before": 0, "model_call_count_after": 0, "real_model_token_mounted": False,
        "container_evidence": "trusted/surface-container.json"})
    # Pins are created once before calibration; write the accepted augmentation
    # as a new file so the original declaration is never silently replaced.
    accepted = {**pins, "tool_surface_sha256": p.sha(raw)}
    p.create(task.paths.run / "trusted/surface-pins.json", accepted)
    p.dsh_contract(task.paths, accepted, task.scope)


def task_pins(task):
    return p.read(task.paths.run / "trusted/surface-pins.json")
