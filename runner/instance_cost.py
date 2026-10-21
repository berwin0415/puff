"""I25 fixed allocation plan and Docker-bench idle-subtrial materials/checks.

Only the history preparation + initial 60-second idle subset is implemented.
No synthetic active tasks, overlap, wake result, fault recovery or savings ratio
is generated. The full matrix remains explicit in the plan's remaining work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import stat
import statistics

from runner.metrics import summarize
from runner.resource_budget import allocation_verdict


SCHEMA = "harness-lab-instance-cost/1"
GIB = 1024 ** 3
MEMORY = 6 * GIB
CPU = 3_000_000_000
CAPS = {"worker": (2 * GIB, 750_000_000), "tools": (256 * 1024 ** 2, 250_000_000),
    "model-proxy": (256 * 1024 ** 2, 250_000_000), "fixtures": (256 * 1024 ** 2, 250_000_000),
    "backend": (256 * 1024 ** 2, 250_000_000), "bench": (512 * 1024 ** 2, 500_000_000)}
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
REMAINING = ["I24 required authorization/isolation evidence and new-quota single-task baseline",
    "two actual overlapping successful tools tasks with at least two overlap samples",
    "normal completion/recycle plus another 60-second observation",
    "second new-task wake with original user history retained and fresh scope/budget",
    "independent single-Worker fault subtrial and unaffected-user evidence",
    "complete shared backend/Fixture/tool/proxy inventory and comparative grading",
    "DeepSeek Harness resident entry and all its I25 measurements"]
INACTIVE_WAKE_KIND = "inactive-wake"
INACTIVE_WAKE_USERS = ("user-003", "user-004")


def rotation_users(plan):
    """Opt-in frozen pair; an old plan has no additional business wave."""
    rotation = plan.get("inactive_user_rotation")
    if rotation is None:
        return []
    if (plan.get("users") not in {5, 10} or plan.get("active_users") != ["user-001", "user-002"]
            or rotation != {"kind": INACTIVE_WAKE_KIND, "users": list(INACTIVE_WAKE_USERS), "after": "wake"}
            or plan.get("flow_variant") != "inactive-user-rotation"):
        raise ValueError("Invalid frozen inactive-user rotation")
    return list(INACTIVE_WAKE_USERS)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Linked instance-cost material is forbidden")
    with path.open("xb") as stream:
        stream.write(canonical(value) + b"\n")


def read(path):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Linked instance-cost evidence is forbidden")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024 ** 2:
        raise ValueError("Instance-cost evidence must be a bounded regular file")
    return json.loads(path.read_bytes())


def fixed_allocation(strategy, users, *, phase):
    if strategy not in {"resident", "on-demand"} or users not in {2, 5, 10} or phase not in {"idle", "active"}:
        raise ValueError("Unknown frozen instance-cost strategy/count/phase")
    counts = {"worker": users if strategy == "resident" else (2 if phase == "active" else 0),
        "fixtures": 1, "backend": 1, "tools": 2 if phase == "active" else 0,
        "model-proxy": 2 if phase == "active" else 0}
    memory = sum(CAPS[role][0] * count for role, count in counts.items())
    cpu = sum(CAPS[role][1] * count for role, count in counts.items())
    return {"phase": phase, "counts": counts, "allocated_memory_bytes": memory, "allocated_cpu_nano": cpu,
        "allowed": memory <= MEMORY and cpu <= CPU,
        "kind": "planned_cgroup_allocation_not_observed_RSS_or_framework_capacity"}


def make_plan(*, trial_id, pair_id, strategy, users, candidate_image, bench_image, model_name,
              rotate_inactive_users=False):
    if not ID.fullmatch(trial_id) or not ID.fullmatch(pair_id):
        raise ValueError("Use a short lowercase trial/pair ID")
    if not IMAGE.fullmatch(candidate_image) or not IMAGE.fullmatch(bench_image):
        raise ValueError("Use exact local Docker image IDs")
    if not isinstance(model_name, str) or not 0 < len(model_name) <= 256 or any(c in model_name for c in "\r\n\0"):
        raise ValueError("A frozen selected model name is required even for no-model preparation")
    if type(rotate_inactive_users) is not bool or (rotate_inactive_users and users not in {5, 10}):
        raise ValueError("Inactive-user rotation requires an explicit five- or ten-user variant")
    phases = [fixed_allocation(strategy, users, phase=phase) for phase in ("idle", "active")]
    plan = {"schema": SCHEMA, "trial_id": trial_id, "pair_id": pair_id, "candidate": "deepagents_langgraph",
        "candidate_image_id": candidate_image, "bench_image_id": bench_image, "model_name": model_name,
        "strategy": strategy, "users": users, "active_users": ["user-001", "user-002"],
        "idle_seconds": 60, "sampling_interval_seconds": 2, "max_active_tasks": 2,
        "memory_limit": MEMORY, "cpu_limit": CPU, "fixed_allocations": phases,
        "full_fixed_allocation_allowed": all(item["allowed"] for item in phases),
        "scope_policy": "new task has new variant/session/store/tool-token/model-token/ledger/budget; original history retained",
        "full_network_policy": "I24 hl-mu-<trial>-a/-b; owner-only tools/model-proxy aliases; shared fixtures/backend",
        "implemented_subset": "application-prepared native histories + initial 60-second idle only",
        "subset_service_policy": "network none; no shared backend/Fixture/tools/proxy in either idle subset; no vendor key or model requests",
        "business_tasks_bound": 0, "i24_authorization_verified": False, "i25_complete": False,
        "remaining_work": list(REMAINING),
        "refusal_reason": None if all(item["allowed"] for item in phases) else "fixed_planned_allocation_exceeds_total_before_start"}
    if rotate_inactive_users:
        plan.update(flow_variant="inactive-user-rotation", inactive_user_rotation={
            "kind": INACTIVE_WAKE_KIND, "users": list(INACTIVE_WAKE_USERS), "after": "wake"})
    return plan


def prepare_idle_materials(root):
    """Run in frozen bench, not on host; export its actual tool definitions."""
    from execution.tools.server import TOOLS
    import execution.tools.server as tool_module
    root = Path(root)
    plan = read(root / "protected/plan.json")
    if plan.get("schema") != SCHEMA:
        raise ValueError("Wrong instance-cost plan")
    schema = {"tools": TOOLS}
    records = []
    for number in range(1, plan["users"] + 1):
        owner = f"user-{number:03d}"
        user = root / "users" / owner
        config = user / "config"
        for part in ("config", "state/history", "workspace/history", "evidence/history"):
            (user / part).mkdir(parents=True, exist_ok=False)
        for filename in ("tools.token", "model.token", "principal.token"):
            path = config / filename
            path.write_text(secrets.token_urlsafe(32) + "\n")
            path.chmod(0o600)
        exclusive(config / "tool-schema.json", schema)
        scope = {"experiment_id": "instance-cost-idle", "variant": plan["trial_id"] + "-history",
            "user_id": owner, "session_id": "main"}
        binding = {"schema": "harness-lab-resident-runtime/1", "candidate": "deepagents_langgraph", "owner": owner,
            "model_name": plan["model_name"], "limits": {"max_output_tokens": 2048, "timeout_seconds": 300, "max_retries": 0},
            "history": {"task_id": "history-" + owner, "scope": scope, "state_dir": "/state/history",
                "workspace": "/workspace/history", "output_dir": "/evidence/history",
                "tool_token_file": "/config/tools.token", "model_token_file": "/config/model.token",
                "tools_schema_file": "/config/tool-schema.json", "sentinel": "prepared-history:" + plan["pair_id"] + ":" + owner},
            "tasks": []}
        exclusive(config / "resident.json", binding)
        records.append({"owner": owner, "scope": scope, "history_task_id": binding["history"]["task_id"],
            "sentinel": binding["history"]["sentinel"], "binding_sha256": sha((config / "resident.json").read_bytes()),
            "principal_credential_sha256": sha((config / "principal.token").read_bytes().strip()),
            "identity_kind": "credential_material_only_no_backend_authentication_test"})
    exclusive(root / "protected/users.json", {"users": records, "tool_schema_sha256": sha(canonical(schema)),
        "schema_origin": "frozen_bench_execution.tools.server.TOOLS_not_observed_live_endpoint",
        "schema_module_sha256": sha(Path(tool_module.__file__).read_bytes()), "bench_image_id": plan["bench_image_id"],
        "kind": "preparation_material_not_completed_native_history_or_business_tasks"})


def verify_native_projection(root, user, response):
    """Bind a host-observed public API response to original nonempty data."""
    source = response.get("source", {})
    relative = source.get("path")
    if not isinstance(relative, str) or Path(relative).name != relative or not relative.startswith("native-state-"):
        raise ValueError("Unexpected original-state projection path")
    path = Path(root) / "users" / user["owner"] / "evidence/history" / relative
    data = path.read_bytes()
    if sha(data) != source.get("sha256") or source.get("public_api") != "CompiledStateGraph.aget_state":
        raise ValueError("Original-state projection hash/API mismatch")
    value = read(path)
    expected = "prepared-history-" + user["history_task_id"]
    messages = value.get("values", {}).get("messages", [])
    if (not response.get("checkpoint_id") or response.get("scope") != user["scope"]
            or value.get("config", {}).get("configurable", {}).get("thread_id") != user["scope"]["session_id"]
            or value.get("next") or value.get("tasks") or value.get("interrupts")
            or not any(message.get("id") == expected and message.get("content") == user["sentinel"] for message in messages if isinstance(message, dict))):
        raise ValueError("History lacks the exact original sentinel or is not native idle state")
    return {"path": path.relative_to(root).as_posix(), "sha256": sha(data),
        "native_checkpoint_id": response["checkpoint_id"], "scope": user["scope"],
        "trust_limit": "source_verified_candidate_public_API_projection; no_I24_adversarial_boundary_claim"}


def grade_idle(root):
    root = Path(root)
    plan, users = read(root / "protected/plan.json"), read(root / "protected/users.json")["users"]
    phase = read(root / "protected/idle-phase.json")
    samples = [json.loads(line) for line in (root / "protected/resource-samples.jsonl").read_text().splitlines()]
    checks = {}
    checks["fixed_allocation_accepted_before_start"] = plan["full_fixed_allocation_allowed"] is True
    checks["exact_user_inventory"] = (len(users) == plan["users"]
        and {item["owner"] for item in users} == {f"user-{index:03d}" for index in range(1, plan["users"] + 1)})
    sources, preparations = [], []

    def real_worker(snapshot, *, exited):
        state = snapshot.get("state", {})
        return (snapshot.get("image_id") == plan["candidate_image_id"]
            and snapshot.get("memory_limit") == CAPS["worker"][0] and snapshot.get("cpu_nano") == CAPS["worker"][1]
            and snapshot.get("network_mode") == "none"
            and not {"MODEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"}.intersection(snapshot.get("environment_names", []))
            and (state.get("Status") == "exited" and state.get("ExitCode") == 0 and state.get("Pid") == 0
                 and state.get("OOMKilled") is False and state.get("Dead") is False and state.get("Error") == ""
                 if exited else state.get("Running") is True and state.get("OOMKilled") is False))

    for user in users:
        prepared = read(root / "protected" / ("prepared-" + user["owner"] + ".json"))
        result = prepared["preparation_result"]
        sources.append(verify_native_projection(root, user, result))
        marker = read(root / "users" / user["owner"] / "workspace/history/prepared-history.json")
        preparations.append(
            result.get("agent_task_completed") is False and marker.get("agent_task_completed") is False
            and marker.get("sentinel") == user["sentinel"] and marker.get("scope") == user["scope"]
            and prepared.get("healthy_closed") is True and real_worker(prepared.get("after_exit", {}), exited=True))
    checks["all_users_had_real_native_preparation"] = len(preparations) == plan["users"] and all(preparations)
    expected_workers = plan["users"] if plan["strategy"] == "resident" else 0
    checks["sixty_seconds_observed"] = phase["finished_monotonic"] - phase["started_monotonic"] >= 60
    checks["complete_raw_samples"] = len(samples) >= 2 and not any("error" in sample for sample in samples)
    good_samples = [sample for sample in samples if "error" not in sample]
    checks["recorded_sampling_cadence"] = (len(good_samples) >= 2
        and all(0 <= right["host_started_monotonic"] - left["host_started_monotonic"] <= 4
            for left, right in zip(good_samples, good_samples[1:]))
        and good_samples[0]["host_started_monotonic"] - phase["started_monotonic"] <= 4
        and good_samples[-1]["host_finished_monotonic"] >= phase["finished_monotonic"] - 4)
    expected_ids = {row["id"] for row in phase["inventory"]}
    checks["correct_real_idle_worker_count"] = len(expected_ids) == expected_workers
    allocations = []
    observed_worker_checks, actual_process_checks = [], []
    for sample in samples:
        if "error" in sample:
            continue
        observed = sample["docker_observations"]
        allocations.append(allocation_verdict(observed, phase["inventory"], memory_bytes=MEMORY, cpu_nano=CPU))
        checks["correct_real_idle_worker_count"] &= {row["id"] for row in observed} == expected_ids
        stats_ids = {row["Container"] for row in sample["containers"]}
        checks["correct_real_idle_worker_count"] &= stats_ids == expected_ids
        observed_worker_checks.append(all(real_worker(item, exited=False) for item in observed))
        process_rows = sample.get("process_observations", [])
        actual_process_checks.append({item["container_id"] for item in process_rows} == expected_ids
            and len(process_rows) == len(expected_ids)
            and all(len(item["raw_docker_top"].splitlines()) >= 2
                and item["process_count"] == len(item["raw_docker_top"].splitlines()) - 1 for item in process_rows))
    checks["all_observed_allocations_pass"] = bool(allocations) and all(value["passed"] for value in allocations)
    checks["real_pinned_workers_network_none"] = bool(observed_worker_checks) and all(observed_worker_checks)
    checks["actual_process_inventory"] = bool(actual_process_checks) and all(actual_process_checks)
    checks["no_leftover_trial_containers"] = read(root / "protected/final-containers.json") == []
    closes = read(root / "protected/idle-close.json")
    checks["normal_worker_shutdown"] = (closes["all_healthy_closed"] is True
        and {item["id"] for item in closes["workers"]} == expected_ids
        and len(closes["workers"]) == len(expected_ids)
        and all(real_worker(item["after_exit"], exited=True) for item in closes["workers"]))
    if plan["strategy"] == "resident":
        retained = read(root / "protected/retained-history.json")
        checks["same_real_objects_and_history_retained"] = True
        for user in users:
            before, after = retained[user["owner"]]["before"], retained[user["owner"]]["after"]
            sources.extend([verify_native_projection(root, user, before), verify_native_projection(root, user, after)])
            checks["same_real_objects_and_history_retained"] &= (
                before["source"]["sha256"] == after["source"]["sha256"]
                and before["objects"] == after["objects"] and before["objects"].get("pid") is not None)
    metrics = summarize(samples)
    process_counts = [sum(item["process_count"] for item in sample.get("process_observations", [])) for sample in good_samples]
    metrics["actual_process_count"] = ({"mean": statistics.mean(process_counts), "peak": max(process_counts)} if process_counts else None)
    verdict = {"schema": SCHEMA, "trial_id": plan["trial_id"], "subset_passed": all(checks.values()),
        "checks": checks, "source_projections": sources, "metrics": metrics,
        "scope": plan["implemented_subset"], "i25_complete": False, "business_tasks_run": 0,
        "concurrency_verified": False, "scope_isolation_verified": False, "savings_percent": None,
        "failure_blast_radius": None, "wake_latency": None, "remaining_work": plan["remaining_work"],
        "no_model_network_policy": "every measured/preparation Worker had Docker network none; no vendor key was passed",
        "limitation": "does not measure shared services, two active tasks, recycle/wake/fault or production savings"}
    exclusive(root / "protected/idle-verdict.json", verdict)
    return verdict


def main():
    from runner.bench import environment
    environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare-idle-materials", "grade-idle"))
    parser.add_argument("--root", default="/trial")
    args = parser.parse_args()
    if args.action == "prepare-idle-materials":
        prepare_idle_materials(args.root)
        print(json.dumps({"status": "materials_prepared", "agent_task_completed": False}))
        return 0
    verdict = grade_idle(args.root)
    print(json.dumps({"subset_passed": verdict["subset_passed"], "i25_complete": False}))
    return 0 if verdict["subset_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
