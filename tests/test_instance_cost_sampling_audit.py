"""合成离线审计校准，仅在固定 Docker 验收镜像内运行。"""
from __future__ import annotations

import copy
import json

import pytest

from scripts import audit_instance_cost_sampling as audit


def material():
    phases = ("initial-idle", "starting-active", "active", "closing-active", "business-state-readback",
        "starting-wake", "wake", "closing-wake", "independent-fault-preparation", "starting-fault", "closing-fault")
    samples, controls = [], {}
    sequence = 1
    for number, phase in enumerate(phases, 1):
        start = number * 2.0
        row = {"phase": phase, "sample_sequence": number, **dict(zip(audit.TIMES, (start, start + .1, start + .3, start + .5)))}
        for key in ("observer_before", "observer_after"):
            control = {"sequence": sequence, "phase": phase, "kind": None, "probe_nonce": f"{sequence:032x}"}
            controls[sequence] = control
            row[key] = {"control_sequence": sequence, "phase": phase, "kind": None, "probe_nonce": control["probe_nonce"]}
            sequence += 2  # 实际阶段切换或其他探针也可能消耗 ID。
        samples.append(row)
    status = {"started_monotonic": 1.0, "finished_monotonic": 23.0, "samples_written": len(samples), "completed": True}
    log = {"complete_json_lines": True, "line_count": len(samples), "invalid_line_numbers": []}
    return samples, controls, status, log


def run(samples, controls, status, log, **kwargs):
    return audit.analyze(samples, log=log, controls=controls, status=status,
        host_status=kwargs.get("host_status", copy.deepcopy(status)))


def test_complete_evidence_checks_every_recorded_control_phase_without_filtering():
    values = material()
    result = run(*values)
    assert result["passed"] is True and result["audit_status"] == "passed"
    assert result["all_recorded_phases_checked"] is True
    assert result["phases"]["closing-fault"]["samples"] == 1
    assert result["whole_flow_edges"] == {"start_seconds": 1.0, "stop_seconds": .5}


def test_legacy_missing_sequence_completion_and_edges_remain_unknown_not_zero():
    samples, controls, _, log = material()
    for row in samples:
        row.pop("sample_sequence")
    result = run(samples, controls, None, log)
    assert result["audit_status"] == "unverified" and result["passed"] is False
    assert result["observed_intervals_within_five_seconds"] is True
    assert result["checks"]["sample_sequence_complete"] is None
    assert result["checks"]["sampler_completed"] is None
    assert result["whole_flow_edges"] == {"start_seconds": None, "stop_seconds": None}


@pytest.mark.parametrize("strategy", ("on-demand", "resident"))
@pytest.mark.parametrize("change", (None, "nonce", "phase", "same-nonce-new-sequence"))
def test_old_on_demand_and_resident_control_formats_are_understood_without_fabricating_missing_metadata(strategy, change):
    samples, controls, _, log = material()
    for row in samples:
        row.pop("sample_sequence")
        row.update(time="2026-10-21T00:00:00+00:00", containers=[], process_observations=[],
            docker_observations=[], planned_inventory=[])
        kind = row["phase"] if row["phase"] in {"active", "wake", "fault"} else None
        for key in ("observer_before", "observer_after"):
            observation = row[key]
            control = controls[observation["control_sequence"]]
            observation.update(time="2026-10-21T00:00:00+00:00", clock_domain="Docker_VM_UTC",
                tasks=[{"body": "SECRET MODEL BODY"}], model_or_tool_action_requested=False, kind=kind)
            control["kind"] = kind
    first_phase = "starting-resident-history" if strategy == "resident" else "initial-idle"
    samples[0]["phase"] = first_phase
    for key in ("observer_before", "observer_after"):
        samples[0][key]["phase"] = first_phase
        controls[samples[0][key]["control_sequence"]]["phase"] = first_phase
    controls[2] = {"sequence": 2, "phase": first_phase, "kind": None}  # 仅含阶段的控制请求没有 nonce。
    if change == "nonce":
        samples[2]["observer_before"]["probe_nonce"] = "a" * 32
    elif change == "phase":
        samples[2]["observer_after"]["phase"] = "closing-active"
    elif change == "same-nonce-new-sequence":
        nonce = samples[1]["observer_after"]["probe_nonce"]
        observation = samples[2]["observer_before"]
        observation["probe_nonce"] = nonce
        controls[observation["control_sequence"]]["probe_nonce"] = nonce
    result = run(samples, controls, None, log)
    assert result["checks"]["observer_control_sequence_and_binding"] is (change is None)
    assert result["checks"]["sample_sequence_complete"] is None
    assert result["whole_flow_edges"] == {"start_seconds": None, "stop_seconds": None}
    assert result["audit_status"] == ("unverified" if change is None else "failed")
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("phase", ("starting-active", "closing-active", "business-state-readback", "independent-fault-preparation", "closing-fault"))
def test_long_gaps_in_control_or_closing_phases_fail_even_if_business_phases_look_good(phase):
    samples, controls, status, log = material()
    index = next(index for index, row in enumerate(samples) if row["phase"] == phase)
    for row in samples[index:]:
        for key in audit.TIMES:
            row[key] += 10
    status["finished_monotonic"] += 10
    result = run(samples, controls, status, log)
    assert result["passed"] is False
    assert result["max_observed_start_gap_seconds"] == 12
    assert result["checks"]["all_adjacent_starts_within_five_seconds"] is False


