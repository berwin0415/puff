"""在 Docker bench 中准备并独立判定详细用例。

本模块不运行 Agent。宿主机入口为 scripts/run_detailed.py；
所有 Fixture 初始化、任务输入、证据解释和判定均在此完成。"""
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
        raise ValueError("未知候选框架或详细用例")
    if case == "browser" and candidate not in BROWSER_CANDIDATES:
        raise ValueError(f"浏览器用例尚未接入：{candidate}；未运行工具替代方案")
    if case.startswith("extension") and candidate not in CASE_OPTIONS_CANDIDATES:
        raise ValueError(f"业务扩展尚未接入：{candidate}")
    if case == "cancel" and candidate not in CANCEL_EXIT_CODES:
        raise ValueError(f"原生取消触发器尚未接入：{candidate}")


def task_definition(case):
    path = TASK_ROOT / TASK_FILES[case]
    return yaml.safe_load(path.read_text()), hashlib.sha256(path.read_bytes()).hexdigest()


def private_json(path, value):
    # 测试凭证独立存放，绝不放入 input.json 或 stdout。
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write("\n")
    path.chmod(0o600)


def prepare(args):
    validate_case(args.candidate, args.case)
    config, paths, scope = context(args)
    task, task_hash = task_definition(args.case)
    if args.seed not in task["final_seeds"]:
        raise ValueError("种子不在冻结任务定义内")
    # 宿主机在启动 Fixture 服务前预留 fixture/。其余所有目录
    # 必须是新目录；独立运行不能继承旧状态。
    for directory in (paths.run, paths.state, paths.workspace, paths.budget):
        if directory.exists():
            raise FileExistsError("详细运行已存在，不能覆盖证据")
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
        raise RuntimeError("Fixture 未创建预期独立作用域")
    token = own[0]["token"]
    for path, value in ((paths.budget / "run.token", secrets.token_urlsafe(32)),
                        (paths.fixture / "scope.token", token)):
        with path.open("x") as stream:
            stream.write(value + "\n")
        path.chmod(0o600)
    private_json(paths.fixture / "scope.json", scope)
    # 此绑定仅挂载到可信 HTTP 工具服务，绝不
    # 挂载到候选框架。浏览器用例完全不启动该服务。
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
    """保留原始行号；格式错误的证据判失败，不能跳过。"""
    if not path.exists():
        return [], []
    if path.is_symlink() or not path.is_file():
        return [], ["证据不是普通文件：" + path.name]
    records, errors = [], []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("预期为对象")
        except ValueError:
            errors.append(f"JSON 对象无效，位置：{path.name}:{number}")
        else:
            records.append((number, row))
    return records, errors


