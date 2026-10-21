"""One Docker Worker, one immutable Deep Agents I22 attempt, one retained child.

The private control service never creates containers or supplies Agent steps.
Replacement Workers receive a newly mounted, trusted, prompt-free I22 request.
Signal acknowledgements deliberately make no native completion claim.
"""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess

from runner.shared_backend import AuditJournal, validate_journal_location
from runner.shared_supervisor import (BoundaryError, OwnedAttempt, TaskSupervisor,
    _absolute, _fields, _identifier, linux_process_identity, read_bound_file, sha256, utc_now)


DA_ARGV = ("/opt/venv/bin/python", "/opt/materials/harness-lab/entrypoint.py", "--request", "/config/request.json")
ENVIRONMENT = frozenset({"MODEL_NAME", "MODEL_API_BASE_URL", "MODEL_RUN_TOKEN_FILE",
    "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"})


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def immutable_bytes(path, *, limit=1024 * 1024):
    path = Path(path)
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise BoundaryError("readonly_control_material_required", 400)
    return read_bound_file(path.parent, path.name, limit=limit)


def read_secret(path):
    token = immutable_bytes(path, limit=8192).decode().strip()
    if not token or not token.isascii() or len(token) > 512 or re.search(r"\s", token):
        raise BoundaryError("invalid_control_credential", 400)
    return token


def inventory(root, *, state=False):
    """Actual stable files, including SQLite/WAL; never a synthesized checkpoint."""
    result, total = {}, 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if state and relative in {".i22-chain.lock", "pause-index.json"}:
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or len(result) >= 4096:
            raise BoundaryError("unsupported_native_state_file")
        data = read_bound_file(root, relative, limit=128 * 1024 * 1024)
        total += len(data)
        if total > 256 * 1024 * 1024:
            raise BoundaryError("native_state_inventory_bound")
        result[relative] = {"bytes": len(data), "mode": stat.S_IMODE(info.st_mode), "sha256": sha256(data)}
    if state and "checkpoints.sqlite" not in result:
        raise BoundaryError("native_sqlite_missing")
    return result


def verify_resume_materials(task, request, binding):
    """Check the protected I22 binding against actual closed native materials.

    The original entrypoint still performs graph.aget_state and passes None to
    graph.astream. This guard neither repairs native files nor issues a prompt.
    """
    recovery = binding.get("recovery")
    if not isinstance(recovery, dict) or recovery.get("attempt_id") != "worker-a":
        raise BoundaryError("original_recovery_binding_required")
    data = read_bound_file(task.state_dir, "pause-index.json", limit=64 * 1024 * 1024)
    if sha256(data) != recovery.get("pause_index_sha256"):
        raise BoundaryError("protected_pause_index_changed")
    saved = json.loads(data)
    if not isinstance(saved, dict):
        raise BoundaryError("closed_safe_native_pause_required")
    if (saved.get("schema") != "harness-lab-pause-index/1" or saved.get("candidate") != "deepagents_langgraph"
            or saved.get("attempt_id") != "worker-a" or saved.get("chain_id") != task.scope["variant"]
            or saved.get("scope") != task.scope or saved.get("session_id") != task.scope["session_id"]
            or saved.get("checkpoint_ns") != ""
            or any(saved.get(key) is not True for key in ("saver_closed", "tools_closed", "safe_pause"))
            or any(saved.get(key) != 0 for key in ("observed_active_graph_tasks", "observed_active_http_calls", "observed_http_errors_or_unknown_results"))):
        raise BoundaryError("closed_safe_native_pause_required")
    for key in ("source_export_sha256", "adapter_export_sha256", "runtime_sha256"):
        if saved.get(key) != binding.get(key):
            raise BoundaryError("resume_runtime_identity_changed")
    for key in ("checkpoint_id", "exact_snapshot_sha256", "head_snapshot_sha256", "state_files_sha256", "workspace_files_sha256"):
        if saved.get(key) != recovery.get(key):
            raise BoundaryError("resume_native_reference_changed")
    if not isinstance(saved.get("checkpoint_id"), str) or not saved["checkpoint_id"]:
        raise BoundaryError("original_native_checkpoint_required")
    for field in ("exact_snapshot", "head_snapshot"):
        if sha256(canonical(saved.get(field))) != saved.get(field + "_sha256"):
            raise BoundaryError("native_snapshot_hash_mismatch")
    used = saved.get("total_active_seconds")
    budget = binding.get("budget", {})
    if (type(used) not in (int, float) or not math.isfinite(used)
            or budget.get("used_active_seconds", -1) < used
            or request.get("timeout_seconds") != budget.get("remaining_seconds")):
        raise BoundaryError("original_cumulative_budget_required")
    for field, root, native in (("state_files", task.state_dir, True), ("workspace_files", task.workspace, False)):
        actual = inventory(root, state=native)
        if actual != saved.get(field) or sha256(canonical(actual)) != recovery.get(field + "_sha256"):
            raise BoundaryError("closed_native_material_changed")
    return {"path": "pause-index.json", "sha256": sha256(data), "checkpoint_id": saved["checkpoint_id"],
            "source": "actual_I22_pause_index_and_closed_files", "native_resume_completed": False}


