"""I24 Docker-only preparation, real product clients and independent evidence.

This is a DA two-Worker experiment, not a scheduler or an Agent implementation.
The host creates containers; this module never imports the candidate SDK, calls
a model, supplies a recovery prompt or computes the Agent's tool choices.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from runner import pause_recovery as p
from runner.bench import environment, identifier
from runner.config import load_config
from runner.grading import audited_calls, grade_tools, prepare_tools_inputs, same_scope, scoped_ledger
from execution import dsh_materials as dsh
from runner import multi_user_dsh


CANDIDATE = "deepagents_langgraph"
KINDS = ("main", "cancel")
ATTEMPTS = ("worker-a", "worker-b")
ARGV = ["/opt/venv/bin/python", "/opt/materials/harness-lab/entrypoint.py", "--request", "/config/request.json"]


@dataclass(frozen=True)
class Task:
    task_id: str
    owner: str
    kind: str
    letter: str
    scope: dict
    paths: object
    candidate: str = CANDIDATE


def candidate_for(args):
    candidate = getattr(args, "candidate", CANDIDATE)
    p.require(candidate in {CANDIDATE, dsh.CANDIDATE}, "allowlisted_I24_candidate_required")
    return candidate


def task_candidate(task):
    return candidate_for(task)


def trusted_pins(task):
    return multi_user_dsh.task_pins(task) if task_candidate(task) == dsh.CANDIDATE else p.read(task.paths.run / "trusted/pins.json")


def layout(args, *, container=True):
    candidate = candidate_for(args)
    config = load_config()
    if container:
        config = config.for_environment("container")
    experiment = "multi-user-baseline" if getattr(args, "baseline", False) else "multi-user"
    case = "scope-baseline" if getattr(args, "baseline", False) else "scope"
    global_paths = config.paths_for(experiment, args.trial_id, candidate,
        "control", "main", args.trial_id, case=case, date=args.date)
    tasks = []
    for kind in KINDS:
        for letter in ("a", "b"):
            variant = f"{args.trial_id}-{kind}-{letter}"
            owner, session = "user-" + letter, "session-" + letter
            scope = {"experiment_id": experiment, "variant": variant,
                "user_id": owner, "session_id": session}
            paths = config.paths_for(experiment, variant, candidate, owner,
                session, variant, case=case, date=args.date)
            tasks.append(Task("task-" + letter + ("-cancel" if kind == "cancel" else ""),
                owner, kind, letter, scope, paths, candidate))
    return config, global_paths, tasks


def private_text(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def append(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(p.canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def admin(global_paths, method, route, **kwargs):
    token = p.regular(global_paths.fixture / ".admin-token").decode().strip()
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.request(method, "http://fixtures:8080" + route,
            headers={"Authorization": "Bearer " + token}, **kwargs)
        response.raise_for_status()
        return response.json()


def ledger(global_paths, task):
    return admin(global_paths, "GET", "/admin/ledger", params={
        "experiment_id": task.scope["experiment_id"], "variant": task.scope["variant"]})


def environment_for(model, candidate=CANDIDATE, image=None):
    p.require(candidate in {CANDIDATE, dsh.CANDIDATE}, "allowlisted_I24_candidate_required")
    if candidate == dsh.CANDIDATE:
        return multi_user_dsh.environment(model, image)
    return {"MODEL_API_BASE_URL": "http://model-proxy:8082/v1", "MODEL_NAME": model,
        "MODEL_RUN_TOKEN_FILE": "/config/model-run.token", "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false"}


def attempt_spec(task, attempt, model, *, worker, image=None):
    return {"attempt_id": attempt, "candidate": task_candidate(task), "argv": list(dsh.ARGV) if task_candidate(task) == dsh.CANDIDATE else ARGV,
        "environment": environment_for(model, task_candidate(task), image),
        "config_dir": "/config" if worker else str(task.paths.fixture / "attempts" / attempt),
        "evidence_dir": "/evidence" if worker else str(task.paths.run / "attempts" / attempt / "candidate"),
        "events_file": "raw-events.jsonl", "readiness_file": "control-events.jsonl",
        "ready_match": {"phase": "pause_handler_installed", "signal": "SIGUSR2"},
        "native_ids": {"session_id": task.scope["session_id"]}}


def task_spec(task, model, *, worker=False, attempt=None, image=None):
    return {"task_id": task.task_id, "owner": task.owner, "scope": task.scope,
        "state_dir": "/state" if worker else str(task.paths.state),
        "workspace": "/workspace" if worker else str(task.paths.workspace),
        "artifacts": ["report.json"], "attempts": [attempt_spec(task, item, model, worker=worker, image=image)
            for item in ([attempt] if worker else ATTEMPTS)]}


def worker_id(task, attempt):
    return task.scope["variant"] + "-" + attempt


def prepare(args, global_paths, tasks):
    candidate = candidate_for(args)
    baseline = getattr(args, "baseline", False)
    pins = p.read(global_paths.fixture / "material-pins.json")
    hash_keys = ("source_export_sha256", "adapter_export_sha256") + (("adapter_helper_sha256",) if candidate == CANDIDATE else ())
    for key in hash_keys:
        p.require(p.HEX.fullmatch(str(pins.get(key, ""))), "material_hash_required")
    for key in ("worker_image_id", "bench_image_id", "services_image_id"):
        p.require(p.IMAGE.fullmatch(str(pins.get(key, ""))), "immutable_image_id_required")
    p.require(isinstance(pins.get("model_name"), str) and pins["model_name"], "model_pin_required")
    global_paths.run.mkdir(parents=True, exist_ok=False)
    for name in ("trusted", "clients", "backend-main", "backend-cancel"):
        (global_paths.run / name).mkdir()
    credentials = []
    for letter in ("a", "b"):
        token = secrets.token_urlsafe(32)
        private_text(global_paths.fixture / ("user-" + letter + ".token"), token)
        credentials.append({"credential_id": "credential-" + letter, "principal_id": "user-" + letter,
            "token_sha256": p.sha(token.encode())})
    for task in tasks:
        paths, scope = task.paths, task.scope
        for directory in (paths.state, paths.workspace, paths.budget, paths.run, paths.fixture):
            directory.mkdir(parents=True, exist_ok=False)
        (paths.run / "trusted").mkdir()
        setup = admin(global_paths, "POST", "/admin/scenarios", json={
            "experiment_id": scope["experiment_id"], "variant": scope["variant"], "seed": args.seed,
            "users": [{"user_id": task.owner, "session_id": scope["session_id"]}], "write_mode": "idempotent"})
        own = [row for row in setup["scopes"] if same_scope(row, scope)]
        p.require(len(own) == 1, "fixture_scope_missing")
        token = own[0]["token"]
        private_text(paths.fixture / "scope.token", token)
        private_text(paths.budget / "run.token", secrets.token_urlsafe(32))
        p.create(paths.fixture / "scope.json", scope)
        p.create(paths.fixture / "tool-binding.json", {"fixture_url": "http://fixtures:8080",
            "fixture_token": token, "scope": scope, "workspace": "/workspace", "events_path": "/evidence/tool-events.jsonl"})
        public = prepare_tools_inputs(paths.workspace, scope)
        # Public, distinct markers are requirements, not precomputed answers.
        private_text(paths.workspace / "task-marker.txt", task.task_id + ":" + scope["variant"])
        prompt = "本任务可信作用域为 " + json.dumps(scope, ensure_ascii=False) + "。\n" + public["prompt"]
        request = {"run_id": scope["variant"], "session_id": scope["session_id"], "prompt": prompt,
            "workspace": "/workspace", "state_dir": "/state", "output_dir": "/evidence",
            "tools_url": "http://tools:8081", "tool_token_file": "/config/scope.token",
            "max_output_tokens": 4096, "timeout_seconds": 300, "max_retries": 2,
            "candidate_options": {"case": "tools", "trusted_scope_file": "/config/scope.json"}}
        p.create(paths.fixture / "candidate-request.json", request)
        p.create(paths.run / "input.json", {"candidate": candidate, "run_id": scope["variant"],
            "scope": scope, "seed": args.seed, "prompt": prompt, "expected_answer_delivered": False})
        p.create(paths.run / "chain.json", {"schema": "harness-lab-pause-chain/1", "specialty": "multi-user",
            "candidate": candidate, "chain_id": scope["variant"], "scope": scope,
            "input_sha256": p.sha(p.regular(paths.run / "input.json")),
            "original_request_sha256": p.sha(p.regular(paths.fixture / "candidate-request.json"))})
        task_pins = {**pins, "candidate": candidate, "candidate_image_id": pins["worker_image_id"],
            "chain_id": scope["variant"], "scope": scope, "seed": args.seed, "date": args.date}
        p.create(paths.run / "trusted/pins.json", task_pins)
        for attempt in ATTEMPTS:
            target = paths.fixture / "attempts" / attempt
            target.mkdir(parents=True)
            (paths.run / "attempts" / attempt / "candidate").mkdir(parents=True)
            (paths.run / "attempts" / attempt / "private").mkdir()
            private_text(target / "worker.token", secrets.token_urlsafe(32))
            p.create(target / "worker-policy.json", {"schema": "harness-lab-worker-control/1",
                "worker_id": worker_id(task, attempt), "mode": "new" if attempt == "worker-a" else "resume",
                "task": task_spec(task, pins["model_name"], worker=True, attempt=attempt, image=pins["worker_image_id"]),
                "control_token_file": "/control/worker.token", "private_root": "/worker-private"})
    for kind in KINDS:
        selected = [task for task in tasks if task.kind == kind]
        routes = [{"task_id": task.task_id, "attempt_id": attempt, "worker_id": worker_id(task, attempt),
            "url": "http://worker-" + task.task_id + "-" + attempt + ":8092",
            "policy_file": str(task.paths.fixture / "attempts" / attempt / "worker-policy.json"),
            "token_file": str(task.paths.fixture / "attempts" / attempt / "worker.token")}
            for task in selected for attempt in ATTEMPTS]
        p.create(global_paths.fixture / ("backend-" + kind + ".json"), {
            "schema": "harness-lab-container-control-backend/1", "tasks": [task_spec(t, pins["model_name"], image=pins["worker_image_id"]) for t in selected],
            "routes": routes, "credentials": credentials})
    p.create(global_paths.run / "plan.json", {"schema": "harness-lab-multi-user-plan/1", "trial_id": args.trial_id,
        "candidate": candidate, "seed": args.seed, "date": args.date, "pins": pins,
        "tasks": [{"task_id": t.task_id, "owner": t.owner, "kind": t.kind, "scope": t.scope,
            "run": str(t.paths.run)} for t in tasks], "browser_isolation": "not_tested",
        "baseline_only": baseline,
        "executed_tasks": ["task-a"] if baseline else [task.task_id for task in tasks],
        "worker_reuse_supported": False, "resume_mechanism": "new_container_original_native_checkpoint",
        "budget": {"memory_bytes": 3 * 1024**3 if baseline else 5905580032,
            "cpu_nano": 1750000000 if baseline else 3000000000, "worker_memory_bytes": 2147483648,
            "worker_cpu_nano": 750000000, "model_calls_per_logical_task": 20, "active_seconds_per_logical_task": 300}})


def bind_start(args, tasks):
    for task in tasks:
        if task.kind != args.kind or (args.letter and task.letter != args.letter):
            continue
        tools = p.trusted_tools(task.paths)
        p.create(task.paths.run / "trusted/http-tool-surface.json", {"tools": tools})
        bound = p.bind(task.paths, task.scope, trusted_pins(task), "worker-a", used=0)
        p.create(task.paths.run / "start-binding.json", bound)


def prefix(path, relative, *, empty=False):
    try:
        rows = p.jsonl(path)
    except FileNotFoundError:
        p.require(empty, "required_observation_missing")
        rows = []
    return rows, {"path": relative, "observed_through_line": len(rows),
        "observed_prefix_sha256": p.sha(b"".join(encoded for _, encoded, _ in rows))}


def activity(task, *, require_write=True):
    if task_candidate(task) == dsh.CANDIDATE:
        return multi_user_dsh.activity(task, require_write=require_write)
    root = task.paths.run
    start = time.monotonic()
    raw, raw_ref = prefix(root / "attempts/worker-a/candidate/raw-events.jsonl",
        "attempts/worker-a/candidate/raw-events.jsonl")
    tools, tools_ref = prefix(root / "tool-events.jsonl", "tool-events.jsonl", empty=True)
    controls, control_ref = prefix(root / "attempts/worker-a/candidate/control-events.jsonl",
        "attempts/worker-a/candidate/control-events.jsonl", empty=True)
    active = {}
    for line, encoded, row in raw:
        p.require(row.get("scope") == task.scope, "foreign_native_observation")
        event = row.get("data", {})
        if row.get("stream_mode") == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
            if "input" in event and "result" not in event:
                active[event["id"]] = (line, encoded, event.get("name"))
            elif any(key in event for key in ("result", "error", "interrupts")):
                active.pop(event["id"], None)
        if row.get("type") == "native_graph_drained":
            active.clear()
    live = [value for value in active.values() if value[2] in {"model", "tools"}]
    writes = [(line, encoded) for line, encoded, row in tools if row.get("scope") == task.scope
        and row.get("tool") == "write" and row.get("phase") == "finish" and row.get("status") == 200
        and isinstance(row.get("result", {}).get("receipt"), dict)]
    p.require(live and (writes or not require_write), "no_eligible_native_activity")
    p.require(not any(row.get("phase") == "requested" for _, _, row in controls), "native_control_already_requested")
    line, encoded, _ = live[-1]
    value = {"observation_clock": "host_monotonic_prefix/1", "observation_started_monotonic": start,
        "observation_finished_monotonic": time.monotonic(),
        "activity_source": {**raw_ref, "line": line, "line_sha256": p.sha(encoded)}, "control_source": control_ref,
        "collector_clock_domain": "Docker_VM_monotonic",
        "tools_source": tools_ref,
        "native_prefix_lines": len(raw), "successful_tools": sum(row.get("phase") == "finish"
            and row.get("status") == 200 for _, _, row in tools)}
    if writes:
        line, encoded = writes[-1]
        value["write_source"] = {**tools_ref, "line": line, "line_sha256": p.sha(encoded)}
    return value


class ProductClient:
    def __init__(self, endpoint, token, journal, principal):
        parsed = urlsplit(endpoint)
        p.require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port == 8091
            and not parsed.path and not parsed.query and not parsed.username, "local_product_endpoint_required")
        self.host, self.port, self.token = parsed.hostname, parsed.port, token
        self.journal, self.principal, self.connection = journal, principal, None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def request(self, task, operation, *, query="", authenticated=True):
        if self.connection is None:
            self.connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        method = "GET" if operation in {"state", "events", "artifacts"} else "POST"
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        started = time.monotonic()
        self.connection.request(method, "/tasks/" + task.task_id + "/" + operation + query,
            body=None if method == "GET" else b"{}", headers=headers)
        reply = self.connection.getresponse()
        data = reply.read(8 * 1024 * 1024 + 1)
        p.require(len(data) <= 8 * 1024 * 1024, "product_response_too_large")
        row = {"kind": "actual_product_http_response", "time": p.now(), "principal": self.principal,
            "task_id": task.task_id, "operation": operation, "method": method, "query": query,
            "authenticated": authenticated, "status": reply.status, "request_id": reply.getheader("X-Request-ID"),
            "connection_id": reply.getheader("X-Connection-ID"), "started_monotonic": started,
            "finished_monotonic": time.monotonic(), "response_bytes": len(data), "body_sha256": p.sha(data),
            "raw_body_base64": base64.b64encode(data).decode("ascii"), "body": p.decode(data)}
        append(self.journal, row)
        return row


def observed_files(task):
    # These are actual snapshots. A naturally changed snapshot stays changed.
    return {"scope": task.scope, "native_and_workspace": p.inventory(task.paths.state, task.paths.workspace),
        "tool_audit_sha256": p.sha(p.regular(task.paths.run / "tool-events.jsonl"))}


def foreign_probe(global_paths, client, task, operation, record_path):
    attempts = []
    for _ in range(3):
        before = after = response = None
        try:
            before = {"files": observed_files(task), "ledger": ledger(global_paths, task)}
            response = client.request(task, operation)
            after = {"files": observed_files(task), "ledger": ledger(global_paths, task)}
            stable = before == after
            item = {"before": before, "response": response, "after": after, "stable_window_observed": stable}
        except (OSError, ValueError) as error:
            stable = False
            item = {"before": before, "response": response, "after": after,
                "stable_window_observed": False, "observation_error": type(error).__name__}
        attempts.append(item)
        if stable:
            break
    p.create(record_path, {"task_id": task.task_id, "operation": operation, "attempts": attempts,
        "passed": stable and response is not None and response["status"] == 403
            and response["body"] == {"error": {"code": "scope_denied"}}})


def wait_activity(task, deadline):
    while time.monotonic() < deadline:
        try:
            return activity(task)
        except (OSError, ValueError):
            if (task.paths.run / "attempts/worker-a/candidate/result.json").exists():
                raise ValueError("task_ended_before_control_activity_window") from None
            time.sleep(0.1)
    raise ValueError("control_activity_window_not_observed")


def wait_closed(client, task, deadline):
    while time.monotonic() < deadline:
        row = client.request(task, "state")
        process = row["body"].get("process_observation", {})
        if row["status"] == 200 and process.get("availability") == "observed" and process.get("process_status") == "exited":
            allowed = (0, 1) if task_candidate(task) == dsh.CANDIDATE and task.kind == "cancel" else (0,)
            p.require(type(process.get("exit_code")) is int and process["exit_code"] in allowed, "native_child_failed")
            return row
        time.sleep(0.3)
    raise ValueError("native_child_exit_not_observed_before_deadline")


def exercise(args, global_paths, tasks):
    selected = [task for task in tasks if task.kind == args.kind]
    output = global_paths.run / "clients" / (args.kind + "-" + args.phase)
    output.mkdir()
    clients = {letter: ProductClient(args.endpoint, p.regular(global_paths.fixture / ("user-" + letter + ".token")).decode().strip(),
        output / "http.jsonl", "user-" + letter) for letter in ("a", "b")}
    own = lambda task: clients[task.letter]
    started, deadline = time.monotonic(), time.monotonic() + 285
    try:
        if args.phase == "resume":
            for task in selected:
                response = own(task).request(task, "resume")
                p.require(response["status"] == 202, "product_resume_rejected")
            for task in selected:
                # Waiting for another owner's task may outlast this HTTP
                # server's idle timeout. Reconnect with the same identity;
                # never replay the already accepted resume POST.
                own(task).close()
                wait_closed(own(task), task, deadline)
                for operation in ("state", "events", "artifacts"):
                    p.require(own(task).request(task, operation)["status"] == 200, "owner_final_read_failed")
                p.require(own(task).request(task, "events", query="?attempt=worker-a&after=0")["status"] == 200,
                    "original_event_cursor_unavailable")
            p.create(output / "finished.json", {"elapsed_seconds": time.monotonic() - started, "phase": "resume"})
            return
        previous, overlap = None, None
        while time.monotonic() < deadline:
            try:
                samples = {task.task_id: activity(task, require_write=False) for task in selected}
            except (OSError, ValueError):
                time.sleep(0.2)
                continue
            observation = {"time": p.now(), "monotonic": time.monotonic(), "tasks": samples}
            append(output / "overlap-observations.jsonl", observation)
            if previous and all(samples[key]["native_prefix_lines"] > previous["tasks"][key]["native_prefix_lines"]
                    and samples[key]["successful_tools"] > previous["tasks"][key]["successful_tools"] for key in samples):
                overlap = {"before": previous, "after": observation}
                break
            if previous is None:
                previous = observation
            time.sleep(0.2)
        p.require(overlap is not None, "two_native_tasks_with_effective_overlapping_progress_not_observed")
        p.create(output / "overlap.json", overlap)
        for task in selected:
            for operation in ("state", "events"):
                p.require(own(task).request(task, operation)["status"] == 200, "owner_active_read_failed")
        target = selected[1]
        operations = ("state", "events", "artifacts", "pause", "cancel") if args.kind == "main" else ("cancel",)
        for operation in operations:
            foreign_probe(global_paths, clients["a"], target, operation, output / ("foreign-" + operation + ".json"))
        for task in selected:
            trigger = wait_activity(task, deadline)
            trigger.update({"sent_at": p.now(), "signal": "SIGUSR2" if args.kind == "main" else "SIGUSR1",
                "container_id": p.read(global_paths.run / "trusted" / (args.kind + "-worker-a.json"))[task.task_id]["id"],
                "signal_sent_monotonic": time.monotonic()})
            # The native activity window can outlast the backend's socket idle
            # timeout. Open a fresh same-owner connection before the one and
            # only control POST; an uncertain POST is never replayed.
            own(task).close()
            response = own(task).request(task, "pause" if args.kind == "main" else "cancel")
            trigger["signal_acknowledged_monotonic"] = time.monotonic()
            trigger["product_http"] = response
            p.create(task.paths.run / "trusted/control-trigger.json", trigger)
            p.require(response["status"] == 202, "product_control_dispatch_failed")
        for task in selected:
            # Polling the previous owner may have left this owner's accepted
            # control socket idle. Reconnect only for the following reads.
            own(task).close()
            closed = wait_closed(own(task), task, deadline)
            starts = [row for _, _, row in p.jsonl(global_paths.run / ("backend-" + args.kind) / "control.jsonl")
                if row.get("kind") == "worker_start_attempt" and row.get("task_id") == task.task_id
                and row.get("attempt_id") == "worker-a" and row.get("observation_clock") == "Docker_VM_monotonic/1"]
            p.require(len(starts) == 1 and type(starts[0].get("controller_monotonic")) in (int, float), "actual_start_clock_missing")
            p.create(task.paths.run / "trusted/closed-observation.json", {"http": closed,
                "controller_elapsed_seconds": time.monotonic() - starts[0]["controller_monotonic"],
                "observation_clock": "Docker_VM_monotonic/1"})
        if args.kind == "main":
            # B's drain may have outlasted A's socket idle timeout.
            clients["a"].close()
            foreign_probe(global_paths, clients["a"], target, "resume", output / "foreign-resume.json")
            # Close the actual client socket, observe server-side EOF, reconnect
            # and authenticate again. No proxy/model connection is touched.
            for task in selected:
                # Start the dedicated disconnect proof with a live same-owner
                # socket even if the previous owner's proof took a long time.
                own(task).close()
                first = own(task).request(task, "state")
                own(task).close()
                eof = None
                while time.monotonic() < deadline:
                    for number, encoded, row in p.jsonl(global_paths.run / "backend-main/control.jsonl"):
                        if row.get("kind") == "connection_peer_eof" and row.get("connection_id") == first["connection_id"]:
                            eof = {"path": "backend-main/control.jsonl", "line": number, "line_sha256": p.sha(encoded)}
                    if eof:
                        break
                    time.sleep(0.05)
                p.require(eof is not None, "product_peer_eof_not_observed")
                second = own(task).request(task, "state")
                p.require(second["status"] == 200 and second["connection_id"] != first["connection_id"], "product_reconnect_not_observed")
                p.create(output / ("reconnect-" + task.letter + ".json"), {"first": first, "peer_eof": eof, "second": second})
                if task.letter == "a":
                    # Use this exact newly authenticated A socket before
                    # waiting for B's independent disconnect/EOF proof.
                    foreign_probe(global_paths, clients["a"], target, "state", output / "foreign-reconnect-state.json")
        p.create(output / "finished.json", {"elapsed_seconds": time.monotonic() - started, "phase": args.phase})
    finally:
        for client in clients.values():
            client.close()


def baseline_exercise(args, global_paths, tasks):
    p.require(getattr(args, "baseline", False), "baseline_action_requires_explicit_mode")
    task = next(task for task in tasks if task.kind == "main" and task.letter == "a")
    output = global_paths.run / "clients/main-baseline"
    output.mkdir()
    client = ProductClient(args.endpoint, p.regular(global_paths.fixture / "user-a.token").decode().strip(),
        output / "http.jsonl", "user-a")
    try:
        closed = wait_closed(client, task, time.monotonic() + 285)
        for operation in ("events", "artifacts"):
            p.require(client.request(task, operation)["status"] == 200, "baseline_owner_final_read_failed")
        starts = [row for _, _, row in p.jsonl(global_paths.run / "backend-main/control.jsonl")
            if row.get("kind") == "worker_start_attempt"]
        p.require(len(starts) == 1 and starts[0].get("task_id") == "task-a"
            and starts[0].get("attempt_id") == "worker-a"
            and starts[0].get("observation_clock") == "Docker_VM_monotonic/1", "baseline_single_actual_start_missing")
        elapsed = time.monotonic() - starts[0]["controller_monotonic"]
        p.require(0 < elapsed <= 300, "baseline_active_budget_exceeded")
        p.create(output / "finished.json", {"baseline_only": True, "task_id": "task-a", "closed": closed,
            "controller_elapsed_seconds": elapsed, "observation_clock": "Docker_VM_monotonic/1"})
    finally:
        client.close()


def bind_replacements(global_paths, tasks):
    for task in tasks:
        if task.kind != "main":
            continue
        paths, scope = task.paths, task.scope
        pins = trusted_pins(task)
        trigger = p.read(paths.run / "trusted/control-trigger.json")
        closed = p.read(paths.run / "trusted/closed-observation.json")
        p.validate_trigger(paths.run, scope, task_candidate(task), trigger)
        validator = p.validate_dsh_pause if task_candidate(task) == dsh.CANDIDATE else p.validate_da_pause
        native = validator(paths, scope, pins, {
            "active_seconds": closed["controller_elapsed_seconds"], "pause_request": trigger})
        audit = p.audit_snapshot(paths.run / "tool-events.jsonl", scope)
        budget = p.budget_snapshot(paths.budget)
        native_elapsed = (p.read(paths.run / "attempts/worker-a/candidate/result.json")["elapsed_seconds"]
            if task_candidate(task) == dsh.CANDIDATE else native["index"]["total_active_seconds"])
        p.require(p.number(native_elapsed) and p.number(closed["controller_elapsed_seconds"]), "actual_active_clock_required")
        used = max(closed["controller_elapsed_seconds"], native_elapsed)
        p.require(used < 300, "original_active_budget_exhausted")
        bound = p.bind(paths, scope, pins, "worker-b", used=used, native=native)
        p.create(paths.run / "resume-binding.json", bound)
        p.create(paths.run / "trusted/verified-pause.json", {"native": native, "audit": audit,
            "budget": budget, "used_active_seconds": used, "idle_wait_required": False})


def validate_product_dispatch(global_paths, task, trigger):
    response = trigger.get("product_http", {})
    action = "pause" if task.kind == "main" else "cancel"
    p.require(response.get("status") == 202 and response.get("principal") == task.owner
        and response.get("task_id") == task.task_id and response.get("operation") == action,
        "owner_product_control_response_missing")
    request_id = response.get("request_id")
    rows = [row for _, _, row in p.jsonl(global_paths.run / ("backend-" + task.kind) / "control.jsonl")]
    selected = [row for row in rows if row.get("request_id") == request_id]
    p.require(sum(row.get("kind") == "authorization" and row.get("decision") == "allow"
        and row.get("principal_id") == task.owner and row.get("task_id") == task.task_id
        and row.get("operation") == action for row in selected) == 1, "actual_owner_authorization_missing")
    p.require(sum(row.get("kind") == "worker_control_dispatch_attempt" and row.get("action") == action
        and row.get("task_id") == task.task_id and row.get("attempt_id") == "worker-a" for row in selected) == 1,
        "backend_worker_dispatch_source_missing")
    p.require(any(row.get("kind") == "response" and row.get("status") == 202
        and row.get("body") == response.get("body") and row.get("connection_id") == response.get("connection_id")
        for row in selected), "actual_product_response_not_in_journal")
    worker = [row for _, _, row in p.jsonl(task.paths.run / "attempts/worker-a/private/worker-control.jsonl")]
    p.require(any(row.get("kind") in {"control_dispatch_attempt", "cancel_dispatch_attempt"}
        and row.get("request_id") == request_id and row.get("scope") == task.scope
        and row.get("attempt_id") == "worker-a" and row.get("signal") == trigger.get("signal") for row in worker),
        "actual_retained_child_signal_dispatch_missing")
    fixed = p.read(global_paths.run / "trusted" / (task.kind + "-worker-a.json"))[task.task_id]
    p.require(trigger.get("container_id") == fixed.get("id"), "control_container_identity_substituted")


def validate_http_source(global_paths, kind, response):
    raw = base64.b64decode(response["raw_body_base64"], validate=True)
    p.require(len(raw) == response["response_bytes"] and p.sha(raw) == response["body_sha256"]
        and p.decode(raw) == response["body"], "original_product_response_bytes_changed")
    journal = [row for _, _, row in p.jsonl(global_paths.run / ("backend-" + kind) / "control.jsonl")]
    selected = [row for row in journal if row.get("request_id") == response["request_id"]]
    p.require(any(row.get("kind") == "request" and row.get("principal_id") == response["principal"]
        and row.get("task_id") == response["task_id"] and row.get("operation") == response["operation"] for row in selected),
        "actual_product_request_source_missing")
    p.require(any(row.get("kind") == "response" and row.get("status") == response["status"]
        and row.get("body") == response["body"] and row.get("connection_id") == response["connection_id"] for row in selected),
        "actual_product_response_source_missing")


def validate_owner_reads(global_paths, task, *, phase="main-resume", attempt="worker-b", require_original=True):
    if task_candidate(task) == dsh.CANDIDATE:
        multi_user_dsh.assert_native_scope(task, attempt)
    rows = [row for _, _, row in p.jsonl(global_paths.run / ("clients/" + phase + "/http.jsonl"))
        if row.get("principal") == task.owner and row.get("task_id") == task.task_id]
    p.require(rows, "owner_product_read_evidence_missing")
    for row in rows:
        validate_http_source(global_paths, "main", row)
    for operation in ("state", "events", "artifacts"):
        selected = [row for row in rows if row["operation"] == operation and row["status"] == 200 and row.get("query") == ""]
        p.require(selected and all(row["body"].get("scope") == task.scope
            and row["body"].get("task_id") == task.task_id for row in selected), "owner_product_read_foreign_scope")
        if operation == "state":
            p.require(any(row["body"].get("attempt_id") == attempt
                and row["body"].get("process_observation", {}).get("availability") == "observed"
                and row["body"]["process_observation"].get("process_status") == "exited"
                and row["body"]["process_observation"].get("exit_code") == 0 for row in selected),
                "owner_final_process_observation_missing")
        elif operation == "artifacts":
            report = p.regular(task.paths.workspace / "report.json")
            p.require(any(any(item.get("path") == "report.json" and item.get("status") == "observed"
                and item.get("bytes") == len(report) and item.get("sha256") == p.sha(report)
                and base64.b64decode(item["content_base64"], validate=True) == report
                for item in row["body"].get("items", [])) for row in selected), "owner_product_artifact_not_actual_nonempty_report")
        else:
            for row in selected:
                value = row["body"]
                p.require(value.get("attempt_id") == attempt and value.get("cursor_domain") == {
                    "kind": "task_attempt_file", "task_id": task.task_id, "attempt_id": attempt}, "owner_event_cursor_domain_invalid")
                native = p.jsonl(task.paths.run / ("attempts/" + attempt + "/candidate/raw-events.jsonl"))
                p.require(value.get("records"), "owner_native_events_missing")
                for item in value["records"]:
                    reference = item["source"]
                    line = reference["line"]
                    p.require(reference.get("path") == "raw-events.jsonl" and 1 <= line <= len(native)
                        and item["record"] == native[line - 1][2]
                        and (multi_user_dsh.event_scope(task, item["record"]) if task_candidate(task) == dsh.CANDIDATE
                             else item["record"].get("scope") == task.scope)
                        and reference.get("sha256") == p.sha(native[line - 1][1].rstrip(b"\n")), "owner_event_not_original_native_line")
    if require_original:
        original = [row for row in rows if row.get("operation") == "events" and row.get("query") == "?attempt=worker-a&after=0"]
        p.require(original and all(row["status"] == 200 and row["body"].get("attempt_id") == "worker-a"
            and row["body"].get("scope") == task.scope for row in original), "original_attempt_events_not_available_after_resume")


def validate_effective_overlap(tasks, record):
    """Recompute overlap from original native intervals and trusted tool results.

    Both candidates and trusted tools ran in the same Docker VM clock domain;
    these timestamps are never compared with the macOS host clock. Collector
    prefix observations separately prove that the selected starts were live.
    """
    if tasks and all(task_candidate(task) == dsh.CANDIDATE for task in tasks):
        return multi_user_dsh.validate_overlap(tasks, record)
    native_intervals, finishes = {}, {}
    for task in tasks:
        for point in ("before", "after"):
            sample = record[point]["tasks"][task.task_id]
            ref = sample["activity_source"]
            rows = p.observed_prefix(task.paths.run, ref)
            starts = {}
            for line, _, row in rows:
                p.require(row.get("scope") == task.scope, "overlap_foreign_native_scope")
                event = row.get("data", {})
                if row.get("stream_mode") == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
                    if "input" in event and "result" not in event:
                        starts[event["id"]] = (line, event.get("name"))
                    elif any(key in event for key in ("result", "error", "interrupts")):
                        starts.pop(event["id"], None)
                if row.get("type") == "native_graph_drained":
                    starts.clear()
            p.reference(task.paths.run, ref, required_hash=True)
            p.require(any(line == ref["line"] and name in {"model", "tools"} for line, name in starts.values()),
                "overlap_prefix_has_no_live_native_task")
            tools = p.observed_prefix(task.paths.run, sample["tools_source"], empty=True)
            p.require(all(row.get("scope") == task.scope for _, _, row in tools), "overlap_foreign_tool_scope")
            actual = sum(row.get("phase") == "finish" and row.get("status") == 200 for _, _, row in tools)
            p.require(actual == sample["successful_tools"] and len(rows) == sample["native_prefix_lines"],
                "overlap_progress_projection_changed")
        before, after = (record[point]["tasks"][task.task_id] for point in ("before", "after"))
        p.require(after["native_prefix_lines"] > before["native_prefix_lines"]
            and after["successful_tools"] > before["successful_tools"], "overlap_missing_effective_progress")
        current, intervals = {}, []
        for line, encoded, row in p.jsonl(task.paths.run / "attempts/worker-a/candidate/raw-events.jsonl"):
            p.require(row.get("scope") == task.scope, "overlap_foreign_raw_scope")
            event = row.get("data", {})
            if row.get("stream_mode") != "tasks" or not isinstance(event, dict) or not isinstance(event.get("id"), str):
                continue
            if "input" in event and "result" not in event:
                current[event["id"]] = (p.instant(row["time"]), event.get("name"))
            elif any(key in event for key in ("result", "error", "interrupts")):
                opened = current.pop(event["id"], None)
                p.require(opened is not None and not event.get("error"), "overlap_unpaired_or_failed_native_task")
                if opened[1] in {"model", "tools"}:
                    intervals.append((opened[0], p.instant(row["time"])))
        native_intervals[task.task_id] = intervals
        finishes[task.task_id] = [p.instant(row["time"]) for _, _, row in p.jsonl(task.paths.run / "tool-events.jsonl")
            if row.get("scope") == task.scope and row.get("phase") == "finish" and row.get("status") == 200]
    first, second = tasks
    intersections = [(max(a, c), min(b, d)) for a, b in native_intervals[first.task_id]
        for c, d in native_intervals[second.task_id] if max(a, c) < min(b, d)]
    p.require(intersections and all(any(start <= finish <= end for start, end in intersections
        for finish in finishes[task.task_id]) for task in tasks), "tool_progress_not_within_actual_native_overlap")
    return {"native_clock_domain": "same_Docker_VM_UTC", "overlapping_native_intervals": len(intersections),
        "both_tasks_have_actual_tool_success_within_overlap": True}


def same_container_observation(first, second):
    """Compare complete observations, ignoring only Docker Mounts array order.

    Sorting canonical mount values preserves every field and repeated item.
    Canonical bytes also keep JSON booleans distinct from numeric values.
    The original observations are retained without mutation.
    """
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    normalized = []
    for value in (first, second):
        ordered = dict(value)
        if "mounts" in value:
            mounts = value["mounts"]
            if not isinstance(mounts, list) or not all(isinstance(mount, dict) for mount in mounts):
                return False
            ordered["mounts"] = sorted(mounts, key=p.canonical)
        normalized.append(ordered)
    return p.canonical(normalized[0]) == p.canonical(normalized[1])


def same_transition_record_subset(captured, record):
    """Recheck each captured field; only container mount order is immaterial."""
    return all(key in record and (same_container_observation(value, record[key])
        if key in {"before_stop", "after_stop"} else p.canonical(value) == p.canonical(record[key]))
        for key, value in captured.items())


def validate_sampling_transition(item, running, registry, sample):
    """Recheck original Docker terminal data; a host label is insufficient."""
    identity = item.get("container_id")
    selected = [row for row in running if row["id"] == identity]
    p.require(len(selected) == 1, "sampling_transition_not_in_original_running_inventory")
    original = selected[0]
    registered = registry["registrations"].get(identity)
    fields = ("id", "name", "image_id", "memory_limit", "cpu_nano", "trial_id", "role", "init_enabled")
    projection = lambda value: {key: value.get(key) for key in fields}
    p.require(registered is not None and same_container_observation(item.get("registration"), registered)
        and projection(original) == projection(registered)
        and registered["state"].get("Status") == "created" and registered["state"].get("Running") is False,
        "sampling_transition_exact_prestart_registration_missing")
    bench = original["role"] == "acceptance"
    p.require(bench or registered.get("init_enabled") is True, "sampling_transition_wrapper_init_missing")
    p.require(item.get("kind") == ("registered_bench_healthy_exit" if bench else "registered_host_requested_wrapper_stop")
        and item.get("native_child_health_claimed") is False, "sampling_transition_claim_invalid")
    record = registry["bench_completions" if bench else "retirements"].get(identity)
    p.require(record is not None, "sampling_transition_terminal_registry_missing")
    terminal = item.get("terminal_observation")
    p.require(isinstance(terminal, dict) and projection(terminal) == projection(original)
        and same_container_observation(terminal, record.get("after_stop")), "sampling_transition_actual_terminal_changed")
    state = terminal["state"]
    p.require(state.get("Status") == "exited" and state.get("Running") is False
        and type(state.get("ExitCode")) is int and state["ExitCode"] in ((0,) if bench else (0, 143))
        and type(state.get("Pid")) is int and state["Pid"] == 0 and state.get("Error") == ""
        and all(state.get(key) is False for key in ("OOMKilled", "Dead", "Paused", "Restarting")), "sampling_transition_unhealthy_exit")
    captured = item.get("completion" if bench else "retirement")
    if not bench:
        p.require(isinstance(captured, dict) and projection(captured["before_stop"]) == projection(original)
            and 0 <= captured["requested_monotonic"] <= sample["finished_monotonic"],
            "sampling_transition_wrapper_stop_not_previously_requested")
    if captured is not None:
        p.require(same_transition_record_subset(captured, record),
            "sampling_transition_registration_was_replaced")
    if item.get("absence_observed") is True:
        absence = item.get("absence_observation", {})
        p.require(absence.get("id") == identity and absence.get("id_absent") is True
            and record.get("deleted", {}).get("id") == identity and record["deleted"].get("id_absent") is True
            and captured is not None and 0 <= captured.get("removal_requested_monotonic", -1) <= sample["finished_monotonic"],
            "sampling_transition_exact_absence_or_prior_removal_missing")
    else:
        p.require(item.get("absence_observed") is False and "absence_observation" not in item,
            "sampling_transition_absence_claim_invalid")
    if "removing_observation" in item or "removing_observed_monotonic" in item:
        # A removing/Dead snapshot is evidence of the deletion race only.
        # The independently recorded exited snapshot remains the sole healthy
        # terminal source, and every other field must match exactly.
        removing = item.get("removing_observation")
        p.require(isinstance(removing, dict) and isinstance(removing.get("state"), dict)
            and removing["state"].get("Status") == "removing" and removing["state"].get("Dead") is True,
            "sampling_transition_removing_snapshot_invalid")
        restored = {**removing, "state": {**removing["state"], "Status": "exited", "Dead": False}}
        p.require(same_container_observation(restored, terminal), "sampling_transition_removing_terminal_changed")
        absence = item.get("absence_observation", {})
        stderr = absence.get("stderr")
        exact_absence = isinstance(stderr, str) and re.fullmatch(
            r"(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*" + re.escape(identity),
            stderr.strip(), flags=re.IGNORECASE)
        moments = [sample.get("started_monotonic"), captured.get("removal_requested_monotonic"),
            item.get("removing_observed_monotonic"), absence.get("observed_monotonic"), sample.get("finished_monotonic")]
        p.require(item.get("absence_observed") is True
            and type(absence.get("returncode")) is int and absence["returncode"] == 1
            and isinstance(absence.get("stdout"), str) and absence["stdout"].strip() == "[]" and exact_absence
            and all(type(value) in (int, float) and math.isfinite(value) for value in moments)
            and 0 <= moments[0] <= moments[2] <= moments[3] <= moments[4]
            and 0 <= moments[1] <= moments[2]
            and (bench or (type(captured.get("requested_monotonic")) in (int, float)
                and math.isfinite(captured["requested_monotonic"])
                and 0 <= captured["requested_monotonic"] <= moments[1]))
            and captured.get("deleted", {}).get("id") == identity
            and captured["deleted"].get("id_absent") is True
            and isinstance(captured["deleted"].get("observed_at"), str) and isinstance(absence.get("observed_at"), str)
            and p.instant(captured["deleted"]["observed_at"]) <= p.instant(absence["observed_at"]),
            "sampling_transition_removing_exact_absence_unproven")
    commands = item.get("commands", {})
    p.require(set(commands) == {"stats", "top"}, "sampling_transition_original_commands_missing")
    names = "(?:" + "|".join(re.escape(name) for name in (identity, original["name"], original["name"].lstrip("/"))) + ")"
    known_error = re.compile(r"(?:error(?: response from daemon)?:\s*)?(?:container\s+" + names
        + r"\s+is not running|no such (?:object|container):\s*" + names + r")", re.IGNORECASE)
    top = commands["top"]
    eof_at_recorded_exit = (top.get("returncode") == 1 and isinstance(top.get("stdout"), str)
        and not top["stdout"].strip() and isinstance(top.get("stderr"), str)
        and known_error.fullmatch(top["stderr"].strip()) is not None)
    for command, response in commands.items():
        code, stdout, stderr = response["returncode"], response["stdout"], response["stderr"]
        p.require(type(code) is int and isinstance(stdout, str) and isinstance(stderr, str), "sampling_transition_command_shape_invalid")
        if code:
            eof = command == "stats" and eof_at_recorded_exit and stderr.strip() == "EOF"
            p.require(code == 1 and not stdout.strip() and (known_error.fullmatch(stderr.strip()) or eof),
                "sampling_transition_unknown_Docker_error")
        else:
            p.require(not stderr.strip(), "sampling_transition_unexplained_Docker_stderr")
            if command == "stats":
                rows = [p.decode(line.encode()) for line in stdout.splitlines()]
                p.require(not rows or (len(rows) == 1 and rows[0].get("ID", rows[0].get("Container")) == identity),
                    "sampling_transition_stats_identity_changed")
    return identity


def validate_inventory_absence(item, registry):
    """A partial docker inspect is valid only for already registered removals."""
    p.require(item.get("kind") == "registered_removal_between_list_and_inspect"
        and item.get("native_child_health_claimed") is False and item.get("inspect_returncode") == 1,
        "inventory_absence_claim_invalid")
    listed, inspected = item["listed_ids"], item["inspected_ids"]
    p.require(isinstance(listed, list) and isinstance(inspected, list)
        and len(set(listed)) == len(listed) and len(set(inspected)) == len(inspected)
        and all(isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity) for identity in listed)
        and not set(inspected) - set(listed), "inventory_absence_identity_set_invalid")
    missing = set(listed) - set(inspected)
    p.require(item.get("container_id") in missing, "inventory_absence_target_not_missing")
    lines = item["inspect_stderr"].strip().splitlines()
    pattern = re.compile(r"(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*([0-9a-f]{64})", re.IGNORECASE)
    matched = [pattern.fullmatch(line.strip()) for line in lines]
    p.require(len(lines) == len(missing) and all(matched)
        and {match.group(1) for match in matched} == missing, "inventory_absence_unknown_Docker_error")
    fields = ("id", "name", "image_id", "memory_limit", "cpu_nano", "trial_id", "role", "init_enabled")
    projection = lambda value: {key: value.get(key) for key in fields}
    for identity in missing:
        registered = registry["registrations"].get(identity)
        p.require(registered is not None and registered["id"] == identity
            and registered["state"].get("Status") == "created"
            and registered["state"].get("Running") is False, "inventory_absence_exact_registration_missing")
        bench = registered["role"] == "acceptance"
        p.require(bench or registered.get("init_enabled") is True, "inventory_absence_wrapper_init_missing")
        record = registry["bench_completions" if bench else "retirements"].get(identity)
        p.require(record is not None and 0 <= record.get("removal_requested_monotonic", -1) <= item["observed_monotonic"]
            and record.get("deleted", {}).get("id") == identity and record["deleted"].get("id_absent") is True,
            "inventory_absence_prior_removal_or_final_absence_missing")
        terminal = record["after_stop"]
        p.require(projection(terminal) == projection(registered), "inventory_absence_terminal_identity_changed")
        state = terminal["state"]
        p.require(state.get("Status") == "exited" and state.get("Running") is False
            and type(state.get("ExitCode")) is int and state["ExitCode"] in ((0,) if bench else (0, 143))
            and type(state.get("Pid")) is int and state["Pid"] == 0 and state.get("Error") == ""
            and all(state.get(key) is False for key in ("OOMKilled", "Dead", "Paused", "Restarting")), "inventory_absence_terminal_unhealthy")
        if not bench:
            p.require(projection(record["before_stop"]) == projection(registered)
                and 0 <= record["requested_monotonic"] <= record["removal_requested_monotonic"],
                "inventory_absence_wrapper_stop_not_registered")
        if identity == item["container_id"]:
            p.require(same_container_observation(item.get("registration"), registered) and isinstance(item.get("removal"), dict)
                and same_transition_record_subset(item["removal"], record),
                "inventory_absence_original_registration_changed")


def validate_baseline_allocation(global_paths, task, plan, registry, lifecycle):
    p.require(plan.get("baseline_only") is True and plan.get("executed_tasks") == ["task-a"]
        and task.task_id == "task-a" and task.scope["experiment_id"] == "multi-user-baseline",
        "baseline_execution_scope_changed")
    active = p.decode(p.regular(global_paths.run / "main-active-containers.json"))
    running = [row for row in active if row["state"]["Running"]]
    roles = [row["role"] for row in running]
    p.require(len(running) == 5 and {role: roles.count(role) for role in roles}
        == {"worker": 1, "tools": 1, "model-proxy": 1, "fixtures": 1, "backend": 1}
        and sum(row["memory_limit"] for row in running) == 3 * 1024**3
        and sum(row["cpu_nano"] for row in running) == 1750000000,
        "baseline_five_container_small_quota_allocation_missing")
    p.require(all(row.get("init_enabled") is True for row in running), "baseline_actual_wrapper_init_missing")
    workers = [row for row in registry["registrations"].values() if row["role"] == "worker"]
    if task_candidate(task) == dsh.CANDIDATE:
        surface = p.read(task.paths.run / "trusted/surface-container.json")
        calibration_id = surface["before_start"]["id"]
        calibration = [row for row in workers if row["id"] == calibration_id]
        p.require(len(calibration) == 1 and same_container_observation(calibration[0], surface["before_start"]),
            "baseline_DSH_calibration_exact_registration_missing")
        workers = [row for row in workers if row["id"] != calibration_id]
    p.require(len(workers) == 1, "baseline_started_extra_worker")
    worker = workers[0]
    trusted = p.read(global_paths.run / "trusted/main-worker-a.json")
    p.require(set(trusted) == {"task-a"} and trusted["task-a"]["id"] == worker["id"]
        and any(row["id"] == worker["id"] for row in running)
        and worker["image_id"] == plan["pins"]["worker_image_id"]
        and worker["memory_limit"] == 2 * 1024**3 and worker["cpu_nano"] == 750000000
        and worker.get("init_enabled") is True,
        "baseline_exact_worker_or_quota_changed")
    config = load_config()
    def host_path(path):
        return str(config.host_data_root / path.relative_to(config.container_data_root))
    def mounts(row):
        p.require(len({item["Destination"] for item in row["mounts"]}) == len(row["mounts"]), "baseline_duplicate_mount")
        return {item["Destination"]: (item["Source"], item["RW"]) for item in row["mounts"]}
    target = task.paths.fixture / "attempts/worker-a"
    expected = {"/state": (host_path(task.paths.state), True), "/workspace": (host_path(task.paths.workspace), True),
        "/evidence": (host_path(task.paths.run / "attempts/worker-a/candidate"), True),
        "/worker-private": (host_path(task.paths.run / "attempts/worker-a/private"), True),
        "/control/policy.json": (host_path(target / "worker-policy.json"), False),
        "/control/worker.token": (host_path(target / "worker.token"), False),
        "/config/request.json": (host_path(target / "request.json"), False),
        "/config/lifecycle.json": (host_path(target / "lifecycle.json"), False),
        "/config/scope.json": (host_path(task.paths.fixture / "scope.json"), False),
        "/config/scope.token": (host_path(task.paths.fixture / "scope.token"), False),
        (dsh.TOKEN if task_candidate(task) == dsh.CANDIDATE else "/config/model-run.token"): (host_path(task.paths.budget / "run.token"), False)}
    if task_candidate(task) == dsh.CANDIDATE:
        expected["/config/lifecycle-state.json"] = (host_path(target / "lifecycle-state.json"), False)
    p.require(mounts(worker) == expected, "baseline_original_I24_material_mounts_changed")
    p.require(len(worker["networks"]) == 1, "baseline_worker_network_changed")
    for role, destination, expected_source in (("tools", "/workspace", host_path(task.paths.workspace)),
            ("model-proxy", "/budget", host_path(task.paths.budget))):
        selected = next(row for row in running if row["role"] == role)
        p.require(set(selected["networks"]) == set(worker["networks"])
            and mounts(selected)[destination] == (expected_source, True), "baseline_tools_or_proxy_cross_bound")
    p.require(all("MODEL_API_KEY" not in row["environment_names"] for row in running if row["role"] != "model-proxy"),
        "baseline_remote_secret_outside_proxy")
    retired = [row for row in lifecycle if row.get("before_stop", {}).get("id") == worker["id"]]
    p.require(len(retired) == 1 and retired[0]["after_stop"]["state"]["Running"] is False
        and retired[0]["deleted"].get("id") == worker["id"] and retired[0]["deleted"].get("id_absent") is True,
        "baseline_exact_worker_retirement_missing")
    for row in (retired[0]["before_stop"], retired[0]["after_stop"]):
        p.require(all(row.get(key) == worker.get(key) for key in ("id", "image_id", "memory_limit", "cpu_nano", "role", "trial_id", "init_enabled"))
            and not any(row["state"].get(key) for key in ("OOMKilled", "Dead", "Error")), "baseline_worker_runtime_unhealthy")
    stopped = retired[0]["after_stop"]["state"]
    p.require(stopped.get("Status") == "exited" and stopped.get("Running") is False
        and type(stopped.get("ExitCode")) is int and stopped["ExitCode"] in (0, 143)
        and stopped.get("Pid") == 0 and all(stopped.get(key) is False for key in ("OOMKilled", "Dead", "Paused", "Restarting"))
        and stopped.get("Error") == "", "baseline_wrapper_retirement_not_normal")


def validate_host_resources(global_paths, *, baseline_task=None):
    plan = p.read(global_paths.run / "plan.json")
    pins = plan["pins"]
    host = p.read(global_paths.run / "host-result.json")
    baseline = plan.get("baseline_only") is True
    p.require(host.get("baseline_only") is baseline and (baseline_task is not None) is baseline,
        "baseline_resource_mode_mismatch")
    p.require(host.get("passed") is True and host.get("sampling_errors") == []
        and host.get("resource_overages") == [] and host.get("errors") == [], "host_execution_or_sampling_failed")
    samples = p.jsonl(global_paths.run / "resource-samples.jsonl")
    p.require(len(samples) >= 2, "resource_samples_missing")
    registry = p.read(global_paths.run / "container-transition-registry.json")
    p.require(all(item.get("role") == "acceptance" or item.get("init_enabled") is True
        for item in registry["registrations"].values()), "registered_wrapper_init_missing")
    for item in registry["inventory_absences"]:
        validate_inventory_absence(item, registry)
    previous = None
    for _, _, row in samples:
        p.require("error" not in row, "resource_sampling_failed")
        tick = row["started_monotonic"]
        p.require(previous is None or 0 < tick - previous <= 5, "resource_sampling_gap")
        p.require(0 <= row["finished_monotonic"] - tick <= 5, "resource_sampling_duration")
        previous = tick
        containers = row["docker_observations"]
        p.require(len({item["id"] for item in containers}) == len(containers), "duplicate_container_observation")
        caps = {"worker": (2 * 1024**3, 750000000), "acceptance": (512 * 1024**2, 500000000),
            **{role: (256 * 1024**2, 250000000) for role in ("tools", "model-proxy", "fixtures", "backend")}}
        p.require(all(item.get("trial_id") == plan["trial_id"] and item.get("role") in caps
            and (item["memory_limit"], item["cpu_nano"]) == caps[item["role"]] for item in containers),
            "foreign_container_or_unfrozen_role_caps")
        p.require(all(item["role"] == "acceptance" or item.get("init_enabled") is True for item in containers),
            "actual_wrapper_init_missing")
        running = [item for item in containers if item["state"]["Running"]]
        p.require(all(not item["state"].get("OOMKilled") and not item["state"].get("Dead")
            and not item["state"].get("Error") for item in containers), "container_runtime_health_failed")
        p.require(all(item["image_id"] == pins["worker_image_id" if item["role"] == "worker"
            else "bench_image_id" if item["role"] == "acceptance" else "services_image_id"] for item in containers),
            "actual_container_image_differs_from_frozen_pin")
        p.require(sum(item["memory_limit"] for item in running) <= 6 * 1024**3
            and sum(item["cpu_nano"] for item in running) <= 3000000000, "actual_allocation_exceeds_total")
        if baseline:
            p.require(sum(item["memory_limit"] for item in running) <= 3 * 1024**3
                and sum(item["cpu_nano"] for item in running) <= 1750000000, "baseline_allocation_exceeds_single_task_plan")
        workers = [item for item in running if item["role"] == "worker"]
        p.require(len(workers) <= 2 and not (workers and any(item["role"] == "acceptance" for item in running)),
            "worker_or_bench_concurrency_exceeds_plan")
        p.require(not baseline or len(workers) <= 1, "baseline_concurrent_second_worker")
        p.require(all(item["memory_limit"] == 2 * 1024**3 and item["cpu_nano"] == 750000000 for item in workers),
            "worker_caps_differ")
        transitioned = [validate_sampling_transition(item, running, registry, row)
            for item in row.get("transition_observations", [])]
        p.require(len(set(transitioned)) == len(transitioned), "duplicate_sampling_transition")
        observed_processes = {item["container_id"] for item in row["process_observations"]}
        p.require(not observed_processes & set(transitioned)
            and observed_processes | set(transitioned) == {item["id"] for item in running}, "real_process_listing_incomplete")
        p.require(all(len(item["raw_docker_top"].splitlines()) >= 2 for item in row["process_observations"]),
            "running_container_has_no_actual_process_listing")
        p.require({item.get("ID", item.get("Container")) for item in row["containers"]}
            == observed_processes, "actual_docker_stats_inventory_incomplete")
    lifecycle = p.decode(p.regular(global_paths.run / "worker-lifecycle.json"))
    p.require(isinstance(lifecycle, list), "lifecycle_must_be_array")
    if baseline:
        validate_baseline_allocation(global_paths, baseline_task, plan, registry, lifecycle)
        return
    for kind in KINDS:
        active = p.decode(p.regular(global_paths.run / (kind + "-active-containers.json")))
        running = [item for item in active if item["state"]["Running"]]
        p.require(len(running) == 8 and sum(item["memory_limit"] for item in running) == 5905580032
            and sum(item["cpu_nano"] for item in running) == 3000000000, "eight_container_allocation_missing")
        roles = [item["role"] for item in running]
        p.require({name: roles.count(name) for name in set(roles)} == {"worker": 2, "tools": 2,
            "model-proxy": 2, "backend": 1, "fixtures": 1}, "active_service_inventory_differs")
        p.require(all(item.get("init_enabled") is True for item in running), "active_wrapper_init_missing")
        p.require(all("MODEL_API_KEY" not in item["environment_names"] for item in running
            if item["role"] != "model-proxy"), "remote_model_secret_outside_proxy")
        original = p.read(global_paths.run / "trusted" / (kind + "-worker-a.json"))
        replacement = p.read(global_paths.run / "trusted/main-worker-b.json") if kind == "main" else {}
        original_networks = []
        for task_id, item in original.items():
            p.require(item["role"] == "worker" and len(item["networks"]) == 1, "worker_network_binding_missing")
            original_networks.append(set(item["networks"]))
            services = {role: [value for value in running if value["role"] == role
                and set(value["networks"]) == set(item["networks"])] for role in ("tools", "model-proxy")}
            p.require(all(len(values) == 1 for values in services.values()), "user_services_not_on_distinct_network")
            def mount_source(container, destination):
                selected = [mount for mount in container["mounts"] if mount["Destination"] == destination]
                p.require(len(selected) == 1, "required_task_mount_missing")
                return selected[0]["Source"]
            token_path = dsh.TOKEN if plan.get("candidate") == dsh.CANDIDATE else "/config/model-run.token"
            p.require(mount_source(services["tools"][0], "/workspace") == mount_source(item, "/workspace")
                and mount_source(services["model-proxy"][0], "/budget") == str(Path(mount_source(item, token_path)).parent),
                "user_tool_workspace_or_budget_cross_bound")
            for attempt in ([item, replacement[task_id]] if kind == "main" else [item]):
                retired = [row for row in lifecycle if row.get("before_stop", {}).get("id") == attempt["id"]]
                p.require(len(retired) == 1 and retired[0]["after_stop"]["state"]["Running"] is False
                    and retired[0]["deleted"].get("id_absent") is True, "old_worker_container_retirement_unproven")
                stopped = retired[0]["after_stop"]["state"]
                p.require(stopped.get("Status") == "exited" and type(stopped.get("ExitCode")) is int
                    and stopped["ExitCode"] in (0, 143) and stopped.get("Pid") == 0
                    and all(stopped.get(key) is False for key in ("OOMKilled", "Dead", "Paused", "Restarting"))
                    and stopped.get("Error") == "", "wrapper_retirement_exit_unhealthy")
                p.require(attempt["image_id"] == pins["worker_image_id"]
                    and all(snapshot["image_id"] == pins["worker_image_id"] and not snapshot["state"].get("OOMKilled")
                        and not snapshot["state"].get("Dead") and not snapshot["state"].get("Error")
                        for snapshot in (retired[0]["before_stop"], retired[0]["after_stop"])),
                    "worker_pin_or_runtime_health_failed")
            if kind == "main":
                new = replacement[task_id]
                p.require(new["id"] != item["id"] and new["image_id"] == item["image_id"]
                    and set(new["networks"]) == set(item["networks"]), "replacement_container_identity_invalid")
                for destination in ("/state", "/workspace", token_path, "/config/scope.token"):
                    old_mount = [mount for mount in item["mounts"] if mount["Destination"] == destination]
                    new_mount = [mount for mount in new["mounts"] if mount["Destination"] == destination]
                    p.require(len(old_mount) == 1 and old_mount == new_mount, "original_task_material_mount_changed")
        p.require(len(original_networks) == 2 and not original_networks[0] & original_networks[1], "user_worker_networks_overlap")


def validate_cancel_identity(task, request, api, observation, native_api, native):
    base = {"scope": task.scope, "run_id": task.scope["variant"], "session_id": task.scope["session_id"]}
    common = {**base, "chain_id": task.scope["variant"], "attempt_id": "worker-a",
        "signal": "SIGUSR1", "action": "cancel", "native_api": "RunControl.request_drain",
        "reason": "experiment_cancel", "semantics": "cooperative_drain_at_superstep_boundary", "immediate_cancel": False}
    p.require(all(all(row.get(key) == value for key, value in common.items()) for row in (request, api, observation, native_api))
        and all(native.get(key) == value for key, value in base.items()), "native_cancel_full_identity_mismatch")
    request_id = request.get("request_id")
    p.require(isinstance(request_id, str) and request_id and all(row.get("request_id") == request_id
        for row in (api, observation, native_api)), "native_cancel_request_id_mismatch")
    p.require(request.get("api_executed") is False and api.get("api_executed") is True
        and observation.get("api_executed") is True and native_api.get("returned", "missing") is None
        and all(row.get("drain_requested") is True and row.get("drain_reason") == "experiment_cancel"
            for row in (api, native_api, native))
        and observation.get("native_exception_reason") == "experiment_cancel"
        and observation.get("native_status_after") == "drained"
        and observation.get("inflight_operations_may_finish_after_request") is True
        and observation.get("task_continuation_verified") is False, "native_cancel_drain_state_mismatch")


def cancel_native(task, trigger, tools, actual_ledger, *, seed, global_paths=None):
    if task_candidate(task) == dsh.CANDIDATE:
        p.require(global_paths is not None, "dsh_protected_product_evidence_required")
        return multi_user_dsh.cancel_native(task, trigger, tools, actual_ledger, seed=seed, global_paths=global_paths)
    root = task.paths.run / "attempts/worker-a/candidate"
    result = p.read(root / "result.json")
    p.require(result.get("status") == "drained" and result.get("native_status") == "drained"
        and result.get("stop_reason") == "native_cooperative_drain" and result.get("cancel_confirmed") is True
        and result.get("immediate_cancel") is False and not result.get("error"), "native_cancel_result_missing")
    observation = result.get("control_observation", {})
    p.require(observation.get("action") == "cancel" and observation.get("phase") == "native_drained_observed"
        and observation.get("cancel_confirmed") is True and observation.get("observed_active_tasks") == [], "native_cancel_observation_missing")
    native, native_ref = p.reference(root, observation.get("source"), required_hash=True)
    request, request_ref = p.reference(root, observation.get("request_source"), required_hash=True)
    api, api_ref = p.reference(root, observation.get("api_source"), required_hash=True)
    native_api, native_api_ref = p.reference(root, api.get("source"), required_hash=True)
    validate_cancel_identity(task, request, api, observation, native_api, native)
    p.validate_trigger(task.paths.run, task.scope, CANDIDATE, trigger)
    p.validate_handler_observation(task.paths.run, CANDIDATE, trigger, request_ref)
    p.require(native.get("type") == "native_graph_drained" and native.get("reason") == "experiment_cancel"
        and native.get("exception_type") == "langgraph.errors.GraphDrained"
        and native_api.get("type") == "native_control_api_returned"
        and native_api.get("arguments") == {"reason": "experiment_cancel"}
        and api.get("phase") == "native_api_returned" and api.get("native_api") == "RunControl.request_drain"
        and request.get("phase") == "requested" and request.get("signal") == "SIGUSR1"
        and request.get("admitted") is True and request.get("active_native_task_observed") is True
        and request.get("request_id") == api.get("request_id") == observation.get("request_id"), "native_cancel_chain_invalid")
    p.require(request_ref["line"] < api_ref["line"] and native_api.get("scope") == task.scope
        and native.get("scope") == task.scope, "native_cancel_scope_or_order_invalid")
    linked_request, linked_request_ref = p.reference(root, api.get("request_source"), required_hash=True)
    p.require(linked_request_ref == request_ref and linked_request == request, "native_cancel_api_request_link_invalid")
    active = {}
    for line, _, row in p.jsonl(root / "raw-events.jsonl"):
        p.require(row.get("scope") == task.scope, "foreign_cancel_raw_scope")
        if line >= native_api_ref["line"]:
            continue
        event = row.get("data", {})
        if row.get("stream_mode") == "tasks" and isinstance(event, dict) and isinstance(event.get("id"), str):
            if "input" in event and "result" not in event:
                active[event["id"]] = (line, event.get("name"))
            elif any(key in event for key in ("result", "error", "interrupts")):
                active.pop(event["id"], None)
    reported = {}
    for item in request.get("active_tasks", []):
        source, ref = p.reference(root, item.get("source"), required_hash=True)
        event = source.get("data", {})
        p.require(source.get("stream_mode") == "tasks" and "input" in event and "result" not in event
            and event.get("id") == item.get("id") and event.get("name") == item.get("name")
            and item["id"] not in reported, "cancel_reported_native_task_not_actual_start")
        reported[item["id"]] = (ref["line"], item["name"])
    p.require(active and active == reported, "cancel_handler_activity_not_actual_native_prefix")
    order = [p.instant(row["time"]) for row in (request, native_api, api, native, observation)]
    p.require(order == sorted(order) and native_api_ref["line"] < native_ref["line"], "cancel_native_causal_order_invalid")
    controls = [row for _, _, row in p.jsonl(root / "control-events.jsonl")]
    p.require(sum(row == observation for row in controls) == 1, "cancel_observation_not_in_original_log")
    complete, calls = audited_calls(tools, task.scope)
    p.require(complete and scoped_ledger(actual_ledger, task.scope), "cancel_audits_incomplete")
    partial = grade_tools(seed, task.scope, task.paths.workspace, actual_ledger, tools)
    audit_checks = {"tool_scope", "tool_audit_complete", "ledger_scope_and_counts", "business_audit_complete", "no_sensitive_effect"}
    p.require(all(check["passed"] for check in partial["checks"] if check["name"] in audit_checks),
        "cancel_business_effects_not_fully_accounted")
    confirmed = p.instant(observation["time"])
    p.require(not any(start.get("tool") in {"write", "write_file", "sensitive_export"}
        and p.instant(start["time"]) > confirmed for start, _ in calls), "write_dispatched_after_native_cancel_confirmation")
    p.require(not any(row.get("stream_mode") == "tasks" and isinstance(row.get("data"), dict)
        and "input" in row["data"] and "result" not in row["data"] and p.instant(row["time"]) > confirmed
        for _, _, row in p.jsonl(root / "raw-events.jsonl")), "native_task_started_after_cancel_confirmation")
    # Read the actual SQLite head, not just the candidate's checkpoint label.
    snapshot = p.read(root / "state-snapshot.json")
    checkpoint = snapshot.get("config", {}).get("configurable", {}).get("checkpoint_id")
    p.require(observation.get("checkpoint_source") == {"path": "state-snapshot.json",
        "sha256": p.sha(p.regular(root / "state-snapshot.json")), "json_pointer": "/config/configurable/checkpoint_id"}
        and checkpoint == observation.get("checkpoint_id") == result.get("checkpoint_id")
        and snapshot.get("config", {}).get("configurable", {}).get("thread_id") == task.scope["session_id"]
        and snapshot.get("next") == observation.get("native_next") == result.get("native_next")
        and snapshot.get("tasks") == observation.get("native_tasks") == result.get("native_tasks"), "cancel_checkpoint_source_invalid")
    database = task.paths.state / "checkpoints.sqlite"
    p.regular(database)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        head = db.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
            (task.scope["session_id"],)).fetchone()
    p.require(head == (checkpoint,) and isinstance(checkpoint, str) and checkpoint, "cancel_checkpoint_not_actual_sqlite_head")
    return {"native_request": request_ref, "native_drained": native_ref, "checkpoint_id": checkpoint,
        "semantics": "cooperative_drain_at_superstep_boundary", "immediate_cancel": False}


def validate_no_reissued_writes(paths, scope, paused):
    data = p.regular(paths.run / "tool-events.jsonl")
    saved = paused["audit"]
    p.require(p.sha(data[:saved["bytes"]]) == saved["sha256"], "trusted_tool_prefix_changed")
    tail = [p.decode(line) for line in data[saved["bytes"]:].splitlines()]
    p.require(saved["completed_write_ids"] and not any(row.get("tool") == "write" and row.get("phase") == "start"
        and row.get("arguments", {}).get("operation_id") in saved["completed_write_ids"] for row in tail),
        "completed_business_write_reissued_after_resume")
    p.audit_snapshot(paths.run / "tool-events.jsonl", scope)


def validate_cumulative_budget(paths, paused, final_budget):
    original = p.control_integrity(paths, "worker-a")
    replacement = p.control_integrity(paths, "worker-b")
    expected_token = original["model_token_sha256"]
    p.require(final_budget["token_sha256"] == paused["budget"]["token_sha256"]
        == replacement["model_token_sha256"] == expected_token
        and final_budget["cap"] == paused["budget"]["cap"] == 20
        and final_budget["rows"][:paused["budget"]["count"]] == paused["budget"]["rows"],
        "original_model_budget_token_cap_or_prefix_changed")


def validate_foreign_probe(global_paths, kind, probe, operation, *, connection_id=None):
    p.require(any(item.get("before") is not None and item.get("before") == item.get("after")
        and (item.get("response") or {}).get("status") == 403
        and item["response"].get("body") == {"error": {"code": "scope_denied"}}
        for item in probe["attempts"]), "foreign_operation_or_stable_window_failed")
    journal = [row for _, _, row in p.jsonl(global_paths.run / ("backend-" + kind) / "control.jsonl")]
    responses = []
    for attempt in probe["attempts"]:
        if attempt.get("response") is None:
            continue
        response = attempt["response"]
        p.require(response.get("principal") == "user-a" and response.get("task_id") == (
            "task-b" if kind == "main" else "task-b-cancel") and response.get("operation") == operation
            and response.get("status") == 403 and response.get("body") == {"error": {"code": "scope_denied"}},
            "foreign_request_target_or_rejection_invalid")
        validate_http_source(global_paths, kind, response)
        request_id = response["request_id"]
        p.require(any(row.get("kind") == "authorization" and row.get("request_id") == request_id
            and row.get("decision") == "deny" and row.get("reason") == "scope_mismatch" for row in journal), "foreign_authorization_source_missing")
        p.require(not any(row.get("request_id") == request_id and row.get("kind") in {
            "worker_control_dispatch_attempt", "worker_start_attempt"} for row in journal), "denied_request_reached_worker")
        responses.append(response)
    if connection_id is not None:
        p.require(isinstance(connection_id, str) and connection_id
            and any(row.get("connection_id") == connection_id for row in responses),
            "foreign_denial_not_on_reauthenticated_connection")


def validate_baseline_control(global_paths, task):
    rows = [row for _, _, row in p.jsonl(global_paths.run / "backend-main/control.jsonl")]
    starts = [row for row in rows if row.get("kind") == "worker_start_attempt"]
    acknowledgements = [row for row in rows if row.get("kind") == "worker_start_acknowledged"]
    p.require(len(starts) == len(acknowledgements) == 1
        and starts[0].get("task_id") == acknowledgements[0].get("task_id") == task.task_id
        and starts[0].get("attempt_id") == acknowledgements[0].get("attempt_id") == "worker-a"
        and starts[0].get("request_id") == acknowledgements[0].get("request_id"), "baseline_only_one_original_start_required")
    p.require(not any(row.get("kind") in {"worker_control_dispatch_attempt", "replacement_resume_material_verified",
        "worker_start_result_unknown"} for row in rows), "baseline_pause_cancel_resume_or_unknown_start_forbidden")
    requests = [row for row in rows if row.get("kind") == "request"]
    p.require(requests and all(row.get("task_id") == task.task_id and row.get("principal_id") == task.owner
        and row.get("operation") in {"state", "events", "artifacts"} for row in requests), "baseline_extra_product_task_or_control_request")
    private = [row for _, _, row in p.jsonl(task.paths.run / "attempts/worker-a/private/worker-control.jsonl")]
    actual = [row for row in private if row.get("kind") == "process_started"]
    p.require(len(actual) == 1 and actual[0].get("task_id") == task.task_id
        and actual[0].get("attempt_id") == "worker-a" and actual[0].get("scope") == task.scope
        and actual[0].get("request_id") == starts[0].get("request_id")
        and actual[0].get("identity") == acknowledgements[0].get("process_identity"), "baseline_actual_retained_child_start_missing")
    p.require(not any(row.get("kind") in {"control_dispatch_attempt", "cancel_dispatch_attempt", "signal_dispatch_attempted"}
        for row in private), "baseline_native_control_was_requested")


def validate_baseline_native_binding(task, pins, result):
    """Use the real entrypoint artifact and protected original controls.

    The pinned entrypoint writes provenance to entrypoint.json, not result.json.
    Return hashes of those exact bytes so a scope label cannot replace the
    candidate's actual factory/model/runtime and the bench-owned request chain.
    """
    if task_candidate(task) == dsh.CANDIDATE:
        return multi_user_dsh.baseline_binding(task, pins, result)
    paths, scope = task.paths, task.scope
    p.require(result.get("scope") == scope and result.get("run_id") == scope["variant"]
        and result.get("session_id") == scope["session_id"], "baseline_native_result_scope_changed")
    p.require(type(result.get("elapsed_seconds")) in (int, float) and 0 < result["elapsed_seconds"] <= 300,
        "baseline_native_active_budget_changed")
    integrity = p.control_integrity(paths, "worker-a")
    original = p.read(paths.fixture / "candidate-request.json")
    p.require(p.sha(p.regular(paths.fixture / "candidate-request.json"))
        == p.read(paths.run / "chain.json").get("original_request_sha256"), "baseline_original_request_changed")
    target = paths.fixture / "attempts/worker-a"
    request, binding = p.read(target / "request.json"), p.read(target / "lifecycle.json")
    lifecycle = {"mode": "new", "chain_id": scope["variant"], "attempt_id": "worker-a",
        "binding_file": "/config/lifecycle.json"}
    p.require(request == {**original, "timeout_seconds": 300, "lifecycle": lifecycle}
        and original.get("run_id") == scope["variant"] and original.get("session_id") == scope["session_id"],
        "baseline_original_native_request_or_mode_changed")
    p.require(binding.get("schema") == "harness-lab-lifecycle/1" and binding.get("candidate") == CANDIDATE
        and binding.get("mode") == "new" and binding.get("attempt_id") == "worker-a"
        and binding.get("chain_id") == pins["chain_id"] == scope["variant"] and binding.get("scope") == scope
        and binding.get("session_id") == scope["session_id"] and binding.get("checkpoint_ns") == ""
        and binding.get("recovery") is None, "baseline_original_native_binding_identity_changed")
    p.require(binding.get("budget") == {"active_limit_seconds": 300, "used_active_seconds": 0, "remaining_seconds": 300}
        and integrity.get("remaining_seconds") == 300, "baseline_original_native_active_budget_changed")
    root = paths.run / "attempts/worker-a/candidate"
    entrypoint_data = p.regular(root / "entrypoint.json")
    entrypoint = p.decode(entrypoint_data)
    p.require(isinstance(entrypoint, dict) and entrypoint.get("scope") == scope
        and entrypoint.get("run_id") == scope["variant"] and entrypoint.get("session_id") == scope["session_id"]
        and entrypoint.get("lifecycle") == lifecycle, "baseline_actual_entrypoint_scope_or_mode_changed")
    p.require(isinstance(entrypoint.get("provenance"), dict)
        and all(entrypoint["provenance"].get(key) == binding.get(key) == pins[key]
        for key in ("source_export_sha256", "adapter_export_sha256")), "baseline_actual_entrypoint_materials_changed")
    p.require(entrypoint.get("factory") == "deepagents.create_deep_agent"
        and entrypoint.get("checkpointer") == "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver"
        and entrypoint.get("durability") == "sync" and entrypoint.get("model") == pins["model_name"],
        "baseline_actual_entrypoint_runtime_changed")
    runtime_data = p.regular(root / "lifecycle-runtime.json")
    runtime_sha = p.digest(p.decode(runtime_data))
    p.require(runtime_sha == binding.get("runtime_sha256") == p.da_runtime(paths, pins, original),
        "baseline_actual_runtime_not_original_trusted_binding")
    return {"entrypoint_source": {"path": "entrypoint.json", "sha256": p.sha(entrypoint_data)},
        "runtime_source": {"path": "lifecycle-runtime.json", "sha256": p.sha(runtime_data)},
        "runtime_sha256": runtime_sha,
        **{key: integrity[key] for key in ("request_sha256", "binding_sha256", "scope_sha256")}}


def grade_baseline(args, global_paths, tasks):
    p.require(getattr(args, "baseline", False), "baseline_action_requires_explicit_mode")
    checks, details = [], {}
    task = next(task for task in tasks if task.kind == "main" and task.letter == "a")
    try:
        plan = p.read(global_paths.run / "plan.json")
        p.require(plan.get("baseline_only") is True and plan.get("executed_tasks") == ["task-a"]
            and not (global_paths.run / "scope-verdict.json").exists(), "baseline_must_not_claim_I24_capability")
        actual_ledger = ledger(global_paths, task)
        p.create(task.paths.run / "ledger.json", actual_ledger)
        events = [row for _, _, row in p.jsonl(task.paths.run / "tool-events.jsonl")]
        business = grade_tools(args.seed, task.scope, task.paths.workspace, actual_ledger, events)
        p.require(business["passed"], "baseline_original_business_task_failed")
        p.require(p.regular(task.paths.workspace / "task-marker.txt").decode()
            == task.task_id + ":" + task.scope["variant"] + "\n", "baseline_workspace_marker_changed")
        pins = trusted_pins(task)
        native = p.final_native(task.paths, task.scope, pins, "worker-a")
        result = p.read(task.paths.run / "attempts/worker-a/candidate/result.json")
        native_binding = validate_baseline_native_binding(task, pins, result)
        if task_candidate(task) == CANDIDATE:
            snapshot = p.read(task.paths.run / "attempts/worker-a/candidate/state-snapshot.json")
            checkpoint = snapshot.get("config", {}).get("configurable", {}).get("checkpoint_id")
            p.regular(task.paths.state / "checkpoints.sqlite")
            with sqlite3.connect((task.paths.state / "checkpoints.sqlite").as_uri() + "?mode=ro", uri=True) as db:
                head = db.execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1",
                    (task.scope["session_id"],)).fetchone()
            p.require(isinstance(checkpoint, str) and checkpoint and head == (checkpoint,) == (result.get("checkpoint_id"),),
                "baseline_final_checkpoint_not_actual_SQLite_head")
        integrity = p.control_integrity(task.paths, "worker-a")
        budget = p.budget_snapshot(task.paths.budget)
        p.require(budget["token_sha256"] == integrity["model_token_sha256"] and budget["cap"] == 20,
            "baseline_original_model_budget_changed")
        validate_owner_reads(global_paths, task, phase="main-baseline", attempt="worker-a", require_original=False)
        validate_baseline_control(global_paths, task)
        finished = p.read(global_paths.run / "clients/main-baseline/finished.json")
        p.require(finished.get("baseline_only") is True and finished.get("task_id") == "task-a"
            and 0 < finished["controller_elapsed_seconds"] <= 300, "baseline_actual_completion_clock_missing")
        details = {"business": business, "native": native, "native_binding": native_binding, "model_calls": budget["count"],
            "controller_elapsed_seconds": finished["controller_elapsed_seconds"]}
        checks.append({"name": "single_original_I24_task_business_native_budget_and_HTTP", "passed": True})
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        checks.append({"name": "single_original_I24_task_business_native_budget_and_HTTP", "passed": False, "error": str(error)})
    try:
        for inactive in tasks:
            if inactive.task_id == "task-a":
                continue
            no_effects = ledger(global_paths, inactive)
            p.create(inactive.paths.run / "baseline-inactive-ledger.json", no_effects)
            p.require(no_effects.get("request_count") == no_effects.get("write_count") == 0
                and all(no_effects.get(key) == [] for key in ("requests", "writes", "faults"))
                and not (inactive.paths.budget / "calls.sqlite").exists()
                and not (inactive.paths.run / "tool-events.jsonl").exists()
                and all(not list((inactive.paths.run / "attempts" / attempt / "candidate").iterdir()) for attempt in ATTEMPTS),
                "baseline_B_or_cancel_task_had_actual_activity")
        p.require(not list((task.paths.run / "attempts/worker-b/candidate").iterdir())
            and not (task.paths.fixture / "attempts/worker-b/request.json").exists(), "baseline_replacement_attempt_was_bound")
        checks.append({"name": "B_cancel_and_replacement_not_executed", "passed": True})
    except (OSError, ValueError, KeyError, TypeError) as error:
        checks.append({"name": "B_cancel_and_replacement_not_executed", "passed": False, "error": str(error)})
    try:
        validate_host_resources(global_paths, baseline_task=task)
        checks.append({"name": "same_I24_worker_image_and_075_CPU_single_task_allocation", "passed": True})
    except (OSError, ValueError, KeyError, TypeError) as error:
        checks.append({"name": "same_I24_worker_image_and_075_CPU_single_task_allocation", "passed": False, "error": str(error)})
    verdict = {"schema": "harness-lab-multi-user-baseline/1", "candidate": candidate_for(args),
        "baseline_only": True, "i24_capability_verified": False, "passed": all(row["passed"] for row in checks),
        "checks": checks, "task": details, "executed_tasks": ["task-a"],
        "limitations": ["Single A main task only; no concurrency, cross-user denial, cancel, pause, resume or reuse claim.",
            "Inactive B/cancel materials are prepared without SDK/model execution so the original fixed backend can be reused."]}
    p.create(global_paths.run / "baseline-verdict.json", verdict)
    print(json.dumps({"passed": verdict["passed"], "baseline_only": True, "i24_capability_verified": False,
        "verdict": str(global_paths.run / "baseline-verdict.json")}))
    return 0 if verdict["passed"] else 1


def grade(args, global_paths, tasks):
    p.require(p.read(global_paths.run / "plan.json").get("baseline_only") is False,
        "single_task_baseline_cannot_be_graded_as_I24")
    checks, results = [], {}
    for task in tasks:
        try:
            paths, scope = task.paths, task.scope
            actual_ledger = ledger(global_paths, task)
            p.create(paths.run / "ledger.json", actual_ledger)
            events = [row for _, _, row in p.jsonl(paths.run / "tool-events.jsonl")]
            p.require(p.regular(paths.workspace / "task-marker.txt").decode() == task.task_id + ":" + scope["variant"] + "\n",
                "original_task_workspace_marker_changed")
            trigger = p.read(paths.run / "trusted/control-trigger.json")
            validate_product_dispatch(global_paths, task, trigger)
            validate_http_source(global_paths, task.kind, trigger["product_http"])
            if task.kind == "main":
                paused = p.read(paths.run / "trusted/verified-pause.json")
                result = grade_tools(args.seed, scope, paths.workspace, actual_ledger, events)
                p.require(result["passed"], "original_business_task_failed")
                p.final_native(paths, scope, trusted_pins(task), "worker-b", paused=paused)
                validate_owner_reads(global_paths, task)
                validate_no_reissued_writes(paths, scope, paused)
                final_budget = p.budget_snapshot(paths.budget)
                validate_cumulative_budget(paths, paused, final_budget)
                results[task.task_id] = {"passed": True, "business": result, "model_calls": final_budget["count"]}
            else:
                p.control_integrity(paths, "worker-a")
                cancellation = cancel_native(task, trigger, events, actual_ledger, seed=args.seed, global_paths=global_paths)
                results[task.task_id] = {"passed": cancellation.get("passed", True), "native_cancel": cancellation,
                    "model_calls": p.budget_snapshot(paths.budget)["count"]}
            checks.append({"name": task.task_id, "passed": results[task.task_id]["passed"]})
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            results[task.task_id] = {"passed": False, "error": type(error).__name__ + ":" + str(error)}
            checks.append({"name": task.task_id, "passed": False})
    try:
        for kind in KINDS:
            output = global_paths.run / "clients" / (kind + "-active")
            overlap = p.read(output / "overlap.json")
            validate_effective_overlap([task for task in tasks if task.kind == kind], overlap)
            p.read(output / "finished.json")
            for operation in (("state", "events", "artifacts", "pause", "resume", "cancel") if kind == "main" else ("cancel",)):
                probe = p.read(output / ("foreign-" + operation + ".json"))
                validate_foreign_probe(global_paths, kind, probe, operation)
        reconnects = {}
        for letter in ("a", "b"):
            reconnect = p.read(global_paths.run / "clients/main-active" / ("reconnect-" + letter + ".json"))
            expected_task = next(task for task in tasks if task.kind == "main" and task.letter == letter)
            p.require(all(row.get("principal") == expected_task.owner and row.get("task_id") == expected_task.task_id
                and row.get("operation") == "state" and row.get("status") == 200
                and row.get("body", {}).get("scope") == expected_task.scope
                and row["body"].get("task_id") == expected_task.task_id and row["body"].get("attempt_id") == "worker-a"
                for row in (reconnect["first"], reconnect["second"])), "reconnect_original_task_identity_changed")
            validate_http_source(global_paths, "main", reconnect["first"])
            validate_http_source(global_paths, "main", reconnect["second"])
            eof, _ = p.reference(global_paths.run, reconnect["peer_eof"], required_hash=True)
            p.require(eof.get("kind") == "connection_peer_eof"
                and eof.get("connection_id") == reconnect["first"]["connection_id"]
                and reconnect["second"]["connection_id"] != reconnect["first"]["connection_id"], "actual_reconnect_source_missing")
            reconnects[letter] = reconnect
        validate_foreign_probe(global_paths, "main", p.read(global_paths.run / "clients/main-active/foreign-reconnect-state.json"),
            "state", connection_id=reconnects["a"]["second"]["connection_id"])
        p.read(global_paths.run / "clients/main-resume/finished.json")
        checks.append({"name": "product_boundary_overlap_reconnect", "passed": True})
    except (OSError, ValueError, KeyError, TypeError) as error:
        checks.append({"name": "product_boundary_overlap_reconnect", "passed": False, "error": str(error)})
    try:
        validate_host_resources(global_paths)
        checks.append({"name": "actual_container_resources_and_lifecycle", "passed": True})
    except (OSError, ValueError, KeyError, TypeError) as error:
        checks.append({"name": "actual_container_resources_and_lifecycle", "passed": False, "error": str(error)})
    result = {"schema": "harness-lab-multi-user-verdict/1", "candidate": candidate_for(args),
        "passed": all(row["passed"] for row in checks), "checks": checks, "tasks": results,
        "browser_isolation": "not_tested", "worker_reuse_supported": False,
        "resume_mechanism": "replacement_container_original_native_checkpoint",
        "limitations": ["Candidate-specific original native evidence is required; DSH immediate cancellation may remain incomplete.",
            "HTTP control acknowledgements are dispatch attempts; native evidence is checked separately.",
            "Worker wrapper journal and child share one UID/container; it is corroborating application evidence, not an OS security boundary."]}
    p.create(global_paths.run / "scope-verdict.json", result)
    print(json.dumps({"passed": result["passed"], "verdict": str(global_paths.run / "scope-verdict.json")}))
    return 0 if result["passed"] else 1


def main():
    environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "prepare-surface", "accept-surface", "bind-start", "exercise", "bind-replacements", "grade", "baseline-exercise", "baseline-grade"))
    parser.add_argument("--candidate", choices=(CANDIDATE, dsh.CANDIDATE), default=CANDIDATE)
    parser.add_argument("--trial-id", required=True, type=identifier)
    parser.add_argument("--date", required=True)
    parser.add_argument("--seed", type=int, choices=(101, 202, 303), default=101)
    parser.add_argument("--kind", choices=KINDS, default="main")
    parser.add_argument("--letter", choices=("a", "b"))
    parser.add_argument("--phase", choices=("active", "resume"), default="active")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8091")
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    if args.baseline:
        p.require(args.action in {"prepare", "prepare-surface", "accept-surface", "bind-start", "baseline-exercise", "baseline-grade"}
            and args.kind == "main" and (args.action not in {"bind-start", "prepare-surface", "accept-surface"} or args.letter == "a"),
            "baseline_B_cancel_pause_resume_actions_forbidden")
    else:
        p.require(args.action not in {"baseline-exercise", "baseline-grade"}, "baseline_action_requires_explicit_mode")
    _, global_paths, tasks = layout(args)
    if args.action in {"prepare-surface", "accept-surface"}:
        p.require(args.candidate == dsh.CANDIDATE and args.letter is not None, "dsh_surface_task_selection_required")
        task = next(task for task in tasks if task.kind == args.kind and task.letter == args.letter)
        getattr(multi_user_dsh, args.action.replace("-", "_"))(task)
    elif args.action == "prepare":
        prepare(args, global_paths, tasks)
    elif args.action == "bind-start":
        bind_start(args, tasks)
    elif args.action == "exercise":
        exercise(args, global_paths, tasks)
    elif args.action == "bind-replacements":
        bind_replacements(global_paths, tasks)
    elif args.action == "baseline-exercise":
        baseline_exercise(args, global_paths, tasks)
    elif args.action == "baseline-grade":
        return grade_baseline(args, global_paths, tasks)
    else:
        return grade(args, global_paths, tasks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
