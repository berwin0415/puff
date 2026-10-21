"""Stdlib-only guards for the frozen DSH Goal binding; no runtime or repair."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import stat

from execution.worker_control import canonical, immutable_bytes, read_secret
from runner.dsh_goal import fold_durable_goal, matches_live_view
from runner.shared_supervisor import BoundaryError, read_bound_file, sha256


CANDIDATE = "deepseek_harness"
ARGV = ("/usr/local/bin/node", "--import", "tsx/esm",
        "/opt/materials/harness-lab/goal-runtime-entrypoint.ts", "--request", "/config/request.json")
CWD = "/opt/materials/sources/main"
TOKEN = "/budget/run.token"
HEX = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
SEMANTICS = {"pause": "native_goal_pause_at_empty_step_boundary",
             "cancel": "public_Agent_cancel_immediate_terminal_requires_native_evidence"}
ENVIRONMENT = frozenset({"MODEL_API_BASE_URL", "MODEL_NAME", "MODEL_RUN_TOKEN_FILE",
    "HARNESS_LIFECYCLE_IMAGE_ID", "TSX_TSCONFIG_PATH", "DSH_TELEMETRY_DISABLED"})


def need(value, code):
    if not value:
        raise BoundaryError("dsh_" + code)


def digest(value):
    return sha256(canonical(value))


def inventory(state, workspace):
    """Match Goal stateInventory, including empty directories and every file."""
    files, directories, total = [], [], 0
    def walk(label, root, relative=""):
        nonlocal total
        path = root / relative
        info = path.lstat()
        need(not stat.S_ISLNK(info.st_mode), "inventory_symlink")
        if stat.S_ISDIR(info.st_mode):
            directories.append({"root": label, "path": relative})
            need(len(directories) <= 8192, "inventory_directories_bound")
            for child in sorted(path.iterdir(), key=lambda item: item.name.encode("utf-16-be", "surrogatepass")):
                walk(label, root, (relative + "/" if relative else "") + child.name)
        else:
            need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and len(files) < 4096,
                 "inventory_regular_files_required")
            raw = read_bound_file(root, relative, limit=64 * 1024 * 1024)
            total += len(raw)
            need(total <= 256 * 1024 * 1024, "inventory_bytes_bound")
            files.append({"root": label, "path": relative, "bytes": len(raw), "sha256": sha256(raw)})
    walk("state", Path(state))
    walk("workspace", Path(workspace))
    return {"schema": "harness-lab-native-state-index/1", "files": files, "directories": directories}


def bound_json(root, relative, expected):
    need(isinstance(expected, str) and HEX.fullmatch(expected), "bound_hash_required")
    raw = immutable_bytes(Path(root) / relative, limit=2 * 1024 * 1024)
    need(sha256(raw) == expected, "bound_file_changed")
    return json.loads(raw)


def expanded_events(events):
    """Mirror the frozen JSONL provenance ranges before hashing live events."""
    result = []
    for event in events:
        if "sourceEventSeqs" not in event:
            result.append(event)
            continue
        stored, decoded, ranged = event["sourceEventSeqs"], [], False
        need(isinstance(stored, list), "provenance_array_required")
        for entry in stored:
            if type(entry) is int:
                need(0 <= entry <= 2**53 - 1 and len(decoded) < event["seq"], "provenance_invalid")
                decoded.append(entry)
            else:
                need(isinstance(entry, list) and len(entry) == 2 and all(type(value) is int for value in entry)
                    and 0 <= entry[0] <= entry[1] <= 2**53 - 1
                    and entry[1] - entry[0] + 1 <= event["seq"] - len(decoded), "provenance_range_invalid")
                decoded.extend(range(entry[0], entry[1] + 1))
                ranged = True
        need(not ranged or all(a < b for a, b in zip(decoded, decoded[1:])), "provenance_range_order")
        result.append({**event, "sourceEventSeqs": decoded})
    return result


def verify_binding(task, request, binding, *, config_dir, environment, verify_files=True):
    """Validate the trusted request; the native entrypoint repeats its guards."""
    need(isinstance(request, dict) and isinstance(binding, dict), "binding_objects_required")
    mode, attempt = binding.get("mode"), binding.get("attempt_id")
    need((mode, attempt) in {("new", "worker-a"), ("resume", "worker-b")}, "mode_attempt_mismatch")
    need(request.get("lifecycle") == {"mode": mode, "chain_id": task.scope["variant"],
        "attempt_id": attempt, "binding_file": "/config/lifecycle.json"}, "request_lifecycle_mismatch")
    need(binding.get("schema") == "harness-lab-lifecycle/1" and binding.get("candidate") == CANDIDATE
        and binding.get("variant") == "goal-runtime" and binding.get("scope") == task.scope
        and binding.get("session_id") == request.get("session_id") == task.scope["session_id"]
        and binding.get("chain_id") == request.get("run_id") == task.scope["variant"], "full_identity_mismatch")
    need(all(request.get(key) == value for key, value in {
        "workspace": "/workspace", "state_dir": "/state", "output_dir": "/evidence",
        "tools_url": "http://tools:8081", "tool_token_file": "/config/scope.token"}.items()), "request_routes_changed")
    options = request.get("candidate_options", {})
    need(isinstance(options, dict) and options.get("case") == "tools"
        and options.get("trusted_scope_file") == "/config/scope.json"
        and not {"followup", "session_action"} & options.keys(), "request_options_changed")
    need((mode == "new" and isinstance(request.get("prompt"), str) and request["prompt"].strip()
          and "resume" not in binding)
         or (mode == "resume" and "prompt" not in request and isinstance(binding.get("resume"), dict)),
         "prompt_or_resume_mode_changed")
    need(type(request.get("max_output_tokens")) is int and 1 <= request["max_output_tokens"] <= 16384
         and type(request.get("max_retries")) is int and 0 <= request["max_retries"] <= 2, "model_limits_changed")
    materials = binding.get("materials", {})
    need(set(materials) == {"source_export_sha256", "adapter_export_sha256"}
        and all(isinstance(value, str) and HEX.fullmatch(value) for value in materials.values()), "materials_required")
    image = binding.get("image", {}).get("original_image_id")
    need(isinstance(image, str) and IMAGE.fullmatch(image)
         and environment.get("HARNESS_LIFECYCLE_IMAGE_ID") == image, "image_declaration_changed")
    need(environment.get("MODEL_API_BASE_URL") == "http://model-proxy:8082/v1"
         and environment.get("MODEL_RUN_TOKEN_FILE") == TOKEN and environment.get("MODEL_NAME"), "model_route_changed")
    model = binding.get("model", {})
    config = {"provider": "deepseek-official", "base_url": environment["MODEL_API_BASE_URL"],
        "model": environment["MODEL_NAME"], "max_output_tokens": request["max_output_tokens"],
        "max_retries": request["max_retries"], "thinking": "disabled", "reasoning_effort": "off"}
    need(model.get("config_sha256") == digest(config) and model.get("budget_id") == task.scope["variant"]
         and isinstance(model.get("run_token_sha256"), str) and HEX.fullmatch(model["run_token_sha256"])
         and type(model.get("max_model_calls")) is int and model["max_model_calls"] == 20,
         "model_budget_changed")
    tools = binding.get("tools", {})
    need(all(isinstance(tools.get(key), str) and HEX.fullmatch(tools[key])
        for key in ("config_sha256", "contract_sha256")), "tool_contract_required")
    need(binding.get("goal") == {"max_goal_rounds": 20}, "goal_cap_changed")
    budget = binding.get("active_budget", {})
    used, remaining = budget.get("used_seconds"), budget.get("remaining_seconds")
    need(budget.get("limit_seconds") == 300 and all(type(value) in (int, float) and math.isfinite(value)
        for value in (used, remaining)) and used >= 0 and remaining > 0 and used + remaining <= 300
        and type(request.get("timeout_seconds")) is int and request["timeout_seconds"] == remaining
        and (mode != "new" or (used == 0 and remaining == 300)), "cumulative_budget_changed")
    need(binding.get("state", {}).get("index_file") == "/config/lifecycle-state.json", "state_index_route_changed")
    if verify_files:
        need(json.loads(immutable_bytes(Path(config_dir) / "scope.json")) == task.scope, "scope_file_changed")
        run_token = read_secret(TOKEN)
        need(model.get("run_token_sha256") == sha256(run_token.encode()), "model_token_changed")
        scope_token = read_secret(Path(config_dir) / "scope.token")
        need(tools.get("config_sha256") == digest({"url": request["tools_url"], "scope": task.scope,
            "token_sha256": sha256(scope_token.encode())}), "tool_scope_changed")
        saved = bound_json(config_dir, "lifecycle-state.json", binding["state"].get("index_sha256"))
        need(saved == inventory(task.state_dir, task.workspace), "closed_inventory_changed")
    return {"mode": mode, "attempt_id": attempt, "materials": materials, "image_id": image}


def verify_resume_materials(task, request, binding):
    """Recheck protected closed bytes, never infer a pause from an HTTP ACK."""
    spec = task.attempts["worker-b"]
    need(binding.get("mode") == "resume" and binding.get("attempt_id") == "worker-b", "replacement_binding_required")
    # Backend does not mount /budget/run.token. Token hashes and complete files
    # are checked here; the dedicated Worker also checks the actual local token.
    verify_binding(task, request, binding, config_dir=spec.config_dir, environment=spec.environment, verify_files=False)
    resume = binding.get("resume", {})
    need(resume.get("pause_index_file") == "/config/lifecycle-pause.json", "pause_index_route_changed")
    index = bound_json(spec.config_dir, "lifecycle-pause.json", resume.get("pause_index_sha256"))
    previous = task.attempts.get("worker-a")
    if previous is not None:
        need(json.loads(read_bound_file(previous.evidence_dir, "lifecycle-pause-candidate.json")) == index,
             "original_pause_index_changed")
        closed = json.loads(read_bound_file(previous.evidence_dir, "native-state-index-after-close.json"))
        need(closed == inventory(task.state_dir, task.workspace), "original_closed_files_changed")
        original_binding = json.loads(immutable_bytes(previous.config_dir / "lifecycle.json"))
        need(binding.get("materials") == original_binding.get("materials")
            and binding.get("model") == original_binding.get("model")
            and binding.get("tools") == original_binding.get("tools")
            and binding.get("image") == original_binding.get("image"), "original_runtime_or_budget_changed")
        original_request = json.loads(immutable_bytes(previous.config_dir / "request.json"))
        verify_binding(task, original_request, original_binding, config_dir=previous.config_dir,
                       environment=previous.environment, verify_files=False)
        original_result = json.loads(read_bound_file(previous.evidence_dir, "result.json"))
        used = binding.get("active_budget", {}).get("used_seconds")
        elapsed = original_result.get("elapsed_seconds")
        need(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed > 0 and used >= elapsed
            and original_result.get("status") == "paused" and original_result.get("pause_confirmed") is False
            and original_result.get("stop_reason") == "blocked" and not original_result.get("error")
            and original_result.get("session_id") == task.scope["session_id"]
            and all(original_result.get("lifecycle", {}).get(key) == value
                for key, value in original_request["lifecycle"].items()),
            "original_pause_or_cumulative_time_changed")
    else:
        original_request = None
    need(index.get("schema") == "harness-lab-goal-pause/1" and index.get("candidate") == CANDIDATE
         and index.get("scope") == task.scope and index.get("chain_id") == task.scope["variant"]
         and index.get("session_id") == task.scope["session_id"], "pause_identity_changed")
    goal, session, source = (index.get(name, {}) for name in ("goal", "session", "source"))
    need(goal.get("phase") == "paused" and goal.get("activation") == "disarmed"
         and isinstance(goal.get("id"), str) and goal["id"] and type(goal.get("revision")) is int
         and type(goal.get("roundsStarted")) is int and 1 <= goal["roundsStarted"] < 20
         and goal.get("maxGoalRounds") == 20 and session.get("id") == task.scope["session_id"]
         and session.get("cwd") == "/workspace" and type(session.get("next_seq")) is int
         and source.get("flush") == "await_live_sessions_flush_after_native_turn_end_and_whenIdle"
         and source.get("close") == "handle_dispose_private_scopes_and_runtime_shutdown_all_resolved",
         "closed_native_goal_required")
    need(isinstance(goal.get("objective"), str) and goal["objective"].strip()
        and (original_request is None or goal["objective"] == original_request.get("prompt"))
        and binding["active_budget"]["used_seconds"] > 0, "original_goal_or_elapsed_budget_changed")
    saved = bound_json(spec.config_dir, "lifecycle-state.json", binding["state"].get("index_sha256"))
    need(saved == inventory(task.state_dir, task.workspace), "resume_inventory_changed")
    if previous is not None:
        artifact = json.loads(read_bound_file(previous.evidence_dir, "session-artifact-after-flush.json"))
        location = Path(artifact.get("native_location", {}).get("path", ""))
        need(location.is_absolute() and location.is_relative_to("/state") and ".." not in location.parts
             and location.suffix == ".jsonl", "native_location_changed")
        physical = read_bound_file(task.state_dir, location.relative_to("/state").as_posix())
        need(sha256(physical) == artifact.get("physical_sha256") == session.get("artifact_sha256")
             and artifact.get("scope") == task.scope, "physical_session_changed")
        need(physical.endswith(b"\n"), "partial_session")
        rows = [json.loads(line) for line in physical.splitlines()]
        need(rows and rows[0].get("type") == "session" and rows[0].get("id") == task.scope["session_id"]
             and rows[0].get("cwd") == "/workspace", "physical_header_changed")
        events = rows[1:]
        need(all(type(event.get("seq")) is int and event["seq"] == number for number, event in enumerate(events))
             and len(events) == session["next_seq"], "physical_sequence_changed")
        events = expanded_events(events)
        need(digest(events) == session.get("persisted_events_sha256"), "physical_events_changed")
        need(matches_live_view(goal, fold_durable_goal(events), "disarmed"), "physical_goal_changed")
        endings = [event for event in events if event.get("type") == "turn/end"]
        need(endings and endings[-1].get("data", {}).get("reason") == {"kind": "blocked"}
             and endings[-1].get("seq") == source.get("native_turn_end_seq"), "physical_blocked_end_missing")
    return {"path": "lifecycle-pause.json", "sha256": resume["pause_index_sha256"],
        "goal_id": goal["id"], "source": "actual_I22_Goal_binding_and_closed_files", "native_resume_completed": False}
