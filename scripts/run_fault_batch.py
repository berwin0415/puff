"""调度固定的 I23 Docker 矩阵，保留每条独立链。

宿主进程仅调用 run_fault_recovery.py。候选执行、故障注入和评分均在已固定的 Docker 镜像内完成。子进程输出不公开；标准输出仅包含有大小限制且不含凭证的摘要。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.bench import identifier
from runner.fault_outcome import controlled_failure
from scripts.lab import Lab

SUPPORTED = ("deepagents_langgraph", "deepseek_harness")
FAULTS = ("model_timeout", "fail_once", "drop_receipt_once", "kill_before_write")
MODES = ("idempotent", "non_idempotent")
PHASES = {
    "baseline-101": (("none",), MODES, (101,)),
    "smoke": (FAULTS, ("non_idempotent",), (101,)),
    "formal": (FAULTS, MODES, (101, 202, 303)),
    "baseline-rest": (("none",), MODES, (202, 303)),
    "final": (("kill_before_write", "drop_receipt_once"), MODES, (404, 505)),
}
AXES = {"task_result", "injection_valid", "budget_and_process", "replay_behavior", "handling_outcome"}
REQUIRED_CHECKS = {
    "final_independent_ledger", "cumulative_model_budget", "complete_trusted_tool_audit",
    "operation_sources", "injection_window", "host_lifecycle", "native_terminal",
    "exact_processes_controls_mounts", "resource_limits",
}
CAPABILITY_CHECKS = {"native_terminal", "original_native_crash_recovery"}
HEX = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
HOST_MATERIALS = (
    "scripts/run_fault_batch.py", "scripts/run_fault_recovery.py", "scripts/run_pause_recovery.py",
    "scripts/lab.py", "scripts/native_triggers.py", "runner/config.py", "runner/bench.py",
    "runner/fault_recovery.py", "runner/pause_recovery.py", "runner/detailed.py", "runner/dsh_goal.py",
    "runner/grading.py", "runner/experiment.py", "runner/metrics.py", "runner/process_observation.py",
    "runner/fault_outcome.py",
    "configs/tasks/tools.yaml", "configs/tasks/fault-recovery.yaml", "configs/experiment.yaml",
    "compose/docker-compose.yml", "compose/detailed.override.yml",
    "adapters/deepagents_langgraph/pause_recovery.py",
)


class BatchStopped(RuntimeError):
    """仅使用固定公开消息，不包含候选输出或凭证。"""


def require(condition, message):
    if not condition:
        raise BatchStopped(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def regular(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file()
            and not any(parent.is_symlink() for parent in path.parents),
            '必需材料或证据不是无链接的普通文件')
    return path


def digest(path):
    value = hashlib.sha256()
    with regular(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_object(path):
    path = regular(path)
    require(path.stat().st_size <= 32 * 1024 * 1024, 'JSON 证据超过批次读取大小上限')
    value = json.loads(path.read_text())
    require(isinstance(value, dict), '必需的 JSON 证据不是对象')
    return value


def save(path, value, *, first=False):
    target = path if first else path.with_suffix(".json.tmp")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    if not first:
        target.replace(path)


def plan(phase, prefix):
    """CLI 子集或自动重试不能静默改变固定矩阵。"""
    faults, modes, seeds = PHASES[phase]
    rows = [{"chain_id": identifier(f"{prefix}-{phase}-{fault}-{'id' if mode == 'idempotent' else 'nonid'}-{seed}"),
             "fault": fault, "write_mode": mode, "seed": seed}
            for fault in faults for mode in modes for seed in seeds]
    require(len({row["chain_id"] for row in rows}) == len(rows), '计划链 ID 不唯一')
    return rows


def summary(row):
    return {key: row.get(key) for key in (
        "chain_id", "fault", "write_mode", "seed", "status", "passed", "failed_checks",
        "axes", "exit", "verdict")}


def verify_inventory(context, manifest, *, source=False):
    entries = manifest.get("files")
    require(isinstance(entries, list) and entries, '缺少冻结源码或适配器清单')
    export = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    require(manifest.get("export_sha256") == export, '材料清单导出摘要不一致')
    names = set()
    for row in entries:
        require(isinstance(row, dict) and isinstance(row.get("path"), str), '材料清单条目无效')
        relative = PurePosixPath(row["path"])
        require(not relative.is_absolute() and ".." not in relative.parts and relative.parts
                and relative.as_posix() == row["path"] and row["path"] not in names,
                '材料清单包含不安全或重复的路径')
        names.add(row["path"])
        path = context.joinpath(*relative.parts)
        require(not any(parent.is_symlink() for parent in path.parents), '材料父目录不能跟随链接')
        if row.get("type") == "symlink" and source:
            require(path.is_symlink() and os.readlink(path) == row.get("target")
                    and path.resolve().is_relative_to(context)
                    and hashlib.sha256(os.readlink(path).encode()).hexdigest() == row.get("sha256"),
                    '冻结源码链接与其清单不同')
        else:
            require(row.get("type") == "file" and digest(path) == row.get("sha256")
                    and path.stat().st_size == row.get("size")
                    and ("mode" not in row or path.stat().st_mode & 0o777 == row["mode"]),
                    '冻结源码或适配器内容与其清单不同')
    return export


class FaultBatch:
    def __init__(self, args, *, lab=None):
        self.args, self.lab = args, lab or Lab()
        self.config, self.data_root = self.lab.config, self.lab.config.host_data_root
        self.day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        self.plan = plan(args.phase, args.prefix)
        self.index = self.config.paths_for("fault-recovery", args.batch_id, args.candidate,
            "alice", "main", args.batch_id, case="runtime", date=self.day).run
        self.context = args.materials_context.resolve()
        require("old" not in self.context.parts and self.context != ROOT / "external/build-context"
                and self.context.is_relative_to(ROOT / "external/build-context")
                and not args.materials_context.is_symlink(), '材料上下文必须是当前独立导出')
        self.tags = {"candidate_image_id": args.image_tag, "bench_image_id": args.bench_image,
                     "services_image_id": args.services_image}
        self.record = {"schema": "harness-lab-fault-batch/1", "batch_id": args.batch_id,
            "candidate": args.candidate, "phase": args.phase, "prefix": args.prefix,
            "started_at": now(), "status": "initializing", "serial": True,
            "planned_chains": len(self.plan), "plan": self.plan, "runs": []}

    def assert_clean(self):
        require(not self.lab.containers(), '实验容器仍存在；无法开始下一条链')

    def image_ids(self):
        result = {}
        for key, tag in self.tags.items():
            reply = self.lab.call(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                                  capture=True, check=False, timeout=20)
            require(reply.returncode == 0 and IMAGE.fullmatch(reply.stdout.strip()), '无法固定必需镜像')
            result[key] = reply.stdout.strip()
        return result

    def material_hashes(self):
        manifests = {name: read_object(self.context / name) for name in (
            "SOURCE_MANIFEST.json", "LAB_ADAPTER_MANIFEST.json", "LAB_FAULT_BRIDGE_MANIFEST.json")}
        source, adapter, overlay = manifests.values()
        require(all(row.get("candidate") == self.args.candidate for row in manifests.values())
                and source.get("schema") == "harness-lab-source-manifest/1"
                and adapter.get("schema") == "harness-lab-adapter-manifest/1"
                and overlay.get("schema") == "harness-lab-fault-bridge-manifest/1"
                and adapter.get("source_manifest_sha256") == digest(self.context / "SOURCE_MANIFEST.json")
                and adapter.get("variant") == overlay.get("variant") == "fault-prewrite-bridge",
                '冻结源码、适配器与故障覆盖层身份不一致')
        source_hash = verify_inventory(self.context, source, source=True)
        adapter_hash = verify_inventory(self.context, adapter)
        require(overlay.get("adapter_export_sha256") == adapter_hash, '故障覆盖层未绑定适配器导出')
        overlay_rows = overlay.get("sources")
        require(isinstance(overlay_rows, list) and overlay_rows, '缺少故障覆盖层源码清单')
        for row in overlay_rows:
            require(isinstance(row, dict), '故障覆盖层源码清单无效')
            for field, base in (("repository_path", ROOT), ("image_material_path", self.context)):
                name = row.get(field)
                require(isinstance(name, str) and not PurePosixPath(name).is_absolute()
                        and ".." not in PurePosixPath(name).parts, '故障覆盖层路径不安全')
                require(digest(base / name) == row.get("sha256") and (base / name).stat().st_size == row.get("size"),
                        '故障覆盖层内容与记录来源不同')
        return {"source_export_sha256": source_hash, "adapter_export_sha256": adapter_hash,
            "fault_overlay_sha256": digest(self.context / "LAB_FAULT_BRIDGE_MANIFEST.json"),
            "adapter_helper_sha256": digest(ROOT / "adapters/deepagents_langgraph/pause_recovery.py"),
            "manifest_hashes": {name: digest(self.context / name) for name in manifests},
            "host_material_hashes": {name: digest(ROOT / name) for name in HOST_MATERIALS},
            "build_material_hashes": {path.relative_to(self.context).as_posix(): digest(path)
                for path in sorted(self.context.glob("*")) if path.is_file() and path.name not in manifests}}

    def assert_frozen(self):
        require(self.image_ids() == self.record["image_ids"], 'Docker 标签偏离固定镜像 ID')
        require(self.material_hashes() == self.record["materials"], '冻结源码、适配器、任务或宿主材料发生变化')

    def paths(self, item):
        return self.config.paths_for("fault-recovery", item["chain_id"], self.args.candidate,
            "alice", "main", item["chain_id"], case="fault-recovery", date=self.day)

    def assert_fresh_chain(self, item):
        require(not any(path.exists() or path.is_symlink() for path in vars(self.paths(item)).values()),
                '计划链的状态或证据已存在；需要新前缀')
        require(not any((self.data_root / "runs").glob(f"*/*/*/{item['chain_id']}")),
                '计划链 ID 已有外部证据')

    def command(self, item):
        images = self.record["image_ids"]
        return [sys.executable, str(ROOT / "scripts/run_fault_recovery.py"),
            "--candidate", self.args.candidate, "--chain-id", item["chain_id"],
            "--seed", str(item["seed"]), "--fault", item["fault"], "--write-mode", item["write_mode"],
            "--image-tag", images["candidate_image_id"], "--bench-image", images["bench_image_id"],
            "--services-image", images["services_image_id"], "--materials-context", str(self.context)]

    def native_gap(self, directory, item, scope, pins):
        path = directory / "trusted/native-recovery-gap.json"
        if not path.exists():
            return False
        gap = read_object(path)
        boundary_path = directory / "trusted/crash-boundary.json"
        boundary = read_object(boundary_path)
        require(item["fault"] == "kill_before_write" and gap.get("schema") == "harness-lab-native-fault-gap/1"
                and gap.get("status") == "not_implemented" and gap.get("scope") == scope
                and gap.get("chain_id") == item["chain_id"] and gap.get("candidate") == self.args.candidate
                and gap.get("candidate_image_id") == pins["candidate_image_id"]
                and gap.get("crash_boundary_sha256") == digest(boundary_path)
                and gap.get("new_prompt_submitted") is False and gap.get("business_actions_performed_by_bench") is False
                and boundary.get("chain_id") == item["chain_id"] and boundary.get("scope") == scope
                and boundary.get("pause_index_used") is False and boundary.get("native_state_explicitly_flushed") is False
                and not any((directory / "trusted" / name).exists() or (directory / "trusted" / name).is_symlink() for name in (
                    "worker-b.json", "worker-b-created.json", "worker-b-binding.json")),
                '原生恢复缺口未独立绑定到本条被杀链')
        return True

    def completed_evidence(self, item, process_exit):
        matches = list((self.data_root / "runs").glob(f"*/{self.args.candidate}/fault-recovery/{item['chain_id']}"))
        require(len(matches) == 1, '本链未生成且仅生成一个外部证据目录')
        directory = matches[0]
        # 批次可能跨越本地午夜；共享状态仍使用同一个唯一链身份，
        # 子进程自行选择实际开始日期。
        actual_day = directory.parent.parent.parent.name
        paths = self.config.paths_for("fault-recovery", item["chain_id"], self.args.candidate,
            "alice", "main", item["chain_id"], case="fault-recovery", date=actual_day)
        scope = {"experiment_id": "fault-recovery", "variant": item["chain_id"], "user_id": "alice", "session_id": "main"}
        identity = {"candidate": self.args.candidate, **{key: item[key] for key in ("chain_id", "seed", "fault", "write_mode")}}
        verdict, chain, pins = (read_object(directory / name) for name in ("verdict.json", "chain.json", "trusted/pins.json"))
        require(verdict.get("schema") == "harness-lab-fault-verdict/1"
                and chain.get("schema") == "harness-lab-fault-chain/1" and pins.get("schema") == "harness-lab-fault-pins/1"
                and all(row.get(key) == value for row in (verdict, chain, pins) for key, value in identity.items())
                and chain.get("scope") == pins.get("scope") == scope and pins.get("date") == actual_day,
                '链、判定和固定材料与计划身份不一致')
        require(all(pins.get(key) == value for key, value in self.record["image_ids"].items())
                and all(pins.get(key) == self.record["materials"][key] for key in (
                    "source_export_sha256", "adapter_export_sha256", "fault_overlay_sha256", "adapter_helper_sha256"))
                and pins.get("active_limit_seconds") == 300 and pins.get("model_call_cap") == 20,
                '实际链材料或预算与批次冻结值不同')
        require(isinstance(pins.get("model_name"), str) and 0 < len(pins["model_name"]) <= 256
                and self.record.get("model_name", pins["model_name"]) == pins["model_name"],
                '批次运行期间远程模型身份发生变化')
        input_ = read_object(directory / "input.json")
        require(input_.get("candidate") == self.args.candidate and input_.get("chain_id") == item["chain_id"]
                and input_.get("seed") == item["seed"] and input_.get("scope") == scope
                and input_.get("base_task") == "tools" and input_.get("expected_answer_delivered") is False
                and input_.get("task_sha256") == chain.get("task_sha256") == self.record["materials"]["host_material_hashes"]["configs/tasks/tools.yaml"]
                and chain.get("input_sha256") == digest(directory / "input.json")
                and chain.get("request_sha256") == digest(paths.fixture / "candidate-request.json"),
                '实际准备输入与冻结任务或原始请求不同')
        for name in ("orchestration.json", "trusted/orchestration-final.json"):
            outcome = read_object(directory / name)
            seconds = outcome.get("used_active_seconds")
            require(outcome.get("schema") == "harness-lab-fault-orchestration/1"
                    and outcome.get("chain_id") == item["chain_id"] and outcome.get("scope") == scope
                    and outcome.get("errors") == [] and outcome.get("timed_out") is False
                    and outcome.get("native_start_available") is True
                    and type(seconds) in (int, float) and math.isfinite(seconds) and 0 <= seconds <= 300,
                    '编排未完成、超时或存在控制错误')
        cleanup = read_object(directory / "cleanup.json")
        require(cleanup.get("remaining") == [] and isinstance(cleanup.get("finished_at"), str), '链清理证据不完整')
        gap = self.native_gap(directory, item, scope, pins)
        required = REQUIRED_CHECKS | ({"original_budget_and_tool_prefix"} if item["fault"] == "kill_before_write" else set())
        if item["fault"] == "kill_before_write" and not gap:
            required |= {"original_native_crash_recovery"}
        checks = verdict.get("checks")
        require(isinstance(checks, list) and all(isinstance(row, dict) and isinstance(row.get("name"), str)
                and type(row.get("passed")) is bool for row in checks), '判定检查项格式错误')
        by_name = {row["name"]: row["passed"] for row in checks}
        require(len(by_name) == len(checks) and set(by_name) == required, '判定缺少必需检查项或含未知检查项')
        require(all(passed for name, passed in by_name.items() if name not in CAPABILITY_CHECKS),
                '证据、资源、来源、生命周期或注入核验失败')
        axes = verdict.get("axes")
        require(isinstance(axes, dict) and set(axes) == AXES and isinstance(axes.get("task_result"), dict)
                and type(axes["task_result"].get("passed")) is bool
                and axes["task_result"].get("source") == "business-verdict.json"
                and axes.get("injection_valid") is True, '判定缺少有效且独立的结果维度')
        budget_axis, replay = axes.get("budget_and_process"), axes.get("replay_behavior")
        budget = read_object(directory / "model-calls.json")
        resources = read_object(directory / "trusted/resource-summary.json")
        business = read_object(directory / "business-verdict.json")
        read_object(directory / "operation-analysis.json")
        calls = budget.get("count")
        require(isinstance(budget_axis, dict) and budget_axis.get("passed") is True
                and budget_axis.get("active_limit_seconds") == 300 and budget_axis.get("model_call_cap") == 20
                and type(calls) is int and 0 < calls <= 20 and budget_axis.get("model_calls") == calls
                and budget.get("cap") == 20 and budget.get("in_flight") == []
                and resources.get("passed") is True and type(resources.get("sample_count")) is int
                and resources["sample_count"] >= 2
                and resources.get("samples_sha256") == digest(directory / "resource-samples.jsonl")
                and business.get("passed") is axes["task_result"]["passed"], '预算、资源或任务结果证据不完整')
        require(isinstance(replay, dict) and replay.get("operations_file") == "operation-analysis.json"
                and replay.get("server_idempotency_is_candidate_no_replay") is False
                and type(replay.get("reissued_after_commit")) is int and replay["reissued_after_commit"] >= 0,
                '重放结果证据不完整')
        handling = axes.get("handling_outcome")
        require(handling in {"automatic_completed", "incomplete", "unsafe_duplicate"}
                and type(verdict.get("passed")) is bool and verdict["passed"] == (handling == "automatic_completed")
                and process_exit == (0 if verdict["passed"] else 1)
                and (not verdict["passed"] or (all(by_name.values()) and business["passed"] and not gap)),
                '判定结果或子进程退出不一致')
        # 核验实际保留的镜像身份，包括评分进程；
        # 评分进程只在资源评分器取得快照后才退出。
        for attempt in ("worker-a", "worker-b"):
            path = directory / "trusted" / (attempt + ".json")
            if attempt == "worker-b" and not path.exists():
                require(item["fault"] != "kill_before_write" or gap, '被杀链没有恢复或经独立核验的缺口')
                continue
            worker = read_object(path)
            require(worker.get("chain_id") == item["chain_id"] and worker.get("scope") == scope
                    and worker.get("attempt_id") == attempt and worker.get("errors") == [] and worker.get("timed_out") is False
                    and all(worker.get(key, {}).get("image_id") == pins["candidate_image_id"] for key in ("before_start", "after_exit")),
                    '实际 Worker 证据与链固定值不同')
            after = worker.get("after_exit", {})
            code = after.get("state", {}).get("ExitCode")
            if not (item["fault"] == "kill_before_write" and attempt == "worker-a"):
                require(type(code) is int and code in {0, 1}, 'Worker 缺少经审查的退出结果')
                if code == 1:
                    try:
                        failure = controlled_failure(directory, paths.fixture, scope, pins,
                            self.args.candidate, attempt, after, state=paths.state)
                    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                        raise BatchStopped('Worker 退出 1，但缺少完整绑定的原生失败证据') from None
                    retained = resources.get("controlled_failures", {}).get(attempt, {})
                    native = retained.get("native_verification", {})
                    require(retained.get("evidence") == failure and native.get("physical_native_failure_verified") is True,
                            '独立资源评分器未保留已绑定的原生失败')
                    if self.args.candidate == "deepagents_langgraph":
                        physical = failure.get("native_checkpoint", {})
                        require(native.get("reader") == "independent_bench_safe_msgpack_projection"
                                and native.get("original_files_unchanged") is True
                                and all(native.get(key) == physical.get(key) for key in
                                    ("checkpoint_id", "checkpoint_blob_sha256", "metadata_sha256")),
                                '独立的 DA 物理失败审计不完整')
                    require(type(worker.get("attach_exit")) is int and type(worker.get("process_exit")) is int
                            and worker.get("attach_exit") == worker.get("process_exit") == 1
                            and verdict["passed"] is False and process_exit == 1
                            and by_name["native_terminal"] is False
                            and handling != "automatic_completed" and worker.get("kill") is None,
                            '将受控原生失败误当作自动完成')
            if item["fault"] == "kill_before_write" and attempt == "worker-a":
                signal_path = directory / "trusted/gate-signal.json"
                signal = read_object(signal_path)
                boundary = read_object(directory / "trusted/crash-boundary.json")
                gate_after = read_object(directory / "trusted/gate-after-kill.json")
                kill, after = worker.get("kill", {}), worker.get("after_exit", {})
                require(signal.get("observed") is True and signal.get("chain_id") == item["chain_id"]
                        and signal.get("scope") == scope and signal.get("attempt_id") == "worker-a"
                        and signal.get("snapshot_sha256") == digest(directory / "trusted/gate-hit.json")
                        and kill.get("signal") == "SIGKILL" and kill.get("exit_code") == 0
                        and kill.get("container_id") == after.get("id") == boundary.get("worker_id")
                        and kill.get("gate_signal_sha256") == digest(signal_path)
                        and worker.get("process_exit") == worker.get("attach_exit") == after.get("state", {}).get("ExitCode") == 137
                        and worker.get("deleted", {}).get("id_absent") is True
                        and worker.get("deleted", {}).get("name_absent") is True
                        and boundary.get("gate_snapshot_sha256") == digest(directory / "trusted/gate-after-kill.json")
                        and gate_after.get("gate", {}).get("state") == "abandoned"
                        and gate_after.get("unsettled_dispatches") == [],
                        '被杀链缺少已绑定门禁、实际强杀或完整清理证据')
        services = read_object(directory / "trusted/service-host.json")
        require(set(services) == {"fixtures", "tools", "model-proxy"}
                and all(isinstance(row, dict) and row.get("image_id") == pins["services_image_id"] for row in services.values()),
                '实际服务镜像与冻结值不同')
        bench = [read_object(path) for path in sorted((directory / "trusted").glob("bench-process-*.json"))]
        require(bench and sum(row.get("action") == "grade" for row in bench) == 1
                and bench[-1].get("action") == "grade" and bench[-1].get("exit_code") == process_exit,
                '独立评分进程证据不完整')
        for row in bench:
            snapshot = row.get("after_exit", {})
            state = snapshot.get("state", {})
            code = row.get("exit_code")
            allowed = code == 0 or (row.get("action") == "grade" and code == process_exit)
            allowed |= gap and row.get("action") == "bind-crash-resume" and type(code) is int and code == 3
            require(allowed and type(code) is int and snapshot.get("image_id") == pins["bench_image_id"]
                    and snapshot.get("memory_limit") == 512 * 1024**2 and snapshot.get("cpu_nano") == 500000000
                    and state.get("Status") == "exited" and type(state.get("Pid")) is int and state["Pid"] == 0
                    and all(state.get(key) is False for key in ("Running", "OOMKilled", "Dead", "Paused", "Restarting"))
                    and state.get("Error") == ""
                    and type(state.get("ExitCode")) is int and state["ExitCode"] == code,
                    '独立控制进程失败或使用了未固定镜像')
        if gap:
            diagnostics = [row for row in bench if row.get("action") == "bind-crash-resume" and row.get("exit_code") == 3]
            require(len(diagnostics) == 1,
                    '原生缺口缺少独立完成的诊断进程')
            diagnostic = diagnostics[0]
            actual = diagnostic["after_exit"]
            require(isinstance(actual.get("id"), str) and HEX.fullmatch(actual["id"]),
                    '诊断进程缺少确切 Docker 身份')
            registrations = [read_object(path) for path in sorted((directory / "trusted").glob("bench-registration-*.json"))]
            matches = [row for row in registrations if row.get("id") == actual["id"]]
            require(len(matches) == 1, '诊断进程缺少唯一且已保留的启动前登记')
            registration = matches[0]
            created = registration.get("before_start")
            require(isinstance(created, dict) and isinstance(created.get("state"), dict)
                    and registration.get("action") == "bind-crash-resume"
                    and registration.get("scope") == scope and registration.get("chain_id") == item["chain_id"]
                    and registration.get("crash_boundary_sha256") == digest(directory / "trusted/crash-boundary.json")
                    and isinstance(actual.get("name"), str) and actual["name"].startswith("/")
                    and registration.get("name") == diagnostic.get("name") == actual["name"].removeprefix("/")
                    and registration.get("image_id") == created.get("image_id") == actual.get("image_id") == pins["bench_image_id"]
                    and created.get("id") == actual["id"] and created.get("name") == actual["name"]
                    and created.get("memory_limit") == 512 * 1024**2 and created.get("cpu_nano") == 500000000
                    and created["state"].get("Status") == "created" and created["state"].get("Running") is False,
                    '诊断登记与原始创建的控制进程不同')
        safe_axes = {"task_result": {"passed": axes["task_result"]["passed"], "source": "business-verdict.json"},
            "injection_valid": True, "budget_and_process": {"passed": True, "model_calls": calls,
                "active_limit_seconds": 300, "model_call_cap": 20},
            "replay_behavior": {"operations_file": "operation-analysis.json",
                "server_idempotency_is_candidate_no_replay": False, "reissued_after_commit": replay["reissued_after_commit"]},
            "handling_outcome": handling}
        return {"passed": verdict["passed"], "axes": safe_axes, "exit": process_exit,
            "failed_checks": sorted(name for name, passed in by_name.items() if not passed),
            "native_recovery_gap": gap, "model_name": pins["model_name"], "verdict": str(directory / "verdict.json")}

    def run_locked(self):
        require(not any((self.data_root / "runs").glob(f"*/*/runtime/{self.args.batch_id}")),
                '批次 ID 已存在；不能覆盖或续写证据')
        for item in self.plan:
            self.assert_fresh_chain(item)
        self.index.mkdir(parents=True, exist_ok=False)
        self.index.chmod(0o700)
        index_path = self.index / "batch.json"
        save(index_path, self.record, first=True)
        active = None
        try:
            self.assert_clean()
            self.record["image_ids"] = self.image_ids()
            self.record["materials"] = self.material_hashes()
            self.record["status"] = "running"
            save(index_path, self.record)
            for item in self.plan:
                self.assert_clean()
                self.assert_frozen()
                self.assert_fresh_chain(item)
                active = {**item, "status": "running", "started_at": now(), "passed": None,
                    "failed_checks": [], "axes": {}, "exit": None, "verdict": None,
                    "log": str(self.index / (item["chain_id"] + ".log")), "command": self.command(item)}
                self.record["runs"].append(active)
                save(index_path, self.record)
                started = time.monotonic()
                descriptor = os.open(active["log"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    reply = subprocess.run(active["command"], cwd=ROOT, env=self.lab.env,
                        stdout=output, stderr=subprocess.STDOUT, check=False)
                active.update({"exit": reply.returncode, "finished_at": now(), "elapsed_seconds": time.monotonic() - started})
                self.assert_clean()
                self.assert_frozen()
                active.update(self.completed_evidence(item, reply.returncode))
                self.record.setdefault("model_name", active["model_name"])
                active["status"] = "passed" if active["passed"] else "capability_failed"
                save(index_path, self.record)
                print(json.dumps(summary(active), ensure_ascii=False), flush=True)
                active = None
            self.record.update({"status": "completed", "passed": all(row["passed"] for row in self.record["runs"]),
                "failed_checks": sorted({name for row in self.record["runs"] for name in row["failed_checks"]})})
            return 0 if self.record["passed"] else 1
        except (Exception, KeyboardInterrupt) as error:
            reason = str(error) if isinstance(error, BatchStopped) else '批次已停止；请检查私有外部证据'
            self.record.update({"status": "stopped", "passed": False, "stop_reason": reason, "failed_checks": ["batch_integrity"]})
            if active is not None:
                active.update({"status": "integrity_stopped", "passed": False, "finished_at": now(), "failed_checks": ["batch_integrity"]})
                print(json.dumps(summary(active), ensure_ascii=False), flush=True)
            return 1
        finally:
            self.record["finished_at"] = now()
            save(index_path, self.record)
            print(json.dumps({"batch_id": self.args.batch_id, "status": self.record["status"],
                "passed": self.record.get("passed", False), "planned_chains": len(self.plan),
                "completed_chains": sum(row.get("status") in {"passed", "capability_failed"} for row in self.record["runs"]),
                "passed_chains": sum(row.get("status") == "passed" for row in self.record["runs"]),
                "manifest": str(index_path)}, ensure_ascii=False), flush=True)

    def run(self):
        self.config.initialize_layout()
        # 子链持有 .detailed-batch.lock；此处再持有会与自身子进程死锁。
        # 使用独立锁，防止多个故障批次竞争。
        lock_path = self.data_root / "runs/.fault-batch.lock"
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BatchStopped('另一故障批次已持有串行调度锁') from None
            return self.run_locked()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=SUPPORTED, required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--bench-image", required=True)
    parser.add_argument("--services-image", required=True)
    parser.add_argument("--materials-context", type=Path, required=True)
    parser.add_argument("--prefix", type=identifier, required=True)
    parser.add_argument("--batch-id", type=identifier, required=True)
    parser.add_argument("--phase", choices=tuple(PHASES), required=True)
    args = parser.parse_args()
    try:
        return FaultBatch(args).run()
    except Exception:
        print(json.dumps({"batch_id": args.batch_id, "status": "stopped", "passed": False,
            "failed_checks": ["batch_preflight"], "manifest": None}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
