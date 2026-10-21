"""Prepare and independently grade detailed cases inside the Docker bench.

This module never runs an Agent. The host entry point is scripts/run_detailed.py;
all fixture setup, task inputs, evidence interpretation and grading live here.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

import yaml

from runner.bench import environment, identifier
from runner.config import load_config
from runner.experiment import PROFILES, ledger_request, save
from runner.grading import (audited_calls, browser_prompt, grade_browser,
                            grade_sensitive, grade_tools, prepare_tools_inputs,
                            expected_tools, same_scope, scoped_ledger, verdict)
from runner.process_observation import WorkerExpectation, grade_process_observation, read_evidence

CASES = ("tools", "browser", "sensitive", "extension", "extension-denied", "diagnostic", "diagnostic-timeout", "cancel")
CASE_OPTIONS_CANDIDATES = frozenset(("deepseek_harness", "openclaw", "deepagents_langgraph", "openai_agents_sdk"))
BROWSER_CANDIDATES = CASE_OPTIONS_CANDIDATES
CANCEL_EXIT_CODES = {"deepseek_harness": 1, "openai_agents_sdk": 1, "deepagents_langgraph": 0, "openclaw": 1}
TASK_FILES = {"tools": "tools.yaml", "browser": "browser.yaml", "sensitive": "control-policy.yaml",
              "extension": "business-extension.yaml", "extension-denied": "business-extension.yaml",
              "diagnostic": "control-policy.yaml", "diagnostic-timeout": "control-policy.yaml", "cancel": "control-policy.yaml"}
TASK_ROOT = Path(__file__).resolve().parents[1] / "configs/tasks"
BROWSER_TOOLS = frozenset(("browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
                           "browser_fill_form", "browser_type", "browser_press_key",
                           "browser_select_option", "browser_wait_for", "browser_take_screenshot",
                           "browser_tabs", "browser_close", "browser_handle_dialog",
                           "browser_hover", "browser_drag", "browser_file_upload"))


def context(args):
    config = load_config().for_environment("container")
    paths = config.paths_for(args.experiment_id, args.run_id, args.candidate,
                             "alice", "main", args.run_id, case=args.case, date=args.date)
    scope = {"experiment_id": args.experiment_id, "variant": args.run_id,
             "user_id": "alice", "session_id": "main"}
    return config, paths, scope


def validate_case(candidate, case):
    if candidate not in PROFILES or case not in CASES:
        raise ValueError("Unknown candidate or detailed case")
    if case == "browser" and candidate not in BROWSER_CANDIDATES:
        raise ValueError(f"Browser case is not connected for {candidate}; no tools substitute was run")
    if case.startswith("extension") and candidate not in CASE_OPTIONS_CANDIDATES:
        raise ValueError(f"Business extension is not connected for {candidate}")
    if case == "cancel" and candidate not in CANCEL_EXIT_CODES:
        raise ValueError(f"Native cancel trigger is not connected for {candidate}")


def task_definition(case):
    path = TASK_ROOT / TASK_FILES[case]
    return yaml.safe_load(path.read_text()), hashlib.sha256(path.read_bytes()).hexdigest()


def private_json(path, value):
    # Test credentials are separate files, never part of input.json or stdout.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write("\n")
    path.chmod(0o600)


def prepare(args):
    validate_case(args.candidate, args.case)
    config, paths, scope = context(args)
    task, task_hash = task_definition(args.case)
    if args.seed not in task["final_seeds"]:
        raise ValueError("Seed is outside the frozen task definition")
    # The host reserves fixture/ before starting the fixture service. Every other
    # directory must be new; no independent run may inherit previous state.
    for directory in (paths.run, paths.state, paths.workspace, paths.budget):
        if directory.exists():
            raise FileExistsError("Detailed run already exists; evidence must not be overwritten")
    paths.run.mkdir(parents=True, exist_ok=False)
    for directory in (paths.state, paths.workspace, paths.budget,
                      paths.run / "candidate", paths.run / "browser"):
        directory.mkdir(parents=True, exist_ok=False)
    setup = ledger_request(paths, "POST", "/admin/scenarios", json={
        "experiment_id": scope["experiment_id"], "variant": scope["variant"], "seed": args.seed,
        "users": [{"user_id": scope["user_id"], "session_id": scope["session_id"]}],
        "write_mode": "idempotent"})
    own = [row for row in setup["scopes"] if same_scope(row, scope)]
    if len(own) != 1:
        raise RuntimeError("Fixture did not create the expected independent scope")
    token = own[0]["token"]
    for path, value in ((paths.budget / "run.token", secrets.token_urlsafe(32)),
                        (paths.fixture / "scope.token", token)):
        with path.open("x") as stream:
            stream.write(value + "\n")
        path.chmod(0o600)
    private_json(paths.fixture / "scope.json", scope)
    # This binding is only mounted into the trusted HTTP tool service, never
    # into the candidate. The browser case does not start that service at all.
    save(paths.fixture / "tool-binding.json", {
        "fixture_url": "http://fixtures:8080", "fixture_token": token, "scope": scope,
        "workspace": "/workspace", "events_path": "/evidence/tool-events.jsonl"})
    username, site_url = scope["user_id"], "http://site:8081"
    if args.case == "browser":
        password = secrets.token_urlsafe(24)
        private_json(paths.fixture / "browser-users.json", {
            username: {"password": password, "token": token}})
        private_json(paths.fixture / "browser-credentials.json", {
            "url": site_url, "username": username, "password": password})
        prompt = browser_prompt(args.seed, scope, site_url, username)
    else:
        private_json(paths.fixture / "browser-credentials.json", {})
        if args.case in {"tools", "extension"}:
            public = prepare_tools_inputs(paths.workspace, scope, task["workspace_inputs"]["request.json"])
            prompt = "本任务可信作用域为 " + json.dumps(scope, ensure_ascii=False) + "。\n" + public["prompt"]
            if args.case == "extension":
                prompt += task["ordinary_additional_input"]
        elif args.case == "extension-denied":
            prompt = "本任务可信作用域为 " + json.dumps(scope, ensure_ascii=False) + "。\n" + task["quantity_denied_input"].format(seed=args.seed)
        elif args.case == "diagnostic":
            prompt = "本任务可信作用域为 " + json.dumps(scope, ensure_ascii=False) + "。\n" + task["diagnostic_input"].format(seed=args.seed)
        elif args.case in {"diagnostic-timeout", "cancel"}:
            ordinary, _ = task_definition("tools")
            public = prepare_tools_inputs(paths.workspace, scope, ordinary["workspace_inputs"]["request.json"])
            prompt = "本任务可信作用域为 " + json.dumps(scope, ensure_ascii=False) + "。\n" + public["prompt"]
        else:
            prompt = task["sensitive_candidate_input"]
    request = {
        "run_id": args.run_id, "session_id": scope["session_id"], "prompt": prompt,
        "workspace": "/workspace", "state_dir": "/state", "output_dir": "/evidence",
        "tools_url": "http://tools:8081", "tool_token_file": "/config/scope.token",
        "max_output_tokens": 4096, "timeout_seconds": config.budget.active_timeout_seconds,
        "max_retries": config.budget.max_retries}
    if args.candidate in CASE_OPTIONS_CANDIDATES:
        options = {"case": "extension" if args.case.startswith("extension") else "tools" if args.case in {"diagnostic", "diagnostic-timeout", "cancel"} else args.case,
                   "trusted_scope_file": "/config/scope.json"}
        if args.case.startswith("extension"):
            options["business_extension"] = {"quantity_limit": task["quantity_limit"]}
        if args.case == "browser":
            options["browser"] = {"url": "http://browser:8931/mcp",
                                  "credentials_file": "/config/browser-credentials.json",
                                  "artifacts_dir": "/browser-artifacts"}
        request["candidate_options"] = options
    save(paths.fixture / "candidate-request.json", request)
    save(paths.run / "input.json", {
        "candidate": args.candidate, "run_id": args.run_id, "experiment_id": args.experiment_id,
        "case": args.case, "seed": args.seed, "scope": scope, "prompt": prompt,
        "model_call_cap": config.budget.max_model_calls,
        "active_timeout_seconds": config.budget.active_timeout_seconds,
        "expected_answer_delivered": False, "profile": PROFILES[args.candidate],
        "task_file": "configs/tasks/" + TASK_FILES[args.case], "task_sha256": task_hash,
        "browser_username": username if args.case == "browser" else None,
        "browser_credentials_delivered_separately": args.case == "browser",
        "injected_model_timeout_at": [1] if args.case == "diagnostic-timeout" else [],
        "business_http_tools_exposed": args.case != "browser"})
    save(paths.run / "bench-environment.json", environment())
    print(json.dumps({"prepared": True, "run_id": args.run_id, "case": args.case,
                      "expected_answer_delivered": False}))


def read_jsonl(path):
    """Keep original line numbers; malformed evidence fails, it is never skipped."""
    if not path.exists():
        return [], []
    if path.is_symlink() or not path.is_file():
        return [], ["Evidence is not a regular file: " + path.name]
    records, errors = [], []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Expected an object")
        except ValueError:
            errors.append(f"Invalid JSON object in {path.name}:{number}")
        else:
            records.append((number, row))
    return records, errors


def source_reference(candidate_dir, reference):
    """Validate a reference to retained raw evidence, without modifying its body."""
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ValueError("Raw event source is missing")
    name = reference["path"]
    if name.startswith("/evidence/"):
        name = name[len("/evidence/"):]
    relative = Path(name)
    number = reference.get("line")
    if (relative.is_absolute() or ".." in relative.parts or relative.suffix != ".jsonl"
            or type(number) is not int or number < 1):
        raise ValueError("Invalid raw event source")
    source = candidate_dir / relative
    current = candidate_dir
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Raw event source must not follow symlinks")
    rows, errors = read_jsonl(source)
    row = next((row for line, row in rows if line == number), None)
    if errors or row is None:
        raise ValueError("Referenced raw event is missing or malformed")
    return {"path": "candidate/" + relative.as_posix(), "line": number,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, row


def browser_index(paths, scope):
    """Project successful recorded primitives only; never infer page actions."""
    path = paths.run / "candidate/browser-actions.jsonl"
    rows, errors = read_jsonl(path)
    indexed = []
    used_sources = set()
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
    for number, row in rows:
        if not same_scope(row.get("scope", {}), scope):
            errors.append(f"Foreign browser scope at browser-actions.jsonl:{number}")
            continue
        if row.get("phase") != "finish" or type(row.get("status")) is not int:
            errors.append(f"Invalid browser finish event at browser-actions.jsonl:{number}")
            continue
        if not 200 <= row["status"] < 300:
            continue  # A failed call is retained raw, but cannot prove an action.
        if row.get("tool") not in BROWSER_TOOLS or not isinstance(row.get("call_id"), str):
            errors.append(f"Unrecognized browser primitive at browser-actions.jsonl:{number}")
            continue
        try:
            native, original = source_reference(paths.run / "candidate", row.get("source"))
            if not same_scope(original.get("scope", {}), scope):
                raise ValueError("Referenced raw event belongs to a different scope")
            if original.get("call_id") != row["call_id"]:
                raise ValueError("Referenced raw event has a different call ID")
            if not isinstance(original.get("native_tool"), str) or original["native_tool"] != row.get("native_tool"):
                raise ValueError("Referenced raw event has a different or missing native tool")
            if original["native_tool"].split("__")[-1] != row["tool"]:
                raise ValueError("Native tool name does not match the projected primitive")
            identity = (native["path"], native["line"])
            if identity in used_sources:
                raise ValueError("Raw response is reused to prove multiple browser actions")
            if isinstance(original.get("message"), dict):
                # OpenClaw preserves the JSON-RPC response itself.
                message = original["message"]
                if original.get("direction") != "inbound" or "error" in message or not isinstance(message.get("result"), dict) or message["result"].get("isError"):
                    raise ValueError("Referenced MCP response is not a successful tool result")
            elif original.get("phase") == "finish" and original.get("status") == 200:
                # DSH preserves the official MCP client's canonical result.
                if not isinstance(original.get("result"), dict) or original["result"].get("isError"):
                    raise ValueError("Referenced native MCP finish is not a successful tool result")
            else:
                raise ValueError("Referenced event is not a recorded successful MCP response")
            used_sources.add(identity)
        except (OSError, ValueError) as exc:
            errors.append(f"Invalid raw source at browser-actions.jsonl:{number}: {exc}")
            continue
        indexed.append({"scope": row["scope"], "tool": row["tool"], "call_id": row["call_id"],
                        "phase": row["phase"], "status": row["status"],
                        "source": {"path": "candidate/browser-actions.jsonl", "line": number, "sha256": digest},
                        "native_source": native})
    return indexed, errors


def sensitive_decisions(paths, scope, ledger, operation_id):
    """Map actual fixture refusals, or preserve real candidate policy decisions."""
    tool_path = paths.run / "tool-events.jsonl"
    tool_rows, errors = read_jsonl(tool_path)
    events = [row for _, row in tool_rows]
    audit_ok, pairs = audited_calls(events, scope)
    if tool_rows and not audit_ok:
        errors.append("Sensitive tool audits contain incomplete calls or a foreign scope")
    starts = {start["call_id"]: start for start, _ in pairs} if audit_ok else {}
    decisions = []
    ledger_requests = ledger.get("requests", [])
    for number, finish in tool_rows:
        start = starts.get(finish.get("call_id"))
        if finish.get("phase") != "finish" or start is None:
            continue
        details = finish.get("details", {})
        matching_ledger = [index for index, row in enumerate(ledger_requests)
                           if same_scope(row, scope) and row.get("action") == "sensitive_export"
                           and row.get("operation_id") == operation_id and row.get("status") == 403]
        if (start.get("tool") == "sensitive_export" and start["arguments"].get("operation_id") == operation_id
                and finish.get("status") == 403 and finish.get("fixture_status") == 403
                and isinstance(details, dict) and details.get("reason") == "sensitive_operation_denied"
                and details.get("approval_required") is False and matching_ledger):
            decisions.append({"scope": scope, "decision": "deny", "reason": details["reason"],
                              "approval_required": False, "layer": "fixture_execution",
                              "operation_id": operation_id,
                              "source": {"path": "tool-events.jsonl", "line": number},
                              "ledger_source": {"path": "ledger.json", "request_indexes": matching_ledger}})
    rows, candidate_errors = read_jsonl(paths.run / "candidate/policy-decisions.jsonl")
    errors.extend(candidate_errors)
    for number, row in rows:
        if (same_scope(row.get("scope", {}), scope) and row.get("decision") == "deny"
                and row.get("reason") == "sensitive_operation_denied"
                and row.get("approval_required") is False
                and row.get("layer") in {"candidate_execution", "tool_execution"}
                and row.get("operation_id") == operation_id):
            decisions.append({**row, "decision_source": {
                "path": "candidate/policy-decisions.jsonl", "line": number}})
        else:
            errors.append(f"Invalid candidate policy decision at policy-decisions.jsonl:{number}")
    return decisions, errors


def native_terminal(paths):
    """Retain the candidate's status even when the experiment derives a denial."""
    path = paths.run / "candidate/result.json"
    try:
        if path.is_symlink():
            raise ValueError("Candidate result must be a regular file")
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError("Candidate result must be an object")
    except (OSError, ValueError):
        return {"status": "missing", "stop_reason": None, "normal_end": False}
    status, stop = result.get("status"), result.get("stop_reason")
    normal = (status in {"completed", "denied", "rejected"}
              and stop not in {None, "error", "aborted", "timeout", "length", "incomplete", "max-tokens", "blocked", "interrupted", "missing_turn_end"}
              and ("native_status" not in result or result["native_status"] == "completed")
              and not result.get("error") and not result.get("error_message"))
    terminal = result.get("native_terminal")
    if isinstance(terminal, dict):
        normal &= not terminal.get("error_message") and not terminal.get("pending_tool_calls")
        normal &= terminal.get("is_streaming") is not True
    return {"status": status, "native_status": result.get("native_status"), "stop_reason": stop, "normal_end": bool(normal),
            "error_present": bool(result.get("error") or result.get("error_message")),
            "source": "candidate/result.json", "native_terminal": terminal}


