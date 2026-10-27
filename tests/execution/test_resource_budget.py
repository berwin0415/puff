from copy import deepcopy

import pytest

from runner.resource_budget import allocation_verdict

GIB = 1024 ** 3
CPU = 10 ** 9


def material():
    # 两个实际 Worker 与一个共享辅助服务共用同一总预算。
    rows = [{"id": char * 64, "memory_limit": memory, "cpu_nano": cpu,
             "state": {"Running": True}}
            for char, memory, cpu in (("a", 2 * GIB, CPU),
                                      ("b", 2 * GIB, CPU),
                                      ("c", GIB, CPU // 2))]
    inventory = [{"id": row["id"], "memory_limit": row["memory_limit"],
                  "cpu_nano": row["cpu_nano"], "running": True} for row in rows]
    return rows, inventory


def grade(rows, inventory, **kwargs):
    return allocation_verdict(rows, inventory, memory_bytes=6 * GIB,
                              cpu_nano=3 * CPU, active_task_ids=("A", "B"), **kwargs)


def test_complete_two_worker_phase():
    result = grade(*material())
    assert result["passed"]
    assert result["allocated_memory_bytes"] == 5 * GIB
    assert result["allocated_cpu_nano"] == 2.5 * CPU
    assert result["active_tasks"] == 2


def test_omitted_live_grader_is_detected():
    rows, inventory = material()
    grader = deepcopy(rows[-1])
    grader["id"] = "d" * 64
    rows.append(grader)
    result = grade(rows, inventory)
    assert not result["passed"]
    assert not next(check for check in result["checks"]
                    if check["name"] == "allocation_complete_inventory")["passed"]


@pytest.mark.parametrize("field,value", [("memory_limit", 0), ("cpu_nano", 0),
                                          ("memory_limit", True), ("cpu_nano", None)])
def test_unlimited_or_missing_caps_rejected(field, value):
    rows, inventory = material()
    rows[0][field] = inventory[0][field] = value
    assert not grade(rows, inventory)["passed"]


@pytest.mark.parametrize("field,value", [("memory_limit", 5 * GIB),
                                          ("cpu_nano", 2 * CPU)])
def test_total_budget_cannot_multiply_per_user(field, value):
    rows, inventory = material()
    rows[0][field] = inventory[0][field] = value
    assert not grade(rows, inventory)["passed"]


def test_stopped_worker_is_retained_but_has_no_live_allocation():
    rows, inventory = material()
    rows[0]["state"]["Running"] = inventory[0]["running"] = False
    result = grade(rows, inventory)
    assert result["passed"]
    assert result["allocated_memory_bytes"] == 3 * GIB


def test_unexpected_worker_start_is_detected():
    rows, inventory = material()
    inventory[0]["running"] = False
    assert not grade(rows, inventory)["passed"]


def test_duplicate_or_short_id_cannot_hide_a_worker():
    for identity in ("a" * 64, "b" * 12):
        rows, inventory = material()
        rows[1]["id"] = inventory[1]["id"] = identity
        assert not grade(rows, inventory)["passed"]


def test_three_active_tasks_rejected_even_under_resource_budget():
    rows, inventory = material()
    result = allocation_verdict(rows, inventory, memory_bytes=6 * GIB,
                                cpu_nano=3 * CPU, active_task_ids=("A", "B", "C"))
    assert not result["passed"]


def test_live_cap_drift_is_detected_even_if_total_remains_low():
    rows, inventory = material()
    rows[-1]["memory_limit"] //= 2
    assert not grade(rows, inventory)["passed"]


@pytest.mark.parametrize("rows,inventory", [(None, []), ([None], []), ([], None)])
def test_malformed_evidence_fails_closed(rows, inventory):
    assert not grade(rows, inventory)["passed"]


@pytest.mark.parametrize("identity", [[], {}, 1, None])
def test_malformed_id_is_rejected_without_crashing(identity):
    rows, inventory = material()
    rows[0]["id"] = identity
    assert not grade(rows, inventory)["passed"]
