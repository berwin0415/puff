"""每个容器只运行固定的 DSH Goal 尝试；HTTP 包装层不导入 SDK。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from execution import dsh_materials as d
from execution.worker_control import (SingleAttemptWorker, WorkerHTTPServer,
    immutable_bytes, read_secret)
from runner.shared_backend import AuditJournal, validate_journal_location
from runner.shared_supervisor import (BoundaryError, OwnedAttempt, TaskSupervisor,
    _absolute, _fields, _identifier, linux_process_identity, sha256, utc_now)


class DSHAttemptWorker(SingleAttemptWorker):
    """复用私有 HTTP 与子进程控制，不复用 Deep Agents 校验器。"""
    def __init__(self, policy, policy_sha256, journal, *, popen_factory=subprocess.Popen,
                 identity_reader=linux_process_identity):
        _fields(policy, {"schema", "worker_id", "mode", "task", "control_token_file", "private_root"})
        d.need(policy["schema"] == "harness-lab-worker-control/1" and policy["mode"] in {"new", "resume"}, "policy_required")
        self.worker_id, self.policy_sha256, self.mode = _identifier(policy["worker_id"]), policy_sha256, policy["mode"]
        self.private_root = _absolute(policy["private_root"])
        d.need(self.private_root == Path("/worker-private") and journal.path.parent == self.private_root
             and policy["control_token_file"] == "/control/worker.token", "private_policy_required")
        TaskSupervisor.__init__(self, [policy["task"]], journal, max_active=1,
            popen_factory=popen_factory, identity_reader=identity_reader)
        validate_journal_location(self, journal.path)
        self.task = next(iter(self.tasks.values()))
        d.need(len(self.task.attempts) == 1, "one_attempt_required")
        self.spec = next(iter(self.task.attempts.values()))
        expected = "worker-a" if self.mode == "new" else "worker-b"
        env = self.spec.environment
        d.need(self.spec.candidate == d.CANDIDATE and self.spec.argv == d.ARGV and self.spec.attempt_id == expected
             and self.spec.config_dir == Path("/config") and self.spec.evidence_dir == Path("/evidence")
             and self.task.state_dir == Path("/state") and self.task.workspace == Path("/workspace")
             and not env.keys() - d.ENVIRONMENT and env.get("TSX_TSCONFIG_PATH") == d.CWD + "/tsconfig.json"
             and env.get("DSH_TELEMETRY_DISABLED") == "1", "fixed_Goal_worker_required")
        self.control_token, self.pending_control = read_secret(policy["control_token_file"]), None

    def _bound_request(self):
        raw, bound = immutable_bytes("/config/request.json"), immutable_bytes("/config/lifecycle.json")
        request, binding = json.loads(raw), json.loads(bound)
        checked = d.verify_binding(self.task, request, binding, config_dir=self.spec.config_dir,
                                   environment=self.spec.environment)
        d.need(checked["mode"] == self.mode and checked["attempt_id"] == self.spec.attempt_id, "policy_mode_changed")
        source = json.loads(immutable_bytes("/opt/materials/SOURCE_MANIFEST.json", limit=8 * 1024 * 1024))
        adapter = json.loads(immutable_bytes("/opt/materials/LAB_ADAPTER_MANIFEST.json"))
        d.need(checked["materials"]["source_export_sha256"] == source.get("export_sha256")
             and checked["materials"]["adapter_export_sha256"] == adapter.get("export_sha256"), "image_materials_changed")
        reference = d.verify_resume_materials(self.task, request, binding) if self.mode == "resume" else None
        return {"request_sha256": sha256(raw), "binding_sha256": sha256(bound), "original_pause": reference}

    def start_fixed(self, request_id):
        with self.lock:
            if self.current:
                raise BoundaryError("fixed_attempt_already_used")
            checked = self._bound_request()
            d.need(not any(self.spec.evidence_dir.iterdir()), "attempt_evidence_not_empty")
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", **self.spec.environment}
            try:
                out_fd = os.open(self.private_root / "candidate.stdout", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(out_fd, "wb") as out:
                    err_fd = os.open(self.private_root / "candidate.stderr", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(err_fd, "wb") as err:
                        child = self.popen_factory(list(d.ARGV), cwd=d.CWD, env=env, stdin=subprocess.DEVNULL,
                            stdout=out, stderr=err, shell=False, start_new_session=True, close_fds=True)
            except OSError:
                raise BoundaryError("child_start_failed", 500) from None
            owned = OwnedAttempt(self.spec, child, self.identity_reader(child.pid), utc_now())
            self.owned[(self.task.task_id, self.spec.attempt_id)] = owned
            self.current[self.task.task_id] = self.spec.attempt_id
            self._record("process_started", self.task, request_id=request_id, worker_id=self.worker_id,
                attempt_id=self.spec.attempt_id, identity=owned.identity, mode=self.mode, **checked,
                native_resume_completed=False, claim="actual_new_DSH_Goal_process_in_separate_worker_container")
            return {**self.state(self.task.task_id), "request_id": request_id, "status": "child_started", "native_resume_completed": False}

    def signal_control(self, action, request_id):
        result = super().signal_control(action, request_id)
        # 这里只描述请求的公开接口，不代表原生能力或完成结论；
        # 固定 I17 运行时仍有已知终态证据缺口。
        return {**result, "semantics": d.SEMANTICS[action], "immediate_cancel": False,
            "immediate_cancel_requested": action == "cancel",
            "native_request_id_transport": "POSIX_signal_without_HTTP_request_id"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="/control/policy.json")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    data = immutable_bytes(args.policy)
    policy = json.loads(data)
    journal = AuditJournal(_absolute(policy["private_root"]) / "worker-control.jsonl")
    worker = None
    try:
        worker = DSHAttemptWorker(policy, sha256(data), journal)
        with WorkerHTTPServer(("0.0.0.0", args.port), worker) as server:
            server.serve_forever()
    finally:
        if worker is not None and any(item.process.poll() is None for item in worker.owned.values()):
            worker._record("service_exit_with_live_child", worker.task, native_pause_confirmed=False, native_cancel_confirmed=False)
        journal.close()


if __name__ == "__main__":
    try:
        main()
    except (BoundaryError, OSError, ValueError, KeyError, TypeError):
        raise SystemExit("dsh_worker_control_start_failed") from None