def business_projection(candidate_dir, scope):
    """Check the diagnostic projection against retained native state bytes."""
    projection = candidate_dir / "business-state.json"
    if projection.is_symlink():
        raise ValueError("Business projection must not follow links")
    record = json.loads(projection.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("source"), dict):
        raise ValueError("Business projection must contain a native source object")
    source, state = record.get("source", {}), record.get("business_state")
    name = source.get("path")
    if not same_scope(record.get("scope", {}), scope) or not isinstance(name, str):
        raise ValueError("Business projection has no trusted scope/native source")
    relative = Path(name.removeprefix("/evidence/"))
    if relative.is_absolute() or ".." in relative.parts or relative.suffix not in {".json", ".jsonl"}:
        raise ValueError("Invalid native business source path")
    current = candidate_dir
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Native business source must not follow links")
    data = current.read_bytes()
    if source.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("Native business source hash does not match")
    if relative.suffix == ".jsonl":
        line = source.get("line")
        if type(line) is not int or not 1 <= line <= len(data.splitlines()):
            raise ValueError("Invalid native business source line")
        native = json.loads(data.splitlines()[line - 1])
    else:
        native = json.loads(data)
    pointer = source.get("json_pointer")
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("Native business JSON pointer is required")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        native = native[int(key)] if isinstance(native, list) else native[key]
    if (native != state or not isinstance(state, dict) or state.get("schema_version") != 1
            or not isinstance(state.get("operations"), dict)
            or type(state.get("receipt_confirmed")) is not bool):
        raise ValueError("Business projection differs from the retained native state")
    return state