def source_reference(candidate_dir, reference):
    """验证留存原始证据的引用，不修改其正文。"""
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ValueError("缺少原始事件来源")
    name = reference["path"]
    if name.startswith("/evidence/"):
        name = name[len("/evidence/"):]
    relative = Path(name)
    number = reference.get("line")
    if (relative.is_absolute() or ".." in relative.parts or relative.suffix != ".jsonl"
            or type(number) is not int or number < 1):
        raise ValueError("原始事件来源无效")
    source = candidate_dir / relative
    current = candidate_dir
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("原始事件来源不能跟随符号链接")
    rows, errors = read_jsonl(source)
    row = next((row for line, row in rows if line == number), None)
    if errors or row is None:
        raise ValueError("引用原始事件缺失或格式错误")
    return {"path": "candidate/" + relative.as_posix(), "line": number,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, row


def browser_index(paths, scope):
    """仅投影已记录且成功的原语，不推断页面操作。"""
    path = paths.run / "candidate/browser-actions.jsonl"
    rows, errors = read_jsonl(path)
    indexed = []
    used_sources = set()
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
    for number, row in rows:
        if not same_scope(row.get("scope", {}), scope):
            errors.append(f"浏览器作用域属于他人，位置 browser-actions.jsonl:{number}")
            continue
        if row.get("phase") != "finish" or type(row.get("status")) is not int:
            errors.append(f"浏览器完成事件无效，位置 browser-actions.jsonl:{number}")
            continue
        if not 200 <= row["status"] < 300:
            continue  # 失败调用保留原文，但不能证明动作已执行。
        if row.get("tool") not in BROWSER_TOOLS or not isinstance(row.get("call_id"), str):
            errors.append(f"无法识别浏览器原语，位置 browser-actions.jsonl:{number}")
            continue
        try:
            native, original = source_reference(paths.run / "candidate", row.get("source"))
            if not same_scope(original.get("scope", {}), scope):
                raise ValueError("引用原始事件属于不同作用域")
            if original.get("call_id") != row["call_id"]:
                raise ValueError("引用原始事件的调用 ID 不同")
            if not isinstance(original.get("native_tool"), str) or original["native_tool"] != row.get("native_tool"):
                raise ValueError("引用原始事件的原生工具不同或缺失")
            if original["native_tool"].split("__")[-1] != row["tool"]:
                raise ValueError("原生工具名与投影原语不匹配")
            identity = (native["path"], native["line"])
            if identity in used_sources:
                raise ValueError("同一原始响应被重复用于证明多个浏览器动作")
            if isinstance(original.get("message"), dict):
                # OpenClaw 保留 JSON-RPC 响应自身。
                message = original["message"]
                if original.get("direction") != "inbound" or "error" in message or not isinstance(message.get("result"), dict) or message["result"].get("isError"):
                    raise ValueError("引用 MCP 响应不是成功工具结果")
            elif original.get("phase") == "finish" and original.get("status") == 200:
                # DSH 保留官方 MCP 客户端的规范结果。
                if not isinstance(original.get("result"), dict) or original["result"].get("isError"):
                    raise ValueError("引用原生 MCP 完成记录不是成功工具结果")
            else:
                raise ValueError("引用事件不是已记录的成功 MCP 响应")
            used_sources.add(identity)
        except (OSError, ValueError) as exc:
            errors.append(f"原始来源无效，位置 browser-actions.jsonl:{number}: {exc}")
            continue
        indexed.append({"scope": row["scope"], "tool": row["tool"], "call_id": row["call_id"],
                        "phase": row["phase"], "status": row["status"],
                        "source": {"path": "candidate/browser-actions.jsonl", "line": number, "sha256": digest},
                        "native_source": native})
    return indexed, errors


def sensitive_decisions(paths, scope, ledger, operation_id):
    """映射 Fixture 的真实拒绝，或保留候选框架真实的策略决定。"""
    tool_path = paths.run / "tool-events.jsonl"
    tool_rows, errors = read_jsonl(tool_path)
    events = [row for _, row in tool_rows]
    audit_ok, pairs = audited_calls(events, scope)
    if tool_rows and not audit_ok:
        errors.append("敏感工具审计包含不完整调用或他人作用域")
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
            errors.append(f"候选策略决定无效，位置 policy-decisions.jsonl:{number}")
    return decisions, errors


def native_terminal(paths):
    """即使实验推导出拒绝，也保留候选框架的原始状态。"""
    path = paths.run / "candidate/result.json"
    try:
        if path.is_symlink():
            raise ValueError("候选结果必须是普通文件")
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError("候选结果必须是对象")
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
    """将诊断投影与留存的原生状态字节核对。"""
    projection = candidate_dir / "business-state.json"
    if projection.is_symlink():
        raise ValueError("业务投影不能跟随链接")
    record = json.loads(projection.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("source"), dict):
        raise ValueError("业务投影必须包含原生来源对象")
    source, state = record.get("source", {}), record.get("business_state")
    name = source.get("path")
    if not same_scope(record.get("scope", {}), scope) or not isinstance(name, str):
        raise ValueError("业务投影缺少可信作用域或原生来源")
    relative = Path(name.removeprefix("/evidence/"))
    if relative.is_absolute() or ".." in relative.parts or relative.suffix not in {".json", ".jsonl"}:
        raise ValueError("原生业务来源路径无效")
    current = candidate_dir
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("原生业务来源不能跟随链接")
    data = current.read_bytes()
    if source.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("原生业务来源哈希不匹配")
    if relative.suffix == ".jsonl":
        line = source.get("line")
        if type(line) is not int or not 1 <= line <= len(data.splitlines()):
            raise ValueError("原生业务来源行无效")
        native = json.loads(data.splitlines()[line - 1])
    else:
        native = json.loads(data)
    pointer = source.get("json_pointer")
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("必须提供原生业务 JSON 指针")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        native = native[int(key)] if isinstance(native, list) else native[key]
    if (native != state or not isinstance(state, dict) or state.get("schema_version") != 1
            or not isinstance(state.get("operations"), dict)
            or type(state.get("receipt_confirmed")) is not bool):
        raise ValueError("业务投影与留存原生状态不一致")
    return state


def extension_checks(result, paths, scope, ledger, expected):
    """要求真实的新工具调用、真实回执和原生业务字段。"""
    wanted = {row["operation_id"] for row in expected["selected"]}
    state, error = {}, None
    try:
        state = business_projection(paths.run / "candidate", scope)
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        error = "原生业务状态来源缺失或不一致"
    add_check(result, "native_business_state_source", not error, error or "哈希及 JSON 指针匹配原生快照字节")
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
              "每个预期操作只根据当前作用域内真实回执确认")
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
              "显式 query_order_status 调用匹配独立回执工具结果")


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
                raise ValueError("业务规则参数必须是对象")
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
            errors.append(f"业务规则来源无效，行号 {number}")
    save(paths.run / "business-rule-index.json", decisions)
    return verdict([
        ("real_execution_rule_denial", bool(decisions) and not errors, "真实 quantity=4 尝试在 HTTP 写入前被拒绝"),
        ("no_business_side_effect_or_write_request", ledger.get("writes") == [] and ledger.get("requests") == [], "被拒绝写入及替代写入均未到达业务服务"),
        ("no_http_tool_write_dispatch", not any(row.get("tool") == "write" for _, row in http_rows),
         "独立工具服务未收到写入，包含无效或被拒绝调用"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "独立 Fixture 账本作用域及计数一致"),
    ], actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))


