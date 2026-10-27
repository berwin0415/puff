"""仅用于 I25 轮换变体的受限生命周期控制；所有 Agent 工作都留在 Docker 内。

导入本模块不会执行 Docker 或 SDK 操作。这是显式的新变体入口，不是通用调度器，也不替代旧空闲流程。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from uuid import uuid4

from runner.instance_cost import CAPS, CPU, MEMORY, canonical, exclusive, read, sha
from runner.instance_cost_lifecycle import (SCHEMA, VARIANT, exact_absence, require, same_identity,
    finite, resource_complete, terminal_ok, validate_current, validate_sample)
from scripts.run_instance_cost import NativeWorker, ROOT, now


class RotationLifecycle:
    def __init__(self, lab):
        self.lab = lab
        self.registry_lock = threading.RLock()
        self.inventory_lock = threading.RLock()
        self.control_lock = threading.RLock()
        self.failure_lock = threading.Lock()
        self.first_sampling_failure, self.last_sample, self.inflight_before = None, None, None
        self.registrations, self.starts, self.requests, self.terminals, self.deletions = {}, {}, {}, {}, {}

    def _latch_failure(self, reason, sequence):
        # 调用方只持有 failure_lock。保留首个固定原因和序号；
        # 采样器始终写入原始观察，不以重试替代。
        if self.first_sampling_failure is None:
            self.first_sampling_failure = {"reason_code": reason, "sample_sequence": sequence,
                "observed_monotonic": time.monotonic()}
            self.lab.sampling_status["first_failure"] = deepcopy(self.first_sampling_failure)
            exclusive(self.lab.root / "protected/rotation-sampling-failure.json", self.first_sampling_failure)

    def sample_started(self, before):
        with self.failure_lock:
            self.inflight_before = before

    def sample_written(self, value):
        # 原始样本刷盘后调用。
        with self.failure_lock:
            sequence, reason = value["sample_sequence"], None
            moments = [value.get(key) for key in ("host_before_observer_monotonic", "host_started_monotonic",
                "host_finished_monotonic", "host_after_observer_monotonic")]
            if "error" in value:
                reason = "rotation_sampling_error"
            elif not all(finite(moment) for moment in moments) or moments != sorted(moments):
                reason = "rotation_sampling_clock_invalid"
            else:
                before, _, _, after = moments
                if after - before > 5:
                    reason = "rotation_sampling_span_exceeded"
                elif self.last_sample is None and not 0 <= before - self.lab.sampling_status["started_monotonic"] <= 5:
                    reason = "rotation_sampling_first_edge_exceeded"
                elif self.last_sample is not None and (not self.last_sample[1] <= before
                        or not 0 <= before - self.last_sample[0] <= 5):
                    reason = "rotation_sampling_start_gap_exceeded"
                self.last_sample = before, after
            self.inflight_before = None
            if reason is not None:
                self._latch_failure(reason, sequence)

    def assert_sampling_ok(self):
        with self.failure_lock:
            status = self.lab.sampling_status
            if status is not None and self.first_sampling_failure is None:
                current, sequence = time.monotonic(), status["samples_written"] + 1
                if self.inflight_before is not None:
                    if current - self.inflight_before > 5:
                        self._latch_failure("rotation_sampling_span_exceeded", sequence)
                    elif self.last_sample is not None and self.inflight_before - self.last_sample[0] > 5:
                        self._latch_failure("rotation_sampling_start_gap_exceeded", sequence)
                    elif self.last_sample is None and self.inflight_before - status["started_monotonic"] > 5:
                        self._latch_failure("rotation_sampling_first_edge_exceeded", sequence)
                elif not self.lab.stop_sampling.is_set():
                    previous = self.last_sample[0] if self.last_sample is not None else status["started_monotonic"]
                    if current - previous > 5:
                        self._latch_failure("rotation_sampling_start_gap_exceeded" if self.last_sample else
                            "rotation_sampling_first_edge_exceeded", sequence)
            require(self.first_sampling_failure is None, "host_rotation_sampling_integrity_failed")

    def snapshot_registry(self):
        with self.registry_lock:
            return deepcopy({"schema": SCHEMA, "sampling_variant": VARIANT, "trial_id": self.lab.plan["trial_id"],
                "registrations": self.registrations, "starts": self.starts, "requests": self.requests,
                "terminals": self.terminals, "deletions": self.deletions})

    def save_registry(self):
        path = self.lab.root / "protected/rotation-lifecycle-registry.json"
        exclusive(path, self.snapshot_registry())
        return {"path": "protected/rotation-lifecycle-registry.json", "sha256": sha(path.read_bytes())}

    def inspect(self, target):
        reply = self.lab.call(["docker", "inspect", target], check=False, timeout=10)
        proof = {"target": target, "return_code": reply.returncode,
            "stdout": "[]" if reply.stdout.strip() == "[]" else None,
            "stderr": reply.stderr, "observed_monotonic": time.monotonic()}
        if reply.returncode:
            require(exact_absence(proof, target), "lifecycle_inspect_error_not_exact_absence")
            return None, proof
        require(not reply.stderr.strip(), "lifecycle_inspect_success_with_stderr")
        values = json.loads(reply.stdout)
        require(len(values) == 1, "lifecycle_inspect_ambiguous")
        raw = values[0]
        require(raw["Config"].get("Labels", {}).get("org.harness-lab.instance-cost-trial") == self.lab.plan["trial_id"],
            "lifecycle_inspect_foreign_trial")
        # 与 IdleDockerLab.observations 的安全字段投影一致；不保留 Env 值。
        value = {"id": raw["Id"], "name": raw["Name"], "image_id": raw["Image"], "state": raw["State"],
            "role": raw["Config"].get("Labels", {}).get("org.harness-lab.role"),
            "memory_limit": raw["HostConfig"]["Memory"], "cpu_nano": raw["HostConfig"]["NanoCpus"],
            "network_mode": raw["HostConfig"]["NetworkMode"],
            "host_config_init": raw["HostConfig"].get("Init"),
            "environment_names": sorted(entry.split("=", 1)[0] for entry in raw["Config"].get("Env", [])),
            "mounts": [{key: mount[key] for key in ("Source", "Destination", "RW")} for mount in raw["Mounts"]]}
        require(target in (value["id"], value["name"], value["name"].removeprefix("/")), "lifecycle_inspect_wrong_identity")
        return value, proof

    def reserve(self, role):
        rows = self.lab.observations()
        # 已创建但未就绪的分配也占用资源预留。已证明退出的
        # 容器在 rm 前仍保留登记，但不再占用资源分配。
        pending = [row for row in rows if row["state"].get("Status") != "exited"]
        memory, cpu = self.lab.caps(role)
        require(sum(row["memory_limit"] for row in pending) + memory <= MEMORY
            and sum(row["cpu_nano"] for row in pending) + cpu <= CPU
            and (role != "worker" or sum(row["role"] == "worker" for row in pending) < 2),
            "lifecycle_allocation_refused")

    def create(self, argv, role, image, *, owner=None):
        # 仅确切创建和登记共用 ps → inspect 锁；锁内不执行
        # 进程启动、导入、就绪等待、网络连接或停止等待。
        with self.inventory_lock:
            self.reserve(role)
            identity = self.lab.call(argv, timeout=30).stdout.strip()
            require(re.fullmatch('[0-9a-f]{64}', identity) is not None, "lifecycle_create_id_invalid")
            self.lab.pin(identity, role, image)
            created, _ = self.inspect(identity)
            require(created is not None and created["state"].get("Status") == "created"
                and created["state"].get("Running") is False, "lifecycle_registration_not_created")
            require(role == "worker" or created.get("host_config_init") is True,
                "lifecycle_service_init_not_enabled")
            registration = {"trial_id": self.lab.plan["trial_id"], "owner": owner,
                "registered_monotonic": time.monotonic(), "created": created}
            with self.registry_lock:
                require(identity not in self.registrations, "lifecycle_duplicate_registration")
                self.registrations[identity] = registration
            return identity

    def starting(self, identity):
        with self.registry_lock:
            require(identity in self.registrations and identity not in self.starts, "lifecycle_start_not_registered")
            self.starts[identity] = {"id": identity, "requested_monotonic": time.monotonic()}

    def request(self, identity, kind, **facts):
        before, _ = self.inspect(identity)
        require(before is not None and before["state"].get("Running") is True,
            "lifecycle_request_not_live")
        with self.registry_lock:
            require(identity in self.registrations and identity not in self.requests
                and same_identity(before, self.registrations[identity]["created"]), "lifecycle_request_identity_invalid")
            validate_current(before, self.registrations[identity], None, self.starts.get(identity))
            value = {"id": identity, "kind": kind, "before": before,
                "requested_monotonic": time.monotonic(), **facts}
            self.requests[identity] = deepcopy(value)
        return value

    def terminal(self, identity, observation, **facts):
        with self.registry_lock:
            require(identity not in self.terminals and identity in self.requests
                and same_identity(observation, self.registrations[identity]["created"])
                and terminal_ok(observation, self.requests[identity]), "lifecycle_terminal_not_expected")
            self.terminals[identity] = {"observation": observation,
                "observed_monotonic": time.monotonic(), **facts}
            self.lab.registered[identity]["running"] = False

    def remove(self, identity):
        with self.inventory_lock:
            with self.registry_lock:
                terminal = deepcopy(self.terminals.get(identity))
                registration = deepcopy(self.registrations.get(identity))
            before, _ = self.inspect(identity)
            require(terminal is not None and registration is not None and before is not None
                and same_identity(before, terminal["observation"])
                and canonical(before["state"]) == canonical(terminal["observation"]["state"]),
                "lifecycle_remove_without_exact_terminal")
            requested = time.monotonic()
            removed = self.lab.call(["docker", "rm", identity], timeout=15)
            require(removed.stdout.strip() == identity, "lifecycle_rm_wrong_id")
            absent, id_proof = self.inspect(identity)
            by_name, name_proof = self.inspect(before["name"].removeprefix("/"))
            require(absent is None and by_name is None, "lifecycle_removal_absence_missing")
            with self.registry_lock:
                self.deletions[identity] = {"id": identity, "before_rm": before, "requested_monotonic": requested,
                    "rm_return_code": removed.returncode, "rm_stdout": removed.stdout.strip(),
                    "id_absence": id_proof, "name_absence": name_proof}
                self.lab.registered.pop(identity)

    def service(self, role, suffix, network, alias, mounts, command, *, image=None, env_file=None, other_network=False):
        image = image or self.lab.plan["services_image_id"]
        memory, cpu = self.lab.caps(role)
        name = "hl-ic-" + self.lab.plan["trial_id"] + "-" + suffix
        self.lab.owned.append(name)
        argv = ["docker", "create", "--name", name, "--platform", "linux/arm64", *self.lab.labels(role),
            "--network", network, "--network-alias", alias, "--init", "--read-only", "--memory", str(memory),
            "--cpus", str(cpu / 1_000_000_000), "--pids-limit", "128", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:size=128m", "-e", "HARNESS_LAB_ENVIRONMENT=container"]
        for source, destination, readonly in mounts:
            argv += self.lab.mount(source, destination, readonly)
        if env_file:
            argv += ["--env-file", str(env_file)]
        argv += ["--entrypoint", "python", image, *command]
        identity = self.create(argv, role, image)
        created = self.registrations[identity]["created"]
        require(created["network_mode"] == network
            and len(created["mounts"]) == len(mounts)
            and {item["Destination"]: (item["Source"], item["RW"]) for item in created["mounts"]}
                == {destination: (str(source), not readonly) for source, destination, readonly in mounts},
            "lifecycle_service_fixed_mount_or_network_invalid")
        if other_network:
            from runner.instance_cost import rotation_users
            for owner in [*self.lab.plan["active_users"][1:], *rotation_users(self.lab.plan)]:
                self.lab.call(["docker", "network", "connect", "--alias", alias, self.lab.network(owner), identity])
        self.starting(identity)
        self.lab.call(["docker", "start", identity], timeout=30)
        with self.registry_lock:
            self.lab.registered[identity]["running"] = True
        return identity

    def retire(self, identity):
        self.request(identity, "service_stop")
        self.lab.call(["docker", "stop", "--time", "10", identity], timeout=15)
        stopped, _ = self.inspect(identity)
        self.terminal(identity, stopped)
        self.lab.sequence += 1
        exclusive(self.lab.root / "protected" / ("retired-" + str(self.lab.sequence) + ".json"), stopped)
        self.remove(identity)

    def retire_observer(self, identity):
        # 在停止文件可能使对象退出前，先登记活跃对象。
        stop = {"requested": True}
        self.request(identity, "service_stop", stop_control_sha256=sha(canonical(stop) + b"\n"))
        exclusive(self.lab.root / "protected/observer/stop.json", stop)
        result = self.lab.call(["docker", "wait", identity], timeout=15)
        require(result.stdout.strip() == "0", "lifecycle_observer_natural_exit_invalid")
        stopped, _ = self.inspect(identity)
        self.terminal(identity, stopped)
        self.remove(identity)

    def worker(self, user, stage, *, online=False):
        network = self.lab.network(user["owner"]) if online else "none"
        return RotationNativeWorker(self, user, stage, network=network)

    def close_worker(self, worker):
        closed = worker.close()
        self.lab.normal_closes.append(closed)
        return closed

    def kill(self, worker):
        trigger = self.lab.root / "protected/observer/kill-ready.json"
        self.request(worker.id, "injected_fault", owner=worker.user["owner"], trigger_sha256=sha(trigger.read_bytes()))
        result = self.lab.call(["docker", "kill", "--signal", "KILL", worker.id], timeout=15)
        after, _ = self.inspect(worker.id)
        self.terminal(worker.id, after, kill_return_code=result.returncode)
        return after

    def remove_fault_worker(self, worker):
        worker.process.wait(timeout=20)
        worker.reader.join(timeout=5)
        require(not worker.reader.is_alive(), "lifecycle_fault_reader_not_closed")
        self.remove(worker.id)
        worker.closed = True

    def phase_to(self, phase, kind=None):
        with self.control_lock:
            self.lab.publish_control(phase, kind)
            self.lab.phase, self.lab.kind = phase, kind

    def probe(self, *, phase=None, kind=None):
        with self.control_lock:
            if phase is None:
                phase, kind = self.lab.phase, self.lab.kind
            nonce, before = uuid4().hex, time.monotonic()
            control = self.lab.publish_control(phase, kind, nonce=nonce)
        # 等待期间不持有清单、登记或控制锁。
        path = self.lab.root / "protected/observer/replies" / (nonce + ".json")
        while not path.exists():
            if time.monotonic() > before + 8:
                raise TimeoutError("observer_probe_timeout")
            time.sleep(.05)
        value = read(path)
        require(isinstance(value, dict) and value.get("probe_nonce") == nonce
            and type(value.get("control_sequence")) is int and value["control_sequence"] == control["sequence"]
            and value.get("phase") == control["phase"] and "kind" in value and value["kind"] == control["kind"],
            "observer_probe_binding_changed")
        return value

    def sample_flow(self):
        with self.control_lock:
            phase, kind = self.lab.phase, self.lab.kind
        host_before = time.monotonic()
        self.sample_started(host_before)
        before_probe = self.probe(phase=phase, kind=kind)
        started = time.monotonic()
        with self.inventory_lock:
            before = self.lab.observations()
            with self.registry_lock:
                require(set(self.lab.registered) == {row["id"] for row in before}, "lifecycle_inventory_pin_set_changed")
                planned = [{**deepcopy(self.lab.registered[row["id"]]), "running": row["state"]["Running"]} for row in before]
            before_inventory_at = time.monotonic()
        running = [row for row in before if row["state"]["Running"] is True]

        def observe(row):
            replies = {}
            for kind_, argv in (("stats", ["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}", row["id"]]),
                    ("top", ["docker", "top", row["id"], "-eo", "pid,ppid,args"])):
                entered = time.monotonic()
                result = self.lab.call(argv, timeout=5, check=False)
                replies[kind_] = {"target": row["id"], "argv": argv, "started_monotonic": entered,
                    "finished_monotonic": time.monotonic(), "return_code": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr}
            return replies

        with ThreadPoolExecutor(max_workers=max(1, len(running))) as pool:
            future = {row["id"]: pool.submit(observe, row) for row in running}
            replies = {identity: task.result() for identity, task in future.items()}
        with self.inventory_lock:
            after = self.lab.observations()
            registry = self.snapshot_registry()
            after_inventory_at = time.monotonic()
        after_by_id = {row["id"]: row for row in after}
        stats, processes, transitions = [], [], []
        for row in running:
            identity = row["id"]
            reply = replies[identity]
            stat = [json.loads(line) for line in reply["stats"]["stdout"].splitlines()] if reply["stats"]["return_code"] == 0 else []
            good = (reply["stats"]["return_code"] == reply["top"]["return_code"] == 0
                and not reply["stats"]["stderr"].strip() and not reply["top"]["stderr"].strip()
                and len(stat) == 1 and stat[0].get("Container") == identity
                and len(reply["top"]["stdout"].splitlines()) >= 2)
            if good:
                stats.extend(stat)
                processes.append({"container_id": identity, "raw_docker_top": reply["top"]["stdout"],
                    "process_count": len(reply["top"]["stdout"].splitlines()) - 1,
                    "kind": "actual_process_listing_not_Docker_mixed_PIDs_metric"})
                continue
            request = registry["requests"].get(identity)
            require(request is not None, "lifecycle_missing_stats_without_request")
            terminal = after_by_id.get(identity) or registry["terminals"].get(identity, {}).get("observation")
            transitions.append({"id": identity, "registration_sha256": sha(canonical(registry["registrations"][identity])),
                "request_sha256": sha(canonical(request)), "terminal_observation": terminal, "commands": reply,
                "native_task_health_claimed": False})
        finished = time.monotonic()
        after_probe = self.probe(phase=phase, kind=kind)
        value = {"time": now(), "phase": phase, "sampling_variant": VARIANT,
            "host_before_observer_monotonic": host_before, "host_started_monotonic": started,
            "host_finished_monotonic": finished, "host_after_observer_monotonic": time.monotonic(),
            "containers": stats, "process_observations": processes, "planned_inventory": planned,
            "docker_observations": before, "docker_after_observations": after,
            "before_inventory_monotonic": before_inventory_at, "after_inventory_monotonic": after_inventory_at,
            "observer_before": before_probe, "observer_after": after_probe,
            "resource_commands": replies, "lifecycle_transitions": transitions}
        value["resource_measurement_complete"] = resource_complete(value)
        validate_sample(value, registry, self.lab.plan)
        return value


class RotationNativeWorker(NativeWorker):
    """沿用固定的 DA resident 命令，并限制生命周期边界。"""
    def __init__(self, lifecycle, user, stage, *, network="none"):
        lab = lifecycle.lab
        self.lab, self.lifecycle, self.user = lab, lifecycle, user
        self.closed, self.events = False, queue.Queue()
        self.name = "hl-ic-" + lab.plan["trial_id"] + "-" + stage + "-" + user["owner"]
        lab.owned.append(self.name)
        user_root = lab.root / "users" / user["owner"]
        command = ["docker", "create", "--name", self.name, "--platform", "linux/arm64", *lab.labels("worker"),
            "--network", network, "--read-only", "--memory", str(CAPS["worker"][0]), "--cpus", "0.75", "--pids-limit", "256",
            "--tmpfs", "/tmp:size=128m", "-i"]
        mounts = {}
        for source, destination, readonly in (("config", "/config", True), ("state", "/state", False),
                ("workspace", "/workspace", False), ("evidence", "/evidence", False)):
            command += lab.mount(user_root / source, destination, readonly)
            mounts[destination] = (str(user_root / source), not readonly)
        command += ["--entrypoint", "/opt/venv/bin/python", lab.plan["candidate_image_id"],
            "/opt/materials/harness-lab/resident_runtime.py", "--binding", "/config/resident.json"]
        self.id = lifecycle.create(command, "worker", lab.plan["candidate_image_id"], owner=user["owner"])
        created = lifecycle.registrations[self.id]["created"]
        require(created["network_mode"] == network
            and {item["Destination"]: (item["Source"], item["RW"]) for item in created["mounts"]} == mounts
            and len(created["mounts"]) == len(mounts)
            and not {"MODEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"}.intersection(created["environment_names"]),
            "lifecycle_worker_fixed_mount_or_secret_invalid")
        exclusive(lab.root / "protected" / (self.name + "-created.json"), created)
        self.console = (lab.root / "protected" / (self.name + "-stdout.jsonl")).open("x")
        self.stderr = (lab.root / "protected" / (self.name + "-stderr.log")).open("x")
        lifecycle.starting(self.id)
        self.process = subprocess.Popen(["docker", "start", "--attach", "--interactive", self.id], cwd=ROOT,
            text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, bufsize=1)

        def consume():
            try:
                for line in self.process.stdout:
                    self.console.write(line)
                    self.console.flush()
                    self.events.put(line)
            finally:
                self.events.put(None)
        self.reader = threading.Thread(target=consume, daemon=True)
        self.reader.start()
        lab.attached_workers.append(self)
        # 真实原生导入和就绪检查均在所有采样锁之外执行。
        ready = self.receive()
        require(ready.get("phase") == "ready" and ready.get("owner") == user["owner"], "lifecycle_worker_ready_invalid")
        live, _ = lifecycle.inspect(self.id)
        require(live is not None and live["state"].get("Running") is True, "lifecycle_worker_not_live_after_ready")
        with lifecycle.registry_lock:
            lab.registered[self.id]["running"] = True

    def close(self):
        require(not self.closed, "lifecycle_worker_already_closed")
        command_id = uuid4().hex
        self.lifecycle.request(self.id, "native_close", owner=self.user["owner"], command_id=command_id)
        self.process.stdin.write(json.dumps({"command_id": command_id, "action": "close"}) + "\n")
        self.process.stdin.flush()
        acknowledgement = self.receive(timeout=60)
        require(acknowledgement.get("command_id") == command_id and acknowledgement.get("status") == "closed",
            "lifecycle_native_close_ack_invalid")
        self.process.stdin.close()
        exit_code = self.process.wait(timeout=60)
        self.reader.join(timeout=5)
        require(not self.reader.is_alive(), "lifecycle_close_reader_incomplete")
        self.console.close()
        self.stderr.close()
        after, _ = self.lifecycle.inspect(self.id)
        require(exit_code == 0, "lifecycle_native_close_attach_exit_invalid")
        self.lifecycle.terminal(self.id, after, acknowledgement=acknowledgement, attach_exit=exit_code,
            stdout_source="protected/" + self.name + "-stdout.jsonl")
        result = {"id": self.id, "owner": self.user["owner"], "healthy_closed": True, "after_exit": after}
        exclusive(self.lab.root / "protected" / (self.name + "-closed.json"), result)
        self.lifecycle.remove(self.id)
        self.closed = True
        return result