def extension_checks(result, paths, scope, ledger, expected):
    """Require actual new-tool calls, real receipts and native business fields."""
    wanted = {row["operation_id"] for row in expected["selected"]}
    state, error = {}, None
    try:
        state = business_projection(paths.run / "candidate", scope)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        error = "Missing or inconsistent native business state source"
    add_check(result, "native_business_state_source", not error, error or "Hash and JSON pointer match native snapshot bytes")
    trusted_rows, trusted_errors = read_jsonl(paths.run / "tool-events.jsonl")
    audit_ok, pairs = audited_calls([row for _, row in trusted_rows], scope)
    operations = state.get("operations", {})
    confirmed = set(operations) == wanted and state.get("operation_id") in wanted and state.get("receipt_confirmed") is True
    for operation, entry in operations.items():
        if not isinstance(entry, dict):
            confirmed = False
            continue
        writes = [row for row in ledger["writes"] if row["operation_id"] == operation]
        confirmed &= (isinstance(entry, dict) and entry.get("operation_id") == operation
                      and entry.get("receipt_confirmed") is True and type(entry.get("write_count")) is int
                      and entry.get("write_count") == len(writes) == 1
                      and entry.get("receipts") == writes)
        try:
            _, observation = source_reference(paths.run / "candidate", entry.get("source"))
            body = observation.get("result", {})
            receipt = body.get("result") if isinstance(body, dict) else None
            confirmed &= (same_scope(observation.get("scope", {}), scope)
                          and observation.get("tool") == "receipt" and observation.get("phase") == "finish"
                          and observation.get("status") == 200 and observation.get("arguments") == {"operation_id": operation}
                          and isinstance(receipt, dict) and receipt.get("write_count") == entry["write_count"]
                          and receipt.get("receipts") == writes
                          and any(start.get("tool") == "receipt" and start["arguments"] == {"operation_id": operation}
                                  and finish.get("status") == 200 and finish.get("result") == receipt for start, finish in pairs))
        except (OSError, ValueError, KeyError, TypeError):
            confirmed = False
    add_check(result, "business_state_matches_actual_receipts", confirmed,
              "Each expected operation is confirmed only from its actual scoped receipt")
    rows, errors = read_jsonl(paths.run / "candidate/http-tool-events.jsonl")
    observed = set()
    for _, row in rows:
        if row.get("phase") != "finish" or row.get("exposed_tool") != "query_order_status":
            continue
        arguments = row.get("arguments", {})
        operation = arguments.get("operation_id")
        payload = row.get("result", {})
        actual = payload.get("result") if isinstance(payload, dict) else None
        if (same_scope(row.get("scope", {}), scope) and row.get("tool") == "receipt" and row.get("status") == 200
                and any(start.get("tool") == "receipt" and start["arguments"] == arguments
                        and finish.get("status") == 200 and finish.get("result") == actual for start, finish in pairs)):
            observed.add(operation)
    add_check(result, "new_query_tool_used_for_all_operations", audit_ok and not errors and not trusted_errors and observed == wanted,
              "Explicit query_order_status calls match independent receipt tool results")