class SingleAttemptWorker(TaskSupervisor):
    """The only launch command is the pinned original DA entrypoint."""
    def __init__(self, policy, policy_sha256, journal, *, popen_factory=subprocess.Popen,
                 identity_reader=linux_process_identity):
        _fields(policy, {"schema", "worker_id", "mode", "task", "control_token_file", "private_root"})
        if policy["schema"] != "harness-lab-worker-control/1" or policy["mode"] not in {"new", "resume"}:
            raise BoundaryError("invalid_worker_policy", 400)
        self.worker_id = _identifier(policy["worker_id"])
        self.policy_sha256, self.mode = policy_sha256, policy["mode"]
        self.private_root = _absolute(policy["private_root"])
        if (journal.path.parent != self.private_root or self.private_root != Path("/worker-private")
                or policy["control_token_file"] != "/control/worker.token"):
            raise BoundaryError("private_worker_journal_required", 400)
        super().__init__([policy["task"]], journal, max_active=1,
                         popen_factory=popen_factory, identity_reader=identity_reader)
        validate_journal_location(self, journal.path)
        self.task = next(iter(self.tasks.values()))
        if len(self.task.attempts) != 1:
            raise BoundaryError("one_fixed_attempt_required", 400)
        self.spec = next(iter(self.task.attempts.values()))
        expected = "worker-a" if self.mode == "new" else "worker-b"
        if (self.spec.candidate != "deepagents_langgraph" or self.spec.attempt_id != expected
                or self.spec.argv != DA_ARGV or self.spec.config_dir != Path("/config")
                or self.task.state_dir != Path("/state") or self.task.workspace != Path("/workspace")
                or self.spec.evidence_dir != Path("/evidence")
                or self.spec.environment.keys() - ENVIRONMENT
                or self.spec.environment.get("MODEL_API_BASE_URL") != "http://model-proxy:8082/v1"
                or self.spec.environment.get("MODEL_RUN_TOKEN_FILE") != "/config/model-run.token"
                or not self.spec.environment.get("MODEL_NAME")):
            raise BoundaryError("fixed_DA_I22_worker_required", 400)
        # Never inherit the backend's credential or the remote provider key.
        self.control_token = read_secret(policy["control_token_file"])
        self.pending_control = None

    def describe(self):
        return {"schema": "harness-lab-worker-description/1", "worker_id": self.worker_id,
                "policy_sha256": self.policy_sha256, "task_id": self.task.task_id,
                "scope": self.task.scope, "attempt_id": self.spec.attempt_id, "mode": self.mode,
                "candidate": self.spec.candidate}

    def _bound_request(self):
        request = json.loads(immutable_bytes("/config/request.json"))
        binding = json.loads(immutable_bytes("/config/lifecycle.json"))
        scope = json.loads(immutable_bytes("/config/scope.json"))
        if not isinstance(request, dict) or not isinstance(binding, dict):
            raise BoundaryError("fixed_worker_request_binding_mismatch", 400)
        lifecycle = {"mode": self.mode, "chain_id": self.task.scope["variant"],
                     "attempt_id": self.spec.attempt_id, "binding_file": "/config/lifecycle.json"}
        if (request.get("lifecycle") != lifecycle or request.get("run_id") != self.task.scope["variant"]
                or request.get("session_id") != self.task.scope["session_id"] or scope != self.task.scope
                or request.get("tools_url") != "http://tools:8081" or request.get("tool_token_file") != "/config/scope.token"
                or request.get("workspace") != "/workspace" or request.get("state_dir") != "/state"
                or request.get("output_dir") != "/evidence"
                or request.get("candidate_options") != {"case": "tools", "trusted_scope_file": "/config/scope.json"}
                or binding.get("schema") != "harness-lab-lifecycle/1" or binding.get("candidate") != self.spec.candidate
                or any(binding.get(key) != value for key, value in lifecycle.items() if key != "binding_file")
                or binding.get("scope") != self.task.scope or binding.get("session_id") != self.task.scope["session_id"]):
            raise BoundaryError("fixed_worker_request_binding_mismatch", 400)
        budget = binding.get("budget", {})
        used, remaining = budget.get("used_active_seconds"), budget.get("remaining_seconds")
        if (budget.get("active_limit_seconds") != 300 or type(used) not in (int, float)
                or not math.isfinite(used) or not 0 <= used < 300 or type(remaining) is not int
                or not 1 <= remaining <= 300 - used or request.get("timeout_seconds") != remaining):
            raise BoundaryError("invalid_original_worker_budget", 400)
        for name in ("/config/model-run.token", "/config/scope.token"):
            read_secret(name)
        if self.mode == "resume":
            if "prompt" in request:
                raise BoundaryError("resume_prompt_forbidden", 400)
            original = verify_resume_materials(self.task, request, binding)
        else:
            if not isinstance(request.get("prompt"), str) or not request["prompt"] or used != 0 or binding.get("recovery") is not None:
                raise BoundaryError("invalid_original_new_request", 400)
            original = None
        return {"request_sha256": sha256(canonical(request)), "binding_sha256": sha256(canonical(binding)),
                "original_pause": original}

    def start_fixed(self, request_id):
        with self.lock:
            if self.current:
                raise BoundaryError("fixed_attempt_already_used")
            checked = self._bound_request()
            if any(self.spec.evidence_dir.iterdir()):
                raise BoundaryError("attempt_evidence_not_empty")
            env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", **self.spec.environment}
            # Candidate evidence must remain empty until the I22 entry opens it.
            try:
                out_fd = os.open(self.private_root / "candidate.stdout", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(out_fd, "wb") as out:
                    err_fd = os.open(self.private_root / "candidate.stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(err_fd, "wb") as err:
                        child = self.popen_factory(list(DA_ARGV), cwd="/workspace", env=env,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False,
                            start_new_session=True, close_fds=True)
            except OSError:
                raise BoundaryError("child_start_failed", 500) from None
            owned = OwnedAttempt(self.spec, child, self.identity_reader(child.pid), utc_now())
            self.owned[(self.task.task_id, self.spec.attempt_id)] = owned
            self.current[self.task.task_id] = self.spec.attempt_id
            self._record("process_started", self.task, request_id=request_id, worker_id=self.worker_id,
                attempt_id=self.spec.attempt_id, identity=owned.identity, mode=self.mode, **checked,
                native_resume_completed=False, claim="actual_new_process_in_separate_worker_container")
            return {**self.state(self.task.task_id), "request_id": request_id, "status": "child_started",
                    "native_resume_completed": False}

    def signal_control(self, action, request_id):
        with self.lock:
            if self.pending_control is not None:
                raise BoundaryError("control_already_pending")
            if action == "cancel":
                result = super().cancel(self.task.task_id, request_id, attempt_id=self.spec.attempt_id)
            elif action == "pause":
                task, owned = self._active(self.task.task_id, self.spec.attempt_id)
                if owned.process.poll() is not None:
                    raise BoundaryError("attempt_not_running")
                markers, _ = self._rows(task, owned, self.spec.readiness_file)
                if not any(isinstance(row["record"], dict) and all(row["record"].get(key) == value
                    for key, value in {**self.spec.ready_match, "scope": task.scope,
                                      "chain_id": task.scope["variant"], "attempt_id": self.spec.attempt_id}.items()) for row in markers):
                    raise BoundaryError("native_handlers_not_observed")
                actual = self.identity_reader(owned.process.pid)
                if actual.get("start_ticks") is None or actual != owned.identity:
                    raise BoundaryError("process_identity_changed")
                self._record("control_dispatch_attempt", task, request_id=request_id, action=action,
                    attempt_id=self.spec.attempt_id, signal="SIGUSR2", identity=owned.identity)
                try:
                    owned.process.send_signal(signal.SIGUSR2)
                except OSError:
                    raise BoundaryError("control_dispatch_failed") from None
                result = {"task_id": task.task_id, "attempt_id": self.spec.attempt_id,
                    "request_id": request_id, "status": "signal_dispatch_attempted",
                    "signal_delivery_confirmed": False, "native_pause_confirmed": False}
                self._record("signal_dispatch_attempted", task, action=action, **{key: value for key, value in result.items() if key != "task_id"})
            else:
                raise BoundaryError("operation_not_found", 404)
            self.pending_control = {"action": action, "request_id": request_id}
            return {**result, "semantics": "cooperative_drain_at_superstep_boundary", "immediate_cancel": False}

    def handle(self, token, action, method, body):
        if not isinstance(token, str) or not hmac.compare_digest(token, "Bearer " + self.control_token):
            raise BoundaryError("unauthenticated", 401)
        if action not in {"describe", "state", "events", "artifacts", "start", "pause", "cancel"}:
            raise BoundaryError("operation_not_found", 404)
        if method != ("GET" if action in {"describe", "state", "events", "artifacts"} else "POST"):
            raise BoundaryError("method_not_allowed", 405)
        if action in {"start", "pause", "cancel"}:
            _fields(body, {"request_id"})
            request_id = _identifier(body["request_id"])
            result = self.start_fixed(request_id) if action == "start" else self.signal_control(action, request_id)
            return 202, {**self.describe(), **result}
        if body:
            raise BoundaryError("request_fields_forbidden", 400)
        if action == "describe":
            return 200, self.describe()
        return 200, {**self.describe(), **getattr(self, action)(self.task.task_id)}


class WorkerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, worker):
        self.worker = worker
        super().__init__(address, WorkerHandler)


class WorkerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _handle(self):
        status, value = 500, {"error": {"code": "worker_error"}}
        try:
            worker = self.server.worker
            # Authenticate before reading any attacker-controlled body.
            header = self.headers.get("Authorization")
            if not isinstance(header, str) or not hmac.compare_digest(header, "Bearer " + worker.control_token):
                raise BoundaryError("unauthenticated", 401)
            if self.path.count("/") != 2 or not self.path.startswith("/control/") or "?" in self.path:
                raise BoundaryError("route_not_found", 404)
            length = self.headers.get("Content-Length", "0")
            if self.headers.get("Transfer-Encoding") is not None or not re.fullmatch(r"[0-9]{1,4}", length) or int(length) > 4096:
                raise BoundaryError("invalid_body", 400)
            raw = self.rfile.read(int(length))
            body = json.loads(raw) if raw else {}
            status, value = worker.handle(header, self.path[len("/control/"):], self.command, body)
        except BoundaryError as error:
            status, value = error.status, {"error": {"code": error.code}}
        except (OSError, ValueError, KeyError, TypeError):
            status, value = 409, {"error": {"code": "worker_material_unavailable"}}
        data = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    do_GET = _handle
    do_POST = _handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="/control/policy.json")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    data = immutable_bytes(args.policy)
    policy = json.loads(data)
    private_root = _absolute(policy["private_root"])
    with_journal = AuditJournal(private_root / "worker-control.jsonl")
    worker = None
    try:
        worker = SingleAttemptWorker(policy, sha256(data), with_journal)
        with WorkerHTTPServer(("0.0.0.0", args.port), worker) as server:
            server.serve_forever()
    finally:
        # Never translate forced service/container teardown to native success.
        if worker is not None and any(owned.process.poll() is None for owned in worker.owned.values()):
            worker._record("service_exit_with_live_child", worker.task, native_pause_confirmed=False,
                           native_cancel_confirmed=False)
        with_journal.close()


if __name__ == "__main__":
    try:
        main()
    except (BoundaryError, OSError, ValueError, KeyError, TypeError):
        raise SystemExit("worker_control_start_failed") from None