@pytest.mark.parametrize("change", ("missing-head", "missing-middle", "missing-tail", "boolean-sequence", "missing-sequence",
    "repeated-sample", "reversed-clock", "overlap-clock", "missing-clock", "boolean-clock", "nan-clock", "infinite-clock",
    "long-sample", "start-gap", "stop-gap", "boundary-reversed", "boundary-nan", "sampler-incomplete", "wrong-count",
    "boolean-count", "status-copy-changed", "error-row", "phase-body", "control-reused", "control-missing", "control-phase",
    "control-nonce", "partial-line"))
def test_incomplete_or_inconsistent_evidence_never_passes(change):
    samples, controls, status, log = material()
    kwargs = {}
    if change.startswith("missing-") and change.split("-", 1)[1] in {"head", "middle", "tail"}:
        samples.pop({"missing-head": 0, "missing-middle": 3, "missing-tail": -1}[change])
    elif change == "boolean-sequence":
        samples[0]["sample_sequence"] = True
    elif change == "missing-sequence":
        samples[3].pop("sample_sequence")
    elif change == "repeated-sample":
        samples[3] = copy.deepcopy(samples[2])
    elif change in {"reversed-clock", "overlap-clock", "missing-clock", "boolean-clock", "nan-clock", "infinite-clock"}:
        if change == "missing-clock":
            samples[3].pop(audit.TIMES[1])
        else:
            samples[3][audit.TIMES[0]] = {"reversed-clock": 0.0, "overlap-clock": 6.25,
                "boolean-clock": True, "nan-clock": float("nan"), "infinite-clock": float("inf")}[change]
    elif change == "long-sample":
        samples[-1][audit.TIMES[-1]] = 28
        status["finished_monotonic"] = 29
    elif change == "start-gap":
        status["started_monotonic"] = 0
        for row in samples:
            for key in audit.TIMES:
                row[key] += 10
        status["finished_monotonic"] += 10
    elif change == "stop-gap":
        status["finished_monotonic"] += 10
    elif change == "boundary-reversed":
        status["finished_monotonic"] = 0
    elif change == "boundary-nan":
        status["started_monotonic"] = float("nan")
    elif change == "sampler-incomplete":
        status["completed"] = False
    elif change == "wrong-count":
        status["samples_written"] += 1
    elif change == "boolean-count":
        status["samples_written"] = True
    elif change == "status-copy-changed":
        kwargs["host_status"] = {**status, "completed": False}
    elif change == "error-row":
        samples[3]["error"] = "SECRET BODY MUST NOT BE EMITTED"
    elif change == "phase-body":
        samples[3]["phase"] = "SECRET BODY MUST NOT BE EMITTED"
    elif change == "control-reused":
        samples[3]["observer_before"]["control_sequence"] = samples[2]["observer_after"]["control_sequence"]
    elif change == "control-missing":
        controls.pop(samples[3]["observer_before"]["control_sequence"])
    elif change == "control-phase":
        controls[samples[3]["observer_before"]["control_sequence"]]["phase"] = "initial-idle"
    elif change == "control-nonce":
        samples[3]["observer_before"]["probe_nonce"] = "different-nonce"
    else:
        log["complete_json_lines"] = False
    result = run(samples, controls, status, log, **kwargs)
    assert result["passed"] is False and result["audit_status"] == "failed"
    assert "SECRET BODY" not in json.dumps(result)


def test_unsampled_control_phase_is_reported_without_inventing_its_duration():
    samples, controls, status, log = material()
    controls[1000] = {"sequence": 1000, "phase": "finishing", "kind": None}
    result = run(samples, controls, status, log)
    assert result["phases"]["finishing"] == {"samples": 0, "long_sample_spans": 0}
    assert "duration_seconds" not in result["phases"]["finishing"]


@pytest.mark.parametrize("data", (b'', b'{"phase":"active"}', b'{}\n\n', b'{}\n{"x":', b'[]\n', b'{"x":1,"x":2}\n', b'{"x":NaN}\n'))
def test_partial_blank_nonobject_duplicate_or_nonfinite_lines_are_not_silently_dropped(data):
    samples, log = audit.load_samples(data)
    assert log["complete_json_lines"] is False
    assert len(samples) == log["line_count"]