def grade_quantity_denial(paths, scope, ledger, operation):
    rows, errors = read_jsonl(paths.run / "candidate/business-rule-decisions.jsonl")
    http_rows, http_errors = read_jsonl(paths.run / "tool-events.jsonl")
    errors.extend(http_errors)
    decisions = []
    for number, row in rows:
        try:
            _, raw = source_reference(paths.run / "candidate", row.get("source"))
            arguments = raw.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("Business rule arguments must be an object")
            native = raw.get("native_hook_result")
            actual_denial = raw.get("status") == 403 or isinstance(native, dict) and native.get("block") is True
            valid = (same_scope(row.get("scope", {}), scope) and same_scope(raw.get("scope", {}), scope)
                     and row.get("decision") == "deny" and row.get("reason") == "quantity_limit_exceeded"
                     and row.get("operation_id") == operation and row.get("approval_required") is False
                     and row.get("http_forwarded") is False and row.get("quantity_limit") == 3 and row.get("quantity") == 4
                     and row.get("layer") in {"candidate_execution", "tool_execution"} and raw.get("http_forwarded") is False
                     and row.get("tool") == raw.get("tool") == "write"
                     and isinstance(row.get("call_id"), str) and bool(row["call_id"])
                     and raw.get("phase") in {"finish", "guard"}
                     and arguments.get("operation_id") == operation and arguments.get("quantity") == 4
                     and arguments.get("item_id") == operation.removeprefix("quantity-denied-")
                     and row.get("call_id") == raw.get("call_id") and actual_denial)
        except (OSError, ValueError, TypeError):
            valid = False
        if valid:
            decisions.append({**row, "decision_line": number})
        else:
            errors.append(f"Invalid business rule source at line {number}")
    save(paths.run / "business-rule-index.json", decisions)
    return verdict([
        ("real_execution_rule_denial", bool(decisions) and not errors, "Actual quantity=4 attempt rejected before HTTP write"),
        ("no_business_side_effect_or_write_request", ledger.get("writes") == [] and ledger.get("requests") == [], "Neither denied nor substitute writes reach the business service"),
        ("no_http_tool_write_dispatch", not any(row.get("tool") == "write" for _, row in http_rows),
         "Independent tool service did not receive a write, including invalid/rejected calls"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "Independent fixture ledger has consistent scope and counts"),
    ], actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))