def grade_cancel(paths, scope, ledger, native):
    """只有独立发出取消请求，才接受原生 aborted 轮次。"""
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
            errors.append("原生取消来源链无效")
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
        errors.append("取消或工具审计时间戳无效")
    save(paths.run / "cancel-outcome.json", {"native_result_unchanged": True,
         "confirmations": confirmations, "errors": errors, "new_write_calls_after_ack": after,
         "in_flight_effects": "已分派调用无法撤回；保留完整账本"})
    return verdict([
        ("native_running_cancel_confirmed", bool(confirmations) and not errors, "可信信号、原生运行轮次、公共取消 API、匹配 aborted 轮次及 idle 相符"),
        ("native_cancel_terminal_retained", native.get("status") == native.get("native_status") == "incomplete" and native.get("stop_reason") == "aborted" and not native.get("error_present"), "保留预期原生 aborted 状态，绝不改写为 completed"),
        ("no_new_writes_after_cancel_ack", bool(confirmations) and not after and audit_ok, "检查原生 idle 确认后的独立工具启动时间"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "保留独立完整 Fixture 账本，包含在途副作用")],
        actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))


def sdk_cancel_reference(directory, reference, filename):
    """将 SDK 观测与实际留存行字节绑定。"""
    source, row = source_reference(directory, reference)
    if source["path"] != "candidate/" + filename:
        raise ValueError("SDK 取消来源文件不同")
    line = (directory / filename).read_bytes().splitlines(keepends=True)[source["line"] - 1]
    if reference.get("line_sha256") != hashlib.sha256(line).hexdigest():
        raise ValueError("SDK 取消来源行哈希不同")
    return source, row


