#!/usr/bin/env python3
"""Host-only Docker orchestration for the DA I24 two-Worker experiment.

Images must already contain the reviewed code. There is no build/download,
candidate import, task preparation, model request or grading on this host.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.bench import identifier
from runner.config import load_config


DSH = "deepseek_harness"
DA = "deepagents_langgraph"


def candidate_for(args):
    candidate = getattr(args, "candidate", DA)
    if candidate not in {DA, DSH}:
        raise ValueError("Allowlisted I24 candidate required")
    return candidate


def worker_python(args):
    return "/usr/bin/python3" if candidate_for(args) == DSH else "/opt/venv/bin/python"


def now():
    return datetime.now(timezone.utc).isoformat()


def exclusive(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def container_identity(value):
    return {key: value.get(key) for key in ("id", "name", "image_id", "memory_limit", "cpu_nano", "trial_id", "role", "init_enabled")}


SAMPLING_RUNTIME_CODES = {
    "Sampling transition lacks its exact pre-start registration": "transition_registration_mismatch",
    "Sampling transition was not requested for this exact object": "transition_not_requested",
    "Sampling transition lacks an allowed actual terminal observation": "transition_terminal_not_healthy",
    "Missing container has no previously observed exact removal": "transition_removal_unproven",
    "Unexplained Docker sampling error during container transition": "transition_command_error_unexplained",
    "Unexpected Docker sampling stderr during container transition": "transition_command_stderr_unexpected",
    "Docker inspection failed; absence is unproven": "observation_absence_unproven",
    "Complete Docker inventory lost the requested object": "observation_requested_identity_missing",
    "Exact transitioned container absence was not observed": "transition_absence_unproven",
    "Removing container lacks its exact recorded terminal and absence evidence": "transition_removing_unproven",
}


class SamplingFailure(RuntimeError):
    """Fixed reason and safe Docker observations, never raw inspect or Env."""

    def __init__(self, code, observations=None):
        super().__init__(code)
        self.code, self.observations = code, observations or {}


def container_observation(c):
    # Project inspect before retaining it, including on validation failures.
    # Env values and full inspect output must never enter sampler diagnostics.
    return {"id": c["Id"], "name": c["Name"], "image_id": c["Image"], "image_tag": c["Config"]["Image"],
        "state": c["State"], "memory_limit": c["HostConfig"]["Memory"], "cpu_nano": c["HostConfig"]["NanoCpus"],
        "init_enabled": c["HostConfig"].get("Init") is True,
        "trial_id": c["Config"].get("Labels", {}).get("org.harness-lab.multi-user-trial"),
        "role": c["Config"].get("Labels", {}).get("org.harness-lab.role"),
        "networks": {name: {"network_id": value["NetworkID"], "aliases": value.get("Aliases", [])}
            for name, value in c["NetworkSettings"]["Networks"].items()},
        "environment_names": sorted(value.split("=", 1)[0] for value in c["Config"].get("Env", [])),
        # Docker does not promise a stable Mounts array order across inspect
        # calls. Keep every projected mount, including duplicates, in a stable
        # order before storing the independent observations.
        "mounts": sorted(({key: mount[key] for key in ("Source", "Destination", "RW")} for mount in c["Mounts"]),
            key=lambda mount: (mount["Destination"], mount["Source"], mount["RW"]))}


def stopped_container(value, *, bench=False):
    state = value["state"]
    return (state.get("Status") == "exited" and state.get("Running") is False
        and type(state.get("ExitCode")) is int and state["ExitCode"] in ((0,) if bench else (0, 143))
        and type(state.get("Pid")) is int and state["Pid"] == 0 and state.get("Error") == ""
        and all(state.get(key) is False for key in ("OOMKilled", "Dead", "Paused", "Restarting")))


def recorded_removal_matches(after, terminal):
    """A removing snapshot is retained as a transition, never a healthy exit."""
    if not isinstance(after, dict) or not isinstance(terminal, dict):
        return False
    state = after.get("state", {})
    if state.get("Status") != "removing" or state.get("Dead") is not True:
        return False
    restored = deepcopy(after)
    restored["state"].update({"Status": "exited", "Dead": False})
    def canonical(value):
        value = deepcopy(value)
        if "mounts" in value:
            value["mounts"] = sorted(value["mounts"], key=lambda item: json.dumps(item, sort_keys=True))
        return json.dumps(value, sort_keys=True, allow_nan=False)
    return canonical(restored) == canonical(terminal)


def explained_sampling_transition(before, replies, after, registration, retirement, completion, *,
        removing_observed_monotonic=None, removal_absence=None):
    """Only exact, pre-registered and independently observed terminal objects.

    A wrapper's requested SIGTERM is an infrastructure transition, never native
    child success. Unknown Docker failures and forced exit 137 remain failures.
    No lock or wait is held while Docker observes a transition.
    """
    if registration is None or container_identity(before) != container_identity(registration):
        raise RuntimeError("Sampling transition lacks its exact pre-start registration")
    bench = before["role"] == "acceptance"
    record = completion if bench else retirement
    if not bench and (not record or container_identity(record["before_stop"]) != container_identity(before)):
        raise RuntimeError("Sampling transition was not requested for this exact object")
    removing = after is not None and after.get("state", {}).get("Status") == "removing"
    terminal = (record or {}).get("after_stop") if after is None or removing else after
    if (terminal is None or container_identity(terminal) != container_identity(before)
            or not stopped_container(terminal, bench=bench)):
        raise RuntimeError("Sampling transition lacks an allowed actual terminal observation")
    if after is None and (not record or "removal_requested_monotonic" not in record):
        raise RuntimeError("Missing container has no previously observed exact removal")
    if removing:
        proof, deleted = removal_absence or {}, (record or {}).get("deleted", {})
        moments = [(record or {}).get("removal_requested_monotonic"), removing_observed_monotonic,
            proof.get("observed_monotonic")]
        try:
            deleted_at, absent_at = (datetime.fromisoformat(value["observed_at"]) for value in (deleted, proof))
            ordered_wall_times = deleted_at.utcoffset() is not None and absent_at.utcoffset() is not None and deleted_at <= absent_at
        except (KeyError, TypeError, ValueError):
            ordered_wall_times = False
        exact_absence = isinstance(proof.get("stderr"), str) and re.fullmatch(
            r"(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*"
            + re.escape(before["id"]), proof["stderr"].strip(), flags=re.IGNORECASE)
        if (not recorded_removal_matches(after, terminal)
                or not all(type(value) in (int, float) and math.isfinite(value) for value in moments)
                or not 0 <= moments[0] <= moments[1] <= moments[2]
                or (not bench and (type(record.get("requested_monotonic")) not in (int, float)
                    or not math.isfinite(record["requested_monotonic"])
                    or not 0 <= record["requested_monotonic"] <= moments[0]))
                or deleted.get("id") != before["id"] or deleted.get("id_absent") is not True
                or not ordered_wall_times
                or proof.get("id") != before["id"] or proof.get("id_absent") is not True
                or type(proof.get("returncode")) is not int or proof["returncode"] != 1
                or not isinstance(proof.get("stdout"), str) or proof["stdout"].strip() != "[]" or not exact_absence):
            raise RuntimeError("Removing container lacks its exact recorded terminal and absence evidence")
    names = "(?:" + "|".join(re.escape(name) for name in (before["id"], before["name"], before["name"].lstrip("/"))) + ")"
    known_error = re.compile(r"(?:error(?: response from daemon)?:\s*)?(?:container\s+" + names
        + r"\s+is not running|no such (?:object|container):\s*" + names + r")", re.IGNORECASE)
    top = replies.get("top")
    eof_at_recorded_exit = (record is not None and record.get("after_stop") == terminal
        and top is not None and top.returncode == 1 and not top.stdout.strip()
        and known_error.fullmatch(top.stderr.strip()) is not None)
    for kind, reply in replies.items():
        eof = kind == "stats" and eof_at_recorded_exit and reply.stderr.strip() == "EOF"
        if reply.returncode and (reply.returncode != 1 or reply.stdout.strip()
                or not (known_error.fullmatch(reply.stderr.strip()) or eof)):
            raise RuntimeError("Unexplained Docker sampling error during container transition")
        if not reply.returncode and reply.stderr.strip():
            raise RuntimeError("Unexpected Docker sampling stderr during container transition")
    value = {"kind": "registered_bench_healthy_exit" if bench else "registered_host_requested_wrapper_stop",
        "container_id": before["id"], "registration": registration, "terminal_observation": terminal,
        "retirement": retirement, "completion": completion, "absence_observed": after is None or removing,
        "native_child_health_claimed": False,
        "commands": {kind: {"returncode": reply.returncode, "stdout": reply.stdout, "stderr": reply.stderr}
            for kind, reply in replies.items()}}
    if removing:
        value.update({"removing_observation": after, "removing_observed_monotonic": removing_observed_monotonic,
            "absence_observation": removal_absence})
    return value


def paths_for(config, args, kind=None, letter=None):
    candidate = candidate_for(args)
    experiment = "multi-user-baseline" if getattr(args, "baseline", False) else "multi-user"
    case = "scope-baseline" if getattr(args, "baseline", False) else "scope"
    if kind is None:
        return config.paths_for(experiment, args.trial_id, candidate, "control", "main",
            args.trial_id, case=case, date=args.date)
    variant = f"{args.trial_id}-{kind}-{letter}"
    return config.paths_for(experiment, variant, candidate, "user-" + letter,
        "session-" + letter, variant, case=case, date=args.date)


class MultiUserLab:
    def __init__(self, args):
        self.args, self.config = args, load_config(ROOT / "configs/experiment.yaml")
        self.global_paths = paths_for(self.config, args)
        self.owned, self.networks, self.records, self.sampling_errors, self.overages = [], [], [], [], []
        self.registered = {}
        # Only short in-memory registration updates hold this lock. Docker
        # inspect/start/stop/rm/stats/top never run under it.
        self.registry_lock = threading.RLock()
        self.registrations, self.retirements, self.bench_completions, self.inventory_absences = {}, {}, {}, []
        self.network_registrations = {}
        self.phase, self.stop_sampling, self.sampler = "prepare", threading.Event(), None
        self.env = {**os.environ, "HARNESS_LAB_DATA_ROOT": str(self.config.host_data_root)}
        self.sequence = 0
        self.pins = json.loads(Path(args.pins_file).read_text())
        if candidate_for(args) == DSH and self.pins.get("candidate") != DSH:
            raise ValueError("DSH-specific material pins required")
        self.image = {role: self.pins[key] for role, key in (("worker", "worker_image_id"),
            ("bench", "bench_image_id"), ("services", "services_image_id"))}
        for value in self.image.values():
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ValueError("Prebuilt exact Docker image IDs are required")

    def call(self, argv, *, check=True, timeout=15):
        return subprocess.run(argv, cwd=ROOT, env=self.env, capture_output=True, text=True,
            check=check, timeout=timeout)

    def labels(self, role):
        return ["--label", "com.docker.compose.project=harness-lab", "--label",
            "org.harness-lab.multi-user-trial=" + self.args.trial_id, "--label", "org.harness-lab.role=" + role]

    def inventory(self):
        ids = self.call(["docker", "ps", "-aq", "--no-trunc", "--filter",
            "label=com.docker.compose.project=harness-lab"], timeout=5).stdout.split()
        if not ids:
            return []
        reply = self.call(["docker", "inspect", *ids], timeout=8, check=False)
        diagnostic = {"inventory_listed_ids": ids, "inventory_inspect_returncode": reply.returncode,
            "inventory_inspect_stderr": reply.stderr}
        try:
            raw = json.loads(reply.stdout)
        except (json.JSONDecodeError, TypeError):
            raise SamplingFailure("inventory_inspect_json_invalid", diagnostic) from None
        projected = [container_observation(item) for item in raw]
        diagnostic["inventory_docker_observations"] = projected
        inspected = [item["Id"] for item in raw]
        if len(set(inspected)) != len(inspected) or set(inspected) - set(ids):
            raise SamplingFailure("inventory_identity_set_changed", diagnostic)
        missing = set(ids) - set(inspected)
        if reply.returncode:
            lines = reply.stderr.strip().splitlines()
            pattern = re.compile(r"(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*([0-9a-f]{64})", re.IGNORECASE)
            matched = [pattern.fullmatch(line.strip()) for line in lines]
            if (reply.returncode != 1 or not missing or len(lines) != len(missing) or not all(matched)
                    or {match.group(1) for match in matched} != missing):
                raise SamplingFailure("inventory_inspect_error_unexplained", diagnostic)
            with self.registry_lock:
                registrations, retirements, completions = deepcopy((self.registrations, self.retirements, self.bench_completions))
            for identity in missing:
                registered = registrations.get(identity)
                bench = registered is not None and registered["role"] == "acceptance"
                record = completions.get(identity) if bench else retirements.get(identity)
                terminal = (record or {}).get("after_stop")
                if (registered is None or record is None or "removal_requested_monotonic" not in record
                        or terminal is None or container_identity(terminal) != container_identity(registered)
                        or not stopped_container(terminal, bench=bench)
                        or (not bench and (container_identity(record["before_stop"]) != container_identity(registered)
                            or "requested_monotonic" not in record))):
                    raise SamplingFailure("inventory_registered_removal_unproven", diagnostic)
                value = {"kind": "registered_removal_between_list_and_inspect", "container_id": identity,
                    "listed_ids": ids, "inspected_ids": inspected, "inspect_returncode": reply.returncode,
                    "inspect_stderr": reply.stderr, "registration": registered, "removal": record,
                    "observed_monotonic": time.monotonic(), "observed_at": now(), "native_child_health_claimed": False}
                with self.registry_lock:
                    self.inventory_absences.append(value)
        elif missing or reply.stderr.strip():
            raise SamplingFailure("inventory_output_incomplete", diagnostic)
        return projected

    def observation(self, identity):
        reply = self.call(["docker", "inspect", identity], check=False)
        if reply.returncode:
            if reply.stdout.strip() == "[]" and re.fullmatch(
                r"(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*" + re.escape(identity),
                reply.stderr.strip(), flags=re.IGNORECASE):
                return None
            raise RuntimeError("Docker inspection failed; absence is unproven")
        selected = [row for row in self.inventory() if row["id"] == json.loads(reply.stdout)[0]["Id"]]
        if len(selected) != 1:
            raise RuntimeError("Complete Docker inventory lost the requested object")
        return selected[0]

    def preflight(self):
        self.config.initialize_layout()
        if self.inventory():
            raise RuntimeError("Existing harness-lab containers must be removed by their owning trial first")
        if self.global_paths.fixture.exists() or self.global_paths.run.exists():
            raise FileExistsError("Trial evidence already exists; use another trial ID")
        info = json.loads(self.call(["docker", "info", "--format", "{{json .}}"], timeout=15).stdout)
        if info["OSType"] != "linux" or info["MemTotal"] < 6 * 1024**3 or info["NCPU"] < 3:
            raise RuntimeError("Docker must provide the frozen 6 GiB / 3 CPU total budget")
        for identity in self.image.values():
            actual = self.call(["docker", "image", "inspect", "--format", "{{.Id}}", identity]).stdout.strip()
            if actual != identity:
                raise RuntimeError("Pinned local image identity changed")
        hash_keys = ("source_export_sha256", "adapter_export_sha256") + (("adapter_helper_sha256",) if candidate_for(self.args) == DA else ())
        for key in hash_keys:
            if not re.fullmatch(r"[0-9a-f]{64}", str(self.pins.get(key, ""))):
                raise ValueError("Actual source, adapter and helper hashes are required")
        # Compose alone resolves .env quoting. The expanded secret values never
        # enter a command line, output, run manifest or candidate environment.
        command = ["docker", "compose", "--project-name", "harness-lab", "-f",
            str(ROOT / "compose/docker-compose.yml"), "--profile", "model", "config", "--format", "json"]
        resolved = json.loads(self.call(command).stdout)
        proxy_env = resolved["services"]["model-proxy"]["environment"]
        if proxy_env.get("MODEL_NAME") != self.pins.get("model_name"):
            raise ValueError("Configured remote model differs from frozen model_name")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) or not isinstance(value, str)
            or "\n" in value or "\r" in value for key, value in proxy_env.items()):
            raise ValueError("Resolved proxy environment cannot be transferred exactly")
        self.global_paths.fixture.mkdir(parents=True)
        exclusive(self.global_paths.fixture / "material-pins.json", self.pins)
        self.proxy_env_file = self.global_paths.fixture / "proxy.env"
        descriptor = os.open(self.proxy_env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            for key, value in proxy_env.items():
                stream.write(key + "=" + value + "\n")
        del proxy_env, resolved

    def create_networks(self):
        for letter in ("a", "b"):
            name = "harness-lab-" + self.args.trial_id + "-" + letter
            reply = self.call(["docker", "network", "create", "--label", "org.harness-lab.multi-user-trial=" + self.args.trial_id, name])
            self.networks.append(name)
            identity = reply.stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise RuntimeError("Created network lacks its exact identity")
            self.network_registrations[identity] = {"id": identity, "name": name, "trial_id": self.args.trial_id}

    def network(self, letter):
        return "harness-lab-" + self.args.trial_id + "-" + letter

    @staticmethod
    def mount(source, target, *, readonly=False):
        return ["--mount", "type=bind,src=" + str(source) + ",dst=" + str(target) + (",readonly" if readonly else "")]

    def create(self, role, suffix, network, alias, mounts, command, *, worker=False, env_file=None, second_network=None,
               entrypoint=None, extra_env=None):
        name = "harness-lab-" + self.args.trial_id + "-" + suffix
        self.owned.append(name)
        argv = ["docker", "create", "--init", "--name", name, "--platform", "linux/arm64", *self.labels(role),
            "--network", network, "--network-alias", alias, "--read-only", "--memory", "2g" if worker else "256m",
            "--cpus", "0.75" if worker else "0.25", "--pids-limit", "256" if worker else "96",
            "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:size=128m" if worker else "/tmp:size=32m"]
        for source, target, readonly in mounts:
            argv += self.mount(source, target, readonly=readonly)
        if env_file:
            argv += ["--env-file", str(env_file)]
        for key, value in (extra_env or {}).items():
            argv += ["-e", key + "=" + value]
        argv += ["--entrypoint", entrypoint or (worker_python(self.args) if worker else "python"),
            self.image["worker" if worker else "services"], *command]
        identity = self.call(argv, timeout=30).stdout.strip()
        self.registered[identity] = role
        if second_network:
            self.call(["docker", "network", "connect", "--alias", alias, second_network, identity])
        self.register_container(identity)
        self.call(["docker", "start", identity], timeout=30)
        return identity

    def register_container(self, identity):
        observed = self.observation(identity)
        role = self.registered.get(identity)
        image = self.image["worker" if role == "worker" else "bench" if role == "acceptance" else "services"]
        caps = (2 * 1024**3, 750000000) if role == "worker" else (
            (512 * 1024**2, 500000000) if role == "acceptance" else (256 * 1024**2, 250000000))
        if (observed is None or observed["id"] != identity or observed["trial_id"] != self.args.trial_id
                or not re.fullmatch(r"[0-9a-f]{64}", identity) or observed["role"] != role
                or observed["name"].lstrip("/") not in self.owned or observed["image_id"] != image
                or (observed["memory_limit"], observed["cpu_nano"]) != caps
                or (role != "acceptance" and observed.get("init_enabled") is not True)
                or observed["state"]["Status"] != "created" or observed["state"]["Running"]):
            raise RuntimeError("Exact created container registration is required before start")
        with self.registry_lock:
            if identity in self.registrations:
                raise RuntimeError("Container identity was already registered")
            self.registrations[identity] = observed

    def wait_port(self, identity, port, *, worker=False):
        code = ("import socket,time\n"
            "for attempt in range(100):\n"
            " try:\n"
            f"  connection=socket.create_connection(('127.0.0.1',{port}),timeout=.3); connection.close(); break\n"
            " except OSError: time.sleep(.2)\n"
            "else: raise SystemExit('private_service_not_ready')\n")
        self.call(["docker", "exec", identity, worker_python(self.args) if worker else "python", "-c", code], timeout=30)

    def bench(self, action, *, letter="a", extra=(), check=True):
        running_workers = [row for row in self.inventory() if row["role"] == "worker" and row["state"]["Running"]]
        if running_workers:
            raise RuntimeError("Bench cannot run concurrently with the two Worker wrappers")
        self.sequence += 1
        name = "harness-lab-" + self.args.trial_id + "-bench-" + str(self.sequence)
        self.owned.append(name)
        command = ["docker", "create", "--name", name, "--platform", "linux/arm64", *self.labels("acceptance"),
            "--network", self.network(letter), "--read-only", "--memory", "512m", "--cpus", "0.5",
            "--pids-limit", "128", "--tmpfs", "/tmp:size=128m", "-e", "HARNESS_LAB_ENVIRONMENT=container"]
        for folder in ("state", "workspaces", "fixtures", "budgets", "runs"):
            command += self.mount(self.config.host_data_root / folder, "/data/" + folder)
        command += ["--entrypoint", "python", self.image["bench"], "-m", "runner.multi_user", action,
            "--trial-id", self.args.trial_id, "--date", self.args.date, "--seed", str(self.args.seed), *extra]
        if getattr(self.args, "baseline", False):
            command.append("--baseline")
        if candidate_for(self.args) == DSH:
            command += ["--candidate", DSH]
        identity = self.call(command, timeout=30).stdout.strip()
        self.registered[identity] = "acceptance"
        self.register_container(identity)
        reply = self.call(["docker", "start", "--attach", identity], check=False, timeout=120)
        observed = self.observation(identity)
        with self.registry_lock:
            self.bench_completions[identity] = {"after_stop": observed,
                "removal_requested_monotonic": time.monotonic()}
        if self.global_paths.run.exists():
            log = self.global_paths.run / ("bench-" + str(self.sequence) + "-" + action + ".log")
            log.write_text(reply.stdout + reply.stderr)
            exclusive(log.with_suffix(".container.json"), observed)
        self.call(["docker", "rm", identity])
        if self.observation(identity) is not None:
            raise RuntimeError("Bench removal absence was not observed")
        with self.registry_lock:
            self.bench_completions[identity]["deleted"] = {"id": identity, "id_absent": True, "observed_at": now()}
        if check and reply.returncode:
            raise RuntimeError("Docker bench " + action + " failed; inspect its retained log")
        return reply.returncode

    def start_fixture(self):
        self.fixture_id = self.create("fixtures", "fixtures", self.network("a"), "fixtures",
            [(self.global_paths.fixture, "/fixture", False)],
            ["-m", "fixtures.service.server", "--db", "/fixture/ledger.sqlite"], second_network=self.network("b"))
        self.wait_port(self.fixture_id, 8080)

    def start_services(self, kind, *, letters=("a", "b")):
        services = []
        for letter in letters:
            paths = paths_for(self.config, self.args, kind, letter)
            tools = self.create("tools", kind + "-tools-" + letter, self.network(letter), "tools",
                [(paths.fixture / "tool-binding.json", "/config/binding.json", True),
                 (paths.workspace, "/workspace", False), (paths.run, "/evidence", False)],
                ["-m", "execution.tools.server", "--binding-file", "/config/binding.json"])
            proxy = self.create("model-proxy", kind + "-proxy-" + letter, self.network(letter), "model-proxy",
                [(paths.budget, "/budget", False)], ["-m", "execution.model_proxy", "--database", "/budget/calls.sqlite",
                 "--run-token-file", "/budget/run.token", "--cap", "20", "--port", "8082"], env_file=self.proxy_env_file)
            services += [tools, proxy]
            self.wait_port(tools, 8081)
            self.wait_port(proxy, 8082)
            if candidate_for(self.args) == DSH:
                self.calibrate_surface(kind, letter)
            self.bench("bind-start", letter=letter, extra=("--kind", kind, "--letter", letter))
        return services

    def calibrate_surface(self, kind, letter):
        """New image/full-scope calibration, without a model token or prompt."""
        network_name, alias = self.network(letter), "surface-" + letter
        networks = [(identity, value) for identity, value in self.network_registrations.items()
            if value.get("name") == network_name]
        if (len(networks) != 1 or networks[0][0] != networks[0][1].get("id")
                or not re.fullmatch(r"[0-9a-f]{64}", networks[0][0])
                or networks[0][1].get("trial_id") != self.args.trial_id):
            raise RuntimeError("DSH surface requires its exact created network registration")
        network_binding = {**deepcopy(networks[0][1]), "aliases": [alias]}
        self.bench("prepare-surface", letter=letter, extra=("--kind", kind, "--letter", letter))
        paths = paths_for(self.config, self.args, kind, letter)
        calibration = paths.fixture / "surface"
        mounts = [(calibration / "request.json", "/config/surface.json", True),
            (paths.fixture / "scope.json", "/config/scope.json", True),
            (paths.fixture / "scope.token", "/config/scope.token", True),
            (calibration / "workspace", "/workspace", False),
            (paths.run / "surface-calibration", "/evidence", False)]
        identity = self.create("worker", kind + "-surface-" + letter, network_name, alias,
            mounts, ["--import", "tsx/esm", "/opt/materials/harness-lab/goal-surface-entrypoint.ts",
                     "--request", "/config/surface.json"], worker=True, entrypoint="/usr/local/bin/node",
            extra_env={"MODEL_NAME": self.pins["model_name"], "HARNESS_LIFECYCLE_IMAGE_ID": self.image["worker"],
                       "TSX_TSCONFIG_PATH": "/opt/materials/sources/main/tsconfig.json"})
        with self.registry_lock:
            before = deepcopy(self.registrations[identity])
        waited = self.call(["docker", "wait", identity], check=False, timeout=90)
        after = self.observation(identity)
        logs = self.call(["docker", "logs", identity], check=False)
        (paths.run / "surface-calibration/candidate.log").write_text(logs.stdout + logs.stderr)
        if (waited.returncode != 0 or waited.stdout.strip() != "0" or after is None
                or after["state"]["ExitCode"] != 0 or after["state"]["OOMKilled"]):
            raise RuntimeError("DSH zero-model native tool surface failed; keep original diagnostics")
        self.retire(identity)
        exclusive(paths.run / "trusted/surface-container.json", {"before_start": before, "after_exit": after,
            "network_binding": network_binding,
            "deleted": {"id": identity, "id_absent": True, "observed_at": now()}})
        self.bench("accept-surface", letter=letter, extra=("--kind", kind, "--letter", letter))

    def start_workers(self, kind, attempt, *, letters=("a", "b")):
        selected = {}
        for letter in letters:
            paths = paths_for(self.config, self.args, kind, letter)
            task_id = "task-" + letter + ("-cancel" if kind == "cancel" else "")
            target = paths.fixture / "attempts" / attempt
            mounts = [(paths.state, "/state", False), (paths.workspace, "/workspace", False),
                (paths.run / "attempts" / attempt / "candidate", "/evidence", False),
                (paths.run / "attempts" / attempt / "private", "/worker-private", False),
                (target / "worker-policy.json", "/control/policy.json", True),
                (target / "worker.token", "/control/worker.token", True)]
            for source, target_path in ((target / "request.json", "/config/request.json"),
                    (target / "lifecycle.json", "/config/lifecycle.json"),
                    (paths.fixture / "scope.json", "/config/scope.json"),
                    (paths.fixture / "scope.token", "/config/scope.token"),
                    (paths.budget / "run.token", "/budget/run.token" if candidate_for(self.args) == DSH else "/config/model-run.token")):
                mounts.append((source, target_path, True))
            if candidate_for(self.args) == DSH:
                mounts.append((target / "lifecycle-state.json", "/config/lifecycle-state.json", True))
                if attempt == "worker-b":
                    mounts.append((target / "lifecycle-pause.json", "/config/lifecycle-pause.json", True))
            module = "execution.worker_control_dsh" if candidate_for(self.args) == DSH else "execution.worker_control"
            identity = self.create("worker", kind + "-" + letter + "-" + attempt,
                self.network(letter), "worker-" + task_id + "-" + attempt, mounts,
                ["-m", module, "--policy", "/control/policy.json", "--port", "8092"], worker=True,
                extra_env={"PYTHONPATH": "/opt/materials/harness-lab"} if candidate_for(self.args) == DSH else None)
            self.wait_port(identity, 8092, worker=True)
            selected[task_id] = self.observation(identity)
        exclusive(self.global_paths.run / "trusted" / (kind + "-" + attempt + ".json"), selected)
        return [row["id"] for row in selected.values()]

    def start_backend(self, kind, *, start_tasks=None):
        mounts = [(self.global_paths.fixture / ("backend-" + kind + ".json"), "/control/backend.json", True)]
        for folder in ("state", "workspaces", "fixtures", "budgets", "runs"):
            mounts.append((self.config.host_data_root / folder, "/data/" + folder, True))
        # Only the backend journal and product-client evidence are writable.
        for folder in ("clients", "backend-" + kind):
            source = self.global_paths.run / folder
            destination = str(source).replace(str(self.config.host_data_root), "/data", 1)
            mounts.append((source, destination, False))
        mounts.append((self.global_paths.run / ("backend-" + kind), "/backend-private", False))
        # Collector writes each protected trigger/exit observation; candidates
        # cannot mount these task trusted directories.
        for letter in ("a", "b"):
            source = paths_for(self.config, self.args, kind, letter).run / "trusted"
            mounts.append((source, str(source).replace(str(self.config.host_data_root), "/data", 1), False))
        tasks = ["task-" + letter + ("-cancel" if kind == "cancel" else "") for letter in ("a", "b")]
        command = ["-m", "execution.control_backend", "--fixture", "/control/backend.json",
            "--journal", "/backend-private/control.jsonl", "--port", "8091"]
        for task_id in (tasks if start_tasks is None else start_tasks):
            if task_id not in tasks:
                raise ValueError("Only trusted registered tasks may start")
            command += ["--start", task_id]
        identity = self.create("backend", kind + "-backend", self.network("a"), "backend", mounts,
            command, second_network=self.network("b"))
        self.wait_port(identity, 8091)
        return identity

    def collector(self, backend, kind, phase):
        self.phase = kind + "-" + phase
        action = "baseline-exercise" if getattr(self.args, "baseline", False) else "exercise"
        flags = ["--baseline"] if getattr(self.args, "baseline", False) else []
        candidate_flags = ["--candidate", DSH] if candidate_for(self.args) == DSH else []
        reply = self.call(["docker", "exec", "-e", "HARNESS_LAB_ENVIRONMENT=container", backend, "python",
            "-m", "runner.multi_user", action, "--trial-id", self.args.trial_id, "--date", self.args.date,
            "--seed", str(self.args.seed), "--kind", kind, "--phase", "active" if flags else phase, *flags, *candidate_flags], timeout=305, check=False)
        (self.global_paths.run / ("collector-" + kind + "-" + phase + ".log")).write_text(reply.stdout + reply.stderr)
        if reply.returncode:
            raise RuntimeError("Product collector failed; retained evidence is authoritative")

    def retire(self, identity):
        before = self.observation(identity)
        if before is None or before["trial_id"] != self.args.trial_id:
            raise RuntimeError("Cannot retire an unowned container")
        with self.registry_lock:
            if identity not in self.registrations:
                raise RuntimeError("Cannot retire an unregistered container")
            self.retirements[identity] = {"before_stop": before, "requested_monotonic": time.monotonic()}
        self.call(["docker", "stop", "--time", "3", identity], timeout=15)
        stopped = self.observation(identity)
        with self.registry_lock:
            self.retirements[identity].update({"after_stop": stopped, "removal_requested_monotonic": time.monotonic()})
        self.call(["docker", "rm", identity])
        deleted = self.observation(identity)
        if deleted is not None:
            raise RuntimeError("Container absence was not observed")
        with self.registry_lock:
            self.retirements[identity]["deleted"] = {"id": identity, "id_absent": True, "observed_at": now()}
        self.records.append({"time": now(), "before_stop": before, "after_stop": stopped,
            "deleted": {"id": identity, "id_absent": True, "observed_at": now()},
            "claim": "wrapper_container_retirement_not_native_child_cancel_or_pause"})

    def sample_once(self):
        diagnostic = {"started_monotonic": time.monotonic(), "docker_observations": [],
            "docker_after_observations": [], "command_observations": {}}
        try:
            return self._sample_once(diagnostic)
        except Exception as error:
            diagnostic["finished_monotonic"] = time.monotonic()
            if isinstance(error, SamplingFailure):
                diagnostic.update(error.observations)
                code = error.code
            elif isinstance(error, RuntimeError):
                reason = error.args[0] if error.args and isinstance(error.args[0], str) else None
                code = SAMPLING_RUNTIME_CODES.get(reason, "sampling_runtime_error")
            elif isinstance(error, json.JSONDecodeError):
                code = "sampling_command_json_invalid"
            elif isinstance(error, subprocess.TimeoutExpired):
                code = "sampling_inventory_timeout"
            elif isinstance(error, subprocess.CalledProcessError):
                code = "sampling_inventory_command_failed"
            else:
                code = "sampling_observation_failed"
            diagnostic["error_type"] = type(error).__name__
            raise SamplingFailure(code, diagnostic) from None

    def _sample_once(self, diagnostic):
        started = diagnostic["started_monotonic"]
        observations = self.inventory()
        diagnostic["docker_observations"] = observations
        running = [row for row in observations if row["state"]["Running"]]
        memory, cpu = sum(row["memory_limit"] for row in running), sum(row["cpu_nano"] for row in running)
        foreign = [row["id"] for row in observations if row["trial_id"] != self.args.trial_id]
        worker_count = sum(row["role"] == "worker" for row in running)
        if foreign or memory > 6 * 1024**3 or cpu > 3000000000 or worker_count > 2:
            self.overages.append({"time": now(), "foreign": foreign, "memory": memory, "cpu": cpu, "workers": worker_count})
        ids = [row["id"] for row in running]
        def observe(identity):
            replies, commands = {}, {}
            specs = (("stats", ["docker", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}", identity], 10),
                ("top", ["docker", "top", identity, "-eo", "pid,ppid,args"], 5))
            for kind, argv, timeout in specs:
                try:
                    reply = self.call(argv, timeout=timeout, check=False)
                    replies[kind] = reply
                    commands[kind] = {"returncode": reply.returncode, "stdout": reply.stdout, "stderr": reply.stderr}
                except Exception as error:
                    # Only these exact stats/top commands may retain partial
                    # output. Never retain exception text, argv, env or inspect.
                    partial = {}
                    for field in ("stdout", "stderr"):
                        value = getattr(error, field, None)
                        if isinstance(value, bytes):
                            value = value.decode("utf-8", errors="replace")
                        partial[field] = value if isinstance(value, str) else None
                    commands[kind] = {"returncode": None, **partial, "error_type": type(error).__name__,
                        "reason_code": "sampling_command_timeout" if isinstance(error, subprocess.TimeoutExpired)
                            else "sampling_command_invocation_failed"}
            return {"replies": replies, "commands": commands}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {identity: pool.submit(observe, identity) for identity in ids}
            replies = {identity: future.result() for identity, future in futures.items()}
        diagnostic["command_observations"] = {identity: value["commands"] for identity, value in replies.items()}
        after_observations = self.inventory()
        after_observed_monotonic = time.monotonic()
        diagnostic["docker_after_observations"] = after_observations
        after = {row["id"]: row for row in after_observations}
        with self.registry_lock:
            registrations, retirements, completions = deepcopy((self.registrations, self.retirements, self.bench_completions))
        stats, processes, transitions = [], [], []
        for row in running:
            identity = row["id"]
            diagnostic["container_id"] = identity
            observed = replies[identity]
            if len(observed["replies"]) != 2:
                codes = [value["reason_code"] for value in observed["commands"].values() if "reason_code" in value]
                raise SamplingFailure(codes[0], diagnostic)
            result = observed["replies"]
            actual = [json.loads(line) for line in result["stats"].stdout.splitlines()] if result["stats"].returncode == 0 else []
            if (result["stats"].returncode == result["top"].returncode == 0
                    and not result["stats"].stderr.strip() and not result["top"].stderr.strip()
                    and len(actual) == 1 and actual[0].get("ID", actual[0].get("Container")) == identity
                    and len(result["top"].stdout.splitlines()) >= 2):
                stats += actual
                processes.append({"container_id": identity, "raw_docker_top": result["top"].stdout,
                    "source": "actual_process_listing_not_Docker_mixed_PIDs"})
                continue
            after_row = after.get(identity)
            removal = {}
            if after_row is not None and after_row.get("state", {}).get("Status") == "removing":
                # A fresh exact-ID inspect must prove absence after the retained
                # removing snapshot. A successful inspect can contain Env, so
                # retain stdout only when it is the actual empty JSON array.
                reply = self.call(["docker", "inspect", identity], check=False, timeout=8)
                proof = {"id": identity, "id_absent": reply.returncode == 1 and reply.stdout.strip() == "[]",
                    "returncode": reply.returncode, "stdout": reply.stdout if reply.stdout.strip() == "[]" else None,
                    "stderr": reply.stderr, "observed_monotonic": time.monotonic(), "observed_at": now()}
                diagnostic.setdefault("removal_absence_observations", {})[identity] = proof
                # rm can finish and register its exact deletion while this
                # read-only inspect runs. Refresh only this ID, without holding
                # the registry lock across any Docker call. Existing captured
                # fields must stay byte-for-byte equal; only new fields may
                # have appeared during the confirmation.
                with self.registry_lock:
                    latest = deepcopy((self.registrations.get(identity), self.retirements.get(identity),
                        self.bench_completions.get(identity)))
                previous = (registrations.get(identity), retirements.get(identity), completions.get(identity))
                canonical = lambda value: json.dumps(value, sort_keys=True, allow_nan=False)
                if any(old is not None and (not isinstance(new, dict) or any(key not in new
                        or canonical(value) != canonical(new[key]) for key, value in old.items()))
                        for old, new in zip(previous, latest)):
                    raise SamplingFailure("transition_registry_changed_during_confirmation", diagnostic)
                registrations[identity], retirements[identity], completions[identity] = latest
                removal = {"removing_observed_monotonic": after_observed_monotonic, "removal_absence": proof}
            transition = explained_sampling_transition(row, result, after_row, registrations.get(identity),
                retirements.get(identity), completions.get(identity), **removal)
            if transition["absence_observed"] and "absence_observation" not in transition:
                if self.observation(identity) is not None:
                    raise RuntimeError("Exact transitioned container absence was not observed")
                transition["absence_observation"] = {"id": identity, "id_absent": True, "observed_at": now()}
            transitions.append(transition)
        return {"time": now(), "phase": self.phase, "started_monotonic": started,
            "finished_monotonic": time.monotonic(), "containers": stats,
            "docker_observations": observations, "process_observations": processes, "transition_observations": transitions,
            "allocated_memory_bytes": memory, "allocated_cpu_nano": cpu, "running_workers": worker_count}

    def sample_loop(self):
        path, previous = self.global_paths.run / "resource-samples.jsonl", None
        while not self.stop_sampling.is_set():
            tick = time.monotonic()
            try:
                value = self.sample_once()
                if value["finished_monotonic"] - value["started_monotonic"] > 5:
                    self.sampling_errors.append({"time": now(), "error": "sampling_duration_over_5_seconds",
                        "duration": value["finished_monotonic"] - value["started_monotonic"]})
                if previous is not None and tick - previous > 5:
                    self.sampling_errors.append({"time": now(), "error": "sampling_gap_over_5_seconds", "gap": tick - previous})
            except Exception as error:
                value = {"time": now(), "phase": self.phase, "error": type(error).__name__,
                    "reason_code": error.code if isinstance(error, SamplingFailure) else "sampling_unclassified_failure"}
                if isinstance(error, SamplingFailure):
                    value["observations"] = error.observations
                self.sampling_errors.append(value)
            with path.open("a") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                stream.flush()
            previous = tick
            self.stop_sampling.wait(max(0, 2 - (time.monotonic() - tick)))

    def cleanup_inspect(self, kind, identity):
        """Retain safe exact-ID observations; unknown Docker output is not absence."""
        command = ["docker", "inspect", identity] if kind == "container" else ["docker", "network", "inspect", identity]
        result = {"id": identity, "kind": kind, "status": "unknown"}
        try:
            reply = self.call(command, check=False, timeout=8)
            result["query"] = {"returncode": reply.returncode, "stderr": reply.stderr,
                "stdout": reply.stdout if reply.stdout.strip() == "[]" else None, "observed_at": now()}
            prefix = r"(?:error(?: response from daemon)?:\s*)?"
            missing = (r"no such (?:object|container):\s*" + re.escape(identity) if kind == "container" else
                r"(?:no such (?:object|network):\s*" + re.escape(identity) + r"|network\s+" + re.escape(identity) + r"\s+not found)")
            if (type(reply.returncode) is int and reply.returncode == 1 and reply.stdout.strip() == "[]"
                    and re.fullmatch(prefix + missing, reply.stderr.strip(), flags=re.IGNORECASE)):
                result.update(status="absent", id_absent=True)
            elif type(reply.returncode) is int and reply.returncode == 0 and not reply.stderr.strip():
                raw = json.loads(reply.stdout)
                if not isinstance(raw, list) or len(raw) != 1 or raw[0].get("Id") != identity:
                    return result
                snapshot = container_observation(raw[0]) if kind == "container" else {
                    "id": raw[0]["Id"], "name": raw[0]["Name"],
                    "trial_id": (raw[0].get("Labels") or {}).get("org.harness-lab.multi-user-trial"),
                    "attached_container_ids": sorted((raw[0].get("Containers") or {}).keys())}
                result.update(status="present", observation=snapshot)
        except Exception as error:
            result["error_type"] = type(error).__name__
        return result

    def cleanup_inventory(self, kind):
        command = (["docker", "ps", "-aq", "--no-trunc"] if kind == "container" else
            ["docker", "network", "ls", "-q", "--no-trunc"])
        command += ["--filter", "label=org.harness-lab.multi-user-trial=" + self.args.trial_id]
        result = {"kind": kind, "confirmed": False}
        try:
            reply = self.call(command, check=False, timeout=8)
            ids = reply.stdout.split()
            result["query"] = {"returncode": reply.returncode, "stderr": reply.stderr, "observed_at": now()}
            if (type(reply.returncode) is int and reply.returncode == 0 and not reply.stderr.strip() and len(ids) == len(set(ids))
                    and all(re.fullmatch(r"[0-9a-f]{64}", identity) for identity in ids)):
                result.update(confirmed=True, ids=ids)
        except Exception as error:
            result["error_type"] = type(error).__name__
        return result

    def cleanup(self):
        # Only create-returned exact IDs are mutation targets. A final list is
        # evidence, never permission to delete another object's resources.
        failures, resolved_names = [], set()
        result = {"trial_id": self.args.trial_id, "started_at": now(), "containers": [], "networks": [],
            "native_child_health_claimed": False}
        for identity, role in reversed(list(self.registered.items())):
            item = {"id": identity, "role": role, "passed": False}
            result["containers"].append(item)
            try:
                if not re.fullmatch(r"[0-9a-f]{64}", identity):
                    raise RuntimeError("invalid_owned_identity")
                before = self.cleanup_inspect("container", identity)
                item["before_cleanup"] = before
                registered = self.registrations.get(identity)
                record = self.bench_completions.get(identity) if role == "acceptance" else self.retirements.get(identity)
                if before["status"] == "absent":
                    terminal = (record or {}).get("after_stop")
                    if (registered is None or terminal is None or container_identity(terminal) != container_identity(registered)
                            or registered["id"] != identity or registered["trial_id"] != self.args.trial_id or registered["role"] != role
                            or registered["name"].lstrip("/") not in self.owned
                            or terminal["state"].get("Status") != "exited" or terminal["state"].get("Running") is not False
                            or type(terminal["state"].get("Pid")) is not int or terminal["state"]["Pid"] != 0
                            or (record or {}).get("deleted", {}).get("id") != identity
                            or record["deleted"].get("id_absent") is not True
                            or not isinstance(record["deleted"].get("observed_at"), str)):
                        raise RuntimeError("prior_exact_deletion_unproven")
                    resolved_names.add(registered["name"].lstrip("/"))
                    item.update(prior_terminal_observation=terminal, prior_deleted=record["deleted"], passed=True)
                    continue
                if before["status"] != "present":
                    raise RuntimeError("owned_inspection_unproven")
                snapshot = before["observation"]
                expected_image = self.image["worker" if role == "worker" else "bench" if role == "acceptance" else "services"]
                if (snapshot["trial_id"] != self.args.trial_id or snapshot["role"] != role
                        or snapshot["name"].lstrip("/") not in self.owned or snapshot["image_id"] != expected_image
                        or (registered is not None and container_identity(snapshot) != container_identity(registered))):
                    raise RuntimeError("owned_identity_mismatch")
                resolved_names.add(snapshot["name"].lstrip("/"))
                if self.global_paths.run.exists():
                    output = self.global_paths.run / ("final-" + identity[:12] + ".log")
                    if not output.exists():
                        try:
                            log = self.call(["docker", "logs", identity], check=False)
                            with output.open("x") as stream:
                                stream.write(log.stdout + log.stderr)
                        except Exception as error:
                            item["log_error_type"] = type(error).__name__
                if snapshot["state"]["Running"]:
                    stopped = self.call(["docker", "stop", "--time", "3", identity], check=False, timeout=15)
                    item["stop_returncode"] = stopped.returncode
                terminal = self.cleanup_inspect("container", identity)
                item["terminal_observation"] = terminal
                final = terminal.get("observation", {})
                terminal_confirmed = (terminal["status"] == "present" and container_identity(final) == container_identity(snapshot)
                    and final["state"].get("Status") == "exited" and final["state"].get("Running") is False
                    and type(final["state"].get("Pid")) is int and final["state"]["Pid"] == 0)
                removed = self.call(["docker", "rm", "-f", identity], check=False, timeout=15)
                item["remove_returncode"] = removed.returncode
                item["after_remove"] = self.cleanup_inspect("container", identity)
                item["passed"] = terminal_confirmed and removed.returncode == 0 and item["after_remove"]["status"] == "absent"
            except Exception as error:
                item["error_type"] = type(error).__name__
            if not item["passed"]:
                failures.append(identity)
        unconfirmed_names = sorted(set(self.owned) - resolved_names)
        result["unconfirmed_owned_names"] = unconfirmed_names
        failures.extend(unconfirmed_names)
        network_registrations = getattr(self, "network_registrations", {})
        resolved_networks = set()
        for identity, registered in reversed(list(network_registrations.items())):
            item = {"id": identity, "registration": registered, "passed": False}
            result["networks"].append(item)
            try:
                if not re.fullmatch(r"[0-9a-f]{64}", identity):
                    raise RuntimeError("invalid_owned_network_identity")
                before = self.cleanup_inspect("network", identity)
                item["before_cleanup"] = before
                snapshot = before.get("observation", {})
                if (before["status"] != "present" or snapshot.get("name") not in self.networks
                        or snapshot.get("name") != registered["name"] or snapshot.get("trial_id") != self.args.trial_id
                        or registered.get("trial_id") != self.args.trial_id or registered.get("id") != identity):
                    raise RuntimeError("owned_network_identity_unproven")
                resolved_networks.add(snapshot["name"])
                removed = self.call(["docker", "network", "rm", identity], check=False, timeout=15)
                item["remove_returncode"] = removed.returncode
                item["after_remove"] = self.cleanup_inspect("network", identity)
                item["passed"] = removed.returncode == 0 and item["after_remove"]["status"] == "absent"
            except Exception as error:
                item["error_type"] = type(error).__name__
            if not item["passed"]:
                failures.append(identity)
        result["unconfirmed_network_names"] = sorted(set(self.networks) - resolved_networks)
        failures.extend(result["unconfirmed_network_names"])
        result["final_inventory"] = [self.cleanup_inventory(kind) for kind in ("container", "network")]
        for inventory in result["final_inventory"]:
            if not inventory["confirmed"] or inventory["ids"]:
                failures.append("final_" + inventory["kind"] + "_inventory")
        result.update(passed=not failures, failures=list(dict.fromkeys(failures)), finished_at=now())
        try:
            self.global_paths.run.mkdir(parents=True, exist_ok=True)
            exclusive(self.global_paths.run / "cleanup-result.json", result)
        except Exception:
            failures.append("cleanup_result_not_saved")
        return list(dict.fromkeys(failures))

    def run(self):
        self.preflight()
        baseline = getattr(self.args, "baseline", False)
        outcome, errors, grade_exit = False, [], None
        try:
            self.create_networks()
            self.start_fixture()
            self.bench("prepare")
            self.sampler = threading.Thread(target=self.sample_loop, daemon=True)
            self.sampler.start()
            for kind in (("main",) if baseline else ("main", "cancel")):
                self.phase = kind + "-prepare"
                letters = ("a",) if baseline else ("a", "b")
                services = self.start_services(kind, letters=letters)
                workers = self.start_workers(kind, "worker-a", letters=letters)
                backend = self.start_backend(kind, start_tasks=("task-a",) if baseline else None)
                active = self.inventory()
                selected = [row for row in active if row["state"]["Running"]]
                expected = (5, 3 * 1024**3, 1750000000) if baseline else (8, 5905580032, 3000000000)
                if (len(selected), sum(row["memory_limit"] for row in selected), sum(row["cpu_nano"] for row in selected)) != expected:
                    raise RuntimeError("Frozen active container allocation differs")
                exclusive(self.global_paths.run / (kind + "-active-containers.json"), active)
                self.collector(backend, kind, "baseline" if baseline else "active")
                for identity in workers:
                    self.retire(identity)
                if kind == "main" and not baseline:
                    self.phase = "bind-replacements"
                    self.bench("bind-replacements")
                    workers = self.start_workers(kind, "worker-b")
                    self.collector(backend, kind, "resume")
                    for identity in workers:
                        self.retire(identity)
                for identity in (backend, *services):
                    log = self.call(["docker", "logs", identity], check=False).stdout
                    (self.global_paths.run / (identity[:12] + ".log")).write_text(log)
                    self.retire(identity)
            outcome = True
        except Exception as error:
            errors.append(type(error).__name__ + ":" + str(error))
        finally:
            try:
                # Exact IDs returned by this trial's docker create are retained
                # even if a later inventory read fails. Stop paid execution
                # before any grading or final evidence operation can fail.
                if not outcome:
                    for identity, role in self.registered.items():
                        if role == "fixtures":
                            continue
                        try:
                            self.call(["docker", "stop", "--time", "3", identity], check=False)
                        except Exception as error:
                            errors.append("stop_owned_execution:" + type(error).__name__)
                self.phase = "grading"
                self.stop_sampling.set()
                if self.sampler:
                    self.sampler.join(timeout=20)
                    if self.sampler.is_alive():
                        self.sampling_errors.append({"error": "sampler_did_not_stop"})
                if self.global_paths.run.exists():
                    with self.registry_lock:
                        registration_evidence = deepcopy({"registrations": self.registrations,
                            "retirements": self.retirements, "bench_completions": self.bench_completions,
                            "inventory_absences": self.inventory_absences})
                    exclusive(self.global_paths.run / "container-transition-registry.json", registration_evidence)
                    exclusive(self.global_paths.run / "worker-lifecycle.json", self.records)
                    exclusive(self.global_paths.run / "host-result.json", {"passed": outcome and not errors
                            and not self.sampling_errors and not self.overages,
                        "sampling_errors": self.sampling_errors, "resource_overages": self.overages, "errors": errors,
                        "browser_isolation": "not_tested", "worker_reuse_supported": False, "baseline_only": baseline})
                    grade_exit = self.bench("baseline-grade" if baseline else "grade", check=False)
            except Exception as error:
                errors.append("finalize:" + type(error).__name__)
                if self.global_paths.run.exists():
                    exclusive(self.global_paths.run / "finalization-error.json", {"errors": errors})
            finally:
                self.stop_sampling.set()
                if self.sampler:
                    self.sampler.join(timeout=20)
                failures = self.cleanup()
                try:
                    if failures and self.global_paths.run.exists():
                        exclusive(self.global_paths.run / "cleanup-failures.json", {"containers_or_networks": failures})
                finally:
                    try:
                        self.proxy_env_file.unlink()
                    except FileNotFoundError:
                        pass
        return 0 if outcome and not errors and not self.sampling_errors and not self.overages and grade_exit == 0 and not failures else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-id", required=True, type=identifier)
    parser.add_argument("--pins-file", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--seed", type=int, choices=(101, 202, 303), default=101)
    parser.add_argument("--baseline", action="store_true", help="Only A's main task; small-quota admission, not I24 capability")
    parser.add_argument("--candidate", choices=(DA, DSH), default=DA)
    args = parser.parse_args()
    lock_path = Path("/tmp/harness-lab-exclusive.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return MultiUserLab(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