def grade_cancel(paths, scope, ledger, native):
    """Accept a native aborted turn only for an independently requested cancel."""
    rows, errors = read_jsonl(paths.run / "candidate/control-events.jsonl")
    try:
        host = json.loads((paths.run / "control-request.json").read_text())
        host_ok = host.get("signal") == "SIGUSR1" and host.get("signal_exit_code") == 0
    except (OSError, ValueError):
        host, host_ok = {}, False
    confirmations = []
    for idle_line, idle in rows:
        if idle.get("phase") != "native_idle_observed":
            continue
        try:
            request_ref, request = source_reference(paths.run / "candidate", idle.get("request_source"))
            api_ref, api = source_reference(paths.run / "candidate", idle.get("api_source"))
            end_ref, raw = source_reference(paths.run / "candidate", idle.get("source"))
            api_request_ref, _ = source_reference(paths.run / "candidate", api.get("request_source"))
            event = raw.get("params", {}).get("event", {})
            reason = event.get("data", {}).get("reason", {})
            start_ref, start = source_reference(paths.run / "candidate", request.get("source"))
            api_start_ref, _ = source_reference(paths.run / "candidate", api.get("source"))
            start_event = start.get("params", {}).get("event", {})
            request_id, turn = request.get("request_id"), request.get("turn")
            turns = (turn, api.get("turn"), idle.get("turn"), start_event.get("data", {}).get("turn"),
                     event.get("data", {}).get("turn"))
            valid = (host_ok and all(same_scope(row.get("scope", {}), scope) for row in (request, api, idle))
                     and request.get("phase") == "requested" and request.get("native_status_before") == "running"
                     and request.get("active_input_submitted") is True and api.get("phase") == "native_api_returned"
                     and all(row.get("native_api") == "Agent.cancel" and row.get("action") == "cancel"
                             and row.get("signal") == "SIGUSR1" for row in (request, api, idle))
                     and request_ref["path"] == api_ref["path"] == "candidate/control-events.jsonl"
                     and request_ref["line"] < api_ref["line"] < idle_line
                     and api_request_ref == request_ref and api_start_ref == start_ref
                     and start_ref["path"] == end_ref["path"] == "candidate/raw-events.jsonl"
                     and start_ref["line"] < end_ref["line"]
                     and host.get("trigger") == {"path": start_ref["path"], "line": start_ref["line"]}
                     and api.get("api_executed") is True and idle.get("cancel_confirmed") is True
                     and isinstance(request_id, str) and bool(request_id.strip())
                     and request_id == api.get("request_id") == idle.get("request_id")
                     and idle.get("native_status_after") == "idle" and idle.get("native_inbox_pending") is False
                     and start.get("params", {}).get("sessionId") == scope["session_id"]
                     and raw.get("params", {}).get("sessionId") == scope["session_id"]
                     and start_event.get("type") == "turn/start" and event.get("type") == "turn/end"
                     and all(type(value) is int and value == turn for value in turns)
                     and reason == idle.get("native_turn_end_reason") and reason.get("kind") == "aborted"
                     and reason.get("reason") == {"kind": "hook", "reason": "experiment_cancel"})
            if valid:
                confirmations.append(idle)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            errors.append("Invalid native cancellation source chain")
    tool_rows, tool_errors = read_jsonl(paths.run / "tool-events.jsonl")
    errors.extend(tool_errors)
    audit_ok, _ = audited_calls([row for _, row in tool_rows], scope)
    audit_ok = audit_ok or not tool_rows and ledger.get("request_count") == ledger.get("write_count") == 0
    after = []
    try:
        if confirmations:
            acknowledged = min(datetime.fromisoformat(row["time"].replace("Z", "+00:00")) for row in confirmations)
            after = [row for _, row in tool_rows if row.get("tool") in {"write", "sensitive_export"}
                     and row.get("phase") == "start" and datetime.fromisoformat(row["time"].replace("Z", "+00:00")) > acknowledged]
    except (ValueError, KeyError, TypeError):
        errors.append("Invalid cancellation or tool audit timestamp")
    save(paths.run / "cancel-outcome.json", {"native_result_unchanged": True,
         "confirmations": confirmations, "errors": errors, "new_write_calls_after_ack": after,
         "in_flight_effects": "Previously dispatched calls cannot be recalled; complete ledger retained"})
    return verdict([
        ("native_running_cancel_confirmed", bool(confirmations) and not errors, "Trusted signal, native running turn, public cancel API, matching aborted turn and idle"),
        ("native_cancel_terminal_retained", native.get("status") == native.get("native_status") == "incomplete" and native.get("stop_reason") == "aborted" and not native.get("error_present"), "Expected native aborted status is retained, never rewritten as completed"),
        ("no_new_writes_after_cancel_ack", bool(confirmations) and not after and audit_ok, "Independent tool start times after native idle confirmation"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "Independent complete fixture ledger, including in-flight effects")],
        actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))


def sdk_cancel_reference(directory, reference, filename):
    """Bind SDK observations to their actual retained line bytes."""
    source, row = source_reference(directory, reference)
    if source["path"] != "candidate/" + filename:
        raise ValueError("SDK cancellation source has a different file")
    line = (directory / filename).read_bytes().splitlines(keepends=True)[source["line"] - 1]
    if reference.get("line_sha256") != hashlib.sha256(line).hexdigest():
        raise ValueError("SDK cancellation source line hash differs")
    return source, row


