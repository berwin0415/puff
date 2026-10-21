"""Validate the complete Docker allocation for a measurement phase.

The host discovers containers owned by this experiment. It supplies all of
them, including short-lived graders, alongside an explicit phase inventory.
This checks configured allocations, not sampled memory use or host capacity.
"""
from __future__ import annotations


def allocation_verdict(containers, inventory, *, memory_bytes, cpu_nano,
                       active_task_ids=(), max_active_tasks=2):
    checks = []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    limits_ok = (type(memory_bytes) is int and memory_bytes > 0
                 and type(cpu_nano) is int and cpu_nano > 0
                 and type(max_active_tasks) is int and max_active_tasks > 0)
    check("allocation_limits", limits_ok, "Positive finite total limits are required")
    rows_ok = (isinstance(containers, list) and isinstance(inventory, list)
               and all(isinstance(row, dict) for row in containers + inventory))
    check("allocation_records", rows_ok, "Observed and planned container records must be lists of objects")
    if not rows_ok:
        return {"passed": False, "checks": checks, "allocated_memory_bytes": None,
                "allocated_cpu_nano": None}

    def ids(rows):
        return [row.get("id") for row in rows]

    observed, planned = ids(containers), ids(inventory)
    identities_ok = (all(isinstance(value, str) and len(value) == 64
                         and all(char in "0123456789abcdef" for char in value)
                         for value in observed + planned)
                     and len(set(observed)) == len(observed)
                     and len(set(planned)) == len(planned))
    check("allocation_unique_ids", identities_ok, "Use complete unique Docker IDs in both inventories")
    complete = identities_ok and set(observed) == set(planned)
    check("allocation_complete_inventory", complete,
          "Every owned container must appear exactly once; no worker or grader may be omitted")
    expected = {row["id"]: row for row in inventory} if identities_ok else {}
    caps_ok = identities_ok
    phase_ok = identities_ok
    memory_total = cpu_total = 0
    for row in containers:
        declaration = expected.get(row.get("id"), {}) if identities_ok else {}
        state = row.get("state")
        running = state.get("Running") if isinstance(state, dict) else None
        phase_ok = phase_ok and type(running) is bool and declaration.get("running") is running
        actual_memory, actual_cpu = row.get("memory_limit"), row.get("cpu_nano")
        finite = (type(actual_memory) is int and actual_memory > 0
                  and type(actual_cpu) is int and actual_cpu > 0)
        caps_ok = (caps_ok and finite
                   and type(declaration.get("memory_limit")) is int
                   and type(declaration.get("cpu_nano")) is int
                   and declaration.get("memory_limit") == actual_memory
                   and declaration.get("cpu_nano") == actual_cpu)
        if running is True and finite:
            memory_total += actual_memory
            cpu_total += actual_cpu
    check("allocation_phase_membership", phase_ok,
          "Observed running state must match the frozen phase inventory")
    check("allocation_finite_caps", caps_ok,
          "Every container must have explicit measured memory and CPU caps matching its inventory")
    check("allocation_memory_budget", limits_ok and caps_ok and memory_total <= memory_bytes,
          "Sum the caps of every running owned container, including shared services and graders")
    check("allocation_cpu_budget", limits_ok and caps_ok and cpu_total <= cpu_nano,
          "CPU quota is total allocation, not a per-user allowance")
    task_ids_ok = (isinstance(active_task_ids, (tuple, list))
                   and all(isinstance(value, str) and value for value in active_task_ids)
                   and len(set(active_task_ids)) == len(active_task_ids))
    check("allocation_active_task_limit", limits_ok and task_ids_ok
          and len(active_task_ids) <= max_active_tasks,
          "Count distinct real active tasks separately from retained user states")
    return {"passed": all(row["passed"] for row in checks), "checks": checks,
            "allocated_memory_bytes": memory_total, "allocated_cpu_nano": cpu_total,
            "running_containers": sum(row.get("state", {}).get("Running") is True
                                      for row in containers if isinstance(row.get("state"), dict)),
            "active_tasks": len(active_task_ids) if task_ids_ok else None,
            "scope": "One complete owned-container phase; does not prove task overlap or task success"}
