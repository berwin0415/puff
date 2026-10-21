"""Pure I24 evidence grading; collection/authentication belong to the test backend.

``expectation`` must be pinned by the trusted controller, and ``sources`` loaded
from backend/native/filesystem/fixture captures outside candidate-writable
mounts. A source name or a hash does not establish that trust on its own. This
module performs no I/O and never imports a candidate or changes its result.
See docs/validation/scope-evidence.md for the deliberately small contract.
"""
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
    """Hash retained JSON values, not a mutable pretty-printing representation."""
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
    _require(parsed.tzinfo is not None, "capture time requires a timezone")
    return parsed


def _absolute(value):
    return (_text(value) and PurePosixPath(value).is_absolute()
            and ".." not in PurePosixPath(value).parts and "\x00" not in value)


def _separate(left, right):
    left, right = PurePosixPath(left), PurePosixPath(right)
    return left != right and left not in right.parents and right not in left.parents


def _validate_plan(expectation):
    _require(isinstance(expectation, dict) and type(expectation.get("schema_version")) is int and expectation["schema_version"] == 1,
             "a separately pinned schema_version=1 expectation is required")
    phases = expectation["phases"]
    _require(isinstance(phases, list) and all(_text(phase) for phase in phases) and len(phases) == len(set(phases))
             and {"concurrent", "reconnect"} <= set(phases), "concurrent and reconnect phases are required")
    reuse = expectation["reuse"]
    _require(isinstance(reuse, dict) and type(reuse.get("supported")) is bool,
             "Worker reuse support must be explicit")
    if reuse["supported"]:
        _require({"reuse_b", "transfer_a"} <= set(phases), "supported reuse requires A to B to A observations")
    else:
        _require(_text(reuse.get("reason")), "unsupported reuse requires its observed limitation")
    bindings = {}
    for binding in expectation["bindings"]:
        _require(isinstance(binding, dict) and set(binding) == _BINDING_FIELDS, "incomplete trusted binding")
        ident, scope = binding["binding_id"], binding["scope"]
        _require(_text(ident) and ident not in bindings and binding["phase"] in phases, "duplicate or invalid binding")
        _require(isinstance(scope, dict) and set(scope) == SCOPE_FIELDS
                 and all(_text(value) for value in scope.values()) and scope["user_id"] in USERS,
                 "binding requires all four scope fields")
        _require(all(_text(binding[key]) for key in ("principal_id", "credential_id", "task_id", "worker_id",
                                                    "connection_id", "browser_account", "sentinel")),
                 "binding identity is incomplete")
        ids = binding["native_ids"]
        _require(isinstance(ids, dict) and bool(ids) and all(_text(k) and _text(v) for k, v in ids.items()),
                 "exact native session/run identifiers are required")
        _require(_absolute(binding["state_dir"]) and _absolute(binding["workspace"])
                 and _separate(binding["state_dir"], binding["workspace"]), "invalid state/workspace binding")
        _require(_sha(binding["credential_fingerprint"]), "synthetic credential fingerprint is required")
        artifacts = binding["artifacts"]
        _require(isinstance(artifacts, list) and bool(artifacts), "nonempty pinned artifacts are required")
        paths = []
        for item in artifacts:
            _require(isinstance(item, dict) and set(item) == {"path", "sha256"}
                     and _text(item["path"]) and not PurePosixPath(item["path"]).is_absolute()
                     and ".." not in PurePosixPath(item["path"]).parts and _sha(item["sha256"]),
                     "artifact requires a relative path and content digest")
            paths.append(item["path"])
        _require(len(paths) == len(set(paths)), "duplicate artifact path")
        bindings[ident] = binding
    _require(bool(bindings), "trusted bindings are missing")
    by_user = {user: [b for b in bindings.values() if b["scope"]["user_id"] == user] for user in USERS}
    for user, values in by_user.items():
        _require({b["phase"] for b in values} == set(phases), "both owners require every phase")
        # Reconnection/rebinding may replace native run ids, workers and connections.
        # The controller still pins the original owner, task and persistent locations.
        for key in ("principal_id", "credential_id", "scope", "task_id", "state_dir", "workspace",
                    "credential_fingerprint", "browser_account", "sentinel", "artifacts"):
            _require(all(b[key] == values[0][key] for b in values), "persistent task ownership changed")
    a, b = by_user["user-a"][0], by_user["user-b"][0]
    for key in ("principal_id", "credential_id", "task_id", "credential_fingerprint", "browser_account", "sentinel"):
        _require(a[key] != b[key], "A and B need distinct identities and sentinels")
    _require(all(_separate(a[x], b[y]) for x in ("state_dir", "workspace") for y in ("state_dir", "workspace")),
             "foreign state/workspace locations overlap")
    # Native identifiers are local to a trusted storage/scope binding. Two
    # independent state stores may both expose session_id="main" (or use the
    # same agent configuration name); strings alone cannot locate a task.
    # Above we require distinct persistent locations and authenticated owners;
    # below every source must retain the complete exact target binding.
    probes, matrix = {}, Counter()
    for probe in expectation["probes"]:
        _require(isinstance(probe, dict) and set(probe) == {"probe_id", "phase", "actor_binding", "target_binding",
                                                         "result_binding", "operation"}, "incomplete pinned probe")
        ident = probe["probe_id"]
        _require(_text(ident) and ident not in probes and probe["operation"] in OPERATIONS, "duplicate or invalid probe")
        actor, target, result = (bindings[probe[key]] for key in ("actor_binding", "target_binding", "result_binding"))
        _require(all(x["phase"] == probe["phase"] for x in (actor, target, result)), "probe crosses phases")
        _require(all(target[k] == result[k] for k in _BINDING_FIELDS - {"binding_id", "native_ids", "worker_id", "connection_id"}),
                 "control result rebound another task")
        actor_user, target_user = actor["scope"]["user_id"], target["scope"]["user_id"]
        _require(actor_user == target_user or (actor_user, target_user) == ("user-a", "user-b"), "unexpected identity probe")
        if actor_user != target_user or probe["operation"] != "resume":
            _require(target == result, "only an authorized resume may rebind the target incarnation")
        matrix[(probe["phase"], actor_user, target_user, probe["operation"])] += 1
        probes[ident] = probe
    wanted = Counter((phase, actor, target, operation) for phase in phases
                     for actor, target in (("user-a", "user-a"), ("user-b", "user-b"), ("user-a", "user-b"))
                     for operation in OPERATIONS)
    _require(matrix == wanted, "each phase requires A and B six-operation positives plus all A-to-existing-B denials")
    for user in USERS:
        current = next(x for x in by_user[user] if x["phase"] == "concurrent")
        reconnected = next(x for x in by_user[user] if x["phase"] == "reconnect")
        _require(current["connection_id"] != reconnected["connection_id"], "reconnect must use a new backend connection")
    if reuse["supported"]:
        initial_a = next(x for x in by_user["user-a"] if x["phase"] == "concurrent")
        reused_b = next(x for x in by_user["user-b"] if x["phase"] == "reuse_b")
        transferred_a = next(x for x in by_user["user-a"] if x["phase"] == "transfer_a")
        _require(initial_a["worker_id"] == reused_b["worker_id"] != transferred_a["worker_id"],
                 "reuse requires B on A's released Worker and A on a different Worker")
    return bindings, probes


