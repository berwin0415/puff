"""纯 I24 证据判定；采集和鉴权由测试后端负责。

``expectation`` 必须由可信控制器固定；``sources`` 必须来自候选框架可写挂载之外
的后端、原生、文件系统和 Fixture 采集。仅有来源名称或哈希不能建立这种信任。
此模块不执行 I/O、不导入候选框架，也不修改其结果。
有限契约详见 docs/validation/scope-evidence.md。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import PurePosixPath
import re


OPERATIONS = frozenset({"state", "events", "artifacts", "pause", "resume", "cancel"})
CONTROLS = frozenset({"pause", "resume", "cancel"})
USERS = frozenset({"user-a", "user-b"})
STREAMS = frozenset({"backend", "native", "files", "ledger"})
SCOPE_FIELDS = frozenset({"experiment_id", "variant", "user_id", "session_id"})
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_BINDING_FIELDS = frozenset({
    "binding_id", "phase", "principal_id", "credential_id", "scope", "task_id",
    "native_ids", "state_dir", "workspace", "worker_id", "connection_id",
    "credential_fingerprint", "browser_account", "sentinel", "artifacts",
})


def evidence_sha256(record: dict) -> str:
    """对留存 JSON 值计算哈希，不对可变的美化输出表示计算哈希。"""
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _require(value, message):
    if not value:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _time(record):
    parsed = datetime.fromisoformat(record["time"].replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "采集时间必须包含时区")
    return parsed


def _absolute(value):
    return (_text(value) and PurePosixPath(value).is_absolute()
            and ".." not in PurePosixPath(value).parts and "\x00" not in value)


def _separate(left, right):
    left, right = PurePosixPath(left), PurePosixPath(right)
    return left != right and left not in right.parents and right not in left.parents


def _validate_plan(expectation):
    _require(isinstance(expectation, dict) and type(expectation.get("schema_version")) is int and expectation["schema_version"] == 1,
             "必须提供单独固定且 schema_version=1 的预期")
    phases = expectation["phases"]
    _require(isinstance(phases, list) and all(_text(phase) for phase in phases) and len(phases) == len(set(phases))
             and {"concurrent", "reconnect"} <= set(phases), "必须包含 concurrent 和 reconnect 阶段")
    reuse = expectation["reuse"]
    _require(isinstance(reuse, dict) and type(reuse.get("supported")) is bool,
             "必须明确是否支持 Worker 复用")
    if reuse["supported"]:
        _require({"reuse_b", "transfer_a"} <= set(phases), "支持复用时必须提供 A 到 B 再到 A 的观测")
    else:
        _require(_text(reuse.get("reason")), "不支持复用时必须提供实际观测到的限制")
    bindings = {}
    for binding in expectation["bindings"]:
        _require(isinstance(binding, dict) and set(binding) == _BINDING_FIELDS, "可信绑定不完整")
        ident, scope = binding["binding_id"], binding["scope"]
        _require(_text(ident) and ident not in bindings and binding["phase"] in phases, "绑定重复或无效")
        _require(isinstance(scope, dict) and set(scope) == SCOPE_FIELDS
                 and all(_text(value) for value in scope.values()) and scope["user_id"] in USERS,
                 "绑定必须包含全部四个作用域字段")
        _require(all(_text(binding[key]) for key in ("principal_id", "credential_id", "task_id", "worker_id",
                                                    "connection_id", "browser_account", "sentinel")),
                 "绑定身份不完整")
        ids = binding["native_ids"]
        _require(isinstance(ids, dict) and bool(ids) and all(_text(k) and _text(v) for k, v in ids.items()),
                 "必须提供精确原生 session/run 标识")
        _require(_absolute(binding["state_dir"]) and _absolute(binding["workspace"])
                 and _separate(binding["state_dir"], binding["workspace"]), "state/workspace 绑定无效")
        _require(_sha(binding["credential_fingerprint"]), "必须提供合成凭证指纹")
        artifacts = binding["artifacts"]
        _require(isinstance(artifacts, list) and bool(artifacts), "必须提供非空固定产物")
        paths = []
        for item in artifacts:
            _require(isinstance(item, dict) and set(item) == {"path", "sha256"}
                     and _text(item["path"]) and not PurePosixPath(item["path"]).is_absolute()
                     and ".." not in PurePosixPath(item["path"]).parts and _sha(item["sha256"]),
                     "产物必须包含相对路径和内容摘要")
            paths.append(item["path"])
        _require(len(paths) == len(set(paths)), "产物路径重复")
        bindings[ident] = binding
    _require(bool(bindings), "缺少可信绑定")
    by_user = {user: [b for b in bindings.values() if b["scope"]["user_id"] == user] for user in USERS}
    for user, values in by_user.items():
        _require({b["phase"] for b in values} == set(phases), "两个所有者均须覆盖每个阶段")
        # 重连或重新绑定可能替换原生运行 ID、Worker 和连接。
        # 控制器仍固定原始所有者、任务和持久化位置。
        for key in ("principal_id", "credential_id", "scope", "task_id", "state_dir", "workspace",
                    "credential_fingerprint", "browser_account", "sentinel", "artifacts"):
            _require(all(b[key] == values[0][key] for b in values), "持久化任务归属发生变化")
    a, b = by_user["user-a"][0], by_user["user-b"][0]
    for key in ("principal_id", "credential_id", "task_id", "credential_fingerprint", "browser_account", "sentinel"):
        _require(a[key] != b[key], "A 和 B 必须具有不同身份和哨兵标记")
    _require(all(_separate(a[x], b[y]) for x in ("state_dir", "workspace") for y in ("state_dir", "workspace")),
             "与他人的 state/workspace 位置重叠")
    # 原生标识只在可信存储/作用域绑定内有效。两个
    # 独立状态存储都可能暴露 session_id="main"，或使用
    # 相同 Agent 配置名；仅凭字符串不能定位任务。
    # 上方要求不同的持久化位置和已鉴权所有者；
    # 下方每个来源都必须保留完整且精确的目标绑定。
    probes, matrix = {}, Counter()
    for probe in expectation["probes"]:
        _require(isinstance(probe, dict) and set(probe) == {"probe_id", "phase", "actor_binding", "target_binding",
                                                         "result_binding", "operation"}, "固定探针不完整")
        ident = probe["probe_id"]
        _require(_text(ident) and ident not in probes and probe["operation"] in OPERATIONS, "探针重复或无效")
        actor, target, result = (bindings[probe[key]] for key in ("actor_binding", "target_binding", "result_binding"))
        _require(all(x["phase"] == probe["phase"] for x in (actor, target, result)), "探针跨越了阶段")
        _require(all(target[k] == result[k] for k in _BINDING_FIELDS - {"binding_id", "native_ids", "worker_id", "connection_id"}),
                 "控制结果重新绑定了另一任务")
        actor_user, target_user = actor["scope"]["user_id"], target["scope"]["user_id"]
        _require(actor_user == target_user or (actor_user, target_user) == ("user-a", "user-b"), "非预期身份探针")
        if actor_user != target_user or probe["operation"] != "resume":
            _require(target == result, "只有已授权 resume 可以重新绑定目标实例")
        matrix[(probe["phase"], actor_user, target_user, probe["operation"])] += 1
        probes[ident] = probe
    wanted = Counter((phase, actor, target, operation) for phase in phases
                     for actor, target in (("user-a", "user-a"), ("user-b", "user-b"), ("user-a", "user-b"))
                     for operation in OPERATIONS)
    _require(matrix == wanted, "每个阶段须包含 A/B 各六类操作正向验证，以及 A 对已存在 B 的全部拒绝验证")
    for user in USERS:
        current = next(x for x in by_user[user] if x["phase"] == "concurrent")
        reconnected = next(x for x in by_user[user] if x["phase"] == "reconnect")
        _require(current["connection_id"] != reconnected["connection_id"], "重连必须使用新的后端连接")
    if reuse["supported"]:
        initial_a = next(x for x in by_user["user-a"] if x["phase"] == "concurrent")
        reused_b = next(x for x in by_user["user-b"] if x["phase"] == "reuse_b")
        transferred_a = next(x for x in by_user["user-a"] if x["phase"] == "transfer_a")
        _require(initial_a["worker_id"] == reused_b["worker_id"] != transferred_a["worker_id"],
                 "复用须证明 B 使用 A 已释放的 Worker，而 A 使用另一个 Worker")
    return bindings, probes


def _checkpoint(value):
    return (isinstance(value, dict) and set(value) == {"id", "sha256"}
            and _text(value["id"]) and _sha(value["sha256"]))


def grade_scope_observations(*, expectation: dict, sources: dict, attempts: list[dict]) -> dict:
    """验证有来源绑定的作用域探针，不接受旧式纯断言记录。

    202 响应可以表示已接收，但控制操作只有同时具备可追溯的原生分派、
    实际原生终态和独立后置状态才通过。能力证据失败或缺失时判失败，不抛异常。"""
    checks, errors = [], []
    try:
        bindings, probes = _validate_plan(expectation)
        _require(isinstance(sources, dict) and set(sources) == STREAMS
                 and all(isinstance(rows, list) and bool(rows) and len(rows) <= 100000
                         and all(isinstance(row, dict) for row in rows) for rows in sources.values()),
                 "必须提供四条非空可信采集流")
        _require(isinstance(attempts, list) and all(isinstance(a, dict) for a in attempts), "必须提供尝试引用")
        ids = [a.get("probe_id") for a in attempts]
        _require(len(ids) == len(set(ids)) and set(ids) == set(probes), "尝试与固定的完整探针矩阵不匹配")
        registration_rows = [row for row in sources["backend"] if row.get("kind") == "registration"]
        registrations = [row.get("binding") for row in registration_rows]
        _require(all(registrations.count(binding) == 1 for binding in bindings.values()),
                 "每个目标实例均须有一次独立后端归属登记")
        ownership = {binding["binding_id"]: next(row for row in registration_rows if row.get("binding") == binding)
                     for binding in bindings.values()}
        for row in ownership.values():
            _time(row)
        checks.append(("trusted_plan_and_matrix", True, "固定 A/B 身份、原生 ID、路径、阶段及每阶段全部 18 个探针"))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {"passed": False, "checks": [{"name": "trusted_plan_and_matrix", "passed": False,
                "detail": "可信计划/采集矩阵缺失、格式错误或不完整"}], "errors": ["可信作用域证据契约无效"]}

    def source(ref, stream, kind, probe_id=None, binding=None):
        _require(isinstance(ref, dict) and set(ref) == {"stream", "line", "sha256"}
                 and ref["stream"] == stream and type(ref["line"]) is int and 1 <= ref["line"] <= len(sources[stream]),
                 "来源引用无效")
        row = sources[stream][ref["line"] - 1]
        _require(ref["sha256"] == evidence_sha256(row) and row.get("kind") == kind, "来源摘要或种类不匹配")
        _time(row)
        if probe_id is not None:
            _require(row.get("request_id") == probe_id, "采集属于另一请求")
        if binding is not None:
            _require(row.get("binding") == binding, "采集绑定与可信归属不一致")
        return row

    def snapshot(ref, binding, ident):
        row = source(ref, "native", "snapshot", ident, binding)
        stable = row.get("stable")
        _require(row.get("exists") is True and row.get("status") in {"running", "paused", "cancelled"},
                 "未证明目标存在或其原生状态")
        _require(isinstance(stable, dict) and set(stable) == {"sentinel", "checkpoint_owner", "checkpoint",
                                                            "credential_fingerprint", "browser_account"}
                 and stable["sentinel"] == binding["sentinel"] and stable["checkpoint_owner"] == binding["scope"]
                 and _checkpoint(stable["checkpoint"]) and stable["credential_fingerprint"] == binding["credential_fingerprint"]
                 and stable["browser_account"] == binding["browser_account"], "原生受保护状态缺失或属于他人")
        controls = row.get("control_request_ids")
        _require(isinstance(controls, list) and all(_text(v) for v in controls) and len(controls) == len(set(controls)),
                 "必须提供完整原生控制日志")
        _require(type(row.get("event_cursor")) is int and row["event_cursor"] >= 0, "缺少原生事件游标")
        domain = row.get("event_cursor_domain")
        common = {"scope": binding["scope"], "task_id": binding["task_id"]}
        _require(domain == {"kind": "task", **common}
                 or domain == {"kind": "native_run", **common, "native_ids": binding["native_ids"]},
                 "原生事件游标域缺失或属于他人")
        return row

    def files(ref, binding, ident):
        row = source(ref, "files", "snapshot", ident, binding)
        wanted = [{**item, "scope": binding["scope"]} for item in binding["artifacts"]]
        _require(row.get("items") == wanted, "独立非空文件、内容或归属不一致")
        return row

    def ledger(ref, binding, ident):
        row = source(ref, "ledger", "snapshot", ident, binding)
        value = row.get("ledger")
        _require(isinstance(value, dict) and _text(row.get("ledger_id"))
                 and all(isinstance(value.get(k), list) for k in ("writes", "requests", "faults"))
                 and type(value.get("write_count")) is int and value["write_count"] == len(value["writes"])
                 and type(value.get("request_count")) is int and value["request_count"] == len(value["requests"])
                 and all(isinstance(item, dict) and all(item.get(k) == v for k, v in binding["scope"].items())
                         for key in ("writes", "requests", "faults") for item in value[key]), "Fixture 账本缺失、不完整或属于他人")
        return row

    attempts_by_id = {attempt["probe_id"]: attempt for attempt in attempts}
    for attempt in attempts:
        ident = attempt["probe_id"]
        probe = probes[ident]
        actor, target, result = (bindings[probe[key]] for key in ("actor_binding", "target_binding", "result_binding"))
        operation = probe["operation"]
        cross = actor["scope"]["user_id"] != target["scope"]["user_id"]
        try:
            request = source(attempt["request"], "backend", "request", ident)
            decision = source(attempt["authorization"], "backend", "authorization", ident)
            response = source(attempt["response"], "backend", "response", ident)
            before, after = (snapshot(attempt[key], binding, ident) for key, binding in (("before", target), ("after", result)))
            before_files, after_files = (files(attempt[key], binding, ident) for key, binding in (("files_before", target), ("files_after", result)))
            before_ledger, after_ledger = (ledger(attempt[key], binding, ident) for key, binding in (("ledger_before", target), ("ledger_after", result)))
            auth = {"principal_id": actor["principal_id"], "credential_id": actor["credential_id"], "authenticated": True}
            _require(request.get("auth") == auth and request.get("target_binding") == target["binding_id"]
                     and request.get("operation") == operation and request.get("phase") == probe["phase"],
                     "请求身份或目标与已鉴权控制器输入不一致")
            _require(decision.get("auth") == auth and decision.get("target_binding") == target["binding_id"]
                     and decision.get("operation") == operation, "授权未绑定此身份和已有目标")
            _require(max(_time(x) for x in (before, before_files, before_ledger)) <= _time(request)
                     <= _time(decision) <= _time(response)
                     <= min(_time(x) for x in (after, after_files, after_ledger)), "探针采集未包围真实响应")
            _require(_time(ownership[target["binding_id"]]) <= _time(before)
                     and _time(ownership[result["binding_id"]]) <= _time(after), "归属在观测之后才登记")
            _require(before_ledger["ledger_id"] == after_ledger["ledger_id"], "Fixture 账本被替换")
            resume_lineage_verified = False
            native_controls = [row for row in sources["native"] if row.get("request_id") == ident
                               and row.get("kind") in {"control_dispatch", "control_terminal"}]
            legal_before = "paused" if operation == "resume" else "running" if operation in CONTROLS else None
            if legal_before:
                _require(before["status"] == legal_before, "控制在无效原生状态下测试")
            if operation == "resume":
                owner_pause = next(p for p in probes.values() if p["phase"] == probe["phase"] and p["operation"] == "pause"
                                   and bindings[p["actor_binding"]]["scope"]["user_id"] == target["scope"]["user_id"]
                                   == bindings[p["target_binding"]]["scope"]["user_id"])
                pause_attempt = attempts_by_id[owner_pause["probe_id"]]
                pause_result_binding = bindings[owner_pause["result_binding"]]
                pause_terminal = source(pause_attempt["native_terminal"], "native", "control_terminal",
                                        owner_pause["probe_id"], pause_result_binding)
                _require(pause_result_binding["native_ids"] == target["native_ids"]
                         and pause_terminal.get("operation") == "pause"
                         and pause_terminal.get("result", {}).get("status") == "paused"
                         and pause_terminal.get("result", {}).get("checkpoint") == before["stable"]["checkpoint"]
                         and owner_pause["probe_id"] in before["control_request_ids"]
                         and _time(pause_terminal) <= _time(before), "resume 未沿用此所有者真实暂停检查点")
            if cross:
                _require(decision.get("decision") == "deny" and decision.get("reason") == "scope_mismatch"
                         and type(response.get("status")) is int and response["status"] == 403
                         and response.get("body") == {"error": {"code": "scope_denied"}}, "拒绝不是不泄露内容的作用域拒绝")
                _require(not native_controls and before["control_request_ids"] == after["control_request_ids"],
                         "被拒绝请求到达了原生控制")
                _require(before["status"] == after["status"] and before["stable"] == after["stable"]
                         and before_files["items"] == after_files["items"] and before_ledger["ledger"] == after_ledger["ledger"],
                         "被拒绝请求改变了目标受保护状态、文件或账本")
            else:
                _require(decision.get("decision") == "allow" and decision.get("reason") == "owner", "所有者未获授权")
                common = {"task_id": target["task_id"], "scope": target["scope"], "native_ids": target["native_ids"]}
                if operation in CONTROLS:
                    _require(type(response.get("status")) is int and
                             ((response["status"] in {200, 202} and response.get("body") ==
                               {"request_id": ident, "task_id": target["task_id"], "accepted": True})
                              or (response["status"] == 204 and response.get("body") is None)),
                             "所有者控制缺少匹配的接收确认")
                    dispatch = source(attempt["native_dispatch"], "native", "control_dispatch", ident, target)
                    terminal = source(attempt["native_terminal"], "native", "control_terminal", ident, result)
                    status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[operation]
                    native_result = terminal.get("result")
                    _require(len(native_controls) == 2 and dispatch in native_controls and terminal in native_controls
                             and dispatch.get("operation") == terminal.get("operation") == operation
                             and _time(decision) <= _time(dispatch) <= _time(terminal) <= _time(after),
                             "所有者控制的分派或终态来源链不完整")
                    _require(isinstance(native_result, dict) and native_result.get("status") == status
                             and native_result.get("native_ids") == result["native_ids"] and after["status"] == status
                             and native_result.get("pending") is not True, "202、queued 或 error 不能表示原生控制已完成")
                    if operation == "cancel":
                        _require(native_result.get("stop_reason") == "aborted", "自然完成不能算取消")
                    else:
                        checkpoint = after["stable"]["checkpoint"] if operation == "pause" else before["stable"]["checkpoint"]
                        _require(native_result.get("checkpoint") == checkpoint, "pause/resume 未使用观测检查点")
                        if operation == "resume" and target["native_ids"] != result["native_ids"]:
                            _require(native_result.get("resumed_from") == {
                                "native_ids": target["native_ids"], "checkpoint": before["stable"]["checkpoint"]},
                                "新原生运行缺少真实恢复来源链")
                            resume_lineage_verified = True
                    _require(ident not in before["control_request_ids"]
                             and after["control_request_ids"] == before["control_request_ids"] + [ident],
                             "原生控制日志未精确包含此所有者控制")
                else:
                    query_stream = "files" if operation == "artifacts" else "native"
                    query = source(attempt["query_result"], query_stream, "query_result", ident, target)
                    _require(query.get("operation") == operation and _time(decision) <= _time(query) <= _time(response)
                             and type(response.get("status")) is int and response["status"] == 200
                             and response.get("body") == query.get("body"), "所有者读取缺少独立原始结果")
                    body = query.get("body")
                    if operation == "state":
                        wanted = {**common, "status": before["status"], "checkpoint": before["stable"]["checkpoint"]}
                    elif operation == "artifacts":
                        wanted = {**common, "items": before_files["items"]}
                    else:
                        events = body.get("events") if isinstance(body, dict) else None
                        _require(isinstance(events, list) and bool(events)
                                 and all(isinstance(event, dict) and event.get("scope") == target["scope"]
                                         and event.get("native_ids") == target["native_ids"] and _text(event.get("event_id"))
                                         and event.get("message_marker") == target["sentinel"] for event in events),
                                 "本人事件查询为空或包含他人消息")
                        wanted = {**common, "events": events}
                    _require(body == wanted and not native_controls
                             and before["control_request_ids"] == after["control_request_ids"]
                             and before["stable"] == after["stable"] and before["status"] == after["status"]
                             and before_ledger["ledger"] == after_ledger["ledger"], "所有者读取改变了状态，或返回了错误作用域内容")
            if before["event_cursor_domain"] == after["event_cursor_domain"]:
                _require(after["event_cursor"] >= before["event_cursor"], "事件游标在同一域内倒退")
            else:
                _require(operation == "resume" and not cross and resume_lineage_verified
                         and before["event_cursor_domain"]["kind"] == after["event_cursor_domain"]["kind"] == "native_run",
                         "事件游标域变化，但缺少此任务的原生恢复来源链")
            checks.append((ident, True, "已有目标、可信身份、有来源绑定的响应、原生状态及 Fixture 账本相符"))
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError):
            checks.append((ident, False, "来源证据缺失、属于他人、未授权、未确认或已变化"))
            errors.append(ident + "：作用域证据链无效")
    return {"passed": all(passed for _, passed, _ in checks),
            "checks": [{"name": name, "passed": passed, "detail": detail} for name, passed, detail in checks],
            "errors": errors, "metrics": {"phases": expectation["phases"], "probes": len(attempts),
                                          "worker_reuse_declared": expectation["reuse"]["supported"],
                                          "worker_lifecycle_verified": False},
            "boundary": "仅验收作用域探针；Worker 生命周期/复用、候选任务结果和生产鉴权须另行留证"}
