"""I24 fixed-fixture product boundary, not a production identity/control platform.

Clients select only an already registered task and operation. Owner checks occur
before body/state/control checks. Startup/attempt selection remains trusted code;
pause/resume are explicitly unconnected here, never synthetic successes.
"""
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
    """Owner-side append-only output; mount outside every candidate-writable tree."""
    def __init__(self, filename):
        self.path = Path(filename)
        _absolute(str(self.path.parent))
        if not self.path.is_absolute() or self.path.name in {"", ".", ".."} or self.path.is_symlink():
            raise BoundaryError("invalid_audit_path", 400)
        self.lock = threading.Lock()
        # Fresh journal only; do not silently overwrite failed-trial evidence.
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
        """Returns a trusted principal/task pair or a fixed safe HTTP error.

        Never audit Authorization or attacker-controlled body data. A target's
        actual state is consulted only after the owner matches.
        """
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
        # Even a valid owner cannot substitute fixture ownership/control fields.
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
        # These still undergo real owner authorization; root integrates the
        # already-proven I22 lifecycle before they can become positive controls.
        raise BoundaryError("control_not_connected", 501)

    def dispatch(self, header, task_id, operation, *, method="GET", body=None, query=None, request_id=None):
        """Pure request boundary used by HTTP and no-candidate calibration tests."""
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
        # Server-generated correlation ID; a client cannot overwrite it.
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
            # Authorize first, including for malformed bodies and inapplicable
            # operations: a foreign paused/running task always remains foreign.
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
                # Ordinary finite request close, not proof of SSE reconnect.
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
    """Reject every candidate-visible task tree, including read-only config.

    The containing root still requires real root-only permissions. This path
    check prevents accidental placement; it does not create a UID boundary.
    """
    journal_path = Path(journal_path)
    for task in supervisor.tasks.values():
        roots = [task.workspace, task.state_dir,
                 *(attempt.evidence_dir for attempt in task.attempts.values()),
                 *(attempt.config_dir for attempt in task.attempts.values())]
        if any(journal_path == root or journal_path.is_relative_to(root) for root in roots):
            raise BoundaryError("audit_inside_candidate_tree", 400)


# Deliberately no startup CLI. The trusted Docker experiment must supply its
# real Worker registry and protected journal; importing this boundary must not
# start candidates or offer a same-UID fallback. Separate container Workers can
# reuse ProductBackend without depending on shared_uid.
