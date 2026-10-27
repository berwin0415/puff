"""可选的 I24 root 所有 UID 校准；双 Docker Worker 不依赖此项。

不提供公开注册或启动 API，此处不接受默认 I22 适配器。
候选框架共享入口仍是尚未实现的单独集成。
此模块不是 Agent 运行时、队列、池或生产沙箱。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time

from runner.shared_backend import validate_journal_location
from runner.shared_supervisor import (BoundaryError, OwnedAttempt, TaskSupervisor, _absolute, _fields,
    _open_directory, linux_process_identity, read_bound_file, utc_now)


_HASH = re.compile(r"[a-f0-9]{64}\Z")
_DNS_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_ENV_FIELDS = frozenset({"MODEL_NAME", "MODEL_API_BASE_URL", "MODEL_RUN_TOKEN_FILE", "HARNESS_LIFECYCLE_IMAGE_ID",
    "TSX_TSCONFIG_PATH", "DSH_TELEMETRY_DISABLED", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"})
_CANDIDATE_ENTRIES = {
    "deepagents_langgraph": ("/opt/venv/bin/python", "/opt/materials/harness-lab/shared-entrypoint.py"),
    "deepseek_harness": ("/usr/local/bin/node", "--import", "tsx/esm", "/opt/materials/harness-lab/shared-goal-entrypoint.ts"),
}


def _status(pid):
    """读取真实 Linux 状态字段，不能根据 Fixture 合成子进程身份。"""
    values = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values


def _directory(path, uid, gid, mode):
    descriptor = _open_directory(path)
    try:
        info = os.fstat(descriptor)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
            raise BoundaryError("shared_directory_permissions_invalid", 400)
    finally:
        os.close(descriptor)


def _ancestors(path):
    """只有 root 可替换受保护父目录；校准允许 root 所有且带 sticky 位的 /tmp。"""
    for parent in Path(path).parents:
        descriptor = _open_directory(parent)
        try:
            info = os.fstat(descriptor)
            writable = stat.S_IMODE(info.st_mode) & 0o022
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid != 0 or writable and not sticky_root:
                raise BoundaryError("shared_unprotected_ancestor", 400)
        finally:
            os.close(descriptor)


def _file(path, uid, gid, mode, *, limit=65536):
    data = read_bound_file(path.parent, path.name, limit=limit)
    info = path.lstat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
        raise BoundaryError("shared_file_permissions_invalid", 400)
    return data


def _uids(status):
    values = tuple(int(item) for item in status["Uid"].split())
    if len(values) != 4:
        raise ValueError("无效 UID 字段")
    return values


def uid_processes(uid):
    """对真实 UID 进程作有界观测，不证明任意派生进程均被约束。"""
    found = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            current = _status(pid)
            if uid in _uids(current):
                found.append({"pid": pid, "state": current["State"].split()[0]})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, KeyError):
            raise BoundaryError("shared_uid_observation_unavailable") from None
    return sorted(found, key=lambda row: row["pid"])


@dataclass(frozen=True)
class TaskIdentity:
    uid: int
    gid: int
    root: Path
    entries: dict


class RootTaskSupervisor(TaskSupervisor):
    """每次尝试启动新的非 root 进程，应用层撤销不可逆。

    前提为 root 独占的策略/材料及 Linux 权限。
    此处不隔离网络命名空间，也不证明原生暂停、取消或恢复。"""
    def __init__(self, tasks, identities, journal, *, private_root, max_active=2,
                 allow_calibration=False, popen_factory=subprocess.Popen, handoff_guard=None):
        if os.geteuid() != 0 or _status(os.getpid()).get("NoNewPrivs") != "1":
            raise BoundaryError("shared_root_and_no_new_privileges_required", 400)
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise BoundaryError("shared_pidfd_required", 400)
        if type(allow_calibration) is not bool:
            raise BoundaryError("invalid_fixture", 400)
        self.private_root = _absolute(str(private_root))
        _directory(self.private_root, 0, 0, 0o700)
        _ancestors(self.private_root)
        if not hasattr(journal, "path") or journal.path.parent != self.private_root:
            raise BoundaryError("shared_private_journal_required", 400)
        _file(journal.path, 0, 0, 0o600, limit=8 * 1024 * 1024)
        self.real_popen, self.allow_calibration = popen_factory, allow_calibration
        self.handoff_guard = handoff_guard
        self.revoked = set()
        super().__init__(tasks, journal, max_active=max_active, popen_factory=self._spawn)
        validate_journal_location(self, journal.path)
        self.identities = self._identities(identities)

    def _identities(self, values):
        if not isinstance(values, dict) or set(values) != set(self.tasks):
            raise BoundaryError("shared_identity_missing", 400)
        result, used_uids, used_gids = {}, set(), set()
        for task_id, value in values.items():
            _fields(value, {"uid", "gid", "root", "entries"})
            uid, gid = value["uid"], value["gid"]
            if (type(uid) is not int or type(gid) is not int or not 10000 <= uid < 2**31
                    or not 10000 <= gid < 2**31 or uid in used_uids or gid in used_gids
                    or not _DNS_ID.fullmatch(task_id)):
                raise BoundaryError("shared_unique_nonroot_identity_required", 400)
            used_uids.add(uid); used_gids.add(gid)
            root = _absolute(value["root"])
            if root.name != task_id or root == self.private_root or root.is_relative_to(self.private_root) or self.private_root.is_relative_to(root):
                raise BoundaryError("shared_task_root_invalid", 400)
            _directory(root, 0, 0, 0o711)
            _ancestors(root)
            task = self.tasks[task_id]
            if task.state_dir != root / "state" or task.workspace != root / "workspace":
                raise BoundaryError("shared_task_paths_mismatch", 400)
            for name in ("state", "workspace", "home", "tmp"):
                _directory(root / name, uid, gid, 0o700)
            for name in ("config", "attempts"):
                _directory(root / name, 0, 0, 0o711)
            if not isinstance(value["entries"], dict) or set(value["entries"]) != set(task.attempts):
                raise BoundaryError("shared_entry_missing", 400)
            for attempt_id, spec in task.attempts.items():
                if spec.config_dir != root / "config" / attempt_id or spec.evidence_dir != root / "attempts" / attempt_id:
                    raise BoundaryError("shared_attempt_paths_mismatch", 400)
                _directory(spec.config_dir, 0, gid, 0o750)
                _directory(spec.evidence_dir, uid, gid, 0o700)
                entry = value["entries"][attempt_id]
                _fields(entry, {"kind", "script", "script_sha256", "binding_sha256"})
                if any(not isinstance(entry[key], str) or not _HASH.fullmatch(entry[key]) for key in ("script_sha256", "binding_sha256")):
                    raise BoundaryError("shared_material_digest_required", 400)
                if entry["kind"] not in {"candidate", "calibration"} or entry["kind"] == "calibration" and not self.allow_calibration:
                    raise BoundaryError("shared_entry_not_enabled", 400)
            result[task_id] = TaskIdentity(uid, gid, root, value["entries"])
        return result

    def _binding(self, task, spec, identity):
        entry = identity.entries[spec.attempt_id]
        script = Path(entry["script"])
        if not script.is_absolute():
            raise BoundaryError("shared_entry_invalid", 400)
        _ancestors(script)
        try:
            data = _file(script, 0, 0, 0o444, limit=2 * 1024 * 1024)
        except (OSError, ValueError):
            raise BoundaryError("shared_entry_not_implemented", 400) from None
        if hashlib.sha256(data).hexdigest() != entry["script_sha256"]:
            raise BoundaryError("shared_entry_material_changed", 400)
        request = spec.config_dir / "request.json"
        if entry["kind"] == "candidate":
            expected = (*_CANDIDATE_ENTRIES[spec.candidate], "--request", str(request))
            if tuple(spec.argv) != expected or script != Path(expected[-3]):
                raise BoundaryError("shared_candidate_entry_required", 400)
            if not os.statvfs(script).f_flag & os.ST_RDONLY or not os.statvfs(spec.config_dir).f_flag & os.ST_RDONLY:
                raise BoundaryError("shared_readonly_materials_required", 400)
        elif tuple(spec.argv[-3:]) != (str(script), "--request", str(request)):
            raise BoundaryError("shared_calibration_entry_invalid", 400)
        raw = _file(spec.config_dir / "shared-task.json", 0, identity.gid, 0o440)
        if hashlib.sha256(raw).hexdigest() != entry["binding_sha256"]:
            raise BoundaryError("shared_task_binding_changed", 400)
        binding = json.loads(raw)
        expected = {"schema": "harness-lab-shared-spawn/1", "task_id": task.task_id,
            "scope": task.scope, "attempt_id": spec.attempt_id, "uid": identity.uid, "gid": identity.gid,
            "entry_kind": entry["kind"], "paths": {"config": str(spec.config_dir), "request": str(request),
                "state": str(task.state_dir), "workspace": str(task.workspace), "output": str(spec.evidence_dir),
                "home": str(identity.root / "home"), "tmp": str(identity.root / "tmp")},
            "services": {"tools": f"http://tools-{task.task_id}:8081", "model": f"http://model-proxy-{task.task_id}:8082/v1"}}
        if binding != expected:
            raise BoundaryError("shared_task_binding_mismatch", 400)
        # 候选框架共享入口在 SDK I/O 前，必须将完整 request/token/
        # lifecycle 模式与此 root 所有的启动身份核对。
        _file(request, 0, identity.gid, 0o440)
        return binding

    def start(self, task_id, attempt_id):
        with self.lock:
            if task_id not in self.tasks or task_id not in self.identities:
                raise BoundaryError("shared_identity_missing", 400)
            if task_id in self.revoked:
                raise BoundaryError("shared_task_revoked")
            task = self.tasks[task_id]
            if attempt_id not in task.attempts:
                raise BoundaryError("unknown_fixture_attempt", 404)
            key = (task_id, attempt_id)
            if key in self.owned:
                raise BoundaryError("attempt_already_used")
            identity = self.identities[task_id]
            if uid_processes(identity.uid):
                raise BoundaryError("shared_task_uid_still_active")
            old = self.current.get(task_id)
            if old is not None and self.owned[(task_id, old)].process.poll() is None:
                raise BoundaryError("task_already_active")
            if sum(item.process.poll() is None for item in self.owned.values()) >= self.max_active:
                raise BoundaryError("active_task_limit", 429)
            if old is not None:
                # 不提供默认回退：现有 I22 验证器属于单独的
                # 可信集成，不能在此虚构恢复决定。
                if self.handoff_guard is None:
                    raise BoundaryError("shared_native_handoff_not_connected")
                self.handoff_guard(task, old, attempt_id)
            spec = task.attempts[attempt_id]
            if any(spec.evidence_dir.iterdir()):
                raise BoundaryError("attempt_evidence_not_empty")
            env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", **spec.environment}
            # 将 supervisor 标准输入输出保留在私有存储中。现有生命周期
            # 适配器要求候选输出目录确实为空。
            stdout_path = self.private_root / f"{task_id}-{attempt_id}.stdout"
            stderr_path = self.private_root / f"{task_id}-{attempt_id}.stderr"
            try:
                stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(stdout_fd, "wb") as out:
                    stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(stderr_fd, "wb") as err:
                        child = self._spawn(list(spec.argv), cwd=str(task.workspace), env=env,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=err, shell=False,
                            start_new_session=True, close_fds=True)
            except OSError:
                raise BoundaryError("child_start_failed", 500) from None
            owned = OwnedAttempt(spec, child, linux_process_identity(child.pid), utc_now())
            self.owned[key], self.current[task_id] = owned, attempt_id
            self._record("process_started", task, attempt_id=attempt_id, identity=owned.identity,
                candidate=spec.candidate, claim="nonroot_new_child_not_native_reuse_or_recovery",
                stdout=stdout_path.name, stderr=stderr_path.name)
            return self.state(task_id)

    def _spawn(self, argv, **kwargs):
        matches = [(task, spec) for task in self.tasks.values() for spec in task.attempts.values()
                   if list(spec.argv) == argv and str(task.workspace) == kwargs.get("cwd")]
        if len(matches) != 1:
            raise BoundaryError("shared_launch_not_registered", 400)
        task, spec = matches[0]
        identity = self.identities[task.task_id]
        binding = self._binding(task, spec, identity)
        env = dict(kwargs["env"])
        reserved = {"HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HARNESS_SHARED_TASK_BINDING_FILE"}
        if reserved & spec.environment.keys() or spec.environment.keys() - _ENV_FIELDS:
            raise BoundaryError("shared_reserved_environment", 400)
        if spec.environment.get("MODEL_API_BASE_URL") != binding["services"]["model"]:
            raise BoundaryError("shared_model_route_mismatch", 400)
        if spec.environment.get("MODEL_RUN_TOKEN_FILE") != str(spec.config_dir / "run.token"):
            raise BoundaryError("shared_model_token_path_mismatch", 400)
        _file(spec.config_dir / "run.token", 0, identity.gid, 0o440, limit=8192)
        _file(spec.config_dir / "scope.token", 0, identity.gid, 0o440, limit=8192)
        env.update({"HOME": str(identity.root / "home"), "TMPDIR": str(identity.root / "tmp"),
            "XDG_CACHE_HOME": str(identity.root / "home/cache"), "XDG_CONFIG_HOME": str(identity.root / "home/config"),
            "XDG_DATA_HOME": str(identity.root / "home/data"),
            "HARNESS_TOOLS_URL": binding["services"]["tools"],
            "HARNESS_TOOL_TOKEN_FILE": str(spec.config_dir / "scope.token"),
            "HARNESS_SHARED_TASK_BINDING_FILE": str(spec.config_dir / "shared-task.json")})
        kwargs.update(env=env, user=identity.uid, group=identity.gid, extra_groups=(), umask=0o077)
        child = self.real_popen(argv, **kwargs)
        try:
            observed = _status(child.pid)
            if (_uids(observed) != (identity.uid,) * 4
                    or tuple(int(item) for item in observed["Gid"].split()) != (identity.gid,) * 4
                    or observed.get("Groups", "") or observed.get("NoNewPrivs") != "1" or observed.get("Umask") != "0077"
                    or any(int(observed.get(key, "-1"), 16) != 0 for key in ("CapInh", "CapPrm", "CapEff", "CapAmb"))):
                raise BoundaryError("shared_child_identity_invalid")
            self._record("shared_child_identity_observed", task, attempt_id=spec.attempt_id,
                pid=child.pid, uid=identity.uid, gid=identity.gid, groups=[], umask="0077",
                no_new_privileges=True, capabilities="zero_observed", entry_kind=identity.entries[spec.attempt_id]["kind"],
                source="linux_proc_status_after_explicit_Popen_credentials", native_control_verified=False)
        except (OSError, ValueError, KeyError, BoundaryError):
            self.revoked.add(task.task_id)
            self._reap_uid(task, identity, timeout_seconds=2, extra_children=(child,))
            child.poll()
            raise BoundaryError("shared_child_identity_unverified") from None
        return child

    def cancel(self, task_id, request_id, *, attempt_id=None):
        with self.lock:
            if task_id in self.revoked:
                raise BoundaryError("shared_task_revoked")
            return super().cancel(task_id, request_id, attempt_id=attempt_id)

    def _reap_uid(self, task, identity, *, timeout_seconds, extra_children=()):
        deadline = time.monotonic() + timeout_seconds
        self._record("shared_uid_teardown_started", task, uid=identity.uid,
                     action="application_SIGKILL_teardown", native_cancel_confirmed=False)
        while True:
            current = uid_processes(identity.uid)
            # 回收 supervisor 直接拥有的子进程。僵尸进程无法工作，
            # 但在父进程或命名空间 init 回收前仍明确记录。
            for (task_id, _), owned in self.owned.items():
                if task_id == task.task_id:
                    owned.process.poll()
            for child in extra_children:
                child.poll()
            if not current:
                self._record("shared_uid_empty_observed", task, uid=identity.uid,
                    source="linux_proc_uid_scan", tool_model_quiescence_verified=False,
                    native_cancel_confirmed=False, claim="no_uid_process_at_observation_not_business_recovery")
                return {"uid_processes_empty_observed": True, "tool_model_quiescence_verified": False,
                        "native_cancel_confirmed": False}
            for row in current:
                if row["state"] == "Z":
                    continue
                try:
                    descriptor = os.pidfd_open(row["pid"], 0)
                    try:
                        if identity.uid in _uids(_status(row["pid"])):
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    finally:
                        os.close(descriptor)
                except (FileNotFoundError, ProcessLookupError):
                    continue
            if time.monotonic() >= deadline:
                self._record("shared_uid_teardown_incomplete", task, uid=identity.uid,
                             observed_processes=uid_processes(identity.uid), tool_model_quiescence_verified=False)
                raise BoundaryError("shared_uid_teardown_incomplete")
            time.sleep(0.02)

    def revoke(self, task_id, *, timeout_seconds=2):
        """可信且不可逆的清理，不代表原生任务取消。"""
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 10:
            raise BoundaryError("invalid_teardown_timeout", 400)
        with self.lock:
            if task_id not in self.tasks:
                raise BoundaryError("unknown_fixture_task", 404)
            self.revoked.add(task_id)
            return self._reap_uid(self.tasks[task_id], self.identities[task_id], timeout_seconds=timeout_seconds)
