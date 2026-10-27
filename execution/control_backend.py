"""I24 双独立 Docker Worker 的产品边界服务，本进程不访问 Docker。

任务、所有者、尝试、接口和凭证的绑定只来自只读可信材料。
公开调用方不能启动 Worker、替换路由或提交续跑提示。
宿主编排单独负责创建与替换容器。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from execution.worker_control import immutable_bytes, read_secret, verify_resume_materials
from execution import dsh_materials
from runner.shared_backend import AuditJournal, ProductBackend, validate_journal_location
from runner.shared_supervisor import BoundaryError, TaskSupervisor, _fields, _identifier, read_bound_file, sha256, utc_now


OPERATIONS = frozenset({"state", "events", "artifacts", "pause", "resume", "cancel"})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ContainerWorkerRegistry(TaskSupervisor):
    """读取实际持久文件，只向已登记的 Worker 地址转发控制。

    本进程不创建 Popen。观察到的历史退出可以授权替换，
    但不能作为实时进程状态缓存返回。
    """
    def __init__(self, tasks, routes, journal, *, opener=None):
        super().__init__(tasks, journal, max_active=2)
        if len(self.tasks) != 2:
            raise BoundaryError("two_registered_tasks_required", 400)
        if any(len({spec.candidate for spec in task.attempts.values()}) != 1 for task in self.tasks.values()):
            raise BoundaryError("original_candidate_must_not_change_between_attempts", 400)
        validate_journal_location(self, journal.path)
        self.routes, self.closed_observations, self.start_attempts = {}, {}, set()
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        if not isinstance(routes, list):
            raise BoundaryError("invalid_worker_routes", 400)
        for route in routes:
            _fields(route, {"task_id", "attempt_id", "worker_id", "url", "policy_file", "token_file"})
            task_id, attempt = route["task_id"], route["attempt_id"]
            if task_id not in self.tasks or attempt not in self.tasks[task_id].attempts or (task_id, attempt) in self.routes:
                raise BoundaryError("unregistered_worker_route", 400)
            _identifier(route["worker_id"])
            parsed = urlsplit(route["url"])
            if (parsed.scheme != "http" or not re.fullmatch(r"worker-[a-z0-9-]{1,80}", parsed.hostname or "")
                    or parsed.port != 8092 or parsed.path or parsed.query or parsed.fragment
                    or parsed.username or parsed.password):
                raise BoundaryError("fixed_private_worker_endpoint_required", 400)
            spec = self.tasks[task_id].attempts[attempt]
            if (route["policy_file"] != str(spec.config_dir / "worker-policy.json")
                    or route["token_file"] != str(spec.config_dir / "worker.token")
                    or attempt not in {"worker-a", "worker-b"}):
                raise BoundaryError("fixed_worker_material_path_required", 400)
            self.routes[(task_id, attempt)] = dict(route)
        if set(self.routes) != {(task.task_id, attempt) for task in self.tasks.values() for attempt in task.attempts}:
            raise BoundaryError("incomplete_worker_routes", 400)
        if any(set(task.attempts) != {"worker-a", "worker-b"} for task in self.tasks.values()):
            raise BoundaryError("initial_and_replacement_attempts_required", 400)
        if len({route["url"] for route in self.routes.values()}) != len(self.routes):
            raise BoundaryError("distinct_worker_endpoints_required", 400)

    def _materials(self, task_id, attempt):
        route, task = self.routes[(task_id, attempt)], self.tasks[task_id]
        raw = immutable_bytes(route["policy_file"])
        policy = json.loads(raw)
        mode = "new" if attempt == "worker-a" else "resume"
        if (not isinstance(policy, dict) or not isinstance(policy.get("task"), dict)
                or not isinstance(policy["task"].get("attempts"), list)
                or any(not isinstance(item, dict) for item in policy["task"]["attempts"])):
            raise BoundaryError("worker_policy_scope_mismatch")
        if (policy.get("schema") != "harness-lab-worker-control/1" or policy.get("worker_id") != route["worker_id"]
                or policy.get("mode") != mode or policy.get("task", {}).get("task_id") != task_id
                or policy.get("task", {}).get("owner") != task.owner or policy.get("task", {}).get("scope") != task.scope
                or [item.get("attempt_id") for item in policy.get("task", {}).get("attempts", [])] != [attempt]):
            raise BoundaryError("worker_policy_scope_mismatch")
        expected = {"schema": "harness-lab-worker-description/1", "worker_id": route["worker_id"],
            "policy_sha256": sha256(raw), "task_id": task_id, "scope": task.scope,
            "attempt_id": attempt, "mode": mode, "candidate": task.attempts[attempt].candidate}
        if any(item.get("candidate", "deepagents_langgraph") != expected["candidate"] for item in policy["task"]["attempts"]):
            raise BoundaryError("worker_policy_candidate_mismatch")
        return route, expected, read_secret(route["token_file"])

    def _request(self, task_id, attempt, action, *, request_id=None):
        route, expected, token = self._materials(task_id, attempt)
        payload = None if request_id is None else json.dumps({"request_id": request_id}).encode()
        request = Request(route["url"] + "/control/" + action, data=payload,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="GET" if payload is None else "POST")
        try:
            with self.opener.open(request, timeout=5) as response:
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise BoundaryError("worker_response_bound")
                value = json.loads(data)
                if response.status != (200 if payload is None else 202):
                    raise BoundaryError("worker_response_status_mismatch")
        except HTTPError as error:
            # 不把上游错误正文、地址或凭证值写入公开输出。
            raise BoundaryError("worker_rejected_control", 409 if error.code < 500 else 503) from None
        except (URLError, TimeoutError, socket.timeout, ConnectionError):
            raise BoundaryError("worker_endpoint_unavailable", 503) from None
        if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
            raise BoundaryError("worker_response_identity_mismatch")
        if request_id is not None and value.get("request_id") != request_id:
            raise BoundaryError("worker_request_id_mismatch")
        return value

    def _start(self, task_id, attempt, request_id):
        if (task_id, attempt) in self.start_attempts:
            raise BoundaryError("worker_start_already_attempted")
        self._request(task_id, attempt, "describe")
        self._record("worker_start_attempt", self.tasks[task_id], attempt_id=attempt, request_id=request_id,
            controller_monotonic=time.monotonic(), observation_clock="Docker_VM_monotonic/1")
        self.start_attempts.add((task_id, attempt))
        try:
            result = self._request(task_id, attempt, "start", request_id=request_id)
        except BoundaryError:
            # 响应丢失时任务可能已真正启动，不能自动重试启动，
            # 也不能把这种不确定状态标成尚未启动。
            self._record("worker_start_result_unknown", self.tasks[task_id], attempt_id=attempt, request_id=request_id)
            raise
        if result.get("status") != "child_started" or result.get("native_resume_completed") is not False:
            raise BoundaryError("worker_start_ack_mismatch")
        self.current[task_id] = attempt
        self._record("worker_start_acknowledged", self.tasks[task_id], attempt_id=attempt,
            request_id=request_id, process_identity=result.get("process_identity"), native_resume_completed=False)
        return result

    def start_task(self, task_id):
        """仅供可信启动使用，刻意不暴露为公开 HTTP 接口。"""
        with self.lock:
            if task_id not in self.tasks:
                raise BoundaryError("task_not_found", 404)
            if task_id in self.current:
                raise BoundaryError("logical_task_already_started")
            return self._start(task_id, "worker-a", str(uuid4()))

    def _selected(self, task_id):
        if task_id not in self.current:
            raise BoundaryError("task_not_started")
        task, attempt = self.tasks[task_id], self.current[task_id]
        return task, attempt, task.attempts[attempt]

    def state(self, task_id):
        with self.lock:
            task, attempt, spec = self._selected(task_id)
            try:
                worker = self._request(task_id, attempt, "state")
                process = {"availability": "observed", "source": "actual_worker_retained_Popen_poll",
                    "process_status": worker.get("process_status"), "exit_code": worker.get("exit_code"),
                    "process_identity": worker.get("process_identity"), "observed_at": utc_now()}
                if worker.get("process_status") == "exited" and worker.get("exit_code") == 0:
                    self.closed_observations[task_id] = {"attempt_id": attempt, **process}
                    self._record("worker_clean_exit_observed", task, attempt_id=attempt, observation=process)
            except BoundaryError as error:
                if error.code != "worker_endpoint_unavailable":
                    raise
                process = {"availability": "endpoint_unavailable", "source": "failed_actual_worker_request",
                           "cached_process_status_returned": False}
            materials = []
            native_materials = ((task.state_dir, "pause-index.json", "native_state"),
                    (spec.evidence_dir, "state-snapshot.json", "native_snapshot"),
                    (spec.evidence_dir, "result.json", "candidate_result"))
            if spec.candidate == dsh_materials.CANDIDATE:
                native_materials = tuple((spec.evidence_dir, name, source) for name, source in (
                    ("session-after-idle.json", "native_session_snapshot"),
                    ("lifecycle-pause-candidate.json", "native_goal_pause_candidate"),
                    ("native-state-index-after-close.json", "closed_native_inventory"),
                    ("result.json", "candidate_result")))
            for root, relative, source in native_materials:
                try:
                    raw = read_bound_file(root, relative)
                except FileNotFoundError:
                    continue
                materials.append({"source": source, "reference": {"root": str(root), "path": relative, "sha256": sha256(raw)},
                                  "record": json.loads(raw)})
            return {"task_id": task_id, "scope": task.scope, "attempt_id": attempt,
                    "process_observation": process, "durable_native_materials": materials,
                    "native_state_complete": False}

    def events(self, task_id, *, after=0, limit=1000, attempt_id=None):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise BoundaryError("invalid_cursor", 400)
        with self.lock:
            task, attempt, spec = self._selected(task_id)
            if attempt_id is not None:
                if attempt_id not in task.attempts:
                    raise BoundaryError("unregistered_event_attempt", 400)
                attempt, spec = attempt_id, task.attempts[attempt_id]
            # _rows 只读取已登记文件；这里持有的是证据，
            # 不能伪造进程对象或原生实时状态投影。
            rows, sample = self._rows(task, SimpleNamespace(spec=spec), spec.events_file)
            if after > len(rows):
                raise BoundaryError("cursor_beyond_observed_events", 400)
            selected = rows[after:after + limit]
            return {"task_id": task_id, "scope": task.scope, "attempt_id": attempt,
                "cursor_domain": {"kind": "task_attempt_file", "task_id": task_id, "attempt_id": attempt},
                "after": after, "next_cursor": after + len(selected), "observed_cursor": len(rows), "records": selected, "sample": sample,
                "source": "actual_registered_persistent_event_file"}

    def _signal(self, task_id, action, request_id):
        with self.lock:
            task, attempt, _ = self._selected(task_id)
            self._request(task_id, attempt, "describe")
            self._record("worker_control_dispatch_attempt", task, action=action, request_id=request_id, attempt_id=attempt)
            result = self._request(task_id, attempt, action, request_id=request_id)
            native_field = "native_pause_confirmed" if action == "pause" else "native_cancel_confirmed"
            is_dsh = task.attempts[attempt].candidate == dsh_materials.CANDIDATE
            semantics = dsh_materials.SEMANTICS[action] if is_dsh else "cooperative_drain_at_superstep_boundary"
            if (result.get("status") != "signal_dispatch_attempted" or result.get("signal_delivery_confirmed") is not False
                    or result.get(native_field) is not False or result.get("immediate_cancel") is not False
                    or (is_dsh and result.get("immediate_cancel_requested") is not (action == "cancel"))
                    or result.get("semantics") != semantics):
                raise BoundaryError("worker_signal_ack_mismatch")
            return result

    def pause(self, task_id, request_id):
        return self._signal(task_id, "pause", request_id)

    def cancel(self, task_id, request_id):
        return self._signal(task_id, "cancel", request_id)

    def resume(self, task_id, request_id):
        with self.lock:
            task, attempt, _ = self._selected(task_id)
            if attempt != "worker-a":
                raise BoundaryError("replacement_attempt_already_used")
            observation = self.closed_observations.get(task_id)
            if not observation or observation["attempt_id"] != attempt:
                raise BoundaryError("original_worker_clean_exit_not_observed")
            spec = task.attempts["worker-b"]
            request = json.loads(immutable_bytes(spec.config_dir / "request.json"))
            binding = json.loads(immutable_bytes(spec.config_dir / "lifecycle.json"))
            if (not isinstance(request, dict) or not isinstance(binding, dict)
                    or "prompt" in request or request.get("run_id") != task.scope["variant"]
                    or request.get("session_id") != task.scope["session_id"]
                    or request.get("lifecycle") != {"mode": "resume", "chain_id": task.scope["variant"],
                        "attempt_id": "worker-b", "binding_file": "/config/lifecycle.json"}
                    or binding.get("scope") != task.scope or binding.get("mode") != "resume"):
                raise BoundaryError("original_prompt_free_resume_binding_required")
            verifier = dsh_materials.verify_resume_materials if spec.candidate == dsh_materials.CANDIDATE else verify_resume_materials
            reference = verifier(task, request, binding)
            self._record("replacement_resume_material_verified", task, request_id=request_id,
                previous_exit_observation=observation, original_pause=reference,
                claim="new_container_and_process_not_same_worker_reuse", native_resume_completed=False)
            return self._start(task_id, "worker-b", request_id)


class ConnectedProductBackend(ProductBackend):
    def execute_authorized(self, task, operation, method, body, query, request_id):
        if operation == "events":
            if method != "GET":
                raise BoundaryError("method_not_allowed", 405)
            if body != {} or query.keys() - {"after", "limit", "attempt"}:
                raise BoundaryError("request_fields_forbidden", 400)
            if any(len(values) != 1 for values in query.values()):
                raise BoundaryError("invalid_cursor", 400)
            if any(not re.fullmatch(r"[0-9]{1,8}", query[name][0]) for name in ("after", "limit") if name in query):
                raise BoundaryError("invalid_cursor", 400)
            attempt = query.get("attempt", [None])[0]
            if attempt is not None and attempt not in task.attempts:
                raise BoundaryError("unregistered_event_attempt", 400)
            return 200, self.supervisor.events(task.task_id, after=int(query.get("after", ["0"])[0]),
                limit=int(query.get("limit", ["1000"])[0]), attempt_id=attempt)
        if operation in {"pause", "resume"}:
            if method != "POST":
                raise BoundaryError("method_not_allowed", 405)
            if body != {} or query:
                raise BoundaryError("request_fields_forbidden", 400)
            return 202, getattr(self.supervisor, operation)(task.task_id, request_id)
        return super().execute_authorized(task, operation, method, body, query, request_id)


class ControlBackendServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, backend):
        self.backend = backend
        super().__init__(address, ControlBackendHandler)


class ControlBackendHandler(BaseHTTPRequestHandler):
    """持久产品 HTTP 连接；对端实际 EOF 与主动关闭请求分别记录。"""
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self.connection_id = str(uuid4())
        self.eof_recorded = False
        self.server.backend.record("connection_open", connection_id=self.connection_id,
                                   source="actual_accepted_HTTP_socket")

    def handle_one_request(self):
        super().handle_one_request()
        if getattr(self, "raw_requestline", None) == b"" and not self.eof_recorded:
            self.eof_recorded = True
            self.server.backend.record("connection_peer_eof", connection_id=self.connection_id,
                                       source="actual_HTTP_rfile_readline_returned_empty_bytes")

    def log_message(self, *args):
        pass

    def _handle(self):
        request_id = str(uuid4())
        backend = self.server.backend
        status, value = 500, {"error": {"code": "backend_error"}}
        body_read = False
        try:
            url = urlsplit(self.path)
            parts = url.path.split("/")
            if (len(parts) != 4 or parts[1] != "tasks" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", parts[2])
                    or parts[3] not in OPERATIONS):
                raise BoundaryError("route_not_found", 404)
            task = backend.authorize(self.headers.get("Authorization"), parts[2], parts[3], request_id)
            length = self.headers.get("Content-Length", "0")
            if self.headers.get("Transfer-Encoding") is not None or not re.fullmatch(r"[0-9]{1,4}", length) or int(length) > 4096:
                raise BoundaryError("invalid_body", 400)
            raw = self.rfile.read(int(length))
            body_read = True
            body = json.loads(raw) if raw else {}
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            status, value = backend.execute_authorized(task, parts[3], self.command, body, query, request_id)
        except BoundaryError as error:
            status, value = error.status, {"error": {"code": error.code}}
        except (OSError, ValueError, KeyError, TypeError):
            status, value = 409, {"error": {"code": "control_material_unavailable"}}
        # 被拒绝但未读取的请求体不能成为当前连接的下一条请求。
        if not body_read:
            self.close_connection = True
        backend.record("response", request_id=request_id, connection_id=self.connection_id, status=status, body=value)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", request_id)
        self.send_header("X-Connection-ID", self.connection_id)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default="/control/backend.json")
    parser.add_argument("--journal", default="/backend-private/control.jsonl")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--start", action="append", default=[])
    args = parser.parse_args()
    data = immutable_bytes(args.fixture)
    fixture = json.loads(data)
    _fields(fixture, {"schema", "tasks", "routes", "credentials"})
    if fixture["schema"] != "harness-lab-container-control-backend/1":
        raise BoundaryError("invalid_backend_fixture", 400)
    journal = AuditJournal(args.journal)
    try:
        registry = ContainerWorkerRegistry(fixture["tasks"], fixture["routes"], journal)
        backend = ConnectedProductBackend(registry, fixture["credentials"], journal)
        backend.record("fixture_loaded", fixture_sha256=sha256(data), native_presence_verified=False)
        if len(args.start) != len(set(args.start)) or set(args.start) - registry.tasks.keys():
            raise BoundaryError("invalid_trusted_start_selection", 400)
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(registry.start_task, task_id) for task_id in args.start]:
                future.result()
        with ControlBackendServer(("0.0.0.0", args.port), backend) as server:
            server.serve_forever()
    finally:
        journal.close()


if __name__ == "__main__":
    try:
        main()
    except (BoundaryError, OSError, ValueError, KeyError, TypeError):
        raise SystemExit("control_backend_start_failed") from None
