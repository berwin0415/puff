"""仅校准合成计划与投影，不运行 Docker 或 Agent。

在重新构建的固定验收镜像中运行。这不能证明 I25 资源测量、
原生历史、真实并发业务或资源节省。
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from runner.instance_cost import (CAPS, GIB, CPU, MEMORY, fixed_allocation, make_plan, sha,
    verify_native_projection, rotation_users, INACTIVE_WAKE_KIND)
from scripts import run_instance_cost as host


def plan(strategy="resident", users=2):
    return make_plan(trial_id="idle-calibration", pair_id="same-input", strategy=strategy, users=users,
        candidate_image="sha256:" + "1" * 64, bench_image="sha256:" + "2" * 64, model_name="not-called")


def test_two_worker_full_active_allocation_includes_every_shared_service():
    value = fixed_allocation("resident", 2, phase="active")
    assert value["counts"] == {"worker": 2, "fixtures": 1, "backend": 1, "tools": 2, "model-proxy": 2}
    assert value["allocated_memory_bytes"] == int(5.5 * GIB)
    assert value["allocated_cpu_nano"] == CPU
    assert value["allowed"] is True


@pytest.mark.parametrize("users", (5, 10))
def test_large_resident_inventory_is_rejected_as_planned_allocation_not_observed_oom(users):
    value = plan("resident", users)
    assert value["full_fixed_allocation_allowed"] is False
    assert value["refusal_reason"] == "fixed_planned_allocation_exceeds_total_before_start"
    assert all(item["allocated_memory_bytes"] > MEMORY or item["allocated_cpu_nano"] > CPU for item in value["fixed_allocations"])
    assert all(item["kind"] == "planned_cgroup_allocation_not_observed_RSS_or_framework_capacity" for item in value["fixed_allocations"])


@pytest.mark.parametrize("users", (2, 5, 10))
def test_on_demand_keeps_two_planned_active_workers_and_does_not_claim_task_completion(users):
    value = plan("on-demand", users)
    assert value["full_fixed_allocation_allowed"] is True
    idle, active = value["fixed_allocations"]
    assert idle["counts"]["worker"] == 0
    assert active["counts"]["worker"] == 2
    assert active["allocated_memory_bytes"] == int(5.5 * GIB)
    assert value["i25_complete"] is False and value["i24_authorization_verified"] is False
    assert value["business_tasks_bound"] == 0 and value["remaining_work"]


@pytest.mark.parametrize("users", (5, 10))
@pytest.mark.parametrize("strategy", ("on-demand", "resident"))
def test_rotation_freezes_previously_inactive_pair_without_lowering_resident_allocation(users, strategy):
    options = dict(trial_id="rotation-calibration", pair_id="same-input", strategy=strategy, users=users,
        candidate_image="sha256:" + "1" * 64, bench_image="sha256:" + "2" * 64, model_name="not-called")
    legacy, rotated = make_plan(**options), make_plan(**options, rotate_inactive_users=True)
    assert rotation_users(legacy) == [] and "flow_variant" not in legacy
    assert rotation_users(rotated) == ["user-003", "user-004"]
    assert rotated["inactive_user_rotation"] == {"kind": INACTIVE_WAKE_KIND,
        "users": ["user-003", "user-004"], "after": "wake"}
    assert rotated["fixed_allocations"] == legacy["fixed_allocations"]
    assert rotated["full_fixed_allocation_allowed"] is (strategy == "on-demand")
    assert rotated["i25_complete"] is False


@pytest.mark.parametrize("users,enabled", ((2, True), (5, 1), (10, "yes")))
def test_rotation_requires_an_explicit_boolean_and_sufficient_users(users, enabled):
    with pytest.raises(ValueError, match='明确选择五用户或十用户'):
        make_plan(trial_id="rotation-calibration", pair_id="same-input", strategy="on-demand", users=users,
            candidate_image="sha256:" + "1" * 64, bench_image="sha256:" + "2" * 64,
            model_name="not-called", rotate_inactive_users=enabled)


def projection(tmp_path):
    user = {"owner": "user-001", "history_task_id": "history-user-001", "sentinel": "synthetic-history",
        "scope": {"experiment_id": "instance-cost-idle", "variant": "synthetic", "user_id": "user-001", "session_id": "main"}}
    path = tmp_path / "users/user-001/evidence/history/native-state-synthetic.json"
    path.parent.mkdir(parents=True)
    value = {"values": {"messages": [{"id": "prepared-history-history-user-001", "content": user["sentinel"]}]},
        "next": [], "tasks": [], "interrupts": [], "config": {"configurable": {"thread_id": "main"}}}
    path.write_text(json.dumps(value))
    response = {"checkpoint_id": "synthetic-original-checkpoint", "scope": user["scope"],
        "source": {"path": path.name, "sha256": sha(path.read_bytes()), "public_api": "CompiledStateGraph.aget_state"}}
    return user, path, value, response


def test_exact_nonempty_original_sentinel_has_a_bound_source(tmp_path):
    user, path, _, response = projection(tmp_path)
    checked = verify_native_projection(tmp_path, user, response)
    assert checked["sha256"] == sha(path.read_bytes())
    assert checked["native_checkpoint_id"] == response["checkpoint_id"]
    assert "no_I24" in checked["trust_limit"]


@pytest.mark.parametrize("change", ("content", "thread", "pending", "scope", "hash", "path"))
def test_candidate_labels_cannot_replace_the_original_nonempty_projection(tmp_path, change):
    user, path, value, response = projection(tmp_path)
    if change == "content":
        value["values"]["messages"][0]["content"] = "other-user-sentinel"
    elif change == "thread":
        value["config"]["configurable"]["thread_id"] = "other-thread"
    elif change == "pending":
        value["next"] = ["model"]
    elif change == "scope":
        response["scope"] = {**user["scope"], "user_id": "user-002"}
    elif change == "path":
        response["source"]["path"] = "../native-state-other.json"
    if change in {"content", "thread", "pending"}:
        path.write_text(json.dumps(value))
        response["source"]["sha256"] = sha(path.read_bytes())
    if change == "hash":
        response["source"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        verify_native_projection(tmp_path, user, response)


@pytest.mark.parametrize("message", (
    "error: no such object: ", "Error: No such object: ",
    "Error response from daemon: No such container: ",
))
def test_cleanup_accepts_case_variants_only_for_the_exact_missing_owned_name(tmp_path, message):
    lab = host.IdleDockerLab(tmp_path, plan())
    name = "hl-ic-idle-calibration-idle-user-001"
    lab.owned.append(name)
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "[]\n", message + name + "\n")
    lab.call = call
    assert lab.cleanup() == []
    assert calls == [["docker", "inspect", name]]


@pytest.mark.parametrize("stdout,stderr", (
    ("[]", "error: no such object: unrelated"),
    ("[]", "daemon unavailable; no such object: {name}"),
    ("", "error: no such object: {name}"),
    ("[]", "error: no such object: {name}\nadditional failure"),
    ("[]", "error: no such object: {name}-other"),
))
def test_cleanup_retains_unconfirmed_absence_and_never_removes_another_object(tmp_path, stdout, stderr):
    lab = host.IdleDockerLab(tmp_path, plan())
    name = "hl-ic-idle-calibration-idle-user-001"
    lab.owned.append(name)
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout, stderr.format(name=name))
    lab.call = call
    assert lab.cleanup() == [name]
    assert calls == [["docker", "inspect", name]]


@pytest.mark.parametrize("change", ("none", "name", "id", "trial"))
def test_cleanup_requires_exact_inspected_identity_and_trial_before_removal(tmp_path, change):
    lab = host.IdleDockerLab(tmp_path, plan())
    name = "hl-ic-idle-calibration-idle-user-001"
    identifier = "a" * 64
    lab.owned.append(name)
    observed = {"Name": "/" + name, "Id": identifier, "Config": {"Labels": {
        "org.harness-lab.instance-cost-trial": "idle-calibration"}}}
    if change == "name":
        observed["Name"] = "/another-object"
    elif change == "id":
        observed["Id"] = "short-id"
    elif change == "trial":
        observed["Config"]["Labels"]["org.harness-lab.instance-cost-trial"] = "another-trial"
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0,
            json.dumps([observed]) if command[1] == "inspect" else identifier + "\n", "")
    lab.call = call
    assert lab.cleanup() == ([] if change == "none" else [name])
    assert calls == ([["docker", "inspect", name], ["docker", "rm", "-f", identifier]]
        if change == "none" else [["docker", "inspect", name]])