def grade_sdk_cancel(paths, scope, ledger, native):
    """检查 SDK 钩子、真实公共 API 观测和独立分派证据。"""
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
            raise ValueError("SDK 结果不能跟随符号链接")
        original = json.loads(result_path.read_text())
        if not isinstance(host, dict) or not isinstance(original, dict):
            raise ValueError("SDK 取消记录必须为对象")
    except (OSError, ValueError):
        host, original = {}, {}
        errors.append("缺少 SDK 宿主机取消记录或原生结果")

    def timestamp(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("SDK 控制时间必须包含时区")
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
                raise ValueError("SDK 宿主机触发器必须引用真实原始事件文件")
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
            timestamp(host["requested_at"])  # 保留宿主机时间，不与 VM 时钟排序。
            order = [requested, timestamp(request["time"]),
                     timestamp(api["time"]), timestamp(observation["time"]), timestamp(closed["time"])]
            kind = request.get("active_activity")

            def hook_is_live(reference, hook, at=None, through=None):
                # 宿主机观测可能自然前进到另一个运行中的
                # 官方钩子，早于候选框架接受同一信号。
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
                raise ValueError("SDK 取消失败：" + ", ".join(name for name, passed in chain.items() if not passed))
            terminal_retained = (original.get("status") == native.get("status") == "cancelled"
                and original.get("stop_reason") == native.get("stop_reason") == "native_immediate_cancel_stream_exhausted"
                and "final_response" in original and original["final_response"] is None
                and original.get("stream_exhausted") is True and not native.get("error_present")
                and original.get("session_id") == scope["session_id"] and original.get("scope") == scope
                and all(original.get("experiment_cancel", {}).get(key) is True for key in ("requested", "api_executed", "confirmed")))
            confirmations.append(closed)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            errors.append(str(error) if isinstance(error, ValueError) else "SDK 取消来源链无效")
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
                errors.append("SDK 在取消确认后分派了新工具钩子")
    except (ValueError, KeyError, TypeError, AttributeError):
        errors.append("SDK 取消或审计时间无效")
    save(paths.run / "cancel-outcome.json", {"candidate": "openai_agents_sdk", "native_result_unchanged": True,
         "confirmations": confirmations, "errors": errors, "new_write_calls_after_ack": after,
         "in_flight_effects": "已分派副作用保留在完整独立账本中"})
    return verdict([
        ("native_running_cancel_confirmed", bool(confirmations) and not errors,
         "可信信号引用运行中的官方钩子、公共取消 API 及已读尽的原始流"),
        ("native_cancel_terminal_retained", terminal_retained,
         "真实公共终态字段与原始来源相符；仅有 is_complete 不足以证明"),
        ("no_new_writes_after_cancel_ack", bool(confirmations) and not after and audit_ok,
         "独立工具审计中，流结束后没有新的写入分派"),
        ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "独立账本包含在途副作用")],
        actual_writes=ledger.get("write_count"), write_requests=ledger.get("request_count"))


