"""I25 rotation-only Docker lifecycle evidence checks; no runtime/SDK imports.

The contract follows I24's registered exact-ID terminal/removal approach, with
the stricter native Worker close policy kept here. Unknown failures are errors.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import stat

from runner.instance_cost import CAPS, CPU, MEMORY, canonical, sha

VARIANT = "registered-lifecycle"
SCHEMA = "harness-lab-instance-cost-lifecycle/1"
ID = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY_FIELDS = ("id", "name", "image_id", "role", "memory_limit", "cpu_nano", "network_mode",
    "environment_names", "mounts")


def require(value, reason):
    if not value:
        raise ValueError(reason)


def identity(value):
    result = {key: deepcopy(value[key]) for key in IDENTITY_FIELDS}
    result["mounts"] = sorted(result["mounts"], key=lambda row: canonical(row))
    return result


def same_identity(left, right):
    try:
        return canonical(identity(left)) == canonical(identity(right))
    except (KeyError, TypeError, ValueError):
        return False


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def raw_file(path):
    require(not any(value.is_symlink() for value in (path, *path.parents)), "lifecycle_linked_source")
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 64 * 1024**2,
        "lifecycle_source_not_bounded_regular_file")
    return path.read_bytes()


def expected_exit(request):
    kind = request.get("kind") if isinstance(request, dict) else None
    if kind == "native_close":
        return (0,)
    if kind == "service_stop":
        return (0, 143)
    if kind == "injected_fault":
        return (137,)
    return ()


def terminal_ok(value, request):
    state = value.get("state", {}) if isinstance(value, dict) else {}
    return (state.get("Status") == "exited" and state.get("Running") is False
        and type(state.get("Pid")) is int and state["Pid"] == 0
        and type(state.get("ExitCode")) is int and state["ExitCode"] in expected_exit(request)
        and state.get("Error") == "" and all(state.get(key) is False
            for key in ("OOMKilled", "Dead", "Paused", "Restarting")))


def exact_absence(proof, target):
    return (isinstance(proof, dict) and proof.get("target") == target
        and type(proof.get("return_code")) is int and proof["return_code"] == 1
        and proof.get("stdout") == "[]" and finite(proof.get("observed_monotonic"))
        and isinstance(proof.get("stderr"), str) and re.fullmatch(
            r"(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*" + re.escape(target),
            proof["stderr"].strip(), re.IGNORECASE) is not None)


def validate_current(value, registration, request, start):
    require(isinstance(registration, dict) and same_identity(value, registration.get("created")),
        "lifecycle_unregistered_or_changed_identity")
    state = value["state"]
    require(all(state.get(key) is False for key in ("OOMKilled", "Dead", "Paused", "Restarting"))
        and state.get("Error") == "", "lifecycle_unhealthy_container")
    if state.get("Status") == "created":
        require(state.get("Running") is False and type(state.get("Pid")) is int and state["Pid"] == 0
            and type(state.get("ExitCode")) is int and state["ExitCode"] == 0,
            "lifecycle_created_state_invalid")
    elif state.get("Status") == "running":
        require(start is not None and start.get("id") == value["id"] and state.get("Running") is True
            and type(state.get("Pid")) is int and state["Pid"] > 0
            and type(state.get("ExitCode")) is int and state["ExitCode"] == 0,
            "lifecycle_running_without_start")
    else:
        require(terminal_ok(value, request), "lifecycle_unknown_exit")
        require(request is not None and same_identity(request.get("before"), value),
            "lifecycle_terminal_without_exact_request")


def validate_deletion(record, registration, request, terminal):
    require(isinstance(record, dict) and isinstance(terminal, dict), "lifecycle_removal_record_missing")
    value = terminal["observation"]
    require(same_identity(value, registration["created"]) and same_identity(record.get("before_rm"), value)
        and canonical(record["before_rm"]["state"]) == canonical(value["state"])
        and terminal_ok(value, request), "lifecycle_removal_terminal_invalid")
    require(record.get("id") == value["id"] and record.get("rm_return_code") == 0
        and type(record.get("rm_return_code")) is int and record.get("rm_stdout") == value["id"]
        and exact_absence(record.get("id_absence"), value["id"])
        and exact_absence(record.get("name_absence"), value["name"].removeprefix("/")),
        "lifecycle_exact_removal_unproven")
    moments = [request.get("requested_monotonic"), terminal.get("observed_monotonic"),
        record.get("requested_monotonic"), record["id_absence"].get("observed_monotonic"),
        record["name_absence"].get("observed_monotonic")]
    require(all(finite(value) for value in moments) and moments == sorted(moments),
        "lifecycle_removal_clocks_invalid")


def validate_registry(registry, plan, *, root=None, fault=None):
    require(registry.get("schema") == SCHEMA and registry.get("sampling_variant") == VARIANT
        and registry.get("trial_id") == plan["trial_id"], "lifecycle_registry_binding_invalid")
    registrations, starts, requests, terminals, deletions = [registry[key] for key in
        ("registrations", "starts", "requests", "terminals", "deletions")]
    require(all(isinstance(rows, dict) for rows in (registrations, starts, requests, terminals, deletions)),
        "lifecycle_registry_maps_invalid")
    require(set(starts) <= set(registrations) and set(requests) <= set(starts)
        and set(terminals) <= set(requests) and set(deletions) <= set(terminals),
        "lifecycle_registry_orphan_record")
    require(bool(registrations) and set(registrations) == set(deletions),
        "lifecycle_registered_container_not_retired")
    for ident, registration in registrations.items():
        value = registration["created"]
        require(ID.fullmatch(ident) is not None and value["id"] == ident
            and registration.get("trial_id") == plan["trial_id"]
            and finite(registration.get("registered_monotonic")), "lifecycle_registration_invalid")
        require(value["role"] in {"worker", "observer", "fixtures", "tools", "model-proxy"},
            "lifecycle_registered_role_invalid")
        # HostConfig.Init is fixed when this exact Docker ID is created. Bind
        # its original inspect here; legacy global sample projections need no
        # new field, and Workers retain their original startup policy.
        require(value["role"] == "worker" or value.get("host_config_init") is True,
            "lifecycle_service_init_not_enabled")
        memory, cpu = CAPS["backend" if value["role"] == "observer" else value["role"]]
        require(type(value["memory_limit"]) is int and value["memory_limit"] == memory
            and type(value["cpu_nano"]) is int and value["cpu_nano"] == cpu,
            "lifecycle_registered_caps_invalid")
        require(isinstance(value["environment_names"], list)
            and all(isinstance(name, str) for name in value["environment_names"])
            and (value["role"] == "model-proxy" or not
                {"MODEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"}.intersection(value["environment_names"])),
            "lifecycle_registered_unexpected_secret")
        key = "candidate_image_id" if value["role"] == "worker" else "bench_image_id" if value["role"] == "observer" else "services_image_id"
        require(value["image_id"] == plan[key], "lifecycle_registered_image_changed")
        require(value["state"].get("Status") == "created" and value["state"].get("Running") is False,
            "lifecycle_registration_not_before_start")
        validate_current(value, registration, None, None)
        if ident in starts:
            require(starts[ident].get("id") == ident and finite(starts[ident].get("requested_monotonic"))
                and registration["registered_monotonic"] <= starts[ident]["requested_monotonic"],
                "lifecycle_start_before_registration")
        request, terminal = requests.get(ident), terminals.get(ident)
        if request is not None:
            require(request.get("id") == ident and same_identity(request.get("before"), value)
                and finite(request.get("requested_monotonic")) and bool(expected_exit(request)),
                "lifecycle_request_invalid")
            before = request["before"]
            require(before["state"].get("Status") == "running" and before["state"].get("Running") is True,
                "lifecycle_request_before_not_running")
            validate_current(before, registration, None, starts.get(ident))
            require(starts[ident]["requested_monotonic"] <= request["requested_monotonic"],
                "lifecycle_request_before_start")
            require(request["kind"] in ({"native_close", "injected_fault"} if value["role"] == "worker" else {"service_stop"}),
                "lifecycle_role_exit_policy_invalid")
            if request["kind"] == "injected_fault":
                require(value["role"] == "worker" and request.get("owner") == "user-001"
                    and registration.get("owner") == "user-001"
                    and fault is not None and fault.get("injected") is True and fault.get("victim_id") == ident
                    and terminal is not None and terminal.get("kill_return_code") == 0
                    and type(terminal.get("kill_return_code")) is int,
                    "lifecycle_unknown_exit137")
                if root is not None:
                    require(sha(raw_file(root / "protected/observer/kill-ready.json")) == request.get("trigger_sha256"),
                        "lifecycle_fault_trigger_changed")
        if terminal is not None:
            require(terminal_ok(terminal["observation"], request)
                and same_identity(value, terminal["observation"]) and finite(terminal.get("observed_monotonic")),
                "lifecycle_terminal_invalid")
            require(request["requested_monotonic"] <= terminal["observed_monotonic"],
                "lifecycle_terminal_before_request")
            if request["kind"] == "native_close":
                ack = terminal.get("acknowledgement", {})
                require(isinstance(request.get("command_id"), str) and re.fullmatch('[0-9a-f]{32}', request["command_id"])
                    and request.get("owner") == registration.get("owner")
                    and ack.get("command_id") == request.get("command_id") and ack.get("status") == "closed"
                    and type(terminal.get("attach_exit")) is int and terminal["attach_exit"] == 0,
                    "lifecycle_native_close_ack_or_exit_missing")
                if root is not None:
                    source = terminal["stdout_source"]
                    require(isinstance(source, str) and source.startswith("protected/") and ".." not in source.split("/"),
                        "lifecycle_close_source_invalid")
                    original = raw_file(root / source)
                    require(original.endswith(b"\n"), "lifecycle_close_stdout_incomplete")
                    rows = [json.loads(line) for line in original.splitlines()]
                    require(sum(canonical(row) == canonical(ack) for row in rows) == 1,
                        "lifecycle_close_ack_not_in_original_stdout")
        if ident in deletions:
            validate_deletion(deletions[ident], registration, request, terminal)
    require(sum(request.get("kind") == "injected_fault" for request in requests.values()) <= 1,
        "lifecycle_multiple_fault_victims")
    # Check reservations over the complete lifecycle, including short-lived
    # created/not-ready objects that might fit between two resource samples.
    events = [(row["registered_monotonic"], 1, ident) for ident, row in registrations.items()]
    events += [(row["observed_monotonic"], 0, ident) for ident, row in terminals.items()]
    live = set()
    for _, kind, ident in sorted(events):
        if kind:
            require(ident not in live, "lifecycle_duplicate_live_reservation")
            live.add(ident)
        else:
            require(ident in live, "lifecycle_terminal_before_allocation")
            live.remove(ident)
        values = [registrations[key]["created"] for key in live]
        require(sum(row["memory_limit"] for row in values) <= MEMORY
            and sum(row["cpu_nano"] for row in values) <= CPU
            and sum(row["role"] == "worker" for row in values) <= 2,
            "lifecycle_complete_allocation_exceeded")
    return True


def known_reply(reply, value, *, eof=False):
    require(type(reply.get("return_code")) is int and isinstance(reply.get("stdout"), str)
        and isinstance(reply.get("stderr"), str), "lifecycle_command_status_missing")
    if reply["return_code"] == 0:
        require(not reply["stderr"].strip(), "lifecycle_success_with_stderr")
        return
    names = "(?:" + "|".join(re.escape(name) for name in
        (value["id"], value["name"], value["name"].removeprefix("/"))) + ")"
    pattern = r"(?:error(?: response from daemon)?:\s*)?(?:container\s+" + names + r"\s+is not running|no such (?:object|container):\s*" + names + r")"
    require(reply["return_code"] == 1 and not reply["stdout"].strip()
        and (re.fullmatch(pattern, reply["stderr"].strip(), re.IGNORECASE) is not None
            or eof and reply["stderr"].strip() == "EOF"), "lifecycle_unexplained_sampling_error")


def validate_sample(sample, registry, plan):
    require(sample.get("sampling_variant") == VARIANT and "error" not in sample,
        "lifecycle_sample_variant_or_error")
    registrations, starts, requests = [registry[key] for key in ("registrations", "starts", "requests")]
    moments = [sample[key] for key in ("host_started_monotonic", "before_inventory_monotonic",
        "after_inventory_monotonic", "host_finished_monotonic")]
    require(all(finite(value) for value in moments) and moments == sorted(moments),
        "lifecycle_sample_inventory_clocks_invalid")
    before = {row["id"]: row for row in sample["docker_observations"]}
    after = {row["id"]: row for row in sample["docker_after_observations"]}
    require(len(before) == len(sample["docker_observations"]) and len(after) == len(sample["docker_after_observations"]),
        "lifecycle_duplicate_inventory")
    planned = {row["id"]: row for row in sample["planned_inventory"]}
    require(len(planned) == len(sample["planned_inventory"]) and set(planned) == set(before),
        "lifecycle_planned_inventory_set_changed")
    for ident, row in before.items():
        require(all(planned[ident].get(key) == row.get(key)
            for key in ("id", "role", "image_id", "memory_limit", "cpu_nano"))
            and planned[ident].get("running") is row["state"]["Running"],
            "lifecycle_planned_inventory_fields_changed")
    for values, observed_at in ((before, sample["before_inventory_monotonic"]), (after, sample["after_inventory_monotonic"])):
        expected = {ident for ident, registration in registrations.items()
            if registration["registered_monotonic"] <= observed_at
            and (ident not in registry["deletions"] or
                registry["deletions"][ident]["name_absence"]["observed_monotonic"] > observed_at)}
        require(set(values) == expected, "lifecycle_inventory_registration_set_changed")
        for ident, value in values.items():
            validate_current(value, registrations.get(ident), requests.get(ident), starts.get(ident))
            require(registrations[ident]["registered_monotonic"] <= observed_at, "lifecycle_observed_before_registration")
            if value["state"].get("Status") == "running":
                require(starts[ident]["requested_monotonic"] <= observed_at, "lifecycle_observed_before_start")
            if value["state"].get("Status") == "exited":
                require(requests[ident]["requested_monotonic"] <= observed_at, "lifecycle_observed_before_close_request")
            memory, cpu = CAPS["backend" if value["role"] == "observer" else value["role"]]
            require(value["memory_limit"] == memory and value["cpu_nano"] == cpu, "lifecycle_observed_caps_changed")
    for ident in set(before) - set(after):
        validate_deletion(registry["deletions"].get(ident), registrations[ident], requests.get(ident), registry["terminals"].get(ident))
        require(registry["deletions"][ident]["name_absence"]["observed_monotonic"] <= sample["after_inventory_monotonic"],
            "lifecycle_absence_proof_after_sample")
    transitions = {row["id"]: row for row in sample["lifecycle_transitions"]}
    require(len(transitions) == len(sample["lifecycle_transitions"]), "lifecycle_duplicate_transition")
    stats_ids = [row["Container"] for row in sample["containers"]]
    top_ids = [row["container_id"] for row in sample["process_observations"]]
    running = {ident for ident, row in before.items() if row["state"]["Running"] is True}
    require(len(stats_ids) == len(set(stats_ids)) and len(top_ids) == len(set(top_ids))
        and set(stats_ids) == set(top_ids) and not set(stats_ids) & set(transitions)
        and set(stats_ids) | set(transitions) == running, "lifecycle_stats_identity_set_invalid")
    require(set(sample["resource_commands"]) == running, "lifecycle_command_identity_set_invalid")
    for ident, commands in sample["resource_commands"].items():
        require(set(commands) == {"stats", "top"}, "lifecycle_commands_incomplete")
        for kind, reply in commands.items():
            argv = (["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}", ident]
                if kind == "stats" else ["docker", "top", ident, "-eo", "pid,ppid,args"])
            require(reply.get("target") == ident and reply.get("argv") == argv
                and finite(reply.get("started_monotonic")) and finite(reply.get("finished_monotonic"))
                and sample["before_inventory_monotonic"] <= reply["started_monotonic"]
                <= reply["finished_monotonic"] <= sample["after_inventory_monotonic"],
                "lifecycle_command_target_or_clock_invalid")
        require(commands["stats"]["finished_monotonic"] <= commands["top"]["started_monotonic"],
            "lifecycle_command_order_invalid")
        if ident not in transitions:
            for reply in commands.values():
                require(type(reply.get("return_code")) is int and reply["return_code"] == 0
                    and isinstance(reply.get("stdout"), str) and isinstance(reply.get("stderr"), str)
                    and not reply["stderr"].strip(), "lifecycle_active_command_failed")
            parsed = [json.loads(line) for line in commands["stats"]["stdout"].splitlines()]
            selected = [row for row in sample["containers"] if row["Container"] == ident]
            tops = [row for row in sample["process_observations"] if row["container_id"] == ident]
            require(len(parsed) == len(selected) == len(tops) == 1 and canonical(parsed[0]) == canonical(selected[0])
                and len(commands["top"]["stdout"].splitlines()) >= 2
                and tops[0].get("raw_docker_top") == commands["top"]["stdout"]
                and tops[0].get("process_count") == len(commands["top"]["stdout"].splitlines()) - 1,
                "lifecycle_active_reply_source_changed")
    for ident, item in transitions.items():
        require(item.get("registration_sha256") == sha(canonical(registrations[ident]))
            and item.get("request_sha256") == sha(canonical(requests[ident])), "lifecycle_transition_source_changed")
        terminal = after.get(ident) or registry["terminals"].get(ident, {}).get("observation")
        require(same_identity(before[ident], terminal) and terminal_ok(terminal, requests.get(ident)),
            "lifecycle_transition_terminal_invalid")
        require(canonical(item.get("terminal_observation")) == canonical(terminal),
            "lifecycle_transition_terminal_changed")
        if ident not in after:
            validate_deletion(registry["deletions"].get(ident), registrations[ident], requests[ident], registry["terminals"].get(ident))
        replies = item["commands"]
        require(set(replies) == {"stats", "top"}, "lifecycle_transition_commands_missing")
        require(canonical(replies) == canonical(sample["resource_commands"][ident]), "lifecycle_transition_command_source_changed")
        for kind, reply in replies.items():
            missing = (reply["return_code"] != 0 or not reply["stdout"].strip()
                or kind == "top" and len(reply["stdout"].splitlines()) < 2)
            if missing:
                require(requests[ident]["requested_monotonic"] <= reply["finished_monotonic"],
                    "lifecycle_future_request_cannot_explain_command")
            if reply["return_code"] != 0 and re.search(r"no such (?:object|container):", reply["stderr"], re.IGNORECASE):
                deletion = registry["deletions"].get(ident)
                require(isinstance(deletion, dict)
                    and deletion["requested_monotonic"] <= reply["finished_monotonic"],
                    "lifecycle_future_removal_cannot_explain_command")
        known_reply(replies["top"], before[ident])
        if replies["top"]["return_code"] == 0:
            require(len(replies["top"]["stdout"].splitlines()) >= 2,
                "lifecycle_transition_top_success_without_process")
        # I24's narrow EOF case: top itself proves the exact object stopped.
        eof = replies["top"]["return_code"] == 1 and bool(replies["top"]["stderr"].strip())
        known_reply(replies["stats"], before[ident], eof=eof)
        if replies["stats"]["return_code"] == 0:
            rows = [json.loads(line) for line in replies["stats"]["stdout"].splitlines()]
            require(len(rows) <= 1 and all(row.get("Container") == ident for row in rows),
                "lifecycle_transition_stats_wrong_identity")
    require(sample.get("resource_measurement_complete") is resource_complete(sample),
        "lifecycle_resource_completeness_claim_changed")
    return True


def resource_complete(sample):
    """A lifecycle explanation preserves identity, not missing resource totals."""
    if "error" in sample or sample.get("lifecycle_transitions"):
        return False
    before, after = sample.get("docker_observations", []), sample.get("docker_after_observations", [])
    ids = {row["id"] for row in before}
    return (bool(ids) and len(ids) == len(before) and len(ids) == len(after)
        and ids == {row["id"] for row in after}
        and all(row.get("state", {}).get("Running") is True for row in before + after)
        and len(sample.get("containers", [])) == len(ids)
        and {row["Container"] for row in sample["containers"]} == ids
        and len(sample.get("process_observations", [])) == len(ids)
        and {row["container_id"] for row in sample["process_observations"]} == ids)


def rotation_phase_metrics(samples):
    """Do not turn omitted transition stats/top into zero usage or full peaks."""
    from runner.metrics import summarize
    complete = [sample for sample in samples if resource_complete(sample)]
    whole_phase = bool(samples) and len(complete) == len(samples)
    metric = summarize(samples if whole_phase else [])
    counts = [sum(row["process_count"] for row in sample["process_observations"]) for sample in complete]
    metric.update(samples=len(samples), sample_errors=sum("error" in sample for sample in samples),
        resource_measurement_complete=whole_phase, resource_complete_samples=len(complete),
        resource_incomplete_samples=len(samples) - len(complete),
        actual_process_count={"mean": sum(counts) / len(counts), "peak": max(counts)} if whole_phase else None)
    if not whole_phase:
        metric["note"] = ("Incomplete lifecycle resource observations: phase means/peaks and process totals "
            "are unavailable; retain raw measured rows, never fill missing containers with zero.")
    return metric
