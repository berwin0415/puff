"""One trusted user/session tool endpoint, with no Agent or retry loop.

The simulated product backend writes the binding file. Requests cannot choose
another fixture token, workspace, scope, write mode or injected fault.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from execution.tools.basic import FixtureClient, FixtureHTTPError, FixtureTransportError, ToolError, WorkspaceFiles, calculate_totals
from execution.tools.fault_gate import CALL_HEADER, PERMIT_HEADER, TOKEN_HEADER, GateError, PrewriteGate


_SCOPE_FIELDS = {"experiment_id", "variant", "user_id", "session_id"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_STRING = {"type": "string"}
TOOLS = [
    {"name": "list_items", "description": "Query one page of this user's catalogue; follow next_page until complete.", "input_schema": _schema({"page": {"type": "integer", "minimum": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 100}})},
    {"name": "write", "description": "Commit an authorized business write. Use a stable operation_id; if the reply is lost query its receipt before retrying.", "input_schema": _schema({"operation_id": _STRING, "item_id": _STRING, "quantity": {"type": "integer", "minimum": 1}}, ("operation_id", "item_id", "quantity"))},
    {"name": "receipt", "description": "Query actual writes for an operation belonging to this user/session.", "input_schema": _schema({"operation_id": _STRING}, ("operation_id",))},
    {"name": "ledger", "description": "Read this user's actual business requests and writes.", "input_schema": _schema({})},
    {"name": "read_file", "description": "Read a UTF-8 file within this trusted task workspace.", "input_schema": _schema({"path": _STRING}, ("path",))},
    {"name": "write_file", "description": "Write a UTF-8 file within this trusted task workspace.", "input_schema": _schema({"path": _STRING, "content": _STRING}, ("path", "content"))},
    {"name": "calculate_totals", "description": "Sum quantities and prices using integer cents.", "input_schema": _schema({"items": {"type": "array", "items": _schema({"unit_price_cents": {"type": "integer", "minimum": 0}, "quantity": {"type": "integer", "minimum": 0}}, ("unit_price_cents", "quantity"))}}, ("items",))},
    {"name": "sensitive_export", "description": "Request the simulated sensitive operation; execution policy always denies it directly without approval.", "input_schema": _schema({"operation_id": _STRING}, ("operation_id",))},
]
_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _arguments(name, value):
    if not isinstance(name, str) or name not in _BY_NAME:
        raise ToolError("Unknown tool")
    schema = _BY_NAME[name]["input_schema"]
    if not isinstance(value, dict) or set(value) - schema["properties"].keys() or set(schema["required"]) - value.keys():
        raise ToolError("Invalid tool arguments; scope and credentials cannot be replaced")
    for key, argument in value.items():
        definition = schema["properties"][key]
        kind = definition["type"]
        if kind == "string" and not isinstance(argument, str):
            raise ToolError(f"{key} must be a string")
        if kind == "integer" and (type(argument) is not int or argument < definition.get("minimum", 0) or argument > definition.get("maximum", 2**31 - 1)):
            raise ToolError(f"Invalid {key}")
        if kind == "array" and not isinstance(argument, list):
            raise ToolError(f"{key} must be an array")
    return value


def _trusted_file(path: Path, *, may_create: bool = False):
    if not path.is_absolute() or ".." in path.parts or any(component.lower() == "old" for component in path.parts):
        raise ToolError("Trusted binding and event paths must be absolute and outside archives")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ToolError("Trusted binding and event paths must not contain symbolic links")
    if may_create:
        path.parent.mkdir(parents=True, exist_ok=True)


class BoundTools:
    def __init__(self, binding: dict, *, strict_outcome: bool = False):
        if not isinstance(binding, dict) or set(binding) != {"fixture_url", "fixture_token", "workspace", "scope", "events_path"}:
            raise ToolError("Invalid trusted tool binding")
        scope = binding["scope"]
        if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS or any(not isinstance(value, str) or not _ID.fullmatch(value) or value.lower() == "old" for value in scope.values()):
            raise ToolError("Invalid trusted scope")
        self.scope = scope.copy()
        self.client = FixtureClient(binding["fixture_url"], binding["fixture_token"], strict_outcome=strict_outcome)
        # Fail before listening if the trusted setup paired the wrong token/scope.
        if self.client.list_items(page=1, page_size=1).get("scope") != self.scope:
            raise ToolError("Fixture token does not belong to the configured trusted scope")
        self.workspace = WorkspaceFiles(binding["workspace"])
        self.events_path = Path(binding["events_path"])
        _trusted_file(self.events_path, may_create=True)
        if self.events_path.is_relative_to(self.workspace.root):
            raise ToolError("Tool audit evidence must be outside the Agent-writable workspace")
        self.token_digest = hashlib.sha256(binding["fixture_token"].encode()).digest()
        self.event_lock = threading.Lock()

    def authenticated(self, header: str) -> bool:
        if not header.startswith("Bearer ") or len(header) > 512:
            return False
        digest = hashlib.sha256(header[7:].encode()).digest()
        return hmac.compare_digest(digest, self.token_digest)

    def event(self, event: dict):
        # Never record the Authorization header or the trusted binding contents.
        record = {"time": datetime.now(timezone.utc).isoformat(), "scope": self.scope, **event}
        encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode()
        with self.event_lock:
            descriptor = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())

    def call(self, name: str, arguments: dict):
        if name == "list_items":
            return self.client.list_items(**arguments)
        if name == "write":
            return self.client.write(**arguments)
        if name == "receipt":
            return self.client.receipt(**arguments)
        if name == "ledger":
            return self.client.ledger()
        if name == "sensitive_export":
            return self.client.sensitive_export(**arguments)
        if name == "read_file":
            return {"path": arguments["path"], "content": self.workspace.read_text(arguments["path"])}
        if name == "write_file":
            return self.workspace.write_text(arguments["path"], arguments["content"])
        if name == "calculate_totals":
            return calculate_totals(arguments["items"])
        raise ToolError("Unknown tool")


class ToolsHandler(BaseHTTPRequestHandler):
    server: ToolsHTTPServer

    def log_message(self, format, *args):
        pass

    def _reply(self, status, payload):
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorize(self):
        if not self.server.tools.authenticated(self.headers.get("Authorization", "")):
            self._reply(403, {"error": "Trusted scope token required"})
            return False
        return True

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ToolError("Transfer-Encoding is not supported")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 256 * 1024:
            raise ToolError("Expected at most 256 KiB JSON input")
        return json.loads(self.rfile.read(length))

    def _finish_dispatch(self, call_id, *, known, status):
        if self.server.prewrite_gate is not None:
            self.server.prewrite_gate.finish_dispatch(call_id, known=known, status=status)

    def _experiment(self, path, *, post=False):
        gate = self.server.prewrite_gate
        if gate is None:
            self._reply(404, {"error": "Unknown endpoint"})
            return
        try:
            if post and path == "/experiment/prewrite":
                if not self._authorize():
                    return
                body = self._body()
                # Reuse ordinary argument checks before recording the window.
                # This endpoint never invokes BoundTools.call or FixtureClient.
                if not isinstance(body, dict) or body.get("tool") != "write":
                    raise GateError(400, "Prewrite report requires an ordinary write")
                _arguments("write", body.get("arguments"))
                result = gate.report(self.headers.get(TOKEN_HEADER, ""), body)
            else:
                if not gate.authenticated_admin(self.headers.get("Authorization", "")):
                    self._reply(403, {"error": "Private experiment admin credential required"})
                    return
                if not post and path == "/experiment/admin/prewrite":
                    result = gate.snapshot()
                elif post and path == "/experiment/admin/prewrite/setup":
                    result = gate.setup(self._body())
                elif post and path == "/experiment/admin/prewrite/abandon":
                    result = gate.abandon(self._body())
                else:
                    self._reply(404, {"error": "Unknown endpoint"})
                    return
            self._reply(200, result)
        except GateError as error:
            self._reply(error.status, {"error": str(error)})
        except (ToolError, ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "Invalid experiment request"})
        except (OSError, sqlite3.Error):
            # No unavailable or uncertain gate operation admits an actual call.
            self._reply(503, {"error": "Experiment control unavailable"})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self._reply(200, {"status": "ok", "service": "tools", "mode": "single_scope"})
        elif path.startswith("/experiment/") and self.server.prewrite_gate is not None:
            self._experiment(path)
        elif self._authorize():
            if path == "/tools":
                self._reply(200, {"tools": TOOLS})
            else:
                self._reply(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/experiment/") and self.server.prewrite_gate is not None:
            self._experiment(path, post=True)
            return
        if not self._authorize():
            return
        if path != "/call":
            self._reply(404, {"error": "Unknown endpoint"})
            return
        call_id = uuid4().hex
        name = "invalid"
        started = False
        dispatch_started = False
        prewrite = {}
        audit = {}
        try:
            body = self._body()
            if not isinstance(body, dict) or set(body) != {"name", "arguments"}:
                raise ToolError("Request requires only name and arguments")
            name = body["name"]
            arguments = _arguments(name, body["arguments"])
            if self.server.prewrite_gate is not None:
                gate = self.server.prewrite_gate
                credential = self.headers.get(TOKEN_HEADER, "")
                prewrite = {"chain_id": gate.chain, "attempt_id": gate.identify(credential)}
                if name == "write":
                    prewrite = gate.consume(credential, self.headers.get(CALL_HEADER, ""),
                        self.headers.get(PERMIT_HEADER, ""), arguments, call_id)
                else:
                    prewrite = gate.admit_other(credential, name, arguments, call_id)
                dispatch_started = True
            audit = {"prewrite": prewrite} if self.server.prewrite_gate is not None else {}
            self.server.tools.event({"call_id": call_id, "phase": "start", "tool": name, "arguments": arguments, **audit})
            started = True
            result = self.server.tools.call(name, arguments)
            self.server.tools.event({"call_id": call_id, "phase": "finish", "tool": name, "status": 200, "result": result, **audit})
            self._finish_dispatch(call_id, known=True, status=200)
            self._reply(200, {"result": result})
        except FixtureHTTPError as exc:
            payload = {"error": str(exc), "fixture_status": exc.status_code, "details": exc.payload}
            self.server.tools.event({"call_id": call_id, "phase": "finish", "tool": name, "status": exc.status_code, **payload, **audit})
            self._finish_dispatch(call_id, known=True, status=exc.status_code)
            self._reply(exc.status_code, payload)
        except FixtureTransportError:
            payload = {"error": "Fixture response unavailable; query receipt before retrying", "side_effect_state": "unknown"}
            self.server.tools.event({"call_id": call_id, "phase": "finish", "tool": name, "status": 502, **payload, **audit})
            self._finish_dispatch(call_id, known=False, status=502)
            self._reply(502, payload)
        except GateError as exc:
            # Rejection happens before trusted tool start and before business I/O.
            self.server.tools.event({"call_id": call_id, "phase": "rejected", "tool": name,
                "status": exc.status, "error": str(exc), "prewrite": prewrite,
                "rejection_layer": "experiment_prewrite", "business_call_started": False})
            self._reply(exc.status, {"error": str(exc), "side_effect_state": "not_dispatched_by_this_request"})
        except (ToolError, ValueError, UnicodeDecodeError) as exc:
            # Exception classes do not establish where failure occurred. Once
            # admitted, a tool or audit serializer can raise after business I/O.
            # Keep that dispatch unsettled; never admit B from a guessed 400.
            unknown = dispatch_started
            status = 502 if unknown else 400
            payload = ({"error": "Tool outcome could not be confirmed", "side_effect_state": "unknown"}
                if unknown else {"error": str(exc) if isinstance(exc, ToolError) else "Invalid JSON request"})
            self.server.tools.event({"call_id": call_id, "phase": "finish" if started else "rejected", "tool": name if isinstance(name, str) and name in _BY_NAME else "invalid", "status": status, **payload, **audit})
            self._finish_dispatch(call_id, known=not unknown, status=status)
            self._reply(status, payload)
        except (OSError, sqlite3.Error):
            # If audit persistence fails, do not make another tool request.
            self._reply(503, {"error": "Tool storage unavailable", "side_effect_state": "unknown"})
        finally:
            if dispatch_started:
                try:
                    # Only still-admitted calls transition to unknown. A known
                    # terminal is never overwritten by a disconnected client.
                    self._finish_dispatch(call_id, known=False, status=None)
                except (OSError, sqlite3.Error):
                    # Its durable admitted row remains unsettled. Bench must
                    # refuse B instead of inferring quiescence from A's death.
                    pass


class ToolsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, binding, *, prewrite_binding=None):
        self.tools = BoundTools(binding, strict_outcome=prewrite_binding is not None)
        self.prewrite_gate = PrewriteGate(prewrite_binding, self.tools.scope, self.tools.workspace.root,
            forbidden_credential_sha256={self.tools.token_digest.hex()}) if prewrite_binding is not None else None
        if self.prewrite_gate and self.prewrite_gate.admin_digest == self.tools.token_digest.hex():
            raise ValueError("Prewrite admin credential must differ from the candidate scope credential")
        super().__init__(address, ToolsHandler)

    def server_close(self):
        if self.prewrite_gate:
            self.prewrite_gate.close()
        super().server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--binding-file", type=Path, required=True)
    parser.add_argument("--prewrite-binding-file", type=Path)
    arguments = parser.parse_args()
    binding_path = arguments.binding_file.absolute()
    _trusted_file(binding_path)
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("Cannot load trusted tool binding file") from exc
    prewrite_binding = None
    if arguments.prewrite_binding_file is not None:
        prewrite_path = arguments.prewrite_binding_file.absolute()
        _trusted_file(prewrite_path)
        prewrite_binding = json.loads(prewrite_path.read_text(encoding="utf-8"))
    server = ToolsHTTPServer((arguments.host, arguments.port), binding, prewrite_binding=prewrite_binding)
    print(json.dumps({"service": "tools", "port": server.server_port, "mode": "single_scope"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
