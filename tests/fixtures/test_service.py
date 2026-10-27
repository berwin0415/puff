"""真实 HTTP、进程重启、副作用和可信身份边界测试。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from execution.tools.basic import FixtureClient, FixtureHTTPError, FixtureTransportError


def client_for(process, scope):
    return FixtureClient(process.base_url, scope["token"])


def test_seeded_catalogue_paginates_and_setup_never_resets_ledger(fixture_process):
    process = fixture_process
    catalogues = []
    for seed in (101, 202, 303):
        scope = process.prepare(seed=seed, experiment_id=f"seed-{seed}")["user-a"]
        client = client_for(process, scope)
        page = 1
        catalogue = []
        while page is not None:
            data = client.list_items(page=page, page_size=4)
            catalogue.extend(data["items"])
            page = data["next_page"]
        assert len(catalogue) == data["total"] == scope["item_count"]
        assert len(catalogue) > 4
        assert len({item["item_id"] for item in catalogue}) == len(catalogue)
        catalogues.append(catalogue)
        client.write("ordinary-write", catalogue[0]["item_id"], 1)
        repeated = process.prepare(seed=seed, experiment_id=f"seed-{seed}")["user-a"]
        assert repeated["token"] == scope["token"]
        assert client.ledger()["write_count"] == 1
        assert client.list_items(page_size=100)["items"] == catalogue
    assert len({len(catalogue) for catalogue in catalogues}) == 3
    assert len({catalogue[0]["unit_price_cents"] for catalogue in catalogues}) == 3
    assert process.admin_token not in process.startup_line
    conflict = process.request("POST", "/admin/scenarios", json={"experiment_id": "seed-101", "variant": "baseline", "seed": 202, "users": [{"user_id": "user-a", "session_id": "session-a"}]})
    assert conflict.status_code == 409


def test_token_binding_blocks_cross_scope_queries_writes_receipts_and_admin(fixture_process):
    process = fixture_process
    scopes = process.prepare()
    a, b = scopes["user-a"], scopes["user-b"]
    client_a, client_b = client_for(process, a), client_for(process, b)
    item_a, item_b = client_a.list_items()["items"][0], client_b.list_items()["items"][0]
    assert client_a.list_items()["items"] != client_b.list_items()["items"]
    client_b.write("b-only", item_b["item_id"], 1)
    for field in ("experiment_id", "variant", "user_id", "session_id"):
        other = b[field] if b[field] != a[field] else "another-identity"
        response = process.request("GET", "/ledger", token=a["token"], params={field: other})
        assert response.status_code == 403
        response = process.request("POST", "/writes", token=a["token"], json={"operation_id": "forged", "item_id": item_a["item_id"], "quantity": 1, field: other})
        assert response.status_code == 403
    with pytest.raises(FixtureHTTPError) as failure:
        client_a.receipt("b-only")
    assert failure.value.status_code == 404
    assert client_a.ledger()["write_count"] == 0
    assert client_b.ledger()["write_count"] == 1
    assert process.request("GET", "/admin/ledger", token=a["token"], params={"experiment_id": "tools", "variant": "baseline"}).status_code == 403
    variant = process.prepare(variant="fault-run")["user-a"]
    assert variant["token"] != a["token"]
    assert client_for(process, variant).ledger()["request_count"] == 0


@pytest.mark.parametrize("mode,expected_writes", [("idempotent", 1), ("non_idempotent", 2)])
def test_request_count_is_separate_from_actual_writes(fixture_process, mode, expected_writes):
    scope = fixture_process.prepare(write_mode=mode)["user-a"]
    client = client_for(fixture_process, scope)
    item = client.list_items()["items"][0]
    client.write("same-operation", item["item_id"], 1)
    client.write("same-operation", item["item_id"], 1)
    ledger = client.ledger()
    assert ledger["request_count"] == 2
    assert ledger["write_count"] == expected_writes
    assert client.receipt("same-operation")["write_count"] == expected_writes
    assert sum(write["total_cents"] for write in ledger["writes"]) == expected_writes * item["unit_price_cents"]


def test_fail_once_is_consumed_before_write_and_survives_process_restart(fixture_process):
    process = fixture_process
    scope = process.prepare()["user-a"]
    client = client_for(process, scope)
    item = client.list_items()["items"][0]
    process.fault(scope, "retry-operation", "fail_once")
    with pytest.raises(FixtureHTTPError) as failure:
        client.write("retry-operation", item["item_id"], 1)
    assert failure.value.status_code == 503
    assert client.ledger()["request_count"] == 1
    assert client.ledger()["write_count"] == 0
    old_pid = process.process.pid
    process.restart()
    assert process.process.pid != old_pid
    assert process.fault(scope, "retry-operation", "fail_once")["consumed"] == 1
    client = client_for(process, scope)
    client.write("retry-operation", item["item_id"], 1)
    assert client.ledger()["request_count"] == 2
    assert client.ledger()["write_count"] == 1


@pytest.mark.parametrize("mode,expected_writes", [("idempotent", 1), ("non_idempotent", 2)])
def test_write_commits_before_lost_receipt_and_restart_does_not_erase_it(fixture_process, mode, expected_writes):
    process = fixture_process
    scope = process.prepare(write_mode=mode)["user-a"]
    client = client_for(process, scope)
    item = client.list_items()["items"][0]
    process.fault(scope, "lost-receipt", "drop_receipt_once")
    with pytest.raises(FixtureTransportError, match='回执'):
        client.write("lost-receipt", item["item_id"], 1)
    ledger = client.ledger()
    assert (ledger["request_count"], ledger["write_count"]) == (1, 1)
    assert ledger["requests"][0]["status"] == 0
    assert client.receipt("lost-receipt")["write_count"] == 1
    process.restart()
    client = client_for(process, scope)
    assert client.receipt("lost-receipt")["write_count"] == 1
    client.write("lost-receipt", item["item_id"], 1)
    assert (client.ledger()["request_count"], client.ledger()["write_count"]) == (2, expected_writes)


def test_sensitive_rule_rejects_before_side_effect_after_retry_and_restart(fixture_process):
    process = fixture_process
    scope = process.prepare(write_mode="non_idempotent")["user-a"]
    process.fault(scope, "sensitive-attempt", "drop_receipt_once")
    for attempt in range(3):
        if attempt == 2:
            process.restart()
        client = client_for(process, scope)
        with pytest.raises(FixtureHTTPError) as failure:
            client.sensitive_export("sensitive-attempt")
        assert failure.value.status_code == 403
        assert failure.value.payload["approval_required"] is False
    ledger = client.ledger()
    assert (ledger["request_count"], ledger["write_count"]) == (3, 0)
    assert ledger["faults"][0]["consumed"] == 0
    item = client.list_items()["items"][0]
    client.write("ordinary-authorized", item["item_id"], 1)
    assert client.ledger()["write_count"] == 1


def test_agent_cannot_remove_fault_or_change_service_idempotence(fixture_process):
    process = fixture_process
    scope = process.prepare(write_mode="non_idempotent")["user-a"]
    process.fault(scope, "fault-operation", "fail_once")
    client = client_for(process, scope)
    item = client.list_items()["items"][0]
    for control in ({"mode": "idempotent"}, {"fault": "none"}):
        response = process.request("POST", "/writes", token=scope["token"], json={"operation_id": "fault-operation", "item_id": item["item_id"], "quantity": 1, **control})
        assert response.status_code == 403
    with pytest.raises(FixtureHTTPError) as failure:
        client.write("fault-operation", item["item_id"], 1)
    assert failure.value.status_code == 503
    assert client.ledger()["write_count"] == 0


def test_concurrent_idempotent_requests_are_atomic_and_users_remain_separate(fixture_process):
    process = fixture_process
    scopes = process.prepare()
    a, b = scopes["user-a"], scopes["user-b"]
    item_id = client_for(process, a).list_items()["items"][0]["item_id"]
    def submit(scope):
        return client_for(process, scope).write("concurrent-operation", item_id, 1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, [a, b] * 4))
    assert len(results) == 8
    for scope in (a, b):
        ledger = client_for(process, scope).ledger()
        assert (ledger["request_count"], ledger["write_count"]) == (4, 1)
        assert {row["user_id"] for row in ledger["requests"] + ledger["writes"]} == {scope["user_id"]}
