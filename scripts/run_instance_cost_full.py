#!/usr/bin/env python3
"""I25 可信 Docker 内 DA 成本流程驱动；编写代码不代表执行。

仅使用确切的预构建镜像。宿主控制容器、标准输入和观察文件；材料准备、任务预期与全部评分均在 Docker 内完成。真实观察器的开销会被测量，但它不冒充 I24 产品后端。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.instance_cost import CAPS, CPU, MEMORY, exclusive, read, sha, rotation_users, INACTIVE_WAKE_KIND
from runner.instance_cost_full import make_flow_plan
from runner.config import load_config
from scripts.run_instance_cost import IdleDockerLab, NativeWorker, now


HOST_REASON_CODES = {
    "host_data_root_missing", "host_data_root_invalid", "host_data_root_config_invalid",
    "host_data_root_config_mismatch", "host_output_root_invalid", "host_output_root_outside_data_root",
    "observer_probe_timeout", "observer_probe_nonce_changed", "observer_probe_read_failed",
    "observer_probe_binding_changed", "host_observer_control_already_exists", "host_observer_control_sequence_exhausted",
    "host_rotation_sampler_incomplete",
}
HOST_RUNTIME_REASONS = {
    '固定的本地服务镜像已变化': "host_services_image_pin_changed",
    'Docker 必须提供冻结的 6 GiB / 3 CPU 资源预算': "host_docker_capacity_invalid",
    '远程模型环境与冻结材料不同': "host_model_configuration_changed",
    '拒绝启动下一容器，以免超过冻结的总分配': "host_next_allocation_refused",
    '新容器与明确固定的镜像或配额不同': "host_created_container_pin_changed",
    '仅业务评分允许预期的退出 1': "host_bench_exit_policy_invalid",
    'bench 不能与候选 Worker 同时运行': "host_bench_worker_overlap_refused",
    '固定 Docker bench 的进程完整性失败；保留原始证据': "host_bench_process_integrity_failed",
    '固定 Docker bench 执行失败；保留原始证据': "host_bench_execution_failed",
    '不能回收非本实验拥有的确切容器': "host_retirement_identity_unproven",
    '完整成本观察清单与明确的创建固定值不同': "host_inventory_pin_set_changed",
    '资源采样器未能停止；不能启动 bench': "host_sampler_did_not_stop",
}


def host_failure_reason(error):
    """分类已知失败，不序列化异常文本或凭证。"""
    reason = error.args[0] if error.args and isinstance(error.args[0], str) else None
    if reason in HOST_REASON_CODES:
        return reason
    if reason in HOST_RUNTIME_REASONS:
        return HOST_RUNTIME_REASONS[reason]
    if isinstance(error, subprocess.TimeoutExpired):
        return "host_docker_command_timeout"
    if isinstance(error, subprocess.CalledProcessError):
        return "host_docker_command_failed"
    if isinstance(error, TimeoutError):
        return "host_operation_timeout"
    if isinstance(error, OSError):
        return "host_evidence_io_failed"
    return "host_unclassified_failure"


def validate_data_root(output_root):
    """创建证据或 Docker 对象前必须明确指定宿主根目录。"""
    value = os.environ.get("HARNESS_LAB_DATA_ROOT")
    if value is None or not value.strip():
        raise ValueError("host_data_root_missing")
    data_root = Path(value)
    if (not data_root.is_absolute() or data_root == Path(data_root.anchor)
            or any(part.lower() in {"old", ".."} for part in data_root.parts)
            or any(item.is_symlink() for item in (data_root, *data_root.parents))
            or (data_root.exists() and not data_root.is_dir())
            or any(ord(character) < 32 for character in value) or any(character in value for character in ("~", "\\", ","))):
        raise ValueError("host_data_root_invalid")
    if (not isinstance(output_root, Path) or not output_root.is_absolute()
            or any(part.lower() in {"old", ".."} for part in output_root.parts)
            or any(item.is_symlink() for item in (output_root, *output_root.parents))):
        raise ValueError("host_output_root_invalid")
    resolved = data_root.resolve()
    target = output_root.resolve()
    if target == resolved or not target.is_relative_to(resolved):
        raise ValueError("host_output_root_outside_data_root")
    try:
        config = load_config(ROOT / "configs/experiment.yaml")
    except (OSError, ValueError, TypeError):
        raise ValueError("host_data_root_config_invalid") from None
    if config.environment != "host" or config.host_data_root.resolve() != resolved:
        raise ValueError("host_data_root_config_mismatch")
    return resolved


def bench_process_integrity(observed, *, identity, name, image, cli_exit, expected_exit):
    """已写入的判定不能替代固定评分器的健康退出。"""
    state = observed.get("state", {})
    checks = {
        "exact_container_identity": (re.fullmatch(r"[0-9a-f]{64}", identity or "") is not None
            and observed.get("id") == identity and observed.get("name") == "/" + name),
        "exact_image_and_role": observed.get("image_id") == image and observed.get("role") == "bench",
        "exact_finite_quotas": type(observed.get("memory_limit")) is int
            and type(observed.get("cpu_nano")) is int
            and observed["memory_limit"] == CAPS["bench"][0] and observed["cpu_nano"] == CAPS["bench"][1],
        "actually_exited": state.get("Status") == "exited" and state.get("Running") is False
            and type(state.get("Pid")) is int and state["Pid"] == 0,
        "not_oom_dead_paused_or_restarting": all(state.get(field) is False
            for field in ("OOMKilled", "Dead", "Paused", "Restarting")),
        "no_runtime_error": state.get("Error") == "",
        "cli_and_docker_exit_match_verdict": (type(expected_exit) is int and expected_exit in {0, 1}
            and type(cli_exit) is int and cli_exit == expected_exit
            and type(state.get("ExitCode")) is int and state["ExitCode"] == expected_exit),
    }
    return {"passed": all(checks.values()), "checks": checks,
        "cli_exit": cli_exit, "expected_exit": expected_exit,
        "trust_limit": '仅证明进程完整性；业务判定留在原始 Docker 证据中'}


class CostFlowLab(IdleDockerLab):
    def __init__(self, root, plan):
        super().__init__(root, plan)
        self.networks, self.registered, self.workers = [], {}, {}
        self.normal_closes, self.cold_reads, self.task_timings = [], [], []
        self.phase, self.kind = "preparation", None
        self.lock = threading.RLock()
        self.stop_sampling = threading.Event()
        self.sampler = None
        self.sequence = 0
        self.control_sequence = 0
        self.proxy_secret_dir = None
        self.idle_phases = {}
        self.rotation_history_reads, self.rotation_start = [], None
        self.sampling_status = None
        self.fault = {"injected": False, "recovery_attempted": False,
            "native_recovery_gap": '本成本入口不提交重启提示词、None 重放或替换预算'}
        self.lifecycle = None
        if rotation_users(plan):
            from scripts.instance_cost_lifecycle import RotationLifecycle
            self.lifecycle = RotationLifecycle(self)

    def phase_to(self, phase, kind=None):
        if self.lifecycle is not None:
            return self.lifecycle.phase_to(phase, kind)
        with self.lock:
            self.publish_control(phase, kind)
            self.phase, self.kind = phase, kind

    def publish_control(self, phase, kind, *, nonce=None):
        """一次发布完整控制消息；已发布的文件名不复用。"""
        guard = self.lifecycle.control_lock if self.lifecycle is not None else self.lock
        with guard:
            sequence = self.control_sequence + 1
            if sequence > 999_999_999_999:
                raise ValueError("host_observer_control_sequence_exhausted")
            directory = self.root / "protected/observer/controls"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"control-{sequence:012d}.json"
            temporary = path.with_name(path.name + ".pending")
            value = {"sequence": sequence, "phase": phase, "kind": kind}
            if nonce is not None:
                value["probe_nonce"] = nonce
            if path.exists():
                raise ValueError("host_observer_control_already_exists")
            exclusive(temporary, value)
            os.rename(temporary, path)
            self.control_sequence = sequence
            return value

    def preflight(self):
        super().preflight()
        identity = self.call(["docker", "image", "inspect", "--format", "{{.Id}}", self.plan["services_image_id"]]).stdout.strip()
        if identity != self.plan["services_image_id"]:
            raise RuntimeError('固定的本地服务镜像已变化')
        info = json.loads(self.call(["docker", "info", "--format", "{{json .}}"], timeout=15).stdout)
        if info["OSType"] != "linux" or info["MemTotal"] < MEMORY or info["NCPU"] < 3:
            raise RuntimeError('Docker 必须提供冻结的 6 GiB / 3 CPU 资源预算')
        # Compose 解析 .env；密钥值不打印，也不放入 argv。
        resolved = json.loads(self.call(["docker", "compose", "--project-name", "harness-lab", "-f",
            str(ROOT / "compose/docker-compose.yml"), "--profile", "model", "config", "--format", "json"]).stdout)
        proxy_env = resolved["services"]["model-proxy"]["environment"]
        if proxy_env.get("MODEL_NAME") != self.plan["model_name"] or any(not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key)
                or not isinstance(value, str) or any(c in value for c in "\n\r\0") for key, value in proxy_env.items()):
            raise ValueError('远程模型环境与冻结材料不同')
        # 仅 Docker 接收此 env 文件。它不能位于 /trial 下，
        # 因为可信 observer 和 bench 会挂载 /trial 检查运行证据。
        self.proxy_secret_dir = tempfile.TemporaryDirectory(prefix="harness-lab-i25-proxy-")
        self.proxy_environment = Path(self.proxy_secret_dir.name) / "proxy.env"
        descriptor = os.open(self.proxy_environment, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            for key, value in proxy_env.items():
                stream.write(key + "=" + value + "\n")
        del proxy_env, resolved
        for owner in [*self.plan["active_users"], *rotation_users(self.plan)]:
            name = "hl-ic-" + self.plan["trial_id"] + "-" + owner
            self.call(["docker", "network", "create", "--label", self.label, name])
            self.networks.append(name)

    def network(self, owner):
        return "hl-ic-" + self.plan["trial_id"] + "-" + owner

    @staticmethod
    def mount(source, destination, readonly=False):
        return ["--mount", "type=bind,src=" + str(source) + ",dst=" + destination + (",readonly" if readonly else "")]

    def caps(self, role):
        return CAPS["backend" if role == "observer" else role]

    def reserve(self, role):
        current = [row for row in self.observations() if row["state"]["Running"]]
        memory, cpu = self.caps(role)
        if (sum(row["memory_limit"] for row in current) + memory > MEMORY
                or sum(row["cpu_nano"] for row in current) + cpu > CPU):
            raise RuntimeError('拒绝启动下一容器，以免超过冻结的总分配')

    def pin(self, identity, role, image, *, running=False):
        memory, cpu = self.caps(role)
        self.registered[identity] = {"id": identity, "role": role, "image_id": image,
            "memory_limit": memory, "cpu_nano": cpu, "running": running}
        observed = [row for row in self.observations() if row["id"] == identity]
        if (len(observed) != 1 or observed[0]["image_id"] != image
                or observed[0]["memory_limit"] != memory or observed[0]["cpu_nano"] != cpu):
            raise RuntimeError('新容器与明确固定的镜像或配额不同')
        self.sequence += 1
        exclusive(self.root / "protected" / ("lifecycle-" + str(self.sequence) + ".json"), {
            "time": now(), "phase": self.phase, "pin": self.registered[identity], "observed": observed[0]})

    def service(self, role, suffix, network, alias, mounts, command, *, image=None, env_file=None, other_network=False):
        if self.lifecycle is not None:
            return self.lifecycle.service(role, suffix, network, alias, mounts, command,
                image=image, env_file=env_file, other_network=other_network)
        with self.lock:
            self.reserve(role)
            image = image or self.plan["services_image_id"]
            memory, cpu = self.caps(role)
            name = "hl-ic-" + self.plan["trial_id"] + "-" + suffix
            self.owned.append(name)
            argv = ["docker", "create", "--name", name, "--platform", "linux/arm64", *self.labels(role),
                "--network", network, "--network-alias", alias, "--read-only", "--memory", str(memory),
                "--cpus", str(cpu / 1_000_000_000), "--pids-limit", "128", "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:size=128m", "-e", "HARNESS_LAB_ENVIRONMENT=container"]
            for source, destination, readonly in mounts:
                argv += self.mount(source, destination, readonly)
            if env_file:
                argv += ["--env-file", str(env_file)]
            argv += ["--entrypoint", "python", image, *command]
            identity = self.call(argv, timeout=30).stdout.strip()
            self.pin(identity, role, image)
            if other_network:
                for owner in [*self.plan["active_users"][1:], *rotation_users(self.plan)]:
                    self.call(["docker", "network", "connect", "--alias", alias, self.network(owner), identity])
            self.call(["docker", "start", identity], timeout=30)
            self.registered[identity]["running"] = True
            return identity

    def wait_port(self, identity, port):
        code = ("import socket,time\nfor attempt in range(100):\n try:\n"
            f"  s=socket.create_connection(('127.0.0.1',{port}),timeout=.3);s.close();break\n"
            " except OSError: time.sleep(.2)\nelse: raise SystemExit('private_service_not_ready')\n")
        self.call(["docker", "exec", identity, "python", "-c", code], timeout=30)

    def bench(self, action, *, check=True):
        # 离线 bench 不与任何候选 Worker 并发运行。
        with self.lock:
            if check is False and action != "grade":
                raise ValueError('仅业务评分允许预期的退出 1')
            if any(row["role"] == "worker" and row["state"]["Running"] for row in self.observations()):
                raise RuntimeError('bench 不能与候选 Worker 同时运行')
            self.reserve("bench")
            self.sequence += 1
            name = "hl-ic-" + self.plan["trial_id"] + "-bench-" + str(self.sequence)
            self.owned.append(name)
            command = ["docker", "create", "--name", name, "--platform", "linux/arm64", *self.labels("bench"),
                "--network", self.network("user-001"), "--read-only", "--memory", str(CAPS["bench"][0]), "--cpus", "0.5",
                "--pids-limit", "128", "--tmpfs", "/tmp:size=128m", "-e", "HARNESS_LAB_ENVIRONMENT=container",
                *self.mount(self.root, "/trial"), "--entrypoint", "python", self.plan["bench_image_id"],
                "-m", "runner.instance_cost_full", action, "--root", "/trial"]
            identity = self.call(command, timeout=30).stdout.strip()
            self.pin(identity, "bench", self.plan["bench_image_id"])
            reply = self.call(["docker", "start", "--attach", identity], timeout=120, check=False)
            (self.root / "protected" / (action + "-flow.log")).write_text(reply.stdout + reply.stderr)
            selected = [row for row in self.observations() if row["id"] == identity]
            final = selected[0] if len(selected) == 1 else {}
            exclusive(self.root / "protected" / (action + "-flow-containers.json"), selected)
            exclusive(self.root / "protected" / (action + "-flow-container.json"), final)
            expected_exit, verdict_source, verdict_valid = 0, None, True
            if action == "grade":
                expected_exit, verdict_valid = None, False
                filename = "baseline-verdict.json" if self.plan["baseline_only"] else "flow-verdict.json"
                verdict_path = self.root / "protected" / filename
                try:
                    verdict = read(verdict_path)
                    verdict_valid = isinstance(verdict, dict) and type(verdict.get("cost_subset_passed")) is bool
                    if verdict_valid:
                        expected_exit = 0 if verdict["cost_subset_passed"] else 1
                        verdict_source = {"path": "protected/" + filename, "sha256": sha(verdict_path.read_bytes())}
                except (OSError, ValueError, TypeError):
                    # 保留全部原始输出和容器记录。判定缺失或格式错误
                    # 属于完整性失败，不能仅当作退出 1。
                    pass
            integrity = bench_process_integrity(final, identity=identity, name=name,
                image=self.plan["bench_image_id"], cli_exit=reply.returncode, expected_exit=expected_exit)
            integrity["checks"].update(exactly_one_observed_container=len(selected) == 1,
                original_verdict_boolean_valid=verdict_valid)
            integrity["passed"] = all(integrity["checks"].values())
            integrity["verdict_source"] = verdict_source
            exclusive(self.root / "protected" / (action + "-flow-process-integrity.json"), integrity)
            if not integrity["passed"]:
                raise RuntimeError('固定 Docker bench 的进程完整性失败；保留原始证据')
            self.call(["docker", "rm", identity])
            self.registered.pop(identity)
            if check and reply.returncode != 0:
                raise RuntimeError('固定 Docker bench 执行失败；保留原始证据')
            return reply.returncode

    def worker(self, user, stage, *, online=False):
        if self.lifecycle is not None:
            return self.lifecycle.worker(user, stage, online=online)
        with self.lock:
            self.reserve("worker")
            network = self.network(user["owner"]) if online else "none"
            worker = NativeWorker(self, user, stage, network=network)
            self.pin(worker.id, "worker", self.plan["candidate_image_id"], running=True)
            return worker

    def close_worker(self, worker):
        if self.lifecycle is not None:
            return self.lifecycle.close_worker(worker)
        with self.lock:
            closed = worker.close()
            self.registered.pop(worker.id)
            self.normal_closes.append(closed)
            return closed

    def retire(self, identity):
        if self.lifecycle is not None:
            return self.lifecycle.retire(identity)
        with self.lock:
            before = [row for row in self.observations() if row["id"] == identity]
            if len(before) != 1 or identity not in self.registered:
                raise RuntimeError('不能回收非本实验拥有的确切容器')
            self.call(["docker", "stop", "--time", "10", identity], check=False, timeout=15)
            final = [row for row in self.observations() if row["id"] == identity][0]
            self.sequence += 1
            exclusive(self.root / "protected" / ("retired-" + str(self.sequence) + ".json"), final)
            self.call(["docker", "rm", identity])
            self.registered.pop(identity)

    def probe(self):
        if self.lifecycle is not None:
            return self.lifecycle.probe()
        with self.lock:
            nonce = uuid4().hex
            before = time.monotonic()
            control = self.publish_control(self.phase, self.kind, nonce=nonce)
            path = self.root / "protected/observer/replies" / (nonce + ".json")
            deadline = before + 8
            while not path.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("observer_probe_timeout")
                time.sleep(.05)
            try:
                value = read(path)
            except (OSError, ValueError, TypeError):
                raise ValueError("observer_probe_read_failed") from None
            if not isinstance(value, dict) or value.get("probe_nonce") != nonce:
                raise ValueError("observer_probe_nonce_changed")
            if (type(value.get("control_sequence")) is not int
                    or value["control_sequence"] != control["sequence"]
                    or value.get("phase") != control["phase"] or "kind" not in value or value["kind"] != control["kind"]):
                raise ValueError("observer_probe_binding_changed")
            return value

    def sample_flow(self):
        if self.lifecycle is not None:
            return self.lifecycle.sample_flow()
        with self.lock:
            phase = self.phase
            host_before = time.monotonic()
            before = self.probe()
            observed = self.observations()
            if set(self.registered) != {row["id"] for row in observed}:
                raise RuntimeError('完整成本观察清单与明确的创建固定值不同')
            planned = [dict(self.registered[row["id"]]) for row in observed]
            sample = self.sample([row["id"] for row in observed])
            after = self.probe()
            sample.update(phase=phase, planned_inventory=planned, observer_before=before, observer_after=after,
                host_before_observer_monotonic=host_before, host_after_observer_monotonic=time.monotonic())
            return sample

    def sample_loop(self):
        next_sample = time.monotonic()
        rotated = bool(rotation_users(self.plan))
        if rotated:
            self.sampling_status = {"started_monotonic": next_sample, "samples_written": 0, "completed": False}
        try:
            with (self.root / "protected/flow-resource-samples.jsonl").open("x") as stream:
                while not self.stop_sampling.is_set():
                    try:
                        value = self.sample_flow()
                    except Exception as error:
                        value = {"phase": self.phase, "time": now(), "error": type(error).__name__,
                            "reason_code": host_failure_reason(error),
                            "host_started_monotonic": time.monotonic()}
                    if rotated:
                        value["sample_sequence"] = self.sampling_status["samples_written"] + 1
                    stream.write(json.dumps(value) + "\n")
                    stream.flush()
                    if rotated:
                        self.sampling_status["samples_written"] += 1
                        self.lifecycle.sample_written(value)
                    next_sample = max(next_sample + 2, time.monotonic())
                    self.stop_sampling.wait(max(.05, next_sample - time.monotonic()))
            if rotated:
                self.sampling_status["completed"] = True
        finally:
            if rotated:
                self.sampling_status["finished_monotonic"] = time.monotonic()

    def idle(self, phase):
        self.phase_to(phase)
        start = time.monotonic()
        while time.monotonic() - start < 60:
            time.sleep(max(0, min(1, 60 - (time.monotonic() - start))))
        self.idle_phases[phase] = {"start": start, "end": time.monotonic()}

    def services_for(self, kind, owners):
        identities = []
        for owner in owners:
            home = self.root / "users" / owner
            network = self.network(owner)
            tools = self.service("tools", kind + "-tools-" + owner, network, "tools",
                [(home / "config" / (kind + "-tool-binding.json"), "/config/binding.json", True),
                 (home / "workspace" / kind, "/workspace", False), (home / "trusted" / kind, "/evidence", False)],
                ["-m", "execution.tools.server", "--binding-file", "/config/binding.json"])
            proxy = self.service("model-proxy", kind + "-proxy-" + owner, network, "model-proxy",
                [(home / "budgets" / kind, "/budget", False)],
                ["-m", "execution.model_proxy", "--database", "/budget/calls.sqlite", "--run-token-file", "/budget/run.token",
                 "--cap", "20", "--port", "8082"], env_file=self.proxy_environment)
            self.wait_port(tools, 8081)
            self.wait_port(proxy, 8082)
            identities.extend([tools, proxy])
        return identities

    def task_command(self, worker, kind):
        if self.lifecycle is not None:
            self.lifecycle.assert_sampling_ok()
        started = time.monotonic()
        record = {"owner": worker.user["owner"], "kind": kind, "worker_id": worker.id,
            "command_submitted_monotonic": started, "request_kind": "new_scope_new_budget"}
        try:
            reply = worker.command("run_task", task_id=kind + "-" + worker.user["owner"])
            record.update(response=reply, response_observed_monotonic=time.monotonic())
        except Exception as error:
            record.update(error_type=type(error).__name__, response_observed_monotonic=time.monotonic())
        return record

    def wave(self, kind, users):
        if self.lifecycle is not None:
            self.lifecycle.assert_sampling_ok()
        if kind == INACTIVE_WAKE_KIND:
            if [user["owner"] for user in users] != rotation_users(self.plan) or len(users) != 2:
                raise ValueError("host_inactive_wake_pair_changed")
            with self.lock:
                self.rotation_start = {"previous_task_ids": [row["kind"] + "-" + row["owner"] for row in self.task_timings],
                    "worker_ids_before": [worker.id for worker in self.workers.values()],
                    "observed_before": self.observations(), "started_monotonic": time.monotonic(),
                    "new_task_materials": [{"owner": user["owner"],
                        "empty_execution_directories": all(not list((self.root / "users" / user["owner"] / part / kind).iterdir())
                            for part in ("state", "evidence", "trusted")),
                        "budget_contains_only_new_token": sorted(path.name for path in
                            (self.root / "users" / user["owner"] / "budgets" / kind).iterdir()) == ["run.token"]}
                        for user in users]}
        self.phase_to("starting-" + kind, kind)
        requested = time.monotonic()
        with self.lock:
            request_probe = self.probe()
        owners = [user["owner"] for user in users]
        services = self.services_for(kind, owners)
        for user in users:
            if user["owner"] not in self.workers:
                self.workers[user["owner"]] = self.worker(user, kind + "-start", online=True)
            if kind == INACTIVE_WAKE_KIND:
                self.read_rotation_history(self.workers[user["owner"]], "before-task")
        ready = time.monotonic()
        with self.lock:
            ready_probe = self.probe()
        self.phase_to(kind, kind)
        if self.lifecycle is not None:
            self.lifecycle.assert_sampling_ok()
        records = []
        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            futures = {user["owner"]: pool.submit(self.task_command, self.workers[user["owner"]], kind) for user in users}
            if kind == "fault":
                trigger = self.root / "protected/observer/kill-ready.json"
                while not trigger.exists() and not any(future.done() for future in futures.values()):
                    time.sleep(.1)
                if trigger.exists():
                    with self.lock:
                        before = self.probe()
                        if len(before.get("tasks", [])) == 2 and all(row["live"] for row in before["tasks"]):
                            victim, other = self.workers["user-001"], self.workers["user-002"]
                            if self.lifecycle is not None:
                                after = self.lifecycle.kill(victim)
                            else:
                                self.call(["docker", "kill", "--signal", "KILL", victim.id], timeout=15)
                                self.registered[victim.id]["running"] = False
                                after = [row for row in self.observations() if row["id"] == victim.id][0]
                            after_probe = self.probe()
                            self.fault.update(injected=True, victim_id=victim.id, victim_owner="user-001",
                                unaffected_id=other.id, before_observer=before, after_kill=after,
                                after_observer=after_probe, recovery_attempted=False,
                                original_state_retained=True, original_model_budget_retained=True)
                # 未命中强杀窗口时，本实验明确记为未命中。
            records = [future.result(timeout=330) for future in futures.values()]
        self.phase_to("closing-" + kind)
        if kind == INACTIVE_WAKE_KIND:
            for user in users:
                self.read_rotation_history(self.workers[user["owner"]], "after-task")
        for record in records:
            record.update(request_received_monotonic=requested, workers_ready_monotonic=ready,
                docker_request_observation=request_probe, docker_ready_observation=ready_probe,
                measured_request_to_ready_seconds=ready - requested, queue_latency_seconds=None,
                first_effective_action_latency_seconds=None,
                latency_limit='可信编排回执，不是产品 API 或调度器；Docker 标记界定启动边界')
        self.task_timings.extend(records)
        if kind == "fault" and self.fault["injected"]:
            victim = self.workers.pop("user-001")
            if self.lifecycle is not None:
                self.lifecycle.remove_fault_worker(victim)
            else:
                victim.process.wait(timeout=20)
                victim.reader.join(timeout=5)
                with self.lock:
                    self.call(["docker", "rm", victim.id])
                    self.registered.pop(victim.id)
                victim.closed = True
        for user in users:
            worker = self.workers.get(user["owner"])
            if worker is not None and (self.plan["strategy"] == "on-demand" or kind in {"baseline", "fault"}):
                self.close_worker(worker)
                self.workers.pop(user["owner"])
        for identity in services:
            self.retire(identity)
        return records

    def read_rotation_history(self, worker, stage):
        """读取原用户的原生历史；不注入新的哨兵值。"""
        record = {"owner": worker.user["owner"], "worker_id": worker.id, "stage": stage,
            "started_monotonic": time.monotonic(), "task_id": worker.user["history_task_id"]}
        try:
            record.update(read_succeeded=True,
                restored=worker.command("state", task_id=worker.user["history_task_id"])["result"])
        except Exception as error:
            record.update(read_succeeded=False, error_type=type(error).__name__)
        record["finished_monotonic"] = time.monotonic()
        self.rotation_history_reads.append(record)

    def business_readback(self, users, *, kind="active"):
        self.phase_to("business-state-readback" if kind == "active" else kind + "-business-state-readback")
        for user in users:
            owner = user["owner"]
            try:
                original = read(self.root / "users" / owner / "evidence" / kind / "result.json")["native_state"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                if kind == "active":
                    raise
                # 保留失败业务证据与独立历史读取；
                # 不重试业务任务，也不编造 checkpoint。
                self.cold_reads.append({"owner": owner, "kind": kind,
                    "read_succeeded": False, "error_type": type(error).__name__})
                continue
            worker = self.workers.get(owner)
            temporary = worker is None
            if temporary:
                worker = self.worker(user, "business-cold-read" if kind == "active" else kind + "-business-cold-read")
            record = {"owner": owner, "kind": kind, "mode": "cold_same_store" if temporary else "same_retained_objects",
                "worker_after_id": worker.id, "original": original}
            try:
                record.update(read_succeeded=True, restored=worker.command("state", task_id=kind + "-" + owner)["result"])
            except Exception as error:
                record.update(read_succeeded=False, error_type=type(error).__name__)
            self.cold_reads.append(record)
            if temporary:
                self.close_worker(worker)

    def run(self):
        self.preflight()
        (self.root / "fixture").mkdir()
        fixture = self.service("fixtures", "fixtures", self.network("user-001"), "fixtures",
            [(self.root / "fixture", "/fixture", False)],
            ["-m", "fixtures.service.server", "--db", "/fixture/ledger.sqlite"], other_network=True)
        self.wait_port(fixture, 8080)
        self.bench("prepare")
        users = read(self.root / "protected/users.json")["users"]
        for user in users:
            worker = self.worker(user, "prepare")
            result = worker.command("prepare_history")["result"]
            closed = self.close_worker(worker)
            exclusive(self.root / "protected" / ("prepared-" + user["owner"] + ".json"), {
                "preparation_result": result, "healthy_closed": closed["healthy_closed"],
                "after_exit": closed["after_exit"], "kind": "application_prepared_not_agent_output"})
        self.phase_to("observer-starting")
        (self.root / "protected/observer").mkdir(exist_ok=True)
        (self.root / "protected/observer/replies").mkdir()
        observer = self.service("observer", "observer", self.network("user-001"), "cost-observer",
            [(self.root, "/trial", True), (self.root / "protected/observer", "/trial/protected/observer", False)],
            ["-m", "runner.instance_cost_full", "observe", "--root", "/trial"], image=self.plan["bench_image_id"])
        self.sampler = threading.Thread(target=self.sample_loop, daemon=True)
        self.sampler.start()
        active_users = [user for user in users if user["owner"] in self.plan["active_users"]]
        if self.plan["baseline_only"]:
            self.wave("baseline", active_users[:1])
        else:
            self.phase_to("starting-resident-history")
            if self.plan["strategy"] == "resident":
                for user in users:
                    self.workers[user["owner"]] = self.worker(user, "resident-history", online=user["owner"] in self.plan["active_users"])
            self.idle("initial-idle")
            self.wave("active", active_users)
            self.idle("recycled-idle")
            self.business_readback(active_users)
            self.wave("wake", active_users)
            rotated = [user for user in users if user["owner"] in rotation_users(self.plan)]
            if rotated:
                self.wave(INACTIVE_WAKE_KIND, rotated)
                self.business_readback(rotated, kind=INACTIVE_WAKE_KIND)
            self.phase_to("independent-fault-preparation")
            for worker in list(self.workers.values()):
                self.close_worker(worker)
            self.workers.clear()
            self.wave("fault", active_users)
        self.phase_to("finishing")
        for worker in list(self.workers.values()):
            self.close_worker(worker)
        self.workers.clear()
        self.stop_sampling.set()
        self.sampler.join(timeout=30)
        if self.sampler.is_alive():
            raise RuntimeError('资源采样器未能停止；不能启动 bench')
        if rotation_users(self.plan):
            exclusive(self.root / "protected/rotation-sampler-status.json", self.sampling_status)
            if not self.sampling_status or self.sampling_status.get("completed") is not True:
                raise RuntimeError("host_rotation_sampler_incomplete")
        self.bench("collect")
        if self.lifecycle is not None:
            self.lifecycle.retire_observer(observer)
        else:
            exclusive(self.root / "protected/observer/stop.json", {"requested": True})
            self.retire(observer)
        self.retire(fixture)
        host_record = {"normal_closes": self.normal_closes, "cold_reads": self.cold_reads,
            "task_timings": self.task_timings, "idle_phases": self.idle_phases, "fault": self.fault}
        if rotation_users(self.plan):
            host_record.update(rotation_start=self.rotation_start, rotation_history_reads=self.rotation_history_reads,
                sampling_status=self.sampling_status, lifecycle_registry=self.lifecycle.save_registry())
        exclusive(self.root / "protected/flow-host.json", host_record)
        exclusive(self.root / "protected/final-containers.json", self.observations())
        self.bench("grade", check=False)
        return read(self.root / "protected" / ("baseline-verdict.json" if self.plan["baseline_only"] else "flow-verdict.json"))

    def cleanup(self):
        self.stop_sampling.set()
        if self.sampler is not None:
            self.sampler.join(timeout=15)
        failures = super().cleanup()
        if self.sampler is not None and self.sampler.is_alive():
            failures.append("resource-sampler-not-stopped")
        for network in reversed(self.networks):
            reply = self.call(["docker", "network", "rm", network], check=False, timeout=15)
            if reply.returncode:
                failures.append(network)
        if self.proxy_secret_dir is not None:
            self.proxy_secret_dir.cleanup()
        return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run", "run-baseline"))
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--pair-id", required=True)
    parser.add_argument("--strategy", required=True, choices=("resident", "on-demand"))
    parser.add_argument("--users", required=True, type=int, choices=(2, 5, 10))
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--bench-image", required=True)
    parser.add_argument("--services-image", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--seed", required=True, type=int, choices=(101, 202, 303))
    parser.add_argument("--rotate-inactive-users", action="store_true",
        help='独立的 5/10 用户变体：原用户对唤醒后，再冷唤醒固定用户 003/004')
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    plan = make_flow_plan(trial_id=args.trial_id, pair_id=args.pair_id, strategy=args.strategy, users=args.users,
        candidate_image=args.candidate_image, bench_image=args.bench_image, services_image=args.services_image,
        model_name=args.model_name, seed=args.seed, baseline_only=args.action == "run-baseline",
        rotate_inactive_users=args.rotate_inactive_users)
    if args.action == "plan":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan["full_fixed_allocation_allowed"] else 2
    root = args.output_root
    validate_data_root(root)
    if (root is None or not root.is_absolute() or root.exists() or any(part.lower() in {"old", ".."} for part in root.parts)
            or any(item.is_symlink() for item in root.parents) or "," in str(root)):
        raise ValueError("host_output_root_invalid")
    (root / "protected").mkdir(parents=True, exist_ok=False)
    exclusive(root / "protected/plan.json", plan)
    if not plan["full_fixed_allocation_allowed"]:
        exclusive(root / "protected/refusal.json", {"reason": plan["refusal_reason"], "Docker_started": False,
            "observed_RSS": None, "i25_complete": False})
        return 2
    lab = CostFlowLab(root, plan)
    try:
        result = lab.run()
        print(json.dumps({"cost_subset_passed": result["cost_subset_passed"], "i25_complete": False, "evidence": str(root)}))
        return 0 if result["cost_subset_passed"] else 1
    except Exception as error:
        exclusive(root / "protected/flow-host-error.json", {"error_type": type(error).__name__,
            "reason_code": host_failure_reason(error),
            "time": now(), "i25_complete": False, "original_evidence_retained": True})
        raise
    finally:
        failures = lab.cleanup()
        exclusive(root / "protected/flow-cleanup.json", {"unconfirmed_resources": failures})
        if failures:
            print('清理时未确认：' + ", ".join(failures), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
