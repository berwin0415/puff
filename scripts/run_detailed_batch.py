"""串行运行 Docker 详细用例，并保留冻结的外部批次索引。

宿主调度器使用当前 Python 解释器启动已有的 run_detailed.py 入口。Agent、模型请求、任务准备和判定均留在 Docker 内。子进程输出作为私有外部证据；标准输出仅包含 JSON 摘要。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.bench import identifier
from scripts.lab import CANDIDATES, Lab

SUPPORTED = ("deepseek_harness", "openclaw", "deepagents_langgraph", "openai_agents_sdk")
FORMAL_CASES = ("tools", "browser", "sensitive", "extension", "extension-denied")
SMOKE_CASES = (*FORMAL_CASES, "diagnostic", "diagnostic-timeout", "cancel")
TASK_FILES = {"tools": "tools.yaml", "browser": "browser.yaml", "sensitive": "control-policy.yaml",
    "extension": "business-extension.yaml", "extension-denied": "business-extension.yaml",
    "diagnostic": "control-policy.yaml", "diagnostic-timeout": "control-policy.yaml", "cancel": "control-policy.yaml"}


class BatchStopped(RuntimeError):
    """可写入批次索引的公开完整性原因，不含凭证。"""


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise BatchStopped('必需的固定材料不是普通文件')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path):
    if path.is_symlink() or not path.is_file():
        raise BatchStopped('缺少完整且为普通文件的判定或清单')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise BatchStopped('证据记录不是对象')
    return value


def save(path, record, *, first=False):
    # 目录为新建预留，且从不挂载到候选。
    # 私有文件也使捕获的子进程输出不被其他用户读取。
    if first:
        with path.open("x") as stream:
            path.chmod(0o600)
            stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    else:
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w") as stream:
            temporary.chmod(0o600)
            stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)


def summary(row):
    return {key: row.get(key) for key in ("run_id", "case", "seed", "passed", "failed_checks", "metrics", "exit", "manifest")}


class DetailedBatch:
    def __init__(self, args):
        self.args, self.lab = args, Lab()
        self.config = self.lab.config
        self.cases = tuple(args.cases or (SMOKE_CASES if args.phase == "smoke" else FORMAL_CASES))
        self.seeds = tuple(args.seeds or ((101,) if args.phase == "smoke" else (101, 202, 303)))
        if len(set(self.cases)) != len(self.cases) or len(set(self.seeds)) != len(self.seeds):
            raise BatchStopped('重复用例或种子会复用证据')
        self.day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        self.data_root = self.config.host_data_root
        self.index = self.config.paths_for("detailed", args.batch_id, args.candidate, "alice", "main",
            args.batch_id, case="runtime", date=self.day).run
        self.plan = []
        for case in self.cases:
            for seed in self.seeds:
                suffix = ("smoke" if self.seeds == (101,) else f"smoke-seed-{seed}") if args.phase == "smoke" else f"seed-{seed}"
                run_id = identifier(f"{args.prefix}-{case}-{suffix}-001")
                command = [sys.executable, str(ROOT / "scripts/run_detailed.py"), "--candidate", args.candidate,
                    "--case", case, "--seed", str(seed), "--run-id", run_id]
                self.plan.append({"run_id": run_id, "case": case, "seed": seed, "command": command})
        self.record = {"schema_version": 1, "batch_id": args.batch_id, "candidate": args.candidate,
            "phase": args.phase, "prefix": args.prefix, "started_at": now(), "status": "initializing",
            "data_root": str(self.data_root), "serial": True, "plan": self.plan, "runs": []}
        self.services = yaml.safe_load((ROOT / "compose/docker-compose.yml").read_text())["services"]
        self.worker_service = CANDIDATES[args.candidate][0]
        names = {"bench", "fixtures", "tools", "model-proxy", self.worker_service}
        if "browser" in self.cases:
            names.update(("site", "browser"))
        self.image_tags = {name: self.services[name]["image"] for name in sorted(names)}

    def assert_clean(self):
        # 子进程仅在 finally 和清理结束后返回。即使已生成完整判定，
        # 删除失败也必须停止批次。
        if self.lab.containers():
            raise BatchStopped('当前实验容器仍存在；必须清理完成后再开始下一轮')

    def image_ids(self):
        result = {}
        for tag in sorted(set(self.image_tags.values())):
            reply = self.lab.call(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                capture=True, check=False, timeout=20)
            value = reply.stdout.strip()
            if reply.returncode or not value.startswith("sha256:") or len(value) != 71:
                raise BatchStopped('无法固定必需的本地 Docker 镜像')
            result[tag] = value
        return result

    def material_hashes(self):
        adapter = ROOT / "external/build-context" / self.args.candidate / "LAB_ADAPTER_MANIFEST.json"
        tasks = {path.name: digest(path) for path in sorted((ROOT / "configs/tasks").glob("*.yaml"))}
        adapter_record = read_object(adapter)
        export = adapter_record.get("export_sha256")
        if (any(TASK_FILES[case] not in tasks for case in self.cases)
                or adapter_record.get("candidate") != self.args.candidate
                or not isinstance(export, str) or len(export) != 64):
            raise BatchStopped('必需的冻结任务或候选适配器清单不完整')
        return {"task_hashes": tasks,
            "adapter_manifest_path": str(adapter), "adapter_manifest_sha256": digest(adapter),
            "adapter_export_sha256": export,
            "scheduler_material_hashes": {name: digest(ROOT / name) for name in (
                "scripts/run_detailed.py", "scripts/run_detailed_batch.py", "configs/experiment.yaml",
                "compose/docker-compose.yml", "compose/detailed.override.yml")}}

    def assert_frozen(self):
        if self.image_ids() != self.record["image_ids"]:
            raise BatchStopped('Docker 镜像标签偏离批次固定值')
        if self.material_hashes() != self.record["materials"]:
            raise BatchStopped('批次运行期间任务、适配器清单或调度材料发生变化')

    def assert_fresh_run(self, item):
        run_id = item["run_id"]
        paths = self.config.paths_for("detailed", run_id, self.args.candidate, "alice", "main", run_id,
            case=item["case"], date=self.day)
        if any(path.exists() or path.is_symlink() for path in vars(paths).values()):
            raise BatchStopped('计划运行的证据或状态已存在；请使用新前缀')
        if any((self.data_root / "runs").glob(f"*/*/*/{run_id}")):
            raise BatchStopped('计划运行 ID 在其他日期或用例下已有外部证据')

    def completed_evidence(self, item, process_exit):
        matches = list((self.data_root / "runs").glob(f"*/{self.args.candidate}/{item['case']}/{item['run_id']}"))
        if len(matches) != 1:
            raise BatchStopped('本轮未生成且仅生成一个外部证据目录')
        directory = matches[0]
        manifest_path = directory / "manifest.json"
        manifest, verdict = read_object(manifest_path), read_object(directory / "verdict.json")
        identity = {"run_id": item["run_id"], "candidate": self.args.candidate, "case": item["case"], "seed": item["seed"], "experiment_id": "detailed"}
        if any(record.get(key) != value for record in (manifest, verdict) for key, value in identity.items()):
            raise BatchStopped('清单或判定身份与计划运行不一致')
        checks = verdict.get("checks")
        if (type(verdict.get("passed")) is not bool or not isinstance(verdict.get("metrics"), dict)
                or not isinstance(checks, list) or not checks or any(not isinstance(check, dict)
                    or not isinstance(check.get("name"), str) or type(check.get("passed")) is not bool for check in checks)
                or verdict["passed"] != all(check["passed"] for check in checks)
                or type(manifest.get("candidate_exit_code")) is not int
                or manifest.get("grade_exit_code") != (0 if verdict["passed"] else 1)
                or process_exit != (0 if verdict["passed"] else 1)):
            raise BatchStopped('判定、进程退出和清单不完整或不一致')
        input_ = read_object(directory / "input.json")
        expected_task = self.record["materials"]["task_hashes"][TASK_FILES[item["case"]]]
        if input_.get("task_sha256") != expected_task:
            raise BatchStopped('实际准备的任务与批次冻结的 YAML 不同')
        containers = manifest.get("containers")
        if not isinstance(containers, list):
            raise BatchStopped('清单未保留 Docker Worker 事实')
        tag = self.image_tags[self.worker_service]
        workers = [row for row in containers if row.get("image_tag") == tag]
        if len(workers) != 1 or workers[0].get("image_id") != self.record["candidate_image_id"]:
            raise BatchStopped('实际 Worker 镜像与候选固定镜像不同')
        for container in containers:
            if container.get("image_tag") in self.record["image_ids"] and container.get("image_id") != self.record["image_ids"][container["image_tag"]]:
                raise BatchStopped('实际服务镜像与批次固定镜像不同')
        return {"passed": verdict["passed"], "failed_checks": [check["name"] for check in checks if not check["passed"]],
            "metrics": verdict["metrics"], "exit": process_exit, "manifest": str(manifest_path),
            "candidate_exit_code": manifest["candidate_exit_code"], "grade_exit_code": manifest["grade_exit_code"],
            "candidate_container_id": workers[0].get("id"), "candidate_image_id": workers[0]["image_id"]}

    def run_locked(self):
        if any((self.data_root / "runs").glob(f"*/*/runtime/{self.args.batch_id}")):
            raise BatchStopped('批次 ID 已存在；不能覆盖或续写批次证据')
        for item in self.plan:
            self.assert_fresh_run(item)
        self.index.mkdir(parents=True, exist_ok=False)
        self.index.chmod(0o700)
        index_path = self.index / "batch.json"
        save(index_path, self.record, first=True)
        active = None
        try:
            self.assert_clean()
            self.record["image_ids"] = self.image_ids()
            self.record["candidate_image_id"] = self.record["image_ids"][self.image_tags[self.worker_service]]
            self.record["bench_image_id"] = self.record["image_ids"][self.image_tags["bench"]]
            self.record["materials"] = self.material_hashes()
            self.record["status"] = "running"
            save(index_path, self.record)
            for item in self.plan:
                self.assert_clean()
                self.assert_frozen()
                self.assert_fresh_run(item)
                active = {**item, "started_at": now(), "status": "running", "passed": None,
                    "failed_checks": [], "metrics": {}, "exit": None, "manifest": None,
                    "log": str(self.index / (item["run_id"] + ".log"))}
                self.record["runs"].append(active)
                save(index_path, self.record)
                started = time.monotonic()
                with Path(active["log"]).open("x") as output:
                    Path(active["log"]).chmod(0o600)
                    reply = subprocess.run(item["command"], cwd=ROOT, env=self.lab.env,
                        stdout=output, stderr=subprocess.STDOUT, check=False)
                active.update({"exit": reply.returncode, "finished_at": now(), "elapsed_seconds": time.monotonic() - started})
                evidence = list((self.data_root / "runs").glob(f"*/{self.args.candidate}/{item['case']}/{item['run_id']}"))
                if len(evidence) == 1:
                    active["manifest"] = str(evidence[0] / "manifest.json")
                # 在独立核验前轮全部清理、固定材料
                # 和实际证据之前，不启动下一轮付费运行。
                self.assert_clean()
                self.assert_frozen()
                active.update(self.completed_evidence(item, reply.returncode))
                active["status"] = "passed" if active["passed"] else "capability_failed"
                save(index_path, self.record)
                print(json.dumps(summary(active), ensure_ascii=False), flush=True)
                active = None
            self.record["status"] = "completed"
            self.record["passed"] = all(row["passed"] for row in self.record["runs"])
            self.record["failed_checks"] = sorted({check for row in self.record["runs"] for check in row["failed_checks"]})
            return 0 if self.record["passed"] else 1
        except (Exception, KeyboardInterrupt) as error:
            reason = str(error) if isinstance(error, BatchStopped) else '批次编排已停止；请检查外部批次证据'
            self.record.update({"status": "stopped", "passed": False, "stop_reason": reason, "failed_checks": ["batch_integrity"]})
            if active is not None:
                active.update({"status": "integrity_stopped", "passed": False,
                    "finished_at": now(), "failed_checks": ["batch_integrity"]})
                active.setdefault("elapsed_seconds", time.monotonic() - started)
                print(json.dumps(summary(active), ensure_ascii=False), flush=True)
            return 1
        finally:
            self.record["finished_at"] = now()
            save(index_path, self.record)
            print(json.dumps({"batch_id": self.args.batch_id, "passed": self.record.get("passed", False),
                "failed_checks": self.record.get("failed_checks", []),
                "metrics": {"completed_runs": sum(row.get("status") in {"passed", "capability_failed"} for row in self.record["runs"]),
                    "passed_runs": sum(row.get("passed") is True for row in self.record["runs"])},
                "exit": 0 if self.record.get("passed") else 1, "manifest": str(index_path)}, ensure_ascii=False), flush=True)

    def run(self):
        self.config.initialize_layout()
        # 协调批次，自身不占用任何 Docker 资源。
        lock_path = self.data_root / "runs/.detailed-batch.lock"
        if lock_path.is_symlink():
            raise BatchStopped('批次锁不能跟随符号链接')
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BatchStopped('另一详细批次已持有串行执行锁') from None
            return self.run_locked()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", choices=SUPPORTED, required=True)
    parser.add_argument("--prefix", type=identifier, required=True)
    parser.add_argument("--phase", choices=("smoke", "formal"), required=True)
    parser.add_argument("--batch-id", type=identifier, required=True)
    parser.add_argument("--cases", nargs="+", choices=SMOKE_CASES)
    parser.add_argument("--seeds", nargs="+", type=int, choices=(101, 202, 303, 404, 505))
    args = parser.parse_args()
    try:
        return DetailedBatch(args).run()
    except Exception:
        # 原始子进程错误可能包含捕获的服务输出。启动准备失败
        # 也保持结构化，不回显凭证或原始日志。
        print(json.dumps({"batch_id": args.batch_id, "passed": False, "failed_checks": ["batch_preflight"],
            "metrics": {}, "exit": 1, "manifest": None}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
