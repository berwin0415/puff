"""Optional I24 process ownership boundary, independent of the active I22 runner.

Only a trusted fixture registers commands/paths. HTTP clients cannot start a
process or choose a PID, path, token, endpoint or owner. No process launcher is
connected by default; shared_uid supplies explicit Linux credentials for its
optional calibration. This module supplies no agent loop, model or native state.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import threading
from typing import Callable


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SCOPE = frozenset({"experiment_id", "variant", "user_id", "session_id"})
_MAX_FILE = 8 * 1024 * 1024


class BoundaryError(ValueError):
    """Safe fixed error code; arbitrary exception/command text is never public."""
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise BoundaryError("invalid_fixture", 400)


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value.lower() == "old":
        raise BoundaryError("invalid_fixture", 400)
    return value


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BoundaryError("invalid_fixture", 400)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or part.lower() == "old" for part in value.split("/")):
        raise BoundaryError("invalid_fixture", 400)
    return path.parts


def _absolute(value):
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or "\\" in value:
        raise BoundaryError("invalid_fixture", 400)
    if any(part in {".", ".."} or part.lower() == "old" for part in value.split("/")):
        raise BoundaryError("invalid_fixture", 400)
    path = Path(value)
    descriptor = _open_directory(path)
    os.close(descriptor)
    return path


def _open_directory(path):
    """Walk from / with directory fds; never follow a replaced ancestor symlink."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent(root, relative):
    parts = _relative(relative)
    descriptor = _open_directory(root)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def read_bound_file(root, relative, *, limit=_MAX_FILE, allow_append=False):
    """Sample an actual bounded regular file; log readers may accept append-only growth.

    The returned hash describes exactly the sampled prefix, never an unseen tail.
    Artifacts require a stable size/mtime across the read. Hard links are refused.
    """
    parent, name = _open_parent(root, relative)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise BoundaryError("unsupported_evidence_file")
        chunks, left = [], before.st_size
        while left:
            chunk = os.read(descriptor, min(left, 64 * 1024))
            if not chunk:
                raise BoundaryError("evidence_changed")
            chunks.append(chunk)
            left -= len(chunk)
        after = os.fstat(descriptor)
        if after.st_nlink != 1 or after.st_size < before.st_size or (not allow_append and (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)):
            raise BoundaryError("evidence_changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _separate(left, right):
    return left != right and left not in right.parents and right not in left.parents


def linux_process_identity(pid):
    """Actual Linux process start ticks, separate from mixed Docker PIDs metrics."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        # comm may contain spaces or parentheses; state is field 3.
        fields = text[text.rfind(")") + 2:].split()
        return {"pid": pid, "start_ticks": int(fields[19]), "source": "linux_proc_stat"}
    except (OSError, ValueError, IndexError):
        return {"pid": pid, "start_ticks": None, "source": "linux_process_identity_unavailable"}


@dataclass(frozen=True)
class AttemptSpec:
    attempt_id: str
    candidate: str
    argv: tuple[str, ...]
    environment: dict
    config_dir: Path
    evidence_dir: Path
    events_file: str
    readiness_file: str
    ready_match: dict
    native_ids: dict


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    owner: str
    scope: dict
    state_dir: Path
    workspace: Path
    artifacts: tuple[str, ...]
    attempts: dict[str, AttemptSpec]


@dataclass
class OwnedAttempt:
    spec: AttemptSpec
    process: object
    identity: dict
    started_at: str
    cancel_request_id: str | None = None


class TaskSupervisor:
    """One long-lived container, fresh child per task attempt, no scheduling loop.

    start() is trusted fixture/orchestrator code only; there is deliberately no
    public HTTP start/registration endpoint. The same object is retained until
    that exact child exits; user input can never substitute a numeric PID.
    """
    def __init__(self, tasks, audit: Callable[[dict], None], *, max_active=2,
                 popen_factory=None, identity_reader=linux_process_identity):
        if type(max_active) is not int or not 1 <= max_active <= 2:
            raise BoundaryError("invalid_fixture", 400)
        self.tasks = self._parse_tasks(tasks)
        self.audit, self.max_active = audit, max_active
        self.popen_factory, self.identity_reader = popen_factory, identity_reader
        self.owned: dict[tuple[str, str], OwnedAttempt] = {}
        self.current: dict[str, str] = {}
        self.lock = threading.RLock()

    @staticmethod
    def _parse_tasks(values):
        if not isinstance(values, list) or not values:
            raise BoundaryError("invalid_fixture", 400)
        tasks, roots, output_roots, config_roots = {}, [], [], []
        for item in values:
            _fields(item, {"task_id", "owner", "scope", "state_dir", "workspace", "artifacts", "attempts"})
            task_id, owner = _identifier(item["task_id"]), _identifier(item["owner"])
            _fields(item["scope"], _SCOPE)
            scope = {key: _identifier(value) for key, value in item["scope"].items()}
            if scope["user_id"] != owner or task_id in tasks:
                raise BoundaryError("invalid_fixture", 400)
            state, workspace = _absolute(item["state_dir"]), _absolute(item["workspace"])
            if not _separate(state, workspace):
                raise BoundaryError("invalid_fixture", 400)
            roots.extend([state, workspace])
            artifacts = item["artifacts"]
            if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 16 or any(not isinstance(item, str) for item in artifacts) or len(set(artifacts)) != len(artifacts):
                raise BoundaryError("invalid_fixture", 400)
            for relative in artifacts:
                _relative(relative)
            if not isinstance(item["attempts"], list) or not item["attempts"]:
                raise BoundaryError("invalid_fixture", 400)
            attempts = {}
            for value in item["attempts"]:
                _fields(value, {"attempt_id", "candidate", "argv", "environment", "config_dir", "evidence_dir", "events_file", "readiness_file", "ready_match", "native_ids"})
                attempt_id = _identifier(value["attempt_id"])
                if attempt_id in attempts or value["candidate"] not in {"deepseek_harness", "deepagents_langgraph"}:
                    raise BoundaryError("invalid_fixture", 400)
                argv = value["argv"]
                if not isinstance(argv, list) or not argv or len(argv) > 32 or any(not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 4096 for arg in argv) or not argv[0].startswith("/"):
                    raise BoundaryError("invalid_fixture", 400)
                environment = value["environment"]
                if not isinstance(environment, dict) or len(environment) > 64 or any(not isinstance(k, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", k) or not isinstance(v, str) or "\x00" in v or len(v) > 8192 for k, v in environment.items()):
                    raise BoundaryError("invalid_fixture", 400)
                if not isinstance(value["native_ids"], dict) or not value["native_ids"] or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in value["native_ids"].items()):
                    raise BoundaryError("invalid_fixture", 400)
                config, evidence = _absolute(value["config_dir"]), _absolute(value["evidence_dir"])
                output_roots.append(evidence)
                config_roots.append(config)
                if not all(_separate(evidence, root) and _separate(config, root) for root in [state, workspace]) or not _separate(config, evidence):
                    raise BoundaryError("invalid_fixture", 400)
                _relative(value["events_file"]); _relative(value["readiness_file"])
                match = value["ready_match"]
                # Existing I22 adapters emit this only after installing handlers.
                # This proves handler installation, not active/native safe pause.
                if match != {"phase": "pause_handler_installed", "signal": "SIGUSR2"}:
                    raise BoundaryError("invalid_fixture", 400)
                attempts[attempt_id] = AttemptSpec(attempt_id, value["candidate"], tuple(argv), dict(environment), config, evidence,
                    value["events_file"], value["readiness_file"], dict(match), dict(value["native_ids"]))
            tasks[task_id] = TaskSpec(task_id, owner, scope, state, workspace, tuple(artifacts), attempts)
        if any(not _separate(left, right) for index, left in enumerate(roots) for right in roots[index + 1:]):
            raise BoundaryError("overlapping_task_data", 400)
        if any(not _separate(left, right) for index, left in enumerate(output_roots) for right in output_roots[index + 1:]):
            raise BoundaryError("overlapping_attempt_evidence", 400)
        if any(not _separate(config, evidence) for config in config_roots for evidence in output_roots):
            raise BoundaryError("config_overlaps_attempt_evidence", 400)
        if any(not _separate(left, right) for index, left in enumerate(config_roots) for right in config_roots[index + 1:]):
            raise BoundaryError("overlapping_attempt_config", 400)
        # No task may write another task's protected config/evidence or state.
        for task in tasks.values():
            for attempt in task.attempts.values():
                if any(not _separate(root, other) for root in (attempt.config_dir, attempt.evidence_dir) for other in roots):
                    raise BoundaryError("overlapping_task_data", 400)
        return tasks

    def _record(self, kind, task, **values):
        self.audit({"kind": kind, "time": utc_now(), "task_id": task.task_id,
                    "scope": dict(task.scope), **values})

    def start(self, task_id, attempt_id):
        """Trusted API only. Does not invent a resume binding or checkpoint."""
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or attempt_id not in task.attempts:
                raise BoundaryError("unknown_fixture_attempt", 404)
            key = (task_id, attempt_id)
            if key in self.owned:
                raise BoundaryError("attempt_already_used")
            old_id = self.current.get(task_id)
            if old_id is not None and self.owned[(task_id, old_id)].process.poll() is None:
                raise BoundaryError("task_already_active")
            if sum(owned.process.poll() is None for owned in self.owned.values()) >= self.max_active:
                raise BoundaryError("active_task_limit", 429)
            spec = task.attempts[attempt_id]
            # Fresh output roots prevent a previous readiness marker/stream from
            # granting control to a later process that has not installed handlers.
            if any(spec.evidence_dir.iterdir()):
                raise BoundaryError("attempt_evidence_not_empty")
            if self.popen_factory is None:
                raise BoundaryError("process_launcher_not_connected", 501)
            clean_env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", **spec.environment}
            stdout = spec.evidence_dir / "supervisor-child.stdout"
            stderr = spec.evidence_dir / "supervisor-child.stderr"
            try:
                with stdout.open("xb") as out, stderr.open("xb") as err:
                    child = self.popen_factory(list(spec.argv), cwd=str(task.workspace), env=clean_env,
                        stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False,
                        start_new_session=True, close_fds=True)
            except OSError:
                raise BoundaryError("child_start_failed", 500) from None
            owned = OwnedAttempt(spec, child, self.identity_reader(child.pid), utc_now())
            self.owned[key] = owned
            self.current[task_id] = attempt_id
            self._record("process_started", task, attempt_id=attempt_id, identity=owned.identity,
                         candidate=spec.candidate, claim="new_child_process_not_same_process_reuse")
            return self.state(task_id)

    def _active(self, task_id, attempt_id=None):
        task = self.tasks[task_id]
        current = self.current.get(task_id)
        if current is None:
            raise BoundaryError("task_not_started")
        if attempt_id is not None and attempt_id != current:
            raise BoundaryError("stale_attempt")
        return task, self.owned[(task_id, current)]

    def state(self, task_id):
        with self.lock:
            task = self.tasks[task_id]
            if task_id not in self.current:
                return {"task_id": task_id, "scope": dict(task.scope), "attempt_id": None,
                        "process_status": "not_started", "native_state_complete": False,
                        "source": "supervisor_owned_subprocess_object"}
            _, owned = self._active(task_id)
            code = owned.process.poll()
            return {"task_id": task_id, "scope": dict(task.scope), "attempt_id": owned.spec.attempt_id,
                    "process_status": "running" if code is None else "exited", "exit_code": code,
                    "process_identity": dict(owned.identity), "started_at": owned.started_at,
                    "native_ids": dict(owned.spec.native_ids),
                    "native_ids_source": "trusted_fixture_binding_requires_separate_native_confirmation",
                    "native_state_complete": False, "source": "supervisor_owned_subprocess_object"}

    def _rows(self, task, owned, relative):
        try:
            data = read_bound_file(owned.spec.evidence_dir, relative, allow_append=True)
        except FileNotFoundError:
            return [], {"path": relative, "sampled_bytes": 0, "sampled_sha256": sha256(b""), "exists": False}
        complete = data[:data.rfind(b"\n") + 1]
        lines = complete.splitlines()
        if len(lines) > 32768:
            raise BoundaryError("event_count_bound")
        rows = []
        for index, line in enumerate(lines, 1):
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise BoundaryError("invalid_native_event_file") from None
            rows.append({"source": {"path": relative, "line": index, "sha256": sha256(line)}, "record": value})
        return rows, {"path": relative, "sampled_bytes": len(complete), "sampled_sha256": sha256(complete), "exists": True}

    def events(self, task_id, *, after=0, limit=1000):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise BoundaryError("invalid_cursor", 400)
        with self.lock:
            task, owned = self._active(task_id)
            rows, sample = self._rows(task, owned, owned.spec.events_file)
            if after > len(rows):
                raise BoundaryError("cursor_beyond_observed_events", 400)
            selected = rows[after:after + limit]
            return {"task_id": task_id, "attempt_id": owned.spec.attempt_id, "scope": dict(task.scope),
                    "cursor_domain": {"kind": "task_attempt_file", "task_id": task_id, "attempt_id": owned.spec.attempt_id},
                    "after": after, "next_cursor": after + len(selected), "observed_cursor": len(rows),
                    "records": selected, "sample": sample,
                    "claim": "actual_task_local_file_records_not_complete_realtime_native_state"}

    def artifacts(self, task_id):
        with self.lock:
            task = self.tasks[task_id]
            items = []
            total = 0
            for relative in task.artifacts:
                try:
                    data = read_bound_file(task.workspace, relative, limit=2 * 1024 * 1024)
                except FileNotFoundError:
                    items.append({"path": relative, "status": "not_created"})
                    continue
                total += len(data)
                if total > 8 * 1024 * 1024:
                    raise BoundaryError("artifact_total_bound")
                items.append({"path": relative, "status": "observed", "bytes": len(data),
                              "sha256": sha256(data), "content_base64": base64.b64encode(data).decode("ascii")})
            return {"task_id": task_id, "scope": dict(task.scope), "items": items,
                    "source": "actual_allowlisted_workspace_files"}

    def cancel(self, task_id, request_id, *, attempt_id=None):
        """Attempt a signal on the retained child; neither delivery nor cancel is confirmed."""
        with self.lock:
            task, owned = self._active(task_id, attempt_id)
            if owned.process.poll() is not None:
                raise BoundaryError("attempt_not_running")
            if owned.cancel_request_id is not None:
                raise BoundaryError("control_already_pending")
            markers, _ = self._rows(task, owned, owned.spec.readiness_file)
            ready = any(isinstance(row["record"], dict)
                and all(row["record"].get(k) == v for k, v in owned.spec.ready_match.items())
                and row["record"].get("scope") == task.scope
                and row["record"].get("chain_id") == task.scope["variant"]
                and row["record"].get("attempt_id") == owned.spec.attempt_id for row in markers)
            if not ready:
                raise BoundaryError("native_handlers_not_observed")
            current = self.identity_reader(owned.process.pid)
            if current.get("start_ticks") is None or current != owned.identity:
                raise BoundaryError("process_identity_changed")
            self._record("control_dispatch_attempt", task, request_id=request_id,
                         attempt_id=owned.spec.attempt_id, action="cancel", signal="SIGUSR1", identity=owned.identity)
            try:
                owned.process.send_signal(signal.SIGUSR1)
            except (OSError, ProcessLookupError):
                self._record("control_dispatch_failed", task, request_id=request_id, attempt_id=owned.spec.attempt_id)
                raise BoundaryError("control_dispatch_failed") from None
            owned.cancel_request_id = request_id
            # POSIX Popen.send_signal() can return normally after its internal
            # poll finds an exited child, or after swallowing ProcessLookupError.
            # No exception is evidence of an attempted call, never a signal ACK.
            self._record("signal_dispatch_attempted", task, request_id=request_id, attempt_id=owned.spec.attempt_id,
                         signal="SIGUSR1", signal_delivery_confirmed=False, native_cancel_confirmed=False)
            return {"task_id": task_id, "attempt_id": owned.spec.attempt_id, "request_id": request_id,
                    "status": "signal_dispatch_attempted", "signal_delivery_confirmed": False,
                    "native_cancel_confirmed": False,
                    "meaning": "await_actual_candidate_control_and_terminal_evidence"}