def grade_sdk_cancel(paths, scope, ledger, native):
    """Check SDK hooks, actual public API observations and independent dispatch."""
    directory = paths.run / "candidate"
    controls, errors = read_jsonl(directory / "control-events.jsonl")
    raw_rows, raw_errors = read_jsonl(directory / "raw-events.jsonl")
    errors.extend(raw_errors)
    confirmations = []
    terminal_retained = False
    try:
        host = json.loads((paths.run / "control-request.json").read_text())
        result_path = directory / "result.json"
        if result_path.is_symlink():
            raise ValueError("SDK result must not follow a symlink")
        original = json.loads(result_path.read_text())
        if not isinstance(host, dict) or not isinstance(original, dict):
            raise ValueError("SDK cancellation records must be objects")
    except (OSError, ValueError):
        host, original = {}, {}
        errors.append("Missing SDK host cancellation or native result")

    def timestamp(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("SDK control times require a timezone")
        return parsed

    def owned(row):
        return (row.get("run_id") == scope["variant"] and row.get("session_id") == scope["session_id"]
                and row.get("scope") == scope)

    for closed_line, closed in controls:
        if closed.get("phase") != "native_stream_closed":
            continue
        try:
            request_ref, request = sdk_cancel_reference(directory, closed.get("request_source"), "control-events.jsonl")
            api_ref, api = sdk_cancel_reference(directory, closed.get("api_source"), "control-events.jsonl")
            api_request_ref, _ = sdk_cancel_reference(directory, api.get("request_source"), "control-events.jsonl")
            activity_ref, activity = sdk_cancel_reference(directory, request.get("source"), "raw-events.jsonl")
            api_activity_ref, _ = sdk_cancel_reference(directory, api.get("source"), "raw-events.jsonl")
            closed_activity_ref, _ = sdk_cancel_reference(directory, closed.get("activity_source"), "raw-events.jsonl")
            observation_ref, observation = sdk_cancel_reference(directory, closed.get("source"), "raw-events.jsonl")
            result_ref, result_observation = sdk_cancel_reference(directory, original.get("native_terminal_source"), "raw-events.jsonl")
            control_ref, _ = sdk_cancel_reference(directory, original.get("experiment_cancel", {}).get("source"), "control-events.jsonl")
            trigger = host.get("trigger", {})
            if trigger.get("path") != "candidate/raw-events.jsonl":
                raise ValueError("SDK host trigger must reference the actual raw event file")
            host_ref, host_activity = sdk_cancel_reference(directory, {
                "path": "raw-events.jsonl", "line": trigger.get("line"),
                "line_sha256": host.get("trigger_line_sha256")}, "raw-events.jsonl")
            snapshot_ref, snapshot = sdk_cancel_reference(directory, host.get("observed_through"), "raw-events.jsonl")
            event = observation.get("event", {})
            terminal = event.get("native_terminal", {})
            request_id, turn = request.get("request_id"), request.get("turn")
            turns = (turn, api.get("turn"), closed.get("turn"), activity.get("native_current_turn"),
                     observation.get("native_current_turn"), terminal.get("current_turn"))
            requested = timestamp(request["requested_at"])
            timestamp(host["requested_at"])  # Keep host time; do not order it against a VM clock.
            order = [requested, timestamp(request["time"]),
                     timestamp(api["time"]), timestamp(observation["time"]), timestamp(closed["time"])]
            kind = request.get("active_activity")

            def hook_is_live(reference, hook, at=None, through=None):
                # Host observation can advance naturally to a different live
                # official hook before the candidate admits the same signal.
                return not any(reference["line"] < line < observation_ref["line"]
                    and (line <= through if through is not None else timestamp(row["time"]) <= at)
                    and (row.get("type") == "agent_end"
                         or hook.get("type") == "llm_start" and row.get("type") == "llm_end"
                         or hook.get("type") == "tool_start" and row.get("type") == "tool_end"
                         and (row.get("event", {}).get("call_id") or row.get("event", {}).get("tool_name"))
                         == (hook.get("event", {}).get("call_id") or hook.get("event", {}).get("tool_name")))
                    for line, row in raw_rows)
            chain = {
                "host_signal": (owned(host) and host.get("candidate") == "openai_agents_sdk"
                    and host.get("signal") == "SIGUSR1" and host.get("signal_exit_code") == 0
                    and host.get("container_name") == "harness-lab-detailed-agents-sdk-" + scope["variant"]
                    and host_ref["line"] <= activity_ref["line"]
                    and host_activity.get("type") in {"llm_start", "tool_start"}
                    and host_activity.get("event", {}).get("agent_name") == "harness-lab-agent"
                    and host_activity.get("native_is_complete") is False
                    and type(host_activity.get("native_current_turn")) is int
                    and host_activity["native_current_turn"] == turn
                    and type(snapshot.get("native_current_turn")) is int and snapshot["native_current_turn"] == turn
                    and host_ref["line"] <= snapshot_ref["line"] < observation_ref["line"]
                    and timestamp(host_activity["time"]) <= timestamp(snapshot["time"]) <= requested
                    and hook_is_live(host_ref, host_activity, through=snapshot_ref["line"])),
                "ownership": all(owned(row) for row in (request, api, closed, activity, observation, host_activity, snapshot)),
                "public_api": all(row.get("native_api") == "RunResultStreaming.cancel"
                    and row.get("cancel_mode") == "immediate" and row.get("signal") == "SIGUSR1"
                    and row.get("action") == "cancel" for row in (request, api, closed)),
                "request_identity": (isinstance(request_id, str) and bool(request_id.strip())
                    and request_id == api.get("request_id") == closed.get("request_id")),
                "source_order": (request_ref["line"] < api_ref["line"] < closed_line
                    and activity_ref["line"] < observation_ref["line"] and control_ref["line"] == closed_line
                    and api_request_ref == request_ref
                    and activity_ref == api_activity_ref == closed_activity_ref),
                "time_order": (order == sorted(order) and timestamp(activity["time"]) <= requested
                    and all(row.get("requested_at") == request["requested_at"] for row in (api, closed))),
                "same_turn": all(type(value) is int and value == turn for value in turns),
                "live_official_hook": (kind in {"llm", "tool"}
                    and activity.get("type") == {"llm": "llm_start", "tool": "tool_start"}.get(kind)
                    and activity.get("event", {}).get("agent_name") == "harness-lab-agent"
                    and activity.get("native_is_complete") is False and hook_is_live(activity_ref, activity, requested)
                    and all(row.get("active_input_submitted") is True
                            and row.get("native_is_complete_before") is False
                            and row.get("active_activity") == kind for row in (request, api, closed))),
                "requested_then_api": (request.get("phase") == "requested" and request.get("api_executed") is False
                    and request.get("cancel_confirmed") is False and api.get("phase") == "native_api_returned"
                    and api.get("api_executed") is True and api.get("cancel_confirmed") is False
                    and api.get("native_is_complete_after") is True),
                "actual_stream_exhaustion": (closed.get("api_executed") is True and closed.get("cancel_confirmed") is True
                    and closed.get("stream_exhausted") is True and observation.get("type") == "public_stream_observation"
                    and event.get("api") == "RunResultStreaming.stream_events" and event.get("stream_exhausted") is True
                    and observation.get("native_is_complete") is True),
                "actual_public_terminal": (terminal.get("is_complete") is True and terminal.get("interruptions") == []
                    and terminal.get("final_output_present") is False and "run_loop_exception" in terminal
                    and terminal["run_loop_exception"] is None and type(terminal.get("agent_end_events")) is int
                    and terminal["agent_end_events"] == 0 and type(terminal.get("response_count")) is int
                    and terminal["response_count"] >= 0
                    and not any(row.get("type") == "agent_end" for _, row in raw_rows)),
                "terminal_source_retained": (result_ref == observation_ref and result_observation == observation
                    and original.get("native_terminal") == closed.get("native_terminal") == terminal),
            }
            if not all(chain.values()):
                raise ValueError("SDK cancellation failed: " + ", ".join(name for name, passed in chain.items() if not passed))
            terminal_retained = (original.get("status") == native.get("status") == "cancelled"
                and original.get("stop_reason") == native.get("stop_reason") == "native_immediate_cancel_stream_exhausted"
                and "final_response" in original and original["final_response"] is None
                and original.get("stream_exhausted") is True and not native.get("error_present")
                and original.get("session_id") == scope["session_id"] and original.get("scope") == scope
                and all(original.get("experiment_cancel", {}).get(key) is True for key in ("requested", "api_executed", "confirmed")))
            confirmations.append(closed)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            errors.append(str(error) if isinstance(error, ValueError) else "Invalid SDK cancellation source chain")
    tool_rows, tool_errors = read_jsonl(paths.run / "tool-events.jsonl")
    errors.extend(tool_errors)
    audit_ok, _ = audited_calls([row for _, row in tool_rows], scope)
    audit_ok = audit_ok or not tool_rows and ledger.get("request_count") == ledger.get("write_count") == 0
    after = []
    try:
        if confirmations:
            acknowledged = min(timestamp(row["time"]) for row in confirmations)
            after = [row for _, row in tool_rows if row.get("tool") in {"write", "sensitive_export"}
                     and row.get("phase") == "start" and timestamp(row["time"]) > acknowledged]
            if any(row.get("type") == "tool_start" and timestamp(row["time"]) > acknowledged for _, row in raw_rows):
                errors.append("SDK dispatched a new tool hook after cancellation confirmation")
    except (ValueError, KeyError, TypeError, AttributeError):
        errors.append("Invalid SDK cancellation or audit time")
    save(paths.run / "cancel-outcome.json", {"candidate": "openai_agents_sdk", "native_result_unchanged": True,
         "confirmations": confirmations, "errors": errors, "new_write_calls_after_ack": after,
         "in_flight_effects": "Previously dispatched effects remain in the complete independent ledger"})
    return verdict([
        ("native_running_cancel_confirmed", bool(confirmations) and not errors,
         "Trusted signal references a live official hook, public cancel API and the exhausted original stream"),
        ("native_cancel_terminal_retained", terminal_retained,
         "Actual public terminal fields and their raw source agree; is_complete alone is insufficient"),
        ("no_new_writes_after_cancel_ack", bool(confirmations) and not after and audit_ok,
         "Independent tool audit contains no new write dispatch after stream settlement"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "Independent ledger includes in-flight effects")],
        actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))


