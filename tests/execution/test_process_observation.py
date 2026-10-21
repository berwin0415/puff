"""Adversarial process-evidence calibration; no Docker/candidate/model is invoked.

Run these tests in the rebuilt bench image. Synthetic records exercise the same
host manifest projection retained by screening and detailed orchestration.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pytest

from runner import process_observation as process


@pytest.fixture
def evidence():
    expected = process.WorkerExpectation(
        run_id="i18-openclaw-tools-001", candidate="openclaw", case="tools",
        container_name="harness-lab-detailed-openclaw-i18-openclaw-tools-001",
        container_id="a" * 64, image_id="sha256:" + "b" * 64,
        image_tag="harness-lab-openclaw:local", memory_limit=2 ** 31, cpu_nano=10 ** 9)
    state = {"Status": "exited", "Running": False, "Paused": False, "Restarting": False,
             "OOMKilled": False, "Dead": False, "Pid": 0, "ExitCode": 0, "Error": ""}
    worker = {"id": expected.container_id, "name": "/" + expected.container_name,
              "image_id": expected.image_id, "image_tag": expected.image_tag,
              "state": state, "memory_limit": expected.memory_limit, "cpu_nano": expected.cpu_nano}
    identity = {key: getattr(expected, key) for key in ("run_id", "candidate", "case")}
    host = {**identity, "candidate_exit_code": 0}
    # A service may be active; another may have been killed during normal cleanup.
    manifest = {**host, "containers": [worker,
        {"id": "c" * 64, "name": "/harness-lab-fixtures-1", "image_id": "sha256:" + "d" * 64,
         "image_tag": "harness-lab-services:local", "state": {"Running": True}},
        {"id": "e" * 64, "name": "/harness-lab-model-proxy-1", "image_id": "sha256:" + "d" * 64,
         "image_tag": "harness-lab-services:local", "state": {"Status": "exited", "ExitCode": 137}}]}
    return manifest, host, expected


def grade(evidence):
    return process.grade_process_observation(*evidence)


def test_healthy_unique_worker_does_not_require_services_to_be_exited(evidence):
    original = deepcopy(evidence)
    result = grade(evidence)
    assert result["passed"]
    assert all(check["passed"] is True for check in result["checks"])
    assert evidence == original
    assert "no task" in result["claim"]


def test_declared_native_cancel_exit_one_is_not_treated_as_task_success(evidence):
    manifest, host, expected = evidence
    expected = replace(expected, case="cancel", expected_exit_code=1)
    manifest["case"] = host["case"] = "cancel"
    manifest["candidate_exit_code"] = host["candidate_exit_code"] = 1
    manifest["containers"][0]["state"]["ExitCode"] = 1
    assert process.grade_process_observation(manifest, host, expected)["passed"]


@pytest.mark.parametrize("flag", ["Running", "Paused", "Restarting", "OOMKilled", "Dead"])
@pytest.mark.parametrize("bad", [True, 0, None, "false"])
def test_bad_or_non_boolean_worker_flags_fail(evidence, flag, bad):
    evidence[0]["containers"][0]["state"][flag] = bad
    assert not grade(evidence)["passed"]


@pytest.mark.parametrize("field", ["Status", "Running", "Paused", "Restarting", "OOMKilled", "Dead", "Pid", "ExitCode", "Error"])
def test_missing_required_worker_observation_cannot_pass(evidence, field):
    del evidence[0]["containers"][0]["state"][field]
    assert not grade(evidence)["passed"]


@pytest.mark.parametrize("field,value", [
    ("Status", "created"), ("Status", "dead"), ("Status", "restarting"),
    ("Pid", 7), ("Pid", False), ("ExitCode", True), ("ExitCode", "0"),
    ("ExitCode", 137), ("Error", "container runtime failure"), ("Error", None)])
def test_terminal_state_and_exit_are_not_inferred_from_running_false(evidence, field, value):
    evidence[0]["containers"][0]["state"][field] = value
    assert not grade(evidence)["passed"]


@pytest.mark.parametrize("part", ["manifest", "host"])
@pytest.mark.parametrize("field,value", [("run_id", "foreign-run"), ("candidate", "deepseek_harness"),
    ("case", "browser"), ("candidate_exit_code", False), ("candidate_exit_code", 1), ("candidate_exit_code", None)])
def test_independent_identity_and_host_exit_must_correspond(evidence, part, field, value):
    evidence[0 if part == "manifest" else 1][field] = value
    assert not grade(evidence)["passed"]


@pytest.mark.parametrize("field,value", [("id", "f" * 64), ("name", "/foreign-worker"),
    ("image_id", "sha256:" + "f" * 64), ("image_tag", "foreign:local"),
    ("memory_limit", 0), ("memory_limit", True), ("memory_limit", 2 ** 30),
    ("cpu_nano", None), ("cpu_nano", 500_000_000), ("cpu_nano", "1000000000")])
def test_partial_identity_match_or_wrong_resource_pin_is_not_enough(evidence, field, value):
    evidence[0]["containers"][0][field] = value
    assert not grade(evidence)["passed"]


@pytest.mark.parametrize("duplicate_kind", ["identical", "same_id", "same_name", "same_tag", "same_image"])
def test_ambiguous_workers_cannot_be_hidden_by_changing_only_one_selector(evidence, duplicate_kind):
    worker = deepcopy(evidence[0]["containers"][0])
    if duplicate_kind != "identical":
        worker.update({"id": "f" * 64, "name": "/different-worker", "image_tag": "different:local",
                       "image_id": "sha256:" + "9" * 64})
        key = {"same_id": "id", "same_name": "name", "same_tag": "image_tag", "same_image": "image_id"}[duplicate_kind]
        worker[key] = evidence[0]["containers"][0][key]
    evidence[0]["containers"].append(worker)
    assert not grade(evidence)["passed"]


@pytest.mark.parametrize("inventory", [None, [], {}, [None]])
def test_absent_or_malformed_inventory_fails(evidence, inventory):
    evidence[0]["containers"] = inventory
    assert not grade(evidence)["passed"]


def test_auxiliary_service_cannot_substitute_for_missing_worker(evidence):
    evidence[0]["containers"].pop(0)
    assert not grade(evidence)["passed"]
    assert not process.grade_process_observation(evidence[0], None, evidence[2])["passed"]


@pytest.mark.parametrize("field,value", [("expected_exit_code", 137), ("expected_exit_code", True),
    ("container_id", "a" * 12), ("image_id", "latest"), ("memory_limit", True),
    ("cpu_nano", 0), ("container_name", "/a/b"), ("image_tag", "")])
def test_invalid_expectation_cannot_launder_an_unhealthy_observation(evidence, field, value):
    with pytest.raises(ValueError):
        replace(evidence[2], **{field: value})


def save(path, value):
    path.write_text(json.dumps(value) + "\n")


def stored(tmp_path, evidence):
    manifest, host, expected = evidence
    paths = {key: tmp_path / name for key, name in (
        ("manifest", "manifest.json"), ("host", "orchestration-status.json"),
        ("expected", "expectation.json"), ("verdict", "verdict.json"))}
    for key, value in (("manifest", manifest), ("host", host), ("expected", asdict(expected)),
                       ("verdict", {"passed": True, "claim": "original task result"})):
        save(paths[key], value)
    return paths


def test_offline_snapshot_pass_does_not_invent_independent_identity_or_change_original(tmp_path, evidence):
    paths = stored(tmp_path, evidence)
    before = {key: path.read_bytes() for key, path in paths.items()}
    result = process.audit_manifest(paths["manifest"])
    assert result["snapshot_health"]["status"] == "passed"
    assert result["independent_identity_check"]["status"] == "unknown"
    assert result["status"] == "unknown"
    assert result["sources"]["manifest"]["sha256"] == hashlib.sha256(before["manifest"]).hexdigest()
    assert result["sources"]["original_verdict_unmodified"]["sha256"] == hashlib.sha256(before["verdict"]).hexdigest()
    assert before == {key: path.read_bytes() for key, path in paths.items()}


def test_oom_fails_even_when_old_verdict_passes_and_identity_is_unknown(tmp_path, evidence):
    evidence[0]["containers"][0]["state"]["OOMKilled"] = True
    paths = stored(tmp_path, evidence)
    result = process.audit_manifest(paths["manifest"])
    assert result["snapshot_health"]["status"] == result["status"] == "failed"
    assert result["independent_identity_check"]["status"] == "unknown"
    assert json.loads(paths["verdict"].read_text())["passed"] is True


@pytest.mark.parametrize("oom,expected_status", [(True, "failed"), (False, "unknown")])
def test_screening_manifest_without_case_retains_oom_and_unknown_exit_policy(tmp_path, evidence, oom, expected_status):
    del evidence[0]["case"]
    evidence[0]["containers"][0]["state"]["OOMKilled"] = oom
    paths = stored(tmp_path, evidence)
    result = process.audit_manifest(paths["manifest"])
    assert result["snapshot_health"]["status"] == result["status"] == expected_status
    checks = {check["name"]: check["passed"] for check in result["snapshot_health"]["checks"]}
    assert checks["docker_no_oom"] is (not oom)
    assert checks["docker_host_expected_exit"] is None
    assert result["independent_identity_check"]["status"] == "unknown"


def test_old_incomplete_budget_record_keeps_missing_oom_unknown(tmp_path, evidence):
    evidence[0]["containers"][0]["state"] = {"Status": "exited", "ExitCode": 0}
    paths = stored(tmp_path, evidence)
    result = process.audit_manifest(paths["manifest"])
    assert result["snapshot_health"]["status"] == result["status"] == "unknown"
    assert next(check for check in result["snapshot_health"]["checks"] if check["name"] == "docker_no_oom")["passed"] is None


def test_independent_pins_and_host_can_establish_strict_acceptance(tmp_path, evidence):
    paths = stored(tmp_path, evidence)
    result = process.audit_manifest(paths["manifest"], expectation_path=paths["expected"], host_path=paths["host"])
    assert result["status"] == "passed"
    assert result["independent_identity_check"]["status"] == "passed"
    strict = process.audit_files(paths["manifest"], paths["host"], paths["expected"], paths["verdict"])
    assert strict["passed"]


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '{"x":-1e999}', '[]'])
def test_ambiguous_or_non_object_json_is_not_accepted(tmp_path, payload):
    path = tmp_path / "manifest.json"
    path.write_text(payload)
    with pytest.raises(ValueError):
        process.read_evidence(path)


def test_read_evidence_rejects_symlink_files_and_directories(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    save(original / "manifest.json", {})
    link = tmp_path / "linked.json"
    link.symlink_to(original / "manifest.json")
    alias = tmp_path / "alias"
    alias.symlink_to(original, target_is_directory=True)
    for path in (link, alias / "manifest.json", original):
        with pytest.raises(ValueError):
            process.read_evidence(path)


def test_container_cli_writes_separate_audit_once_and_summarizes_only_identity_status(tmp_path, evidence, monkeypatch, capsys):
    from runner import bench
    monkeypatch.setattr(bench, "environment", lambda: {"model_credentials_injected": False, "synthetic_calibration_only": True})
    paths = stored(tmp_path, evidence)
    output = tmp_path / "process-audit.json"
    args = ["--run-dir", str(tmp_path), "--output", str(output)]
    assert process.main(args) == 1  # Unknown identity is not healthy strict acceptance.
    summary = json.loads(capsys.readouterr().out)
    assert summary["run_id"] == evidence[2].run_id and summary["status"] == "unknown"
    assert "observed_worker" not in summary and "sources" not in summary
    original_bytes = output.read_bytes()
    with pytest.raises(FileExistsError):
        process.main(args)
    assert output.read_bytes() == original_bytes
    assert json.loads(paths["verdict"].read_text())["passed"] is True


def test_cli_accepts_explicit_multiple_manifests_without_scanning(tmp_path, evidence, monkeypatch):
    from runner import bench
    monkeypatch.setattr(bench, "environment", lambda: {"model_credentials_injected": False})
    paths = stored(tmp_path, evidence)
    other = tmp_path / "other"
    other.mkdir()
    second = deepcopy(evidence[0])
    second["containers"][0]["state"]["OOMKilled"] = True
    save(other / "manifest.json", second)
    output = tmp_path / "process-audit.json"
    assert process.main(["--manifest", str(paths["manifest"]), "--run-dir", str(other), "--output", str(output)]) == 1
    audit = json.loads(output.read_text())
    assert [run["status"] for run in audit["runs"]] == ["unknown", "failed"]