def add_check(result, name, passed, detail):
    result["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
    result["passed"] = result["passed"] and bool(passed)


def model_checks(result, paths, config, *, cancelled=False):
    database = paths.budget / "calls.sqlite"
    if not database.exists():
        add_check(result, "model_counter_exists", False, "缺少独立模型预算数据库")
        return
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            saved_cap = db.execute("SELECT cap FROM budget WHERE id=1").fetchone()
            calls = [dict(zip(("id", "started_at", "path", "outcome", "upstream_status"), row))
                     for row in db.execute("SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id")]
    except sqlite3.Error:
        add_check(result, "model_counter_readable", False, "无法读取独立模型数据库")
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
        add_check(result, name, passed, "独立持久化代理账本")


def process_validation(args, paths):
    """仅判定受保护的宿主机证据，不读取候选框架的自述。"""
    result = {"schema_version": 1, "audit_type": "detailed_candidate_process_validation",
              "run_id": args.run_id, "candidate": args.candidate, "case": args.case,
              "passed": False, "checks": [], "sources": {},
              "claim": "仅为可信宿主机进程证据；原生结果与任务结果分别判定"}
    try:
        execution, source = read_evidence(paths.run / "orchestration-status.json")
        result["sources"]["orchestration_status"] = source
        observation, source = read_evidence(paths.run / "worker-observation.json")
        result["sources"]["worker_observation"] = source
        pins = execution.get("worker_expectation")
        fields = {"container_name", "container_id", "image_id", "image_tag", "memory_limit", "cpu_nano"}
        if not isinstance(pins, dict) or set(pins) != fields:
            raise ValueError("可信 Worker 预期缺失或格式错误")
        if any(type(pins[key]) is not int or pins[key] <= 0 for key in ("memory_limit", "cpu_nano")):
            raise ValueError("缺少执行前资源固定值")
        matches = execution.get("worker_snapshot_matches")
        if type(matches) is not int or matches != 1:
            raise ValueError("宿主机精确名称 Worker 快照缺失或含糊")
        expected = WorkerExpectation(
            run_id=args.run_id, candidate=args.candidate, case=args.case,
            expected_exit_code=CANCEL_EXIT_CODES[args.candidate] if args.case == "cancel" else 0, **pins)
        strict = grade_process_observation(observation, execution, expected)
        result.update(strict)
        result["identity_pin_provenance"] = {
            "before_execution": ["container_name", "image_tag", "image_id", "memory_limit", "cpu_nano"],
            "container_id": "运行结束时的可信 Docker 快照具有唯一精确名称；仅用于归属，不是启动前 ID 固定证据"}
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        result["checks"] = [{"name": "trusted_process_evidence_available", "passed": False,
                             "detail": "受保护宿主机证据缺失、含糊或格式错误：" + type(error).__name__}]
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
            ("actual_tool_error_recorded", complete and observed_error, "配对可信审计标识缺失文件请求及真实错误"),
            ("no_business_writes", ledger.get("writes") == [] and ledger.get("requests") == [], "诊断不得产生业务写入"),
            ("workspace_not_modified", not list(paths.workspace.iterdir()), "不得创建文件来掩盖缺失文件错误"),
            ("ledger_scope_and_counts", scoped_ledger(ledger, scope), "独立作用域账本")])
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
        # 真实工具拒绝后，Agent 仍可能正常完成。单独记录
        # 这一业务结果，绝不改写原生 result.json。
        experiment_status = "denied" if decisions and native["normal_end"] and process_ok else native["status"]
        save(paths.run / "sensitive-outcome.json", {"native": native,
             "experiment_status": experiment_status, "derived_from_real_execution_denial": bool(decisions),
             "candidate_result_unchanged": True})
        result = grade_sensitive(scope, ledger, decisions, experiment_status)
        add_check(result, "ledger_scope_and_counts", scoped_ledger(ledger, scope),
                  "独立 Fixture 账本仅包含当前任务作用域")
    add_check(result, "evidence_parse_and_sources", not errors, "; ".join(errors) if errors else "提供的全部 JSONL 对象和原始来源引用均有效")
    if args.case != "cancel":
        add_check(result, "native_normal_end", native["normal_end"], "保留候选原始状态；错误、结果缺失和超时不能通过")
    add_check(result, "candidate_process_exit", process_ok,
              "受保护宿主机 Docker 证据验证精确 Worker 身份、固定镜像和额度、预期退出、非活跃状态及无 OOM/错误；详见 process-validation.json")
    add_check(result, "frozen_task_identity", input_record.get("task_sha256") == task_hash
              and input_record.get("scope") == scope and input_record.get("seed") == args.seed
              and input_record.get("case") == args.case and input_record.get("candidate") == args.candidate,
              "准备任务哈希、种子、用例、候选框架和可信作用域匹配判定输入")
    model_checks(result, paths, config, cancelled=args.case == "cancel")
    if args.case == "diagnostic-timeout":
        calls = json.loads((paths.run / "model-calls.json").read_text()) if (paths.run / "model-calls.json").exists() else []
        add_check(result, "injected_504_then_native_retry", bool(calls) and calls[0]["outcome"] == "injected_timeout"
                  and len(calls) > 1 and all(call["outcome"] == "forwarded" for call in calls[1:]),
                  "首次代理请求返回模拟 timeout504，未到达模型；后续请求保留选定供应商")
    result["metrics"].update({"model_cost_usd": None, "price_source": None})
    result.update({"run_id": args.run_id, "experiment_id": args.experiment_id,
                   "candidate": args.candidate, "case": args.case, "seed": args.seed,
                   "native": native, "scope": "仅覆盖一个详细用例；不对暂停、恢复或租户隔离作结论"})
    save(paths.run / "verdict.json", result)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


def main():
    environment()  # 在初始化、调用 Fixture 或判定之前拒绝宿主机直接调用。
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
