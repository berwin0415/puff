"""Calibrate independent verdicts with real fixture data and deliberately bad artifacts.

Synthetic browser/policy/control evidence below tests the verdict rules only;
it is not a candidate capability or browser execution result.
"""

from copy import deepcopy
from contextlib import contextmanager
import csv
import io
import json
from threading import Thread

import httpx
import pytest

from execution.tools.basic import FixtureClient
from execution.tools.server import ToolsHTTPServer
from runner.grading import (SCOPE_FIELDS, grade_browser, grade_scope_attempts,
                            grade_sensitive, grade_tools, prepare_tools_inputs)


def scope_of(prepared):
    return {key: prepared[key] for key in SCOPE_FIELDS}


@contextmanager
def live_tools(process, prepared, workspace, events_path):
    """Use actual execution tooling to calibrate its real start/finish audit shape."""
    binding = {"fixture_url": process.base_url, "fixture_token": prepared["token"],
               "workspace": str(workspace), "scope": scope_of(prepared), "events_path": str(events_path)}
    server = ToolsHTTPServer(("127.0.0.1", 0), binding)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False,
                          headers={"Authorization": "Bearer " + prepared["token"]}) as client:
            def call(name, **arguments):
                response = client.post("/call", json={"name": name, "arguments": arguments})
                response.raise_for_status()
                return response.json()["result"]
            yield call
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def completed_tools(process, tmp_path, seed=101, write_mode="idempotent"):
    prepared = process.prepare(seed=seed, experiment_id=f"grading-{seed}", write_mode=write_mode)["user-a"]
    scope = scope_of(prepared)
    workspace = tmp_path / "workspace"
    public = prepare_tools_inputs(workspace, scope)
    client = FixtureClient(process.base_url, prepared["token"])
    events_path = tmp_path / "trusted-tool-events.jsonl"
    with live_tools(process, prepared, workspace, events_path) as call:
        catalogue = []
        page = 1
        while page is not None:
            result = call("list_items", page=page, page_size=5)
            catalogue.extend(result["items"])
            page = result["next_page"]
        # Build the answer from the actual paged API, independently of the seed grader.
        selected = sorted((row for row in catalogue if row["category"] in {"office", "garden"}
                           and row["unit_price_cents"] <= 3500 and row["available_quantity"] >= 2),
                          key=lambda row: (row["unit_price_cents"], row["item_id"]))[:3]
        report = {"scope": scope, "selected": [], "total_cents": 0}
        for item in selected:
            operation = "order-" + item["item_id"]
            call("write", operation_id=operation, item_id=item["item_id"], quantity=2)
            total = 2 * item["unit_price_cents"]
            report["selected"].append({"operation_id": operation, "item_id": item["item_id"],
                                       "quantity": 2, "unit_price_cents": item["unit_price_cents"], "total_cents": total})
            report["total_cents"] += total
        call("write_file", path="report.json", content=json.dumps(report))
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    return {"seed": seed, "scope": scope, "workspace": workspace, "ledger": client.ledger(),
            "tool_events": events}, report, client, public


@pytest.mark.parametrize("seed", [101, 202, 303])
def test_seed_reconstruction_agrees_with_real_paginated_fixture(fixture_process, tmp_path, seed):
    evidence, _, _, _ = completed_tools(fixture_process, tmp_path, seed)
    assert grade_tools(**evidence)["passed"]


def test_candidate_input_does_not_include_expected_answer(fixture_process, tmp_path):
    prepared = fixture_process.prepare()["user-a"]
    workspace = tmp_path / "task-inputs"
    public = prepare_tools_inputs(workspace, scope_of(prepared))
    assert {file.name for file in workspace.iterdir()} == {"request.json"}
    assert set(public) == {"scope", "prompt"}
    request = json.loads((workspace / "request.json").read_text())
    assert "selected" not in request and "total_cents" not in request and "expected" not in request