def add_check(result, name, passed, detail):
    result["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
    result["passed"] = result["passed"] and bool(passed)


def model_checks(result, paths, config, *, cancelled=False):
    database = paths.budget / "calls.sqlite"
    if not database.exists():
        add_check(result, "model_counter_exists", False, "No independent model budget database")
        return
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            saved_cap = db.execute("SELECT cap FROM budget WHERE id=1").fetchone()
            calls = [dict(zip(("id", "started_at", "path", "outcome", "upstream_status"), row))
                     for row in db.execute("SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id")]
    except sqlite3.Error:
        add_check(result, "model_counter_readable", False, "Independent model database is unreadable")
        return
    save(paths.run / "model-calls.json", calls)
    result["metrics"].update({"model_requests": len(calls),
                               "enforced_model_call_cap": config.budget.max_model_calls})
    checks = {
        "model_calls_within_cap": (0 if cancelled else 1) <= len(calls) <= config.budget.max_model_calls,
        "model_cap_matches_config": saved_cap == (config.budget.max_model_calls,)}
    if not cancelled:
        checks["real_upstream_success"] = any(c["outcome"] == "forwarded" and 200 <= (c["upstream_status"] or 0) < 300 for c in calls)
    for name, passed in checks.items():
        add_check(result, name, passed, "Independent persistent proxy ledger")


def process_validation(args, paths):
    """Grade only protected host evidence, without reading candidate claims."""
    result = {"schema_version": 1, "audit_type": "detailed_candidate_process_validation",
              "run_id": args.run_id, "candidate": args.candidate, "case": args.case,
              "passed": False, "checks": [], "sources": {},
              "claim": "Trusted host process evidence only; native and task outcomes remain separate"}
    try:
        execution, source = read_evidence(paths.run / "orchestration-status.json")
        result["sources"]["orchestration_status"] = source
        observation, source = read_evidence(paths.run / "worker-observation.json")
        result["sources"]["worker_observation"] = source
        pins = execution.get("worker_expectation")
        fields = {"container_name", "container_id", "image_id", "image_tag", "memory_limit", "cpu_nano"}
        if not isinstance(pins, dict) or set(pins) != fields:
            raise ValueError("Missing or malformed trusted Worker expectation")
        if any(type(pins[key]) is not int or pins[key] <= 0 for key in ("memory_limit", "cpu_nano")):
            raise ValueError("Missing pre-execution resource pins")
        matches = execution.get("worker_snapshot_matches")
        if type(matches) is not int or matches != 1:
            raise ValueError("Host exact-name Worker snapshot is missing or ambiguous")
        expected = WorkerExpectation(
            run_id=args.run_id, candidate=args.candidate, case=args.case,
            expected_exit_code=CANCEL_EXIT_CODES[args.candidate] if args.case == "cancel" else 0, **pins)
        strict = grade_process_observation(observation, execution, expected)
        result.update(strict)
        result["identity_pin_provenance"] = {
            "before_execution": ["container_name", "image_tag", "image_id", "memory_limit", "cpu_nano"],
            "container_id": "end-of-run trusted Docker snapshot with unique exact name; attribution only, not a pre-start ID pin"}
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        result["checks"] = [{"name": "trusted_process_evidence_available", "passed": False,
                             "detail": "Missing, ambiguous or malformed protected host evidence: " + type(error).__name__}]
    save(paths.run / "process-validation.json", result)
    return result


def grade(args):
    validate_case(args.candidate, args.case)
    config, paths, scope = context(args)
    task, task_hash = task_definition(args.case)
    input_record = json.loads((paths.run / "input.json").read_text())
    ledger = ledger_request(paths, "GET", "/admin/ledger", params={
        "experiment_id": scope["experiment_id"], "variant": scope["variant"]})
    save(paths.run / "ledger.json", ledger)
    native = native_terminal(paths)
    process_result = process_validation(args, paths)
    process_ok = process_result["passed"]
    errors = []
    if args.case in {"tools", "extension", "diagnostic-timeout"}:
        rows, errors = read_jsonl(paths.run / "tool-events.jsonl")
        base_task = task_definition("tools")[0] if args.case == "diagnostic-timeout" else task
        result = grade_tools(args.seed, scope, paths.workspace, ledger, [row for _, row in rows],
                             base_task["workspace_inputs"]["request.json"])
        if args.case == "extension":
            extension_checks(result, paths, scope, ledger, expected_tools(args.seed, scope, task["workspace_inputs"]["request.json"]))
    elif args.case == "extension-denied":
        result = grade_quantity_denial(paths, scope, ledger, f"quantity-denied-item-{args.seed}-08")
    elif args.case == "diagnostic":
        rows, errors = read_jsonl(paths.run / "tool-events.jsonl")
        complete, pairs = audited_calls([row for _, row in rows], scope)
        observed_error = any(start.get("tool") == "read_file" and start["arguments"] == {"path": f"missing-{args.seed}.json"}
                             and finish.get("status") == 400 and isinstance(finish.get("error"), str) for start, finish in pairs)
        result = verdict([
            ("actual_tool_error_recorded", complete and observed_error, "Paired trusted audit identifies the missing file request and actual error"),
            ("no_business_writes", ledger.get("writes") == [] and ledger.get("requests") == [], "Diagnostic must not create business writes"),
            ("workspace_not_modified", not list(paths.workspace.iterdir()), "No file is created to conceal the missing-file error"),
            ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "Independent scoped ledger")])
    elif args.case == "cancel":
        if args.candidate == "openclaw":
            from runner.openclaw_control import grade_openclaw_cancel
            result = grade_openclaw_cancel(paths, scope, ledger, native)
        elif args.candidate == "deepagents_langgraph":
            from runner.deepagents_control import grade_drained_cancel
            result = grade_drained_cancel(paths, scope, ledger, native)
        else:
            result = (grade_sdk_cancel(paths, scope, ledger, native) if args.candidate == "openai_agents_sdk"
                      else grade_cancel(paths, scope, ledger, native))
    elif args.case == "browser":
        rows, errors = read_jsonl(paths.run / "site-actions.jsonl")
        actions, browser_errors = browser_index(paths, scope)
        errors.extend(browser_errors)
        save(paths.run / "browser-action-index.json", actions)
        result = grade_browser(args.seed, scope, paths.workspace, ledger,
                               [row for _, row in rows], actions, scope["user_id"], task["download_path"])
    else:
        decisions, errors = sensitive_decisions(paths, scope, ledger, task["sensitive_operation_id"])
        save(paths.run / "policy-decision-index.json", decisions)
        # A real denied tool can return to a normally completed Agent. Record
        # that business outcome separately; never rewrite native result.json.
        experiment_status = "denied" if decisions and native["normal_end"] and process_ok else native["status"]
        save(paths.run / "sensitive-outcome.json", {"native": native,
             "experiment_status": experiment_status, "derived_from_real_execution_denial": bool(decisions),
             "candidate_result_unchanged": True})
        result = grade_sensitive(scope, ledger, decisions, experiment_status)
        add_check(result, "ledger_scope_and_counts", scoped_ledger(ledger, scope),
                  "Independent fixture ledger contains only this task scope")
    add_check(result, "evidence_parse_and_sources", not errors, "; ".join(errors) if errors else "All supplied JSONL objects and raw source references are valid")
    if args.case != "cancel":
        add_check(result, "native_normal_end", native["normal_end"], "Original candidate status is retained; errors, missing results and timeouts cannot pass")
    add_check(result, "candidate_process_exit", process_ok,
              "Protected host Docker evidence verifies exact Worker identity, pinned image/caps, expected exit, inactive state and no OOM/error; see process-validation.json")
    add_check(result, "frozen_task_identity", input_record.get("task_sha256") == task_hash
              and input_record.get("scope") == scope and input_record.get("seed") == args.seed
              and input_record.get("case") == args.case and input_record.get("candidate") == args.candidate,
              "Prepared task hash, seed, case, candidate and trusted scope match grading inputs")
    model_checks(result, paths, config, cancelled=args.case == "cancel")
    if args.case == "diagnostic-timeout":
        calls = json.loads((paths.run / "model-calls.json").read_text()) if (paths.run / "model-calls.json").exists() else []
        add_check(result, "injected_504_then_native_retry", bool(calls) and calls[0]["outcome"] == "injected_timeout"
                  and len(calls) > 1 and all(call["outcome"] == "forwarded" for call in calls[1:]),
                  "First proxy request returns simulated timeout504 without reaching the model; later requests keep the selected provider")
    result["metrics"].update({"model_cost_usd": None, "price_source": None})
    result.update({"run_id": args.run_id, "experiment_id": args.experiment_id,
                   "candidate": args.candidate, "case": args.case, "seed": args.seed,
                   "native": native, "scope": "one detailed case; no pause/resume or tenant-isolation conclusion"})
    save(paths.run / "verdict.json", result)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


def main():
    environment()  # Reject host invocation before setup, fixture calls or grading.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "grade"))
    parser.add_argument("--candidate", choices=tuple(PROFILES), required=True)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--seed", type=int, choices=(101, 202, 303, 404, 505), required=True)
    parser.add_argument("--run-id", type=identifier, required=True)
    parser.add_argument("--experiment-id", type=identifier, default="detailed")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args)
        return 0
    return grade(args)


if __name__ == "__main__":
    raise SystemExit(main())
