"""I24 固定 Fixture 的产品边界，不是生产身份或控制平台。

客户端只能选择已登记任务和操作。先检查所有者，再检查正文、状态和控制。
启动与尝试选择仍属于可信代码；此处未接通暂停/恢复，绝不合成成功。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from runner.shared_supervisor import BoundaryError, _absolute, _fields, _identifier, read_bound_file, sha256


_OPERATIONS = frozenset({"state", "events", "artifacts", "pause", "resume", "cancel"})
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class AuditJournal:
    """所有者侧仅追加输出，必须挂载在所有候选框架可写目录树之外。"""
    def __init__(self, filename):
        self.path = Path(filename)
        _absolute(str(self.path.parent))
        if not self.path.is_absolute() or self.path.name in {"", ".", ".."} or self.path.is_symlink():
            raise BoundaryError("invalid_audit_path", 400)
        self.lock = threading.Lock()
        # 只创建新日志，不静默覆盖失败实验的证据。
        self.descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)

    def __call__(self, value):
        data = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()
        with self.lock:
            view = memoryview(data)
            while view:
                count = os.write(self.descriptor, view)
                view = view[count:]
            os.fsync(self.descriptor)

    def close(self):
        with self.lock:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None


class ProductBackend:
    def __init__(self, supervisor, credentials, audit):
        self.supervisor, self.audit = supervisor, audit
        if not isinstance(credentials, list) or not credentials:
            raise BoundaryError("invalid_fixture", 400)
        self.credentials = []
        ids, digests, principals = set(), set(), set()
        for item in credentials:
            _fields(item, {"credential_id", "principal_id", "token_sha256"})
            credential = _identifier(item["credential_id"])
            principal = _identifier(item["principal_id"])
            digest = item["token_sha256"]
            if not isinstance(digest, str) or not _HASH.fullmatch(digest) or credential in ids or digest in digests:
                raise BoundaryError("invalid_fixture", 400)
            ids.add(credential); digests.add(digest); principals.add(principal)
            self.credentials.append((digest, principal, credential))
        if any(task.owner not in principals for task in supervisor.tasks.values()):
            raise BoundaryError("unregistered_task_owner", 400)

    def record(self, kind, **values):
        self.audit({"kind": kind, "time": datetime.now(timezone.utc).isoformat(), **values})

    def authorize(self, header, task_id, operation, request_id):
        """返回可信的主体/任务对，或固定且安全的 HTTP 错误。

        绝不审计 Authorization 或攻击者可控正文。
        只有所有者匹配后才查询目标实际状态。"""
        hit = None
        if isinstance(header, str) and header.startswith("Bearer ") and 0 < len(header[7:]) <= 512 and not re.search(r"\s", header[7:]):
            digest = hashlib.sha256(header[7:].encode()).hexdigest()
            for expected, principal, credential in self.credentials:
                if hmac.compare_digest(expected, digest):
                    hit = (principal, credential)
        if hit is None:
            self.record("authorization", request_id=request_id, operation=operation, decision="deny", reason="unauthenticated")
            raise BoundaryError("unauthenticated", 401)
        principal, credential = hit
        self.record("request", request_id=request_id, principal_id=principal,
                    credential_id=credential, task_id=task_id, operation=operation)
        task = self.supervisor.tasks.get(task_id)
        if task is None:
            self.record("authorization", request_id=request_id, principal_id=principal,
                        credential_id=credential, task_id=task_id, operation=operation, decision="deny", reason="task_not_found")
            raise BoundaryError("task_not_found", 404)
        if task.owner != principal:
            self.record("authorization", request_id=request_id, principal_id=principal,
                        credential_id=credential, task_id=task_id, operation=operation, decision="deny", reason="scope_mismatch")
            raise BoundaryError("scope_denied", 403)
        self.record("authorization", request_id=request_id, principal_id=principal,
                    credential_id=credential, task_id=task_id, operation=operation, decision="allow", reason="owner_match")
        return task

    def execute_authorized(self, task, operation, method, body, query, request_id):
        if operation not in _OPERATIONS:
            raise BoundaryError("operation_not_found", 404)
        expected_method = "GET" if operation in {"state", "events", "artifacts"} else "POST"
        if method != expected_method:
            raise BoundaryError("method_not_allowed", 405)
        # 即使所有者有效，也不能替换 Fixture 归属或控制字段。
        if body != {}:
            raise BoundaryError("request_fields_forbidden", 400)
        if operation != "events" and query:
            raise BoundaryError("request_fields_forbidden", 400)
        if operation == "events":
            if query.keys() - {"after", "limit"} or any(len(values) != 1 or not re.fullmatch(r"[0-9]{1,8}", values[0]) for values in query.values()):
                raise BoundaryError("invalid_cursor", 400)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["1000"])[0])
            return 200, self.supervisor.events(task.task_id, after=after, limit=limit)
        if operation == "state":
            return 200, self.supervisor.state(task.task_id)
        if operation == "artifacts":
            return 200, self.supervisor.artifacts(task.task_id)
        if operation == "cancel":
            return 202, self.supervisor.cancel(task.task_id, request_id)
        # 这些操作仍执行真实所有者鉴权；根编排器接入
        # 已验证的 I22 生命周期后，才能作为正向控制。
        raise BoundaryError("control_not_connected", 501)

    def dispatch(self, header, task_id, operation, *, method="GET", body=None, query=None, request_id=None):
        """HTTP 和无候选框架校准测试共用的纯请求边界。"""
        request_id = request_id or str(uuid4())
        try:
            task = self.authorize(header, task_id, operation, request_id)
            status, payload = self.execute_authorized(task, operation, method, {} if body is None else body, {} if query is None else query, request_id)
        except BoundaryError as error:
            status, payload = error.status, {"error": {"code": error.code}}
        except (OSError, ValueError, KeyError, TypeError):
            status, payload = 409, {"error": {"code": "evidence_unavailable"}}
        self.record("response", request_id=request_id, status=status, body=payload)
        return status, payload


class ProductHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, backend):
        self.backend = backend
        super().__init__(address, ProductHandler)


class ProductHandler(BaseHTTPRequestHandler):
    server: ProductHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, payload, request_id):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", request_id)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _handle(self):
        # 关联 ID 由服务端生成，客户端不能覆盖。
        request_id = str(uuid4())
        backend = self.server.backend
        connection = str(uuid4())
        backend.record("connection_open", request_id=request_id, connection_id=connection)
        status, payload = 500, {"error": {"code": "backend_error"}}
        try:
            url = urlsplit(self.path)
            parts = url.path.split("/")
            if len(parts) != 4 or parts[1] != "tasks" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", parts[2]) or parts[3] not in _OPERATIONS:
                raise BoundaryError("route_not_found", 404)
            task_id, operation = parts[2], parts[3]
            # 始终先鉴权，包括格式错误正文和不适用操作；
            # 他人的暂停或运行任务始终属于他人。
            task = backend.authorize(self.headers.get("Authorization"), task_id, operation, request_id)
            if self.headers.get("Transfer-Encoding") is not None:
                raise BoundaryError("invalid_body", 400)
            raw_length = self.headers.get("Content-Length", "0")
            if not re.fullmatch(r"[0-9]{1,5}", raw_length) or int(raw_length) > 4096:
                raise BoundaryError("invalid_body", 400)
            raw = self.rfile.read(int(raw_length))
            try:
                body = {} if not raw else json.loads(raw)
                query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            except (ValueError, UnicodeDecodeError):
                raise BoundaryError("invalid_body", 400) from None
            status, payload = backend.execute_authorized(task, operation, self.command, body, query, request_id)
        except BoundaryError as error:
            status, payload = error.status, {"error": {"code": error.code}}
        except (OSError, ValueError, KeyError, TypeError):
            status, payload = 409, {"error": {"code": "evidence_unavailable"}}
        finally:
            backend.record("response", request_id=request_id, connection_id=connection, status=status, body=payload)
            try:
                self._reply(status, payload, request_id)
            finally:
                # 普通有限请求结束，不能证明 SSE 重连。
                backend.record("connection_close_requested", request_id=request_id, connection_id=connection,
                               reason="response_finished_close_requested_not_transport_eof_or_sse_reconnect")

    do_GET = _handle
    do_POST = _handle


def load_fixture(filename):
    path = Path(filename)
    _absolute(str(path.parent))
    data = read_bound_file(path.parent, path.name, limit=1024 * 1024)
    value = json.loads(data)
    _fields(value, {"schema", "credentials", "tasks"})
    if value["schema"] != "harness-lab-task-supervisor/1":
        raise BoundaryError("invalid_fixture", 400)
    return value, sha256(data)


def validate_journal_location(supervisor, journal_path):
    """拒绝所有候选框架可见的任务目录树，包括只读配置。

    所在根目录仍须具有真实的 root 独占权限。此路径检查用于防止误放，
    本身不能建立 UID 边界。"""
    journal_path = Path(journal_path)
    for task in supervisor.tasks.values():
        roots = [task.workspace, task.state_dir,
                 *(attempt.evidence_dir for attempt in task.attempts.values()),
                 *(attempt.config_dir for attempt in task.attempts.values())]
        if any(journal_path == root or journal_path.is_relative_to(root) for root in roots):
            raise BoundaryError("audit_inside_candidate_tree", 400)


# 故意不提供启动 CLI。可信 Docker 实验必须提供
# 真实 Worker 注册表和受保护日志；导入此边界不能
# 启动候选框架或提供同 UID 回退。独立容器 Worker 可
# 复用 ProductBackend，无需依赖 shared_uid。