def test_wrong_report_wrong_user_and_incomplete_pages_are_detected(fixture_process, tmp_path):
    evidence, report, _, _ = completed_tools(fixture_process, tmp_path)
    wrong = deepcopy(report)
    wrong["total_cents"] += 1
    (evidence["workspace"] / "report.json").write_text(json.dumps(wrong))
    assert not grade_tools(**evidence)["passed"]
    wrong = deepcopy(report)
    wrong["scope"]["user_id"] = "user-b"
    (evidence["workspace"] / "report.json").write_text(json.dumps(wrong))
    assert not grade_tools(**evidence)["passed"]
    (evidence["workspace"] / "report.json").write_text(json.dumps(report))
    assert not grade_tools(**{**evidence, "tool_events": evidence["tool_events"][:-1]})["passed"]
    other_events = deepcopy(evidence["tool_events"])
    other_events[0]["scope"] = {**evidence["scope"], "user_id": "user-b"}
    assert not grade_tools(**{**evidence, "tool_events": other_events})["passed"]


def test_duplicate_write_is_detected_even_when_report_looks_correct(fixture_process, tmp_path):
    evidence, report, client, _ = completed_tools(fixture_process, tmp_path, write_mode="non_idempotent")
    row = report["selected"][0]
    client.write(row["operation_id"], row["item_id"], row["quantity"])
    result = grade_tools(**{**evidence, "ledger": client.ledger()})
    assert not result["passed"]
    assert not next(check for check in result["checks"] if check["name"] == "business_writes_exact")["passed"]


def test_other_users_ledger_cannot_satisfy_this_users_task(fixture_process, tmp_path):
    evidence, _, _, _ = completed_tools(fixture_process, tmp_path)
    wrong_ledger = deepcopy(evidence["ledger"])
    for row in wrong_ledger["writes"]:
        row["user_id"] = "user-b"
    assert not grade_tools(**{**evidence, "ledger": wrong_ledger})["passed"]


def test_missing_audits_foreign_ledger_and_missing_call_pairs_fail(fixture_process, tmp_path):
    evidence, _, _, _ = completed_tools(fixture_process, tmp_path)
    missing_requests = deepcopy(evidence["ledger"])
    missing_requests["requests"] = []
    missing_requests["request_count"] = 0
    assert not grade_tools(**{**evidence, "ledger": missing_requests})["passed"]
    foreign = deepcopy(evidence["ledger"])
    foreign_row = {**foreign["writes"][0], "user_id": "user-b"}
    foreign["writes"].append(foreign_row)
    foreign["write_count"] += 1
    assert not grade_tools(**{**evidence, "ledger": foreign})["passed"]
    no_writes = [event for event in evidence["tool_events"] if event["tool"] != "write"]
    assert not grade_tools(**{**evidence, "tool_events": no_writes})["passed"]
    unpaired = [event for event in evidence["tool_events"] if event["phase"] != "start"]
    assert not grade_tools(**{**evidence, "tool_events": unpaired})["passed"]
    wrong_result_scope = deepcopy(evidence["tool_events"])
    next(event for event in wrong_result_scope if event["tool"] == "list_items" and event["phase"] == "finish")["result"]["scope"]["user_id"] = "user-b"
    assert not grade_tools(**{**evidence, "tool_events": wrong_result_scope})["passed"]


def test_sensitive_denial_requires_execution_rule_and_no_side_effect(fixture_process, tmp_path):
    evidence, _, _, _ = completed_tools(fixture_process, tmp_path)
    scope = evidence["scope"]
    decision = {"scope": scope, "decision": "deny", "reason": "sensitive_operation_denied",
                "approval_required": False, "layer": "tool_execution"}
    assert grade_sensitive(scope, {"writes": []}, [decision], "denied")["passed"]
    assert not grade_sensitive(scope, evidence["ledger"], [decision], "denied")["passed"]
    assert not grade_sensitive(scope, {"writes": []}, [], "denied")["passed"]
    assert not grade_sensitive(scope, {"writes": []}, [decision], "awaiting_approval")["passed"]


