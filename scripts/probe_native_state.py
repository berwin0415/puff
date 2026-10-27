"""替换已删除的 Worker，并在不调用模型的情况下检查原生业务状态。

宿主仅调度 Docker、记录 Docker 事实、计算文件哈希及采集资源样本。准备和判定在已有 bench 镜像内执行。这里直接读取现有 Compose 服务定义，不调用 Compose，因此不会打开 model-proxy 的 env_file 或仓库 .env。
"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.bench import identifier
from runner.metrics import summarize
from scripts.lab import CANDIDATES, Lab

SUPPORTED = frozenset(("deepseek_harness", "openclaw", "deepagents_langgraph", "openai_agents_sdk"))
LABEL = "org.harness-lab.native-state.probe"
OWNER_LABEL = "org.harness-lab.native-state.owner"


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def regular(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root) or ".." in path.parts:
        raise ValueError('探针路径必须位于当前数据根目录内')
    current = root
    if current.is_symlink():
        raise ValueError('探针路径不能跟随符号链接')
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError('探针路径不能跟随符号链接')
    return path


class NativeStateLab(Lab):
    def __init__(self):
        super().__init__()
        for key in ("MODEL_API_BASE_URL", "MODEL_API_KEY", "MODEL_NAME", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            self.env.pop(key, None)
        # 不解析配置、不加载 env-file，也不激活模型 profile。
        self.services = yaml.safe_load((ROOT / "compose/docker-compose.yml").read_text())["services"]
        self.owned = []
        self.network = None
        self.owner = uuid4().hex

    def inspect(self, name):
        reply = self.call(["docker", "inspect", name], capture=True, check=False, timeout=10)
        if reply.returncode:
            if "no such object" in reply.stderr.lower() or "no such container" in reply.stderr.lower():
                return None
            raise RuntimeError('Docker 检查失败；尚未证明对象不存在')
        row = json.loads(reply.stdout)[0]
        env = row.get("Config", {}).get("Env", [])
        return {"id": row["Id"], "name": row["Name"], "image_id": row["Image"],
            "image_tag": row["Config"]["Image"], "state": row["State"],
            "memory_limit": row["HostConfig"]["Memory"], "cpu_nano": row["HostConfig"]["NanoCpus"],
            "read_only": row["HostConfig"]["ReadonlyRootfs"], "network_mode": row["HostConfig"]["NetworkMode"],
            "tmpfs": row["HostConfig"].get("Tmpfs", {}), "labels": row["Config"].get("Labels", {}),
            "credential_env_present": any(value.partition("=")[0] in {
                "MODEL_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"} and value.partition("=")[2] for value in env),
            "mounts": [{key: mount[key] for key in ("Source", "Destination", "RW")} for mount in row["Mounts"]]}

    def create(self, name, service, probe_id, image, mounts, command, *, network="none", alias=None, env=None, entrypoint=None):
        spec = self.services[service]
        if not spec.get("read_only") or spec.get("env_file"):
            raise ValueError('仅允许使用已有的只读服务，且不能配置 env_file')
        args = ["docker", "create", "--name", name, "--label", LABEL + "=" + probe_id,
                "--label", OWNER_LABEL + "=" + self.owner,
                "--label", "org.harness-lab.experiment=current", "--platform", spec["platform"],
                "--read-only", "--memory", str(spec["mem_limit"]), "--cpus", str(spec["cpus"]),
                "--pids-limit", str(spec["pids_limit"]), "--network", network]
        for item in spec.get("tmpfs", []):
            args += ["--tmpfs", item]
        if entrypoint is not None:
            args += ["--entrypoint", entrypoint]
        if alias:
            args += ["--network-alias", alias]
        for source, target, writable in mounts:
            if not Path(source).exists():
                raise FileNotFoundError('缺少必需的原始挂载：' + target)
            args += ["--mount", "type=bind,src=" + str(source) + ",dst=" + target + ("" if writable else ",readonly")]
        for key, value in (env or {}).items():
            args += ["--env", key + "=" + value]
        args += [image, *command]
        # 先记录操作意图：create 超时仍可能已成功。清理时
        # 仍须核对本次调用不可猜测的归属标签。
        self.owned.append((name, probe_id))
        reply = self.call(args, capture=True, timeout=20)
        return reply.stdout.strip()

    def execute(self, name, output, timeout):
        try:
            reply = self.call(["docker", "start", "--attach", name], capture=True, check=False, timeout=timeout)
            code = reply.returncode
        except subprocess.TimeoutExpired:
            self.call(["docker", "kill", name], capture=True, check=False, timeout=10)
            code = 124
        logs = self.call(["docker", "logs", name], capture=True, check=False, timeout=10)
        output.write_text(logs.stdout + logs.stderr)
        return code

    def bench(self, action, probe, run, state, probe_id, network="none"):
        name = "harness-lab-native-bench-" + action + "-" + probe_id
        mounts = [(self.config.host_data_root / item, "/data/" + item, False)
                  for item in ("state", "workspaces", "fixtures", "budgets", "runs")]
        mounts += [(state, "/state", False), (probe, "/probe", True)]
        relative = run.relative_to(self.config.host_data_root)
        command = ["-m", "runner.native_state", action, "--run-dir", str(Path("/data") / relative), "--probe-id", probe_id]
        spec = self.services["bench"]
        created = self.create(name, "bench", probe_id, spec["image"], mounts, command,
                              entrypoint="python", network=network)
        code = self.execute(name, probe / ("bench-" + action + ".log"), 90)
        return code, self.inspect(created)

    def cleanup_owned(self):
        failures = []
        for name, probe_id in reversed(self.owned):
            try:
                record = self.inspect(name)
                if record is not None and record["labels"].get(LABEL) == probe_id and record["labels"].get(OWNER_LABEL) == self.owner:
                    reply = self.call(["docker", "rm", "--force", name], capture=True, check=False, timeout=10)
                    if reply.returncode:
                        failures.append(name)
            except Exception:
                failures.append(name)
        if self.network is not None:
            name, probe_id = self.network
            try:
                reply = self.call(["docker", "network", "inspect", name], capture=True, check=False, timeout=10)
                if reply.returncode == 0:
                    labels = json.loads(reply.stdout)[0].get("Labels", {})
                    if labels.get(LABEL) == probe_id and labels.get(OWNER_LABEL) == self.owner:
                        removed = self.call(["docker", "network", "rm", name], capture=True, check=False, timeout=10)
                        if removed.returncode:
                            failures.append(name)
            except Exception:
                failures.append(name)
        return failures

    def run(self, run, probe_id, timeout):
        run = regular(run, self.config.host_data_root)
        manifest = json.loads(regular(run / "manifest.json", self.config.host_data_root).read_text())
        candidate = manifest.get("candidate")
        if candidate not in SUPPORTED or manifest.get("case") != "extension":
            raise ValueError('仅支持已结束的扩展任务，且必须有明确的原生检查 API')
        service, prefix, _, _ = CANDIDATES[candidate]
        workers = [row for row in manifest["containers"] if row["image_tag"] == self.services[service]["image"]
                   and any(m["Destination"] == "/state" for m in row["mounts"])]
        if len(workers) != 1:
            raise ValueError('原始清单必须标明且仅标明一个候选 Worker')
        original = workers[0]
        absent = {key: self.inspect(original[key]) is None for key in ("id", "name")}
        if not all(absent.values()):
            raise RuntimeError('原 Worker 必须已删除；探针不会替你删除')
        if any(row["state"]["Running"] for row in self.containers()):
            raise RuntimeError('另一项当前实验正在运行')
        source_mounts = {row["Destination"]: regular(row["Source"], self.config.host_data_root) for row in original["mounts"]}
        state = source_mounts["/state"]
        active = self.call(["docker", "ps", "--quiet"], capture=True, timeout=10).stdout.split()
        for ident in active:
            other = self.inspect(ident)
            if other and any(Path(m["Source"]) == state and m["RW"] for m in other["mounts"]):
                raise RuntimeError('原生状态已有其他写入进程')
        root = regular(run / "native-state", self.config.host_data_root)
        root.mkdir(exist_ok=True)
        # 串行探测同一原运行，包括 DSH 的可写加载。
        with (root / ".probe.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probe = root / probe_id
            probe.mkdir(exist_ok=False)
            return self.run_locked(run, probe, probe_id, timeout, manifest, original, source_mounts, absent, service, prefix)

    def run_locked(self, run, probe, probe_id, timeout, manifest, original, mounts, absent, service, prefix):
        candidate = manifest["candidate"]
        samples, stop = [], threading.Event()
        sampler = None
        status = {"probe_id": probe_id, "candidate": candidate, "original_worker": original,
            "original_worker_absent_before": absent, "model_proxy_started": False,
            "real_model_credential_injected": False, "task_continuation_claim": False,
            "state_mount_mode": "rw_native_resume_metadata" if candidate == "deepseek_harness" else "ro",
            "errors": []}
        try:
            code, status["prepare_bench"] = self.bench("prepare", probe, run, mounts["/state"], probe_id)
            if code:
                raise RuntimeError('bench 准备阶段拒绝了源运行；请检查 bench-prepare.log')
            network = "none"
            if candidate == "deepseek_harness":
                network = "harness-lab-native-" + probe_id
                self.network = (network, probe_id)
                self.call(["docker", "network", "create", "--internal", "--label", LABEL + "=" + probe_id,
                           "--label", OWNER_LABEL + "=" + self.owner, network], capture=True, timeout=20)
                status["network"] = {"name": network, "internal": True}
                for role in ("fixtures", "tools"):
                    old = next(row for row in manifest["containers"] if row["name"].rstrip("/").endswith("-" + role + "-1"))
                    original_mounts = [(regular(row["Source"], self.config.host_data_root), row["Destination"], row["RW"] and row["Destination"] != "/workspace")
                                       for row in old["mounts"]]
                    command = list(self.services[role]["command"])
                    substitutions = {"${LAB_EXPERIMENT_ID:-infrastructure}": manifest["experiment_id"],
                                     "${LAB_VARIANT:-smoke}": manifest["run_id"]}
                    for key, value in substitutions.items():
                        command = [arg.replace(key, value) for arg in command]
                    name = "harness-lab-native-" + role + "-" + probe_id
                    self.create(name, role, probe_id, old["image_id"], original_mounts, command, network=network, alias=role)
                    self.call(["docker", "start", name], capture=True, timeout=10)
                code, _ = self.bench("wait-services", probe, run, mounts["/state"], probe_id, network=network)
                if code:
                    raise RuntimeError('原 Fixture 或工具服务未就绪')
            else:
                status["network"] = {"name": "none", "internal": False}
            allowed = {"/config/scope.json", "/config/scope.token", "/config/model-run.token"}
            worker_mounts = [(source, target, False) for target, source in mounts.items() if target in allowed]
            worker_mounts += [(probe / "controls/request.json", "/config/request.json", False),
                (mounts["/state"], "/state", candidate == "deepseek_harness"),
                (mounts["/workspace"], "/workspace", False), (probe / "candidate", "/evidence", True)]
            flags = [] if candidate == "deepseek_harness" else ["--inspect-business-state" if candidate == "openclaw" else "--restore-business-state-only"]
            name = "harness-lab-native-" + prefix + "-" + probe_id
            ident = self.create(name, service, probe_id, original["image_id"], worker_mounts,
                ["--request", "/config/request.json", *flags], network=network,
                env={"MODEL_API_BASE_URL": "http://model-proxy:8082/v1", "MODEL_RUN_TOKEN_FILE": "/config/model-run.token", "MODEL_NAME": manifest["model_name"]})
            status["new_worker_before_start"] = self.inspect(ident)
            def sample():
                while not stop.is_set():
                    try:
                        reply = self.call(["docker", "stats", "--no-stream", "--format", "{{json .}}", ident], capture=True, timeout=5)
                        row = {"unix_time": time.time(), "containers": [json.loads(line) for line in reply.stdout.splitlines()]}
                    except Exception:
                        row = {"unix_time": time.time(), "containers": [], "error": 'Docker 采样失败'}
                    samples.append(row)
                    with (probe / "resource-samples.jsonl").open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                    stop.wait(self.config.sampling.interval_seconds)
            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
            status["candidate_exit_code"] = self.execute(name, probe / "candidate.log", timeout)
            stop.set()
            sampler.join(timeout=10)
            status["new_worker"] = self.inspect(ident)
            status["original_worker_absent_after"] = {key: self.inspect(original[key]) is None for key in ("id", "name")}
            save(probe / "orchestration.json", status)
            grade, _ = self.bench("grade", probe, run, mounts["/state"], probe_id)
            return 0 if grade == status["candidate_exit_code"] == 0 else 1
        except Exception as error:
            status["errors"].append(type(error).__name__ + ": " + str(error))
            save(probe / "orchestration.json", status)
            raise
        finally:
            stop.set()
            if sampler is not None:
                sampler.join(timeout=10)
            try:
                save(probe / "resource-metrics.json", summarize(samples))
            finally:
                failures = self.cleanup_owned()
                save(probe / "cleanup.json", {"owned_resources_only": True, "failures": failures})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--probe-id", required=True, type=identifier)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error('timeout 必须为 1 至 600 秒')
    return NativeStateLab().run(args.run_dir, args.probe_id, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
