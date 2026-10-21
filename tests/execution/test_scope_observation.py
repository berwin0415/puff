"""Pure positive/negative scope evidence calibration; no candidate or Docker."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from runner.scope_observation import evidence_sha256, grade_scope_observations


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fixture(*, reuse=False, rebind_resume=False, local_native_ids=False, cursor_kind="native_run"):
    phases = ["concurrent", "reconnect"] + (["reuse_b", "transfer_a"] if reuse else [])
    plan = {"schema_version": 1, "phases": phases, "reuse": {"supported": reuse, "reason": "fixed per-user Worker variant"},
            "bindings": [], "probes": []}
    sources = {stream: [] for stream in ("backend", "native", "files", "ledger")}
    attempts = []
    epoch = datetime(2026, 10, 20, tzinfo=timezone.utc)

    def stamp(number):
        return (epoch + timedelta(seconds=number)).isoformat()

    def add(stream, row):
        sources[stream].append(row)
        return {"stream": stream, "line": len(sources[stream]), "sha256": evidence_sha256(row)}

    def binding(phase, user):
        value = {"binding_id": phase + "-" + user, "phase": phase, "principal_id": "principal-" + user,
                 "credential_id": "backend-credential-" + user,
                 "scope": {"experiment_id": "scope", "variant": "unit-scope", "user_id": user, "session_id": "session-" + user},
                 "task_id": "task-" + user,
                 "native_ids": {"session_id": "main", "run_id": phase + "-run", "agent_id": "default"} if local_native_ids
                               else {"session_id": "native-session-" + user, "run_id": phase + "-run-" + user},
                 "state_dir": "/host-volumes/state/" + user, "workspace": "/host-volumes/workspace/" + user,
                 "worker_id": "worker-" + user, "connection_id": phase + "-connection-" + user,
                 "credential_fingerprint": digest("synthetic-secret-" + user), "browser_account": "browser-" + user,
                 "sentinel": "unique-owned-content-" + user,
                 "artifacts": [{"path": "owned-marker.txt", "sha256": digest("owned-file-" + user)}]}
        if reuse and phase == "reuse_b" and user == "user-b":
            value["worker_id"] = "worker-user-a"
        if reuse and phase == "transfer_a" and user == "user-a":
            value["worker_id"] = "replacement-worker-user-a"
        plan["bindings"].append(value)
        add("backend", {"kind": "registration", "time": stamp(0), "binding": deepcopy(value)})
        return value

    def checkpoint(user):
        return {"id": "checkpoint-" + user, "sha256": digest("native-checkpoint-" + user)}

    def cursor_domain(owner):
        value = {"kind": cursor_kind, "scope": deepcopy(owner["scope"]), "task_id": owner["task_id"]}
        if cursor_kind == "native_run":
            value["native_ids"] = deepcopy(owner["native_ids"])
        return value

    for phase_index, phase in enumerate(phases):
        owners = {user: binding(phase, user) for user in ("user-a", "user-b")}
        resumed = {}
        if rebind_resume:
            for user in owners:
                value = deepcopy(owners[user])
                value["binding_id"] += "-resumed"
                value["native_ids"]["run_id"] += "-new-execution"
                plan["bindings"].append(value)
                add("backend", {"kind": "registration", "time": stamp(0), "binding": deepcopy(value)})
                resumed[user] = value
        sequence = [(user, user, op) for user in ("user-a", "user-b") for op in ("state", "events", "artifacts")]
        sequence += [("user-a", "user-b", op) for op in ("state", "events", "artifacts", "pause", "cancel")]
        sequence += [("user-a", "user-a", op) for op in ("pause", "resume", "cancel")]
        sequence += [("user-b", "user-b", "pause"), ("user-a", "user-b", "resume"),
                     ("user-b", "user-b", "resume"), ("user-b", "user-b", "cancel")]
        journals = {user: [] for user in owners}
        statuses = {user: "running" for user in owners}
        for number, (actor_user, target_user, operation) in enumerate(sequence):
            actor, target = owners[actor_user], owners[target_user]
            result = resumed.get(target_user, target) if actor_user == target_user and operation == "resume" else target
            ident = phase + ":" + actor_user + ":" + target_user + ":" + operation
            plan["probes"].append({"probe_id": ident, "phase": phase, "actor_binding": actor["binding_id"],
                                   "target_binding": target["binding_id"], "result_binding": result["binding_id"], "operation": operation})
            base = 1000 * phase_index + 10 * number + 10
            stable = {"sentinel": target["sentinel"], "checkpoint_owner": target["scope"],
                      "checkpoint": checkpoint(target_user), "credential_fingerprint": target["credential_fingerprint"],
                      "browser_account": target["browser_account"]}
            before = {"kind": "snapshot", "request_id": ident, "time": stamp(base), "binding": deepcopy(target),
                      "exists": True, "status": statuses[target_user], "stable": deepcopy(stable),
                      "control_request_ids": list(journals[target_user]), "event_cursor": number * 100,
                      "event_cursor_domain": cursor_domain(target)}
            items = [{**item, "scope": target["scope"]} for item in target["artifacts"]]
            file_before = {"kind": "snapshot", "request_id": ident, "time": stamp(base), "binding": deepcopy(target), "items": deepcopy(items)}
            ledger_before = {"kind": "snapshot", "request_id": ident, "time": stamp(base), "binding": deepcopy(target),
                             "ledger_id": "fixture-ledger-" + target_user,
                             "ledger": {"writes": [], "requests": [], "faults": [], "write_count": 0, "request_count": 0}}
            attempt = {"probe_id": ident, "before": add("native", before), "files_before": add("files", file_before),
                       "ledger_before": add("ledger", ledger_before)}
            auth = {"principal_id": actor["principal_id"], "credential_id": actor["credential_id"], "authenticated": True}
            attempt["request"] = add("backend", {"kind": "request", "request_id": ident, "time": stamp(base + 1),
                "auth": auth, "target_binding": target["binding_id"], "operation": operation, "phase": phase,
                "body": {"claimed_user_id": target_user, "session_id": target["scope"]["session_id"]}})
            cross = actor_user != target_user
            attempt["authorization"] = add("backend", {"kind": "authorization", "request_id": ident, "time": stamp(base + 2),
                "auth": auth, "target_binding": target["binding_id"], "operation": operation,
                "decision": "deny" if cross else "allow", "reason": "scope_mismatch" if cross else "owner"})
            after = deepcopy(before)
            after.update(time=stamp(base + 7), binding=deepcopy(result), event_cursor=before["event_cursor"] + 8,
                         event_cursor_domain=cursor_domain(result))
            if cross:
                status, body = 403, {"error": {"code": "scope_denied"}}
            elif operation in {"pause", "resume", "cancel"}:
                status, body = 202, {"request_id": ident, "task_id": target["task_id"], "accepted": True}
                state = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[operation]
                attempt["native_dispatch"] = add("native", {"kind": "control_dispatch", "request_id": ident, "time": stamp(base + 3),
                                                            "binding": deepcopy(target), "operation": operation})
                native_result = {"status": state, "native_ids": deepcopy(result["native_ids"])}
                if operation == "cancel":
                    native_result["stop_reason"] = "aborted"
                else:
                    native_result["checkpoint"] = checkpoint(target_user)
                    if operation == "resume" and target["native_ids"] != result["native_ids"]:
                        native_result["resumed_from"] = {"native_ids": deepcopy(target["native_ids"]),
                                                        "checkpoint": checkpoint(target_user)}
                        if cursor_kind == "native_run":
                            after["event_cursor"] = 0
                attempt["native_terminal"] = add("native", {"kind": "control_terminal", "request_id": ident,
                    "time": stamp(base + 6), "binding": deepcopy(result), "operation": operation, "result": native_result})
                journals[target_user].append(ident)
                after.update(status=state, control_request_ids=list(journals[target_user]))
                statuses[target_user] = state
                owners[target_user] = result
            else:
                status = 200
                body = {"task_id": target["task_id"], "scope": target["scope"], "native_ids": target["native_ids"]}
                if operation == "state":
                    body.update(status=before["status"], checkpoint=checkpoint(target_user))
                elif operation == "events":
                    body["events"] = [{"event_id": ident + ":event", "scope": target["scope"],
                                       "native_ids": target["native_ids"], "message_marker": target["sentinel"]}]
                else:
                    body["items"] = deepcopy(items)
                attempt["query_result"] = add("files" if operation == "artifacts" else "native",
                    {"kind": "query_result", "request_id": ident, "time": stamp(base + 3), "binding": deepcopy(target),
                     "operation": operation, "body": deepcopy(body)})
            attempt["response"] = add("backend", {"kind": "response", "request_id": ident, "time": stamp(base + 4),
                                                  "status": status, "body": deepcopy(body)})
            attempt["after"] = add("native", after)
            file_after = deepcopy(file_before)
            file_after.update(time=stamp(base + 7), binding=deepcopy(result))
            ledger_after = deepcopy(ledger_before)
            ledger_after.update(time=stamp(base + 7), binding=deepcopy(result))
            attempt["files_after"] = add("files", file_after)
            attempt["ledger_after"] = add("ledger", ledger_after)
            attempts.append(attempt)
    return {"expectation": plan, "sources": sources, "attempts": attempts}


def row(data, attempt, name):
    ref = attempt[name]
    return data["sources"][ref["stream"]][ref["line"] - 1]


def refresh(data):
    """Make corruptions internally hash-consistent, exercising semantic rejection."""
    for attempt in data["attempts"]:
        for key, ref in attempt.items():
            if isinstance(ref, dict) and "stream" in ref and "line" in ref:
                ref["sha256"] = evidence_sha256(data["sources"][ref["stream"]][ref["line"] - 1])


def attempt(data, actor="user-a", target="user-b", operation="resume", phase="concurrent"):
    ident = ":".join((phase, actor, target, operation))
    return next(a for a in data["attempts"] if a["probe_id"] == ident)


def grade(data):
    return grade_scope_observations(**data)


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("rebind_resume", [False, True])
def test_complete_trusted_matrix_accepts_real_owner_results_and_active_log_growth(reuse, rebind_resume):
    data = fixture(reuse=reuse, rebind_resume=rebind_resume)
    original = deepcopy(data)
    result = grade(data)
    assert result["passed"], result
    assert result["metrics"]["probes"] == (72 if reuse else 36)
    assert data == original  # Grading is pure and never rewrites native evidence.


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("rebind_resume", [False, True])
def test_identical_local_native_ids_in_independent_trusted_storage_are_valid(reuse, rebind_resume):
    data = fixture(reuse=reuse, rebind_resume=rebind_resume, local_native_ids=True)
    assert data["expectation"]["bindings"][0]["native_ids"] == data["expectation"]["bindings"][1]["native_ids"]
    result = grade(data)
    assert result["passed"], result
    assert result["metrics"]["worker_reuse_declared"] is reuse
    assert result["metrics"]["worker_lifecycle_verified"] is False


def test_local_native_id_reuse_does_not_make_a_foreign_binding_valid():
    data = fixture(local_native_ids=True)
    positive = attempt(data, "user-b", "user-b", "state")
    foreign = next(b for b in data["expectation"]["bindings"]
                   if b["phase"] == "concurrent" and b["scope"]["user_id"] == "user-a")
    row(data, positive, "query_result")["binding"] = deepcopy(foreign)
    refresh(data)
    assert not grade(data)["passed"]


@pytest.mark.parametrize("cursor_kind", ["task", "native_run"])
def test_resume_new_run_uses_real_lineage_and_the_declared_cursor_domain(cursor_kind):
    data = fixture(rebind_resume=True, cursor_kind=cursor_kind)
    resume = attempt(data, "user-b", "user-b", "resume")
    before, after = row(data, resume, "before"), row(data, resume, "after")
    terminal = row(data, resume, "native_terminal")
    assert before["binding"]["native_ids"] != after["binding"]["native_ids"]
    assert terminal["result"]["resumed_from"] == {
        "native_ids": before["binding"]["native_ids"], "checkpoint": before["stable"]["checkpoint"]}
    assert grade(data)["passed"]
    if cursor_kind == "native_run":
        assert before["event_cursor"] > after["event_cursor"] == 0
        assert before["event_cursor_domain"] != after["event_cursor_domain"]
    else:
        assert before["event_cursor_domain"] == after["event_cursor_domain"]
        after["event_cursor"] = 0
        refresh(data)
        assert not grade(data)["passed"]  # Task-level cursors cannot reset for a new native run.


@pytest.mark.parametrize("defect", ["missing_domain", "foreign_domain", "domain_kind_switch", "old_run_domain",
    "missing_lineage", "wrong_lineage_run", "wrong_lineage_checkpoint"])
def test_resume_cursor_reset_cannot_use_unknown_domains_or_declared_lineage_only(defect):
    data = fixture(rebind_resume=True)
    resume = attempt(data, "user-b", "user-b", "resume")
    before, after = row(data, resume, "before"), row(data, resume, "after")
    native = row(data, resume, "native_terminal")["result"]
    if defect == "missing_domain":
        after.pop("event_cursor_domain")
    elif defect == "foreign_domain":
        after["event_cursor_domain"]["scope"]["user_id"] = "user-a"
    elif defect == "domain_kind_switch":
        after["event_cursor_domain"] = {"kind": "task", "scope": deepcopy(after["binding"]["scope"]),
                                        "task_id": after["binding"]["task_id"]}
    elif defect == "old_run_domain":
        after["event_cursor_domain"] = deepcopy(before["event_cursor_domain"])
    elif defect == "missing_lineage":
        native.pop("resumed_from")
    elif defect == "wrong_lineage_run":
        native["resumed_from"]["native_ids"]["run_id"] = "another-execution"
    elif defect == "wrong_lineage_checkpoint":
        native["resumed_from"]["checkpoint"]["sha256"] = digest("another-checkpoint")
    refresh(data)
    assert not grade(data)["passed"]


@pytest.mark.parametrize("operation", ["pause", "cancel"])
def test_pause_and_cancel_cannot_use_another_registered_native_run_as_their_result(operation):
    data = fixture()
    own = attempt(data, "user-b", "user-b", operation)
    plan = next(p for p in data["expectation"]["probes"] if p["probe_id"] == own["probe_id"])
    target = next(b for b in data["expectation"]["bindings"] if b["binding_id"] == plan["target_binding"])
    other = deepcopy(target)
    other["binding_id"] += "-different-execution"
    other["native_ids"]["run_id"] += "-different-execution"
    data["expectation"]["bindings"].append(other)
    data["sources"]["backend"].append({"kind": "registration", "time": "2026-10-20T00:00:00+00:00",
                                        "binding": deepcopy(other)})
    plan["result_binding"] = other["binding_id"]
    for name in ("after", "files_after", "ledger_after", "native_terminal"):
        row(data, own, name)["binding"] = deepcopy(other)
    row(data, own, "after")["event_cursor_domain"]["native_ids"] = deepcopy(other["native_ids"])
    row(data, own, "native_terminal")["result"]["native_ids"] = deepcopy(other["native_ids"])
    refresh(data)
    result = grade(data)
    assert not result["passed"]
    assert result["checks"][0]["name"] == "trusted_plan_and_matrix"
    assert result["checks"][0]["passed"] is False  # Invalid target rebinding is rejected before success observations.


def test_legacy_all_a_positive_foreign_content_false_and_empty_snapshots_never_pass():
    old = [{"actor": "user-a", "target": target, "operation": operation,
            "status": 403 if target == "user-b" else 202, "foreign_content": False, "before": {}, "after": {}}
           for target in ("user-a", "user-b") for operation in ("state", "events", "artifacts", "pause", "resume", "cancel")]
    assert not grade_scope_observations(expectation={}, sources={}, attempts=old)["passed"]


@pytest.mark.parametrize("defect", [
    "missing_b_positive", "missing_reconnect", "missing_reuse_phase", "wrong_reuse_worker", "same_connection",
    "duplicate_attempt", "duplicate_registration", "unregistered", "shared_scope", "overlapping_workspace",
    "shared_credential", "shared_browser", "empty_files", "partial_scope", "missing_native_ids", "missing_native_stream",
    "candidate_source", "source_digest", "wrong_source_stream", "wrong_source_kind", "replayed_request", "spoofed_auth",
    "auth_unverified", "wrong_auth_target", "denial_404", "denial_401", "denial_invalid_resume", "foreign_body", "boolean_denial",
    "target_absent", "empty_stable", "changed_checkpoint", "changed_credential", "changed_browser", "wrong_scope_snapshot",
    "changed_files", "foreign_files", "missing_ledger", "ledger_not_equal", "foreign_ledger", "ledger_reset",
    "native_dispatch_after_denial", "native_journal_changed", "invalid_resume_state", "resume_without_owner_pause", "unrelated_resume_checkpoint", "event_cursor_reverse", "time_reversed",
    "b_202_only", "b_missing_terminal", "b_pending_terminal", "b_natural_cancel", "b_wrong_checkpoint", "b_foreign_terminal",
    "b_wrong_dispatch", "b_terminal_before_dispatch", "b_journal_missing", "b_empty_events", "b_foreign_event", "b_empty_artifacts",
    "b_wrong_state_response", "b_response_disagrees", "owner_denied",
])
def test_rejects_internally_consistent_wrong_or_insufficient_evidence(defect):
    data = fixture(reuse=defect in {"missing_reuse_phase", "wrong_reuse_worker"})
    cross = attempt(data)
    own = attempt(data, "user-b", "user-b", "cancel")
    plan = data["expectation"]
    if defect == "missing_b_positive":
        data["attempts"] = [a for a in data["attempts"] if ":user-b:user-b:" not in a["probe_id"]]
    elif defect == "missing_reconnect":
        plan["phases"].remove("reconnect")
    elif defect == "missing_reuse_phase":
        plan["phases"].remove("transfer_a")
    elif defect == "wrong_reuse_worker":
        next(b for b in plan["bindings"] if b["phase"] == "reuse_b" and b["scope"]["user_id"] == "user-b")["worker_id"] = "never-used-worker"
    elif defect == "same_connection":
        plan["bindings"][2]["connection_id"] = plan["bindings"][0]["connection_id"]
    elif defect == "duplicate_attempt":
        data["attempts"].append(deepcopy(data["attempts"][0]))
    elif defect == "duplicate_registration":
        data["sources"]["backend"].append(deepcopy(data["sources"]["backend"][0]))
    elif defect == "unregistered":
        data["sources"]["backend"][0]["kind"] = "not-registration"
    elif defect in {"shared_scope", "overlapping_workspace", "shared_credential", "shared_browser", "empty_files", "partial_scope", "missing_native_ids"}:
        a, b = plan["bindings"][:2]
        if defect == "shared_scope": b["scope"] = deepcopy(a["scope"])
        elif defect == "overlapping_workspace": b["workspace"] = a["workspace"] + "/nested"
        elif defect == "shared_credential": b["credential_id"] = a["credential_id"]
        elif defect == "shared_browser": b["browser_account"] = a["browser_account"]
        elif defect == "empty_files": b["artifacts"] = []
        elif defect == "partial_scope": b["scope"].pop("experiment_id")
        elif defect == "missing_native_ids": b["native_ids"] = {}
    elif defect == "missing_native_stream":
        data["sources"].pop("native")
        assert not grade(data)["passed"]
        return
    elif defect == "candidate_source":
        cross["before"]["stream"] = "candidate"
        assert not grade(data)["passed"]
        return
    elif defect == "source_digest":
        cross["before"]["sha256"] = "0" * 64
        assert not grade(data)["passed"]
        return
    elif defect == "wrong_source_stream":
        cross["before"] = deepcopy(cross["files_before"])
    elif defect == "wrong_source_kind": row(data, cross, "before")["kind"] = "self-reported"
    elif defect == "replayed_request": row(data, cross, "request")["request_id"] = "other-request"
    elif defect == "spoofed_auth": row(data, cross, "request")["auth"]["principal_id"] = "principal-user-b"
    elif defect == "auth_unverified": row(data, cross, "request")["auth"]["authenticated"] = False
    elif defect == "wrong_auth_target": row(data, cross, "authorization")["target_binding"] = "nonexistent"
    elif defect == "denial_404": row(data, cross, "response")["status"] = 404
    elif defect == "denial_401": row(data, cross, "response")["status"] = 401
    elif defect == "denial_invalid_resume": row(data, cross, "authorization")["reason"] = "not_paused"
    elif defect == "foreign_body": row(data, cross, "response")["body"]["leaked"] = "B checkpoint content"
    elif defect == "boolean_denial": row(data, cross, "response")["body"] = {"foreign_content": False}
    elif defect == "target_absent": row(data, cross, "before")["exists"] = False
    elif defect == "empty_stable": row(data, cross, "before")["stable"] = row(data, cross, "after")["stable"] = {}
    elif defect == "changed_checkpoint": row(data, cross, "after")["stable"]["checkpoint"]["sha256"] = digest("changed")
    elif defect == "changed_credential": row(data, cross, "after")["stable"]["credential_fingerprint"] = digest("other-secret")
    elif defect == "changed_browser": row(data, cross, "after")["stable"]["browser_account"] = "foreign-browser"
    elif defect == "wrong_scope_snapshot": row(data, cross, "before")["binding"]["scope"]["user_id"] = "user-a"
    elif defect == "changed_files": row(data, cross, "files_after")["items"][0]["sha256"] = digest("wrong")
    elif defect == "foreign_files": row(data, cross, "files_before")["items"][0]["scope"]["user_id"] = "user-a"
    elif defect == "missing_ledger": row(data, cross, "ledger_before")["ledger"] = {}
    elif defect == "ledger_not_equal":
        value = row(data, cross, "ledger_after")["ledger"]
        value["requests"] = [{**row(data, cross, "before")["binding"]["scope"], "action": "write"}]
        value["request_count"] = 1
    elif defect == "foreign_ledger":
        for name in ("ledger_before", "ledger_after"):
            value = row(data, cross, name)["ledger"]
            value["writes"] = [{"experiment_id": "foreign", "variant": "unit", "user_id": "user-a", "session_id": "other"}]
            value["write_count"] = 1
    elif defect == "ledger_reset": row(data, cross, "ledger_after")["ledger_id"] = "fresh-empty-ledger"
    elif defect == "native_dispatch_after_denial":
        data["sources"]["native"].append({"kind": "control_dispatch", "request_id": cross["probe_id"], "time": "2026-10-20T00:00:00Z"})
    elif defect == "native_journal_changed": row(data, cross, "after")["control_request_ids"].append("foreign-control")
    elif defect == "invalid_resume_state": row(data, cross, "before")["status"] = row(data, cross, "after")["status"] = "running"
    elif defect == "resume_without_owner_pause":
        row(data, cross, "before")["control_request_ids"] = []
        row(data, cross, "after")["control_request_ids"] = []
    elif defect == "unrelated_resume_checkpoint":
        for name in ("before", "after"):
            row(data, cross, name)["stable"]["checkpoint"] = {"id": "other-paused-task", "sha256": digest("other-checkpoint")}
    elif defect == "event_cursor_reverse": row(data, cross, "after")["event_cursor"] = 0
    elif defect == "time_reversed": row(data, cross, "after")["time"] = "2020-01-01T00:00:00Z"
    elif defect == "b_202_only":
        own.pop("native_dispatch"); own.pop("native_terminal")
    elif defect == "b_missing_terminal": own.pop("native_terminal")
    elif defect == "b_pending_terminal": row(data, own, "native_terminal")["result"]["pending"] = True
    elif defect == "b_natural_cancel": row(data, own, "native_terminal")["result"]["stop_reason"] = "stop"
    elif defect == "b_wrong_checkpoint":
        own = attempt(data, "user-b", "user-b", "pause")
        row(data, own, "native_terminal")["result"]["checkpoint"]["sha256"] = digest("wrong-checkpoint")
    elif defect == "b_foreign_terminal": row(data, own, "native_terminal")["result"]["native_ids"]["session_id"] = "native-session-user-a"
    elif defect == "b_wrong_dispatch": row(data, own, "native_dispatch")["binding"]["scope"]["user_id"] = "user-a"
    elif defect == "b_terminal_before_dispatch": row(data, own, "native_terminal")["time"] = "2020-01-01T00:00:00Z"
    elif defect == "b_journal_missing": row(data, own, "after")["control_request_ids"] = []
    elif defect in {"b_empty_events", "b_foreign_event"}:
        own = attempt(data, "user-b", "user-b", "events")
        body = row(data, own, "query_result")["body"]
        if defect == "b_empty_events": body["events"] = []
        else: body["events"][0]["message_marker"] = "unique-owned-content-user-a"
        row(data, own, "response")["body"] = deepcopy(body)
    elif defect == "b_empty_artifacts":
        own = attempt(data, "user-b", "user-b", "artifacts")
        row(data, own, "query_result")["body"]["items"] = []
        row(data, own, "response")["body"]["items"] = []
    elif defect == "b_wrong_state_response":
        own = attempt(data, "user-b", "user-b", "state")
        row(data, own, "query_result")["body"]["checkpoint"]["sha256"] = digest("other-state")
        row(data, own, "response")["body"] = deepcopy(row(data, own, "query_result")["body"])
    elif defect == "b_response_disagrees":
        own = attempt(data, "user-b", "user-b", "state")
        row(data, own, "response")["body"]["status"] = "paused"
    elif defect == "owner_denied": row(data, own, "authorization")["decision"] = "deny"
    else: raise AssertionError(defect)
    refresh(data)
    assert not grade(data)["passed"], defect


def test_cross_resume_is_observed_after_real_b_owner_pause_not_an_invalid_request():
    data = fixture()
    cross = attempt(data)
    pause = attempt(data, "user-b", "user-b", "pause")
    assert row(data, cross, "before")["status"] == "paused"
    assert pause["probe_id"] in row(data, cross, "before")["control_request_ids"]
    assert row(data, pause, "native_terminal")["time"] < row(data, cross, "request")["time"]
    assert grade(data)["passed"]


def test_204_has_no_invented_http_body_but_still_needs_native_completion():
    data = fixture()
    own = attempt(data, "user-b", "user-b", "pause")
    response = row(data, own, "response")
    response.update(status=204, body=None)
    refresh(data)
    assert grade(data)["passed"]
    own.pop("native_terminal")
    assert not grade(data)["passed"]


def test_native_activity_can_grow_without_whole_log_hash_equality():
    data = fixture()
    cross = attempt(data, operation="state")
    after = row(data, cross, "after")
    after["event_cursor"] += 10000
    data["sources"]["native"].append({"kind": "activity", "time": after["time"],
        "binding": deepcopy(after["binding"]), "event_id": "ordinary-progress", "request_id": "ordinary-task"})
    refresh(data)
    assert grade(data)["passed"]


@pytest.mark.parametrize("value", [None, [], {}, "candidate-said-isolated"])
def test_malformed_inputs_are_failed_verdicts(value):
    assert not grade_scope_observations(expectation=value, sources=value, attempts=value)["passed"]
