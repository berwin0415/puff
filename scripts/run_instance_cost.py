#!/usr/bin/env python3
"""Host Docker driver for the explicitly limited I25 initial-idle subtrial.

No Agent SDK/model is executed on the host. No images are built or downloaded.
Every native runtime runs from an exact prebuilt image, with network=none and
only its own data/config mounts. Active-task/wake/fault phases are NOT supplied.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.instance_cost import CAPS, CPU, MEMORY, exclusive, make_plan, read


def now():
    return datetime.now(timezone.utc).isoformat()


class IdleDockerLab:
    def __init__(self, root, plan):
        self.root, self.plan = Path(root), plan
        self.owned = []
        self.attached_workers = []
        self.label = "org.harness-lab.instance-cost-trial=" + plan["trial_id"]

    def call(self, argv, *, timeout=15, check=True):
        return subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, check=check, timeout=timeout)

    def labels(self, role):
        return ["--label", "com.docker.compose.project=harness-lab", "--label", self.label,
            "--label", "org.harness-lab.role=" + role]

    def observations(self, *, allow_foreign=False):
        ids = self.call(["docker", "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=harness-lab"]).stdout.split()
        if not ids:
            return []
        data = json.loads(self.call(["docker", "inspect", *ids]).stdout)
        if not allow_foreign and any(item["Config"].get("Labels", {}).get("org.harness-lab.instance-cost-trial") != self.plan["trial_id"] for item in data):
            raise RuntimeError("Another harness-lab trial exists; this driver cannot touch or exclude it")
        return [{"id": item["Id"], "name": item["Name"], "image_id": item["Image"], "state": item["State"],
            "role": item["Config"].get("Labels", {}).get("org.harness-lab.role"),
            "memory_limit": item["HostConfig"]["Memory"], "cpu_nano": item["HostConfig"]["NanoCpus"],
            "network_mode": item["HostConfig"]["NetworkMode"],
            "environment_names": sorted(entry.split("=", 1)[0] for entry in item["Config"].get("Env", [])),
            "mounts": [{key: mount[key] for key in ("Source", "Destination", "RW")} for mount in item["Mounts"]]} for item in data]

    def preflight(self):
        if self.observations(allow_foreign=True):
            raise RuntimeError("Existing harness-lab containers must settle before this subtrial")
        for key in ("candidate_image_id", "bench_image_id"):
            actual = self.call(["docker", "image", "inspect", "--format", "{{.Id}}", self.plan[key]]).stdout.strip()
            if actual != self.plan[key]:
                raise RuntimeError("Pinned local image identity differs")

    def bench(self, action):
        name = "hl-ic-" + self.plan["trial_id"] + "-" + action
        self.owned.append(name)
        command = ["docker", "run", "--name", name, "--platform", "linux/arm64", *self.labels("acceptance"),
            "--network", "none", "--read-only", "--memory", str(CAPS["bench"][0]), "--cpus", "0.5", "--pids-limit", "128",
            "--tmpfs", "/tmp:size=128m", "-e", "HARNESS_LAB_ENVIRONMENT=container",
            "--mount", "type=bind,src=" + str(self.root) + ",dst=/trial", "--entrypoint", "python",
            self.plan["bench_image_id"], "-m", "runner.instance_cost", action, "--root", "/trial"]
        result = self.call(command, timeout=120, check=False)
        (self.root / "protected" / (action + ".log")).write_text(result.stdout + result.stderr)
        observed = self.observations()
        selected = [item for item in observed if item["name"] == "/" + name]
        exclusive(self.root / "protected" / (action + "-container.json"), selected)
        if len(selected) != 1 or not self.healthy_exit(selected[0]) or result.returncode != 0:
            raise RuntimeError("Docker bench action failed; original evidence is retained")
        self.call(["docker", "rm", selected[0]["id"]])

    @staticmethod
    def healthy_exit(observed):
        state = observed["state"]
        return (state.get("Status") == "exited" and state.get("ExitCode") == 0 and state.get("Pid") == 0
            and state.get("OOMKilled") is False and state.get("Dead") is False and state.get("Error") == "")

    def worker(self, user, stage):
        return NativeWorker(self, user, stage)

    def sample(self, expected_ids):
        started = time.monotonic()
        observations = self.observations()
        if {row["id"] for row in observations} != set(expected_ids):
            raise RuntimeError("Complete owned-container inventory changed during idle")
        running = [row["id"] for row in observations if row["state"]["Running"]]
        stats, processes = [], []
        if running:
            output = self.call(["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}", *running], timeout=10).stdout
            stats = [json.loads(line) for line in output.splitlines()]
            for identifier in running:
                top = self.call(["docker", "top", identifier, "-eo", "pid,ppid,args"], timeout=5).stdout
                rows = top.splitlines()
                if len(rows) < 2:
                    raise RuntimeError("A real resident process was not observed")
                processes.append({"container_id": identifier, "raw_docker_top": top, "process_count": len(rows) - 1,
                    "kind": "actual_process_listing_not_Docker_mixed_PIDs_metric"})
        return {"time": now(), "host_started_monotonic": started, "host_finished_monotonic": time.monotonic(),
            "containers": stats, "docker_observations": observations, "process_observations": processes}

    def cleanup(self):
        failures = []
        for name in reversed(self.owned):
            try:
                result = self.call(["docker", "inspect", name], check=False)
                if result.returncode:
                    absent = (re.fullmatch(r"(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*"
                        + re.escape(name), result.stderr.strip(), flags=re.IGNORECASE)
                        and result.stdout.strip() == "[]")
                    if not absent:
                        failures.append(name)
                    continue
                data = json.loads(result.stdout)
                if (not isinstance(data, list) or len(data) != 1 or data[0].get("Name") != "/" + name
                        or not re.fullmatch(r"[0-9a-f]{64}", data[0].get("Id", ""))
                        or data[0]["Config"].get("Labels", {}).get("org.harness-lab.instance-cost-trial") != self.plan["trial_id"]):
                    failures.append(name)
                    continue
                removed = self.call(["docker", "rm", "-f", data[0]["Id"]], check=False)
                if removed.returncode:
                    failures.append(name)
            except Exception:
                failures.append(name)
        for worker in self.attached_workers:
            try:
                worker.process.wait(timeout=5)
                worker.reader.join(timeout=5)
                if worker.reader.is_alive():
                    failures.append(worker.name)
                    continue
                for stream in (worker.console, worker.stderr, worker.process.stdout, worker.process.stdin):
                    if stream is not None and not stream.closed:
                        stream.close()
            except Exception:
                failures.append(worker.name)
        return failures

    def run(self):
        self.preflight()
        self.bench("prepare-idle-materials")
        users = read(self.root / "protected/users.json")["users"]
        prep_started = time.monotonic()
        for user in users:
            worker = self.worker(user, "prepare")
            result = worker.command("prepare_history")["result"]
            closed = worker.close()
            exclusive(self.root / "protected" / ("prepared-" + user["owner"] + ".json"), {
                "preparation_result": result, "healthy_closed": closed["healthy_closed"],
                "after_exit": closed["after_exit"], "kind": "application_prepared_history_not_agent_output"})
        exclusive(self.root / "protected/preparation-cost.json", {"elapsed_seconds": time.monotonic() - prep_started,
            "users": len(users), "worker_starts": len(users), "model_requests_not_possible_via_network_none": True,
            "prepared_business_tasks": 0, "identity_authentication_tested": False})
        workers, retained = [], {}
        try:
            if self.plan["strategy"] == "resident":
                for user in users:
                    worker = self.worker(user, "idle")
                    workers.append(worker)
                    retained[user["owner"]] = {"before": worker.command("state", task_id=user["history_task_id"])["result"]}
            observed = self.observations()
            inventory = [{"id": row["id"], "memory_limit": CAPS["worker"][0], "cpu_nano": CAPS["worker"][1], "running": True} for row in observed]
            started = time.monotonic()
            next_sample = started
            with (self.root / "protected/resource-samples.jsonl").open("x") as stream:
                while True:
                    try:
                        sample = self.sample([item["id"] for item in inventory])
                    except Exception as error:
                        sample = {"time": now(), "error": type(error).__name__, "host_started_monotonic": time.monotonic()}
                        stream.write(json.dumps(sample) + "\n")
                        stream.flush()
                        raise
                    stream.write(json.dumps(sample) + "\n")
                    stream.flush()
                    if time.monotonic() - started >= self.plan["idle_seconds"]:
                        break
                    next_sample += self.plan["sampling_interval_seconds"]
                    threading.Event().wait(max(0, min(2, next_sample - time.monotonic())))
            finished = time.monotonic()
            exclusive(self.root / "protected/idle-phase.json", {"started_monotonic": started, "finished_monotonic": finished,
                "inventory": inventory, "kind": "initial_idle_only_not_recycle_observation"})
            for worker in workers:
                retained[worker.user["owner"]]["after"] = worker.command("state", task_id=worker.user["history_task_id"])["result"]
        finally:
            closed = [worker.close() for worker in workers]
            exclusive(self.root / "protected/idle-close.json", {"all_healthy_closed": all(item["healthy_closed"] for item in closed), "workers": closed})
        exclusive(self.root / "protected/retained-history.json", retained)
        exclusive(self.root / "protected/final-containers.json", self.observations())
        self.bench("grade-idle")
        return read(self.root / "protected/idle-verdict.json")


class NativeWorker:
    def __init__(self, lab, user, stage, *, network="none"):
        self.lab, self.user = lab, user
        self.closed = False
        self.events = queue.Queue()
        self.name = "hl-ic-" + lab.plan["trial_id"] + "-" + stage + "-" + user["owner"]
        lab.owned.append(self.name)
        user_root = lab.root / "users" / user["owner"]
        command = ["docker", "create", "--name", self.name, "--platform", "linux/arm64", *lab.labels("worker"),
            "--network", network, "--read-only", "--memory", str(CAPS["worker"][0]), "--cpus", "0.75", "--pids-limit", "256",
            "--tmpfs", "/tmp:size=128m", "-i"]
        for source, destination, readonly in (("config", "/config", True), ("state", "/state", False),
                ("workspace", "/workspace", False), ("evidence", "/evidence", False)):
            command += ["--mount", "type=bind,src=" + str(user_root / source) + ",dst=" + destination + (",readonly" if readonly else "")]
        command += ["--entrypoint", "/opt/venv/bin/python", lab.plan["candidate_image_id"],
            "/opt/materials/harness-lab/resident_runtime.py", "--binding", "/config/resident.json"]
        self.id = lab.call(command, timeout=30).stdout.strip()
        created = [item for item in lab.observations() if item["id"] == self.id]
        expected_mounts = {"/config": (str(user_root / "config"), False), "/state": (str(user_root / "state"), True),
            "/workspace": (str(user_root / "workspace"), True), "/evidence": (str(user_root / "evidence"), True)}
        actual_mounts = {item["Destination"]: (item["Source"], item["RW"]) for item in created[0]["mounts"]} if len(created) == 1 else {}
        if (len(created) != 1 or created[0]["state"]["Status"] != "created"
                or created[0]["image_id"] != lab.plan["candidate_image_id"]
                or created[0]["memory_limit"] != CAPS["worker"][0] or created[0]["cpu_nano"] != CAPS["worker"][1]
                or created[0]["network_mode"] != network or actual_mounts != expected_mounts
                or {"MODEL_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"}.intersection(created[0]["environment_names"])):
            raise RuntimeError("Created resident Worker does not match exact planned image/caps/network")
        exclusive(lab.root / "protected" / (self.name + "-created.json"), created[0])
        self.console = (lab.root / "protected" / (self.name + "-stdout.jsonl")).open("x")
        self.stderr = (lab.root / "protected" / (self.name + "-stderr.log")).open("x")
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
        ready = self.receive()
        if ready.get("phase") != "ready" or ready.get("owner") != user["owner"]:
            raise RuntimeError("A real bound resident runtime did not become ready")

    def receive(self, *, timeout=60):
        line = self.events.get(timeout=timeout)
        if line is None:
            raise RuntimeError("Resident entry exited before its native response")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("Resident entry returned a non-object")
        return value

    def command(self, action, *, task_id=None):
        command_id = uuid4().hex
        body = {"command_id": command_id, "action": action}
        if task_id is not None:
            body["task_id"] = task_id
        self.process.stdin.write(json.dumps(body) + "\n")
        self.process.stdin.flush()
        response = self.receive(timeout=320 if action == "run_task" else 60)
        if response.get("command_id") != command_id or response.get("status") == "rejected_or_failed":
            raise RuntimeError("Bound native command failed; no successful result is synthesized")
        return response

    def close(self):
        if self.closed:
            raise RuntimeError("Worker already closed")
        acknowledgement = self.command("close")
        if acknowledgement.get("status") != "closed":
            raise RuntimeError("Native close acknowledgement missing")
        self.process.stdin.close()
        exit_code = self.process.wait(timeout=60)
        self.reader.join(timeout=5)
        self.console.close()
        self.stderr.close()
        after = [item for item in self.lab.observations() if item["id"] == self.id]
        if len(after) != 1:
            raise RuntimeError("Closed Worker identity is missing")
        healthy = exit_code == 0 and self.lab.healthy_exit(after[0])
        result = {"id": self.id, "owner": self.user["owner"], "healthy_closed": healthy, "after_exit": after[0]}
        exclusive(self.lab.root / "protected" / (self.name + "-closed.json"), result)
        if not healthy:
            raise RuntimeError("Resident Worker did not exit normally")
        self.lab.call(["docker", "rm", self.id])
        self.closed = True
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run-idle"))
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--pair-id", required=True)
    parser.add_argument("--strategy", required=True, choices=("resident", "on-demand"))
    parser.add_argument("--users", required=True, type=int, choices=(2, 5, 10))
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--bench-image", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    plan = make_plan(trial_id=args.trial_id, pair_id=args.pair_id, strategy=args.strategy, users=args.users,
        candidate_image=args.candidate_image, bench_image=args.bench_image, model_name=args.model_name)
    if args.action == "plan":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan["full_fixed_allocation_allowed"] else 2
    root = args.output_root
    if (root is None or not root.is_absolute() or any(part in {"old", ".."} for part in root.parts)
            or root.exists() or any(item.is_symlink() for item in root.parents) or "," in str(root)):
        raise ValueError("Use a new absolute current-trial output directory")
    (root / "protected").mkdir(parents=True, exist_ok=False)
    exclusive(root / "protected/plan.json", plan)
    if not plan["full_fixed_allocation_allowed"]:
        exclusive(root / "protected/refusal.json", {"reason": plan["refusal_reason"],
            "docker_or_candidate_started": False, "observed_RSS": None, "i25_complete": False})
        print(json.dumps({"status": "refused_before_Docker_start", "reason": plan["refusal_reason"]}))
        return 2
    lab = IdleDockerLab(root, plan)
    try:
        result = lab.run()
        print(json.dumps({"subset_passed": result["subset_passed"], "i25_complete": False, "evidence": str(root)}))
        return 0 if result["subset_passed"] else 1
    except Exception as error:
        exclusive(root / "protected/host-error.json", {"error_type": type(error).__name__, "time": now(),
            "i25_complete": False, "original_evidence_retained": True})
        raise
    finally:
        failures = lab.cleanup()
        exclusive(root / "protected/cleanup.json", {"unconfirmed_container_names": failures})
        if failures:
            print("Cleanup could not confirm removal of: " + ", ".join(failures), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