def trial_files(tmp_path):
    samples, controls, status, _ = material()
    trial = tmp_path / "trial"
    protected = trial / "protected"
    directory = protected / "observer/controls"
    directory.mkdir(parents=True)
    (protected / "flow-resource-samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in samples))
    (protected / "flow-host.json").write_text(json.dumps({"sampling_status": status, "env": "SECRET ENV"}))
    (protected / "flow-verdict.json").write_text('{"passed":false,"body":"SECRET BODY"}\n')
    (protected / "rotation-sampler-status.json").write_text(json.dumps(status))
    for sequence, control in controls.items():
        (directory / f"control-{sequence:012d}.json").write_text(json.dumps(control))
    return trial


def test_audit_reads_fixed_sources_and_leaves_original_verdict_and_samples_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "require_docker", lambda: None)
    trial = trial_files(tmp_path)
    original = {path: path.read_bytes() for path in trial.rglob("*") if path.is_file()}
    result = audit.audit_trial(trial)
    assert result["passed"] is True
    assert result["sources"]["protected/flow-verdict.json"]["sha256"]
    assert "SECRET" not in json.dumps(result)
    assert all(path.read_bytes() == data for path, data in original.items())


def test_cli_requires_new_output_outside_original_trial_and_does_not_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(audit, "require_docker", lambda: None)
    trial = trial_files(tmp_path)
    original = trial / "protected/flow-verdict.json"
    assert audit.main(["--trial-root", str(trial), "--output", str(original)]) == 2
    output = tmp_path / "audit.json"
    assert audit.main(["--trial-root", str(trial), "--output", str(output)]) == 0
    saved = output.read_bytes()
    assert audit.main(["--trial-root", str(trial), "--output", str(output)]) == 2
    assert output.read_bytes() == saved
    assert "SECRET" not in capsys.readouterr().out


@pytest.mark.parametrize("target", ("trial-root", "output"))
@pytest.mark.parametrize("spelling", ("old", "OLD", "dotdot", "dot", "double-slash", "trailing-slash", "relative"))
def test_cli_refuses_archives_and_noncanonical_spelling_before_reading_sources(tmp_path, monkeypatch, capsys, target, spelling):
    monkeypatch.setattr(audit, "require_docker", lambda: None)
    monkeypatch.setattr(audit, "audit_trial", lambda *_: pytest.fail("被拒绝路径不得读取实验原始证据"))
    base = str(tmp_path)
    invalid = {"old": base + "/old/trial", "OLD": base + "/OLD/trial", "dotdot": base + "/work/../trial",
        "dot": base + "/./trial", "double-slash": base + "//trial", "trailing-slash": base + "/trial/",
        "relative": "trial"}[spelling]
    values = {"trial-root": str(tmp_path / "trial"), "output": str(tmp_path / "new-audit.json"), target: invalid}
    assert audit.main(["--trial-root", values["trial-root"], "--output", values["output"]]) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "archived_or_noncanonical_path_refused"


def test_direct_audit_cannot_read_archive_even_when_cli_is_bypassed(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "require_docker", lambda: None)
    with pytest.raises(audit.AuditError, match="archived_or_noncanonical_path_refused"):
        audit.audit_trial(tmp_path / "old/trial")


@pytest.mark.parametrize("platform,environment,marker", (("darwin", "container", True), ("linux", "host", True), ("linux", "container", False)))
def test_auditor_refuses_host_or_unmarked_runtime(platform, environment, marker, monkeypatch):
    monkeypatch.setattr(audit.sys, "platform", platform)
    monkeypatch.setenv("HARNESS_LAB_ENVIRONMENT", environment)
    monkeypatch.setattr(audit, "_docker_marker_exists", lambda: marker)
    with pytest.raises(audit.AuditError, match="audit_requires_docker"):
        audit.require_docker()


def test_auditor_requires_docker_before_reading_any_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(audit.sys, "platform", "darwin")
    with pytest.raises(audit.AuditError, match="audit_requires_docker"):
        audit.audit_trial(tmp_path / "missing-secret-body")


@pytest.mark.parametrize("mode", ("symlink", "hardlink", "unexpected-control", "invalid-control"))
def test_linked_or_unbound_evidence_cannot_enter_the_audit(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(audit, "require_docker", lambda: None)
    trial = trial_files(tmp_path)
    path = trial / "protected/flow-resource-samples.jsonl"
    if mode in {"symlink", "hardlink"}:
        target = tmp_path / "saved.jsonl"
        path.rename(target)
        if mode == "symlink":
            path.symlink_to(target)
        else:
            path.hardlink_to(target)
    elif mode == "unexpected-control":
        (trial / "protected/observer/controls/SECRET BODY.pending").write_text("SECRET BODY")
    else:
        control = trial / "protected/observer/controls/control-000000000001.json"
        control.write_text('{"sequence":999,"phase":"initial-idle"}')
    with pytest.raises(audit.AuditError):
        audit.audit_trial(trial)
