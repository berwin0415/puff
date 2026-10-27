#!/usr/bin/env python3
"""仅在 Docker 内执行的 I25 离线采样审计；不改写旧判定。

检查所有已记录阶段，包括启动、控制和关闭。旧材料缺少序号、完成或边界证据时保持未知，不记为零秒缺口。输出仅含固定原因码、安全阶段名、数量与耗时；不序列化原始样本正文、环境值或异常文本。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys


SCHEMA = "harness-lab-instance-cost-sampling-audit/1"
LIMIT = 5.0
MAX_BYTES = 64 * 1024 ** 2
PHASES = frozenset({"preparation", "observer-starting", "starting-resident-history", "initial-idle",
    "starting-active", "active", "closing-active", "recycled-idle", "business-state-readback",
    "starting-wake", "wake", "closing-wake", "independent-fault-preparation", "starting-fault", "fault",
    "closing-fault", "finishing", "starting-baseline", "baseline", "closing-baseline",
    "starting-inactive-wake", "inactive-wake", "closing-inactive-wake", "inactive-wake-business-state-readback"})
CONTROL = re.compile(r"control-([0-9]{12})\.json\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
TIMES = ("host_before_observer_monotonic", "host_started_monotonic",
    "host_finished_monotonic", "host_after_observer_monotonic")


class AuditError(ValueError):
    """固定且不含敏感信息的失败码。"""


def _docker_marker_exists():
    return Path("/.dockerenv").is_file()


def require_docker():
    if (sys.platform != "linux" or os.environ.get("HARNESS_LAB_ENVIRONMENT") != "container"
            or not _docker_marker_exists()):
        raise AuditError("audit_requires_docker")


def safe_phase(value):
    return value if isinstance(value, str) and value in PHASES else "invalid-phase"


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def canonical_path(value):
    """在 resolve 隐去原始写法前，拒绝归档路径及非规范写法。"""
    text = os.fspath(value)
    path = Path(text)
    if (not path.is_absolute() or any(part.lower() in {"old", ".."} for part in path.parts)
            or str(path) != text or str(path.resolve()) != text):
        raise AuditError("archived_or_noncanonical_path_refused")
    return path


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise AuditError("nonfinite_json_number")


def decode(data):
    try:
        return json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise AuditError("invalid_json") from None


def regular(path):
    """读取有大小限制的不可变文件快照；不跟随链接，也不打印路径。"""
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise AuditError("linked_source_refused")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise AuditError("invalid_source_file")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    current = path.stat(follow_symlinks=False)
    if (len(data) != before.st_size or signature(before) != signature(after)
            or signature(after) != signature(current)):
        raise AuditError("source_changed_during_read")
    return data


def load_samples(data):
    samples, failures = [], []
    lines = data.splitlines()
    for number, line in enumerate(lines, 1):
        try:
            value = decode(line)
            if not isinstance(value, dict):
                raise AuditError("sample_not_object")
        except AuditError:
            value = {"_audit_invalid_line": True}
            failures.append(number)
        samples.append(value)
    return samples, {"complete_json_lines": bool(data) and data.endswith(b"\n") and not failures,
        "line_count": len(lines), "invalid_line_numbers": failures}


def analyze(samples, *, log, controls, status=None, host_status=None):
    """仅审计已加载快照；不修补或筛掉证据。"""
    checks = {"complete_json_lines": log["complete_json_lines"], "at_least_two_samples": len(samples) >= 2,
        "no_sample_errors": all("error" not in row and "_audit_invalid_line" not in row for row in samples),
        "valid_phase_names": all(safe_phase(row.get("phase")) != "invalid-phase" for row in samples),
        "ordered_sample_clocks": True, "all_sample_spans_within_five_seconds": True,
        "all_adjacent_starts_within_five_seconds": True, "observer_control_sequence_and_binding": True}
    phases = {safe_phase(row.get("phase")): {"samples": 0, "long_sample_spans": 0}
        for row in controls.values()}
    intervals, spans, starts, failures = [], [], [], []
    previous, previous_sequence, used_nonces = None, None, set()
    for number, row in enumerate(samples, 1):
        phase = safe_phase(row.get("phase"))
        phases.setdefault(phase, {"samples": 0, "long_sample_spans": 0})["samples"] += 1
        clock = [row.get(key) for key in TIMES]
        ordered = all(finite(value) for value in clock) and clock == sorted(clock)
        if not ordered:
            checks["ordered_sample_clocks"] = False
            checks["all_sample_spans_within_five_seconds"] = False
            failures.append({"code": "invalid_sample_clock", "line": number, "phase": phase})
            current = None
        else:
            current = clock
            span = clock[-1] - clock[0]
            spans.append(span)
            starts.append((number, clock[0], clock[-1]))
            if span > LIMIT:
                checks["all_sample_spans_within_five_seconds"] = False
                phases[phase]["long_sample_spans"] += 1
                failures.append({"code": "sample_span_exceeds_five_seconds", "line": number,
                    "phase": phase, "seconds": span})
        if number > 1:
            if previous is None or current is None:
                checks["all_adjacent_starts_within_five_seconds"] = False
            else:
                delta = current[0] - previous[0]
                intervals.append(delta)
                if current[0] <= previous[0] or current[0] < previous[-1]:
                    checks["ordered_sample_clocks"] = False
                    failures.append({"code": "sample_clock_reversal_or_overlap", "line": number, "phase": phase})
                if delta < 0 or delta > LIMIT:
                    checks["all_adjacent_starts_within_five_seconds"] = False
                    failures.append({"code": "adjacent_start_gap_exceeds_limit", "line": number,
                        "phase": phase, "seconds": delta})
        previous = current
        for key in ("observer_before", "observer_after"):
            observation = row.get(key)
            observation = observation if isinstance(observation, dict) else {}
            sequence = observation.get("control_sequence")
            nonce = observation.get("probe_nonce")
            control = controls.get(sequence) if type(sequence) is int else None
            bound = (type(sequence) is int and sequence > 0 and isinstance(control, dict)
                and control.get("sequence") == sequence and observation.get("phase") == row.get("phase")
                and control.get("phase") == row.get("phase") and "kind" in observation and "kind" in control
                and observation["kind"] == control["kind"] and isinstance(nonce, str)
                and NONCE.fullmatch(nonce) is not None and nonce not in used_nonces
                and nonce == control.get("probe_nonce")
                and (previous_sequence is None or sequence > previous_sequence))
            if not bound:
                checks["observer_control_sequence_and_binding"] = False
            if isinstance(nonce, str):
                used_nonces.add(nonce)
            previous_sequence = sequence if type(sequence) is int else previous_sequence

    sequences = [row.get("sample_sequence") for row in samples]
    checks["sample_sequence_complete"] = (None if all(value is None for value in sequences) else
        all(type(value) is int for value in sequences) and sequences == list(range(1, len(samples) + 1)))
    checks.update(sampler_completed=None, written_row_count_matches=None, independent_status_preserved=None,
        sampler_boundary_clocks_valid=None, start_edge_within_five_seconds=None, stop_edge_within_five_seconds=None)
    edges = {"start_seconds": None, "stop_seconds": None}
    unknown = []
    if checks["sample_sequence_complete"] is None:
        unknown.append("legacy_sample_sequence_absent")
    if status is None:
        unknown.extend(("sampler_completion_and_written_count_absent", "whole_flow_start_and_stop_boundaries_absent"))
    else:
        status = status if isinstance(status, dict) else {}
        checks["sampler_completed"] = status.get("completed") is True
        checks["written_row_count_matches"] = (type(status.get("samples_written")) is int
            and status["samples_written"] == len(samples) == log["line_count"])
        checks["independent_status_preserved"] = status == host_status
        begin, end = status.get("started_monotonic"), status.get("finished_monotonic")
        valid = finite(begin) and finite(end) and begin <= end
        checks["sampler_boundary_clocks_valid"] = valid
        if valid and len(starts) == len(samples) and starts:
            edges = {"start_seconds": starts[0][1] - begin, "stop_seconds": end - starts[-1][2]}
            checks["start_edge_within_five_seconds"] = 0 <= edges["start_seconds"] <= LIMIT
            checks["stop_edge_within_five_seconds"] = 0 <= edges["stop_seconds"] <= LIMIT
        else:
            checks["start_edge_within_five_seconds"] = checks["stop_edge_within_five_seconds"] = False
    observed_names = ("complete_json_lines", "at_least_two_samples", "no_sample_errors", "valid_phase_names",
        "ordered_sample_clocks", "all_sample_spans_within_five_seconds", "all_adjacent_starts_within_five_seconds",
        "observer_control_sequence_and_binding")
    state = "failed" if any(value is False for value in checks.values()) else (
        "unverified" if any(value is None for value in checks.values()) else "passed")
    return {"schema": SCHEMA, "audit_status": state, "passed": state == "passed", "limit_seconds": LIMIT,
        "original_verdict_rewritten": False, "all_recorded_phases_checked": True,
        "observed_intervals_within_five_seconds": all(checks[key] is True for key in observed_names),
        "checks": checks, "unknown_reason_codes": unknown, "samples": len(samples),
        "invalid_line_numbers": log["invalid_line_numbers"], "phases": dict(sorted(phases.items())),
        "max_observed_start_gap_seconds": max(intervals) if intervals else None,
        "max_observed_sample_span_seconds": max(spans) if spans else None, "whole_flow_edges": edges,
        "timing_failures": failures[:100], "timing_failure_count": len(failures),
        "limits": ['不输出原始正文、环境值或异常文本。',
            '换行符和稳定读取不能证明未记录的尾部已经写入。',
            '控制序号允许合法跳号，不能替代采样序号。',
            '不推算未采样控制阶段的耗时；边界未知不等于零。',
            '不据此推断资源峰值、费用、节省比例、业务结果或 I25 完成状态。']}


def audit_trial(root):
    require_docker()
    root = canonical_path(root)
    sources = {}
    def source(relative):
        data = regular(root / relative)
        sources[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        return data
    samples, log = load_samples(source("protected/flow-resource-samples.jsonl"))
    host = decode(source("protected/flow-host.json"))
    if not isinstance(host, dict):
        raise AuditError("host_record_not_object")
    source("protected/flow-verdict.json")
    controls, manifest = {}, []
    directory = root / "protected/observer/controls"
    if any(item.is_symlink() for item in (directory, *directory.parents)) or not directory.is_dir():
        raise AuditError("invalid_control_directory")
    paths = sorted(directory.iterdir())
    if len(paths) > 100_000:
        raise AuditError("control_count_exceeds_limit")
    for path in paths:
        match = CONTROL.fullmatch(path.name)
        if match is None:
            raise AuditError("unexpected_control_file")
        data = regular(path)
        control = decode(data)
        sequence = int(match[1])
        if (not isinstance(control, dict) or type(control.get("sequence")) is not int
                or control["sequence"] != sequence or safe_phase(control.get("phase")) == "invalid-phase"):
            raise AuditError("invalid_control_binding")
        controls[sequence] = control
        manifest.append(path.name + ":" + hashlib.sha256(data).hexdigest())
    sources["protected/observer/controls"] = {"files": len(manifest),
        "manifest_sha256": hashlib.sha256("\n".join(manifest).encode()).hexdigest()}
    status_path = root / "protected/rotation-sampler-status.json"
    status = decode(source("protected/rotation-sampler-status.json")) if status_path.is_symlink() or status_path.exists() else None
    result = analyze(samples, log=log, controls=controls, status=status, host_status=host.get("sampling_status"))
    result["sources"] = sources
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        require_docker()
        trial_root, output = canonical_path(args.trial_root), canonical_path(args.output)
        if (output.is_relative_to(trial_root)
                or any(item.is_symlink() for item in (output, *output.parents))):
            raise AuditError("audit_output_must_be_new_and_outside_trial")
        result = audit_trial(trial_root)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n")
        print(json.dumps({key: result[key] for key in ("schema", "audit_status", "passed", "samples")}))
        return 0 if result["passed"] else 1
    except AuditError as error:
        print(json.dumps({"schema": SCHEMA, "passed": False, "reason_code": error.args[0]}))
        return 2
    except (OSError, ValueError, TypeError, OverflowError):
        print(json.dumps({"schema": SCHEMA, "passed": False, "reason_code": "audit_input_or_output_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