def _checkpoint(value):
    return (isinstance(value, dict) and set(value) == {"id", "sha256"}
            and _text(value["id"]) and _sha(value["sha256"]))


def grade_scope_observations(*, expectation: dict, sources: dict, attempts: list[dict]) -> dict:
    """Validate source-bound scope probes; never accept legacy assertion-only rows.

    A 202 response can be an acknowledgment, but a control passes only with a
    sourced native dispatch, actual native terminal and independent after-state.
    Failed/missing capability evidence is a failed verdict, not an exception.
    """
    checks, errors = [], []
    try:
        bindings, probes = _validate_plan(expectation)
        _require(isinstance(sources, dict) and set(sources) == STREAMS
                 and all(isinstance(rows, list) and bool(rows) and len(rows) <= 100000
                         and all(isinstance(row, dict) for row in rows) for rows in sources.values()),
                 "four nonempty trusted capture streams are required")
        _require(isinstance(attempts, list) and all(isinstance(a, dict) for a in attempts), "attempt references are required")
        ids = [a.get("probe_id") for a in attempts]
        _require(len(ids) == len(set(ids)) and set(ids) == set(probes), "attempts do not match the pinned complete probe matrix")
        registration_rows = [row for row in sources["backend"] if row.get("kind") == "registration"]
        registrations = [row.get("binding") for row in registration_rows]
        _require(all(registrations.count(binding) == 1 for binding in bindings.values()),
                 "every target incarnation needs one independent backend ownership registration")
        ownership = {binding["binding_id"]: next(row for row in registration_rows if row.get("binding") == binding)
                     for binding in bindings.values()}
        for row in ownership.values():
            _time(row)
        checks.append(("trusted_plan_and_matrix", True, "Pinned A/B identities, native ids, paths, phases and all 18 probes per phase"))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {"passed": False, "checks": [{"name": "trusted_plan_and_matrix", "passed": False,
                "detail": "Missing, malformed or incomplete trusted plan/capture matrix"}], "errors": ["invalid trusted scope evidence contract"]}

    def source(ref, stream, kind, probe_id=None, binding=None):
        _require(isinstance(ref, dict) and set(ref) == {"stream", "line", "sha256"}
                 and ref["stream"] == stream and type(ref["line"]) is int and 1 <= ref["line"] <= len(sources[stream]),
                 "invalid source reference")
        row = sources[stream][ref["line"] - 1]
        _require(ref["sha256"] == evidence_sha256(row) and row.get("kind") == kind, "source digest/kind mismatch")
        _time(row)
        if probe_id is not None:
            _require(row.get("request_id") == probe_id, "capture belongs to another request")
        if binding is not None:
            _require(row.get("binding") == binding, "capture binding differs from trusted ownership")
        return row

    def snapshot(ref, binding, ident):
        row = source(ref, "native", "snapshot", ident, binding)
        stable = row.get("stable")
        _require(row.get("exists") is True and row.get("status") in {"running", "paused", "cancelled"},
                 "target existence/native status is unproven")
        _require(isinstance(stable, dict) and set(stable) == {"sentinel", "checkpoint_owner", "checkpoint",
                                                            "credential_fingerprint", "browser_account"}
                 and stable["sentinel"] == binding["sentinel"] and stable["checkpoint_owner"] == binding["scope"]
                 and _checkpoint(stable["checkpoint"]) and stable["credential_fingerprint"] == binding["credential_fingerprint"]
                 and stable["browser_account"] == binding["browser_account"], "native protected state is missing or foreign")
        controls = row.get("control_request_ids")
        _require(isinstance(controls, list) and all(_text(v) for v in controls) and len(controls) == len(set(controls)),
                 "complete native control journal is required")
        _require(type(row.get("event_cursor")) is int and row["event_cursor"] >= 0, "native event cursor is missing")
        domain = row.get("event_cursor_domain")
        common = {"scope": binding["scope"], "task_id": binding["task_id"]}
        _require(domain == {"kind": "task", **common}
                 or domain == {"kind": "native_run", **common, "native_ids": binding["native_ids"]},
                 "native event cursor domain is missing or foreign")
        return row

    def files(ref, binding, ident):
        row = source(ref, "files", "snapshot", ident, binding)
        wanted = [{**item, "scope": binding["scope"]} for item in binding["artifacts"]]
        _require(row.get("items") == wanted, "independent nonempty files/contents/ownership differ")
        return row

    def ledger(ref, binding, ident):
        row = source(ref, "ledger", "snapshot", ident, binding)
        value = row.get("ledger")
        _require(isinstance(value, dict) and _text(row.get("ledger_id"))
                 and all(isinstance(value.get(k), list) for k in ("writes", "requests", "faults"))
                 and type(value.get("write_count")) is int and value["write_count"] == len(value["writes"])
                 and type(value.get("request_count")) is int and value["request_count"] == len(value["requests"])
                 and all(isinstance(item, dict) and all(item.get(k) == v for k, v in binding["scope"].items())
                         for key in ("writes", "requests", "faults") for item in value[key]), "fixture ledger is missing, incomplete or foreign")
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
                     "request identity/target differs from authenticated controller input")
            _require(decision.get("auth") == auth and decision.get("target_binding") == target["binding_id"]
                     and decision.get("operation") == operation, "authorization is not bound to this identity and existing target")
            _require(max(_time(x) for x in (before, before_files, before_ledger)) <= _time(request)
                     <= _time(decision) <= _time(response)
                     <= min(_time(x) for x in (after, after_files, after_ledger)), "probe captures do not surround the real response")
            _require(_time(ownership[target["binding_id"]]) <= _time(before)
                     and _time(ownership[result["binding_id"]]) <= _time(after), "ownership was registered after its observation")
            _require(before_ledger["ledger_id"] == after_ledger["ledger_id"], "fixture ledger was replaced")
            resume_lineage_verified = False
            native_controls = [row for row in sources["native"] if row.get("request_id") == ident
                               and row.get("kind") in {"control_dispatch", "control_terminal"}]
            legal_before = "paused" if operation == "resume" else "running" if operation in CONTROLS else None
            if legal_before:
                _require(before["status"] == legal_before, "control tested in an invalid native state")
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
                         and _time(pause_terminal) <= _time(before), "resume does not follow this owner's real paused checkpoint")
            if cross:
                _require(decision.get("decision") == "deny" and decision.get("reason") == "scope_mismatch"
                         and type(response.get("status")) is int and response["status"] == 403
                         and response.get("body") == {"error": {"code": "scope_denied"}}, "denial is not a content-free scope rejection")
                _require(not native_controls and before["control_request_ids"] == after["control_request_ids"],
                         "denied request reached native control")
                _require(before["status"] == after["status"] and before["stable"] == after["stable"]
                         and before_files["items"] == after_files["items"] and before_ledger["ledger"] == after_ledger["ledger"],
                         "denied request changed protected target state/files/ledger")
            else:
                _require(decision.get("decision") == "allow" and decision.get("reason") == "owner", "owner was not authorized")
                common = {"task_id": target["task_id"], "scope": target["scope"], "native_ids": target["native_ids"]}
                if operation in CONTROLS:
                    _require(type(response.get("status")) is int and
                             ((response["status"] in {200, 202} and response.get("body") ==
                               {"request_id": ident, "task_id": target["task_id"], "accepted": True})
                              or (response["status"] == 204 and response.get("body") is None)),
                             "owner control lacks a matching acknowledgment")
                    dispatch = source(attempt["native_dispatch"], "native", "control_dispatch", ident, target)
                    terminal = source(attempt["native_terminal"], "native", "control_terminal", ident, result)
                    status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[operation]
                    native_result = terminal.get("result")
                    _require(len(native_controls) == 2 and dispatch in native_controls and terminal in native_controls
                             and dispatch.get("operation") == terminal.get("operation") == operation
                             and _time(decision) <= _time(dispatch) <= _time(terminal) <= _time(after),
                             "owner control dispatch/terminal source chain is incomplete")
                    _require(isinstance(native_result, dict) and native_result.get("status") == status
                             and native_result.get("native_ids") == result["native_ids"] and after["status"] == status
                             and native_result.get("pending") is not True, "202/queued/error is not native control completion")
                    if operation == "cancel":
                        _require(native_result.get("stop_reason") == "aborted", "natural completion is not cancellation")
                    else:
                        checkpoint = after["stable"]["checkpoint"] if operation == "pause" else before["stable"]["checkpoint"]
                        _require(native_result.get("checkpoint") == checkpoint, "pause/resume did not use the observed checkpoint")
                        if operation == "resume" and target["native_ids"] != result["native_ids"]:
                            _require(native_result.get("resumed_from") == {
                                "native_ids": target["native_ids"], "checkpoint": before["stable"]["checkpoint"]},
                                "new native run lacks its actual resumed-from lineage")
                            resume_lineage_verified = True
                    _require(ident not in before["control_request_ids"]
                             and after["control_request_ids"] == before["control_request_ids"] + [ident],
                             "native control journal does not contain exactly this owner control")
                else:
                    query_stream = "files" if operation == "artifacts" else "native"
                    query = source(attempt["query_result"], query_stream, "query_result", ident, target)
                    _require(query.get("operation") == operation and _time(decision) <= _time(query) <= _time(response)
                             and type(response.get("status")) is int and response["status"] == 200
                             and response.get("body") == query.get("body"), "owner read lacks its independent original result")
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
                                 "own event query is empty or contains foreign messages")
                        wanted = {**common, "events": events}
                    _require(body == wanted and not native_controls
                             and before["control_request_ids"] == after["control_request_ids"]
                             and before["stable"] == after["stable"] and before["status"] == after["status"]
                             and before_ledger["ledger"] == after_ledger["ledger"], "owner read changed state or returned wrong scoped content")
            if before["event_cursor_domain"] == after["event_cursor_domain"]:
                _require(after["event_cursor"] >= before["event_cursor"], "event cursor moved backwards within one domain")
            else:
                _require(operation == "resume" and not cross and resume_lineage_verified
                         and before["event_cursor_domain"]["kind"] == after["event_cursor_domain"]["kind"] == "native_run",
                         "event cursor domain changed without this task's native resume lineage")
            checks.append((ident, True, "Existing target, trusted identity, source-bound response, native state and fixture ledger agree"))
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError):
            checks.append((ident, False, "Missing, foreign, unauthorized, unconfirmed or changed source evidence"))
            errors.append(ident + ": invalid scope evidence chain")
    return {"passed": all(passed for _, passed, _ in checks),
            "checks": [{"name": name, "passed": passed, "detail": detail} for name, passed, detail in checks],
            "errors": errors, "metrics": {"phases": expectation["phases"], "probes": len(attempts),
                                          "worker_reuse_declared": expectation["reuse"]["supported"],
                                          "worker_lifecycle_verified": False},
            "boundary": "Scope probe acceptance only; Worker lifecycle/reuse, candidate task results and production authentication require separate evidence"}