def browser_calibration(process, tmp_path):
    prepared = process.prepare(seed=202, experiment_id="browser-grading")["user-a"]
    scope = scope_of(prepared)
    client = FixtureClient(process.base_url, prepared["token"])
    item = next(row for row in client.list_items(page_size=100)["items"] if row["item_id"] == "item-202-08")
    operation = "browser-order-" + item["item_id"]
    client.write(operation, item["item_id"], min(5, item["available_quantity"]))
    receipt = client.receipt(operation)
    workspace = tmp_path / "browser-artifact"
    workspace.mkdir()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["operation_id", "receipt_json"])
    writer.writerow([operation, json.dumps(receipt)])
    (workspace / "result.csv").write_text(output.getvalue())
    site_actions = [{"user": "user-a", "method": method, "path": path, "status": status}
                    for method, path, status in [("POST", "/login", 303), ("GET", "/items?page=1", 200),
                        ("GET", "/items?page=2", 200), ("GET", "/items?page=3", 200), ("GET", "/order", 200),
                        ("POST", "/order", 303), ("GET", "/receipt?operation_id=" + operation, 200),
                        ("GET", "/download?operation_id=" + operation, 200)]]
    actions = [{"scope": scope, "tool": name} for name in ["browser_navigate", "browser_snapshot", "browser_fill_form", "browser_click"]]
    return {"seed": 202, "scope": scope, "workspace": workspace, "ledger": client.ledger(),
            "site_actions": site_actions, "browser_actions": actions, "username": "user-a"}


def test_browser_result_requires_download_contents_and_real_action_evidence(fixture_process, tmp_path):
    evidence = browser_calibration(fixture_process, tmp_path)
    assert grade_browser(**evidence)["passed"]
    assert not grade_browser(**{**evidence, "browser_actions": []})["passed"]
    assert not grade_browser(**{**evidence, "site_actions": []})["passed"]
    (evidence["workspace"] / "result.csv").write_text("looks like a download but contains no receipt")
    assert not grade_browser(**evidence)["passed"]


def test_browser_wrong_user_and_duplicate_writes_fail(fixture_process, tmp_path):
    evidence = browser_calibration(fixture_process, tmp_path)
    assert not grade_browser(**{**evidence, "username": "user-b"})["passed"]
    changed = deepcopy(evidence["ledger"])
    changed["writes"].append(changed["writes"][0].copy())
    assert not grade_browser(**{**evidence, "ledger": changed})["passed"]


def test_browser_receipt_top_operation_and_foreign_ledger_are_checked(fixture_process, tmp_path):
    evidence = browser_calibration(fixture_process, tmp_path)
    path = evidence["workspace"] / "result.csv"
    rows = list(csv.DictReader(io.StringIO(path.read_text())))
    receipt = json.loads(rows[0]["receipt_json"])
    # Real FixtureStore.receipts fields, with only its top-level id corrupted.
    assert set(receipt) == {"operation_id", "write_count", "receipts"}
    receipt["operation_id"] = "different-operation"
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["operation_id", "receipt_json"])
    writer.writerow([rows[0]["operation_id"], json.dumps(receipt)])
    path.write_text(output.getvalue())
    assert not grade_browser(**evidence)["passed"]
    foreign = deepcopy(evidence["ledger"])
    foreign["requests"].append({**foreign["requests"][0], "user_id": "user-b"})
    foreign["request_count"] += 1
    result = grade_browser(**{**evidence, "ledger": foreign})
    assert not next(check for check in result["checks"] if check["name"] == "ledger_scope_and_counts")["passed"]


def test_scope_calibration_rejects_all_deny_and_hidden_target_change():
    operations = ["state", "events", "artifacts", "pause", "resume", "cancel"]
    attempts = []
    for operation in operations:
        attempts.append({"actor": "user-a", "target": "user-b", "operation": operation,
                         "status": 403, "foreign_content": False,
                         "before": {"state": "paused", "writes": 1}, "after": {"state": "paused", "writes": 1}})
        attempts.append({"actor": "user-b", "target": "user-b", "operation": operation, "status": 200})
    assert grade_scope_attempts(attempts)["passed"]
    assert not grade_scope_attempts([row for row in attempts if row["target"] != row["actor"]])["passed"]
    changed = deepcopy(attempts)
    changed[0]["after"]["writes"] = 2
    assert not grade_scope_attempts(changed)["passed"]
    changed = deepcopy(attempts)
    changed[0]["foreign_content"] = True
    assert not grade_scope_attempts(changed)["passed"]
