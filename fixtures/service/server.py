"""A small, deterministic HTTP service for T1/T2 and retry experiments.

The fixture is intentionally boring: its state is explicit, persisted as one
JSON document, and all externally visible side effects require an idempotency
key.  It can be started with ``python -m fixtures.service.server`` or embedded
in a test with :func:`create_server`.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_ITEMS = [
    {"id": "item-1", "name": "alpha", "value": 10},
    {"id": "item-2", "name": "beta", "value": 20},
    {"id": "item-3", "name": "gamma", "value": 30},
]


class FixtureState:
    """Thread-safe state store backed by a JSON file.

    The file is suitable for a local Docker volume.  Writes are atomic so a
    process interruption does not leave a partially written fixture state.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "effects": {},
            "requests": [],
            "failure_attempts": {},
        }
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                with self._lock:
                    self._state.update(value)
        except (OSError, ValueError):
            # A corrupt fixture file should not prevent a local run.  The
            # caller can inspect the file and reset it through /api/reset.
            return

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def reset(self) -> None:
        with self._lock:
            self._state = {"effects": {}, "requests": [], "failure_attempts": {}}
            self._persist_locked()

    def record_request(self, method: str, path: str) -> None:
        with self._lock:
            requests = self._state.setdefault("requests", [])
            requests.append({"method": method, "path": path})
            # Keep fixture state bounded during long retry experiments.
            del requests[:-1000]
            self._persist_locked()

    def apply_effect(
        self,
        key: str,
        operation: str,
        payload: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Apply an idempotent side effect.

        Returns ``("created", record)`` on the first request and
        ``("replayed", record)`` for an identical retry.  Reusing a key with
        a different operation or payload returns ``("conflict", record)``.
        """

        with self._lock:
            effects: dict[str, dict[str, Any]] = self._state.setdefault("effects", {})
            fingerprint = {"operation": operation, "payload": payload}
            old = effects.get(key)
            if old is not None:
                if old.get("fingerprint") == fingerprint:
                    return "replayed", old["record"]
                return "conflict", old["record"]

            record = {
                "effect_id": f"effect-{len(effects) + 1}",
                "idempotency_key": key,
                "operation": operation,
                "payload": payload,
                "request_id": str(uuid.uuid4()),
            }
            effects[key] = {"fingerprint": fingerprint, "record": record}
            self._persist_locked()
            return "created", record

    def failure_attempt(self, key: str) -> int:
        with self._lock:
            attempts: dict[str, int] = self._state.setdefault("failure_attempts", {})
            attempts[key] = int(attempts.get(key, 0)) + 1
            self._persist_locked()
            return attempts[key]

    def _persist_locked(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class FixtureHandler(BaseHTTPRequestHandler):
    server: "FixtureHTTPServer"

    # Keep logs quiet in tests; callers can inspect state.requests instead.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    @property
    def state(self) -> FixtureState:
        return self.server.fixture_state

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be JSON") from None
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        self.state.record_request("GET", parsed.path)
        if parsed.path == "/healthz":
            self._json(200, {"status": "ok", "service": "harness-lab-fixture"})
            return
        if parsed.path == "/api/items":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", [len(DEFAULT_ITEMS)])[0])
            self._json(200, {"items": DEFAULT_ITEMS[: max(0, min(limit, len(DEFAULT_ITEMS)))]})
            return
        if parsed.path == "/api/side-effects":
            snapshot = self.state.snapshot()
            records = [value["record"] for value in snapshot.get("effects", {}).values()]
            self._json(200, {"effects": records, "count": len(records)})
            return
        if parsed.path == "/api/state":
            self._json(200, self.state.snapshot())
            return
        if parsed.path in {"/browser", "/browser/"}:
            self._html(200, BROWSER_PAGE)
            return
        self._json(404, {"error": "not_found", "path": parsed.path})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        self.state.record_request("POST", parsed.path)
        if parsed.path == "/api/reset":
            self.state.reset()
            self._json(200, {"status": "reset"})
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._json(400, {"error": "invalid_json", "message": str(exc)})
            return
        if parsed.path == "/api/side-effects":
            key = self.headers.get("Idempotency-Key") or str(body.get("idempotency_key", ""))
            if not key:
                self._json(400, {"error": "idempotency_key_required"})
                return
            operation = str(body.get("operation", "default"))
            payload = body.get("payload", {})
            status, record = self.state.apply_effect(key, operation, payload)
            if status == "conflict":
                self._json(409, {"error": "idempotency_key_conflict", "record": record})
            else:
                self._json(201 if status == "created" else 200, {"status": status, "record": record})
            return
        if parsed.path == "/api/fail-once":
            key = str(body.get("key", "default"))
            attempt = self.state.failure_attempt(key)
            if attempt == 1:
                self._json(503, {"error": "injected_failure", "attempt": attempt})
            else:
                self._json(200, {"status": "ok", "attempt": attempt})
            return
        if parsed.path in {"/browser/submit", "/browser/submit/"}:
            name = str(body.get("name", "")).strip()
            self._json(200, {"status": "accepted", "name": name})
            return
        self._json(404, {"error": "not_found", "path": parsed.path})


BROWSER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Harness Fixture</title></head>
<body>
  <main><h1 data-testid="title">Harness Lab Fixture</h1>
    <p id="result" data-testid="result">ready</p>
    <form id="fixture-form">
      <label>Name <input id="name" name="name" autocomplete="off"></label>
      <button id="submit" type="submit">Submit</button>
    </form>
  </main>
  <script>
    document.getElementById('fixture-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const name = document.getElementById('name').value;
      const response = await fetch('/browser/submit', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})
      });
      const data = await response.json();
      document.getElementById('result').textContent = data.status + ':' + data.name;
    });
  </script>
</body></html>"""


class FixtureHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], fixture_state: FixtureState):
        super().__init__(address, FixtureHandler)
        self.fixture_state = fixture_state


def create_server(
    host: str = "127.0.0.1",
    port: int = 0,
    state_file: str | os.PathLike[str] | None = None,
) -> FixtureHTTPServer:
    """Create (but do not start) a fixture server."""

    return FixtureHTTPServer((host, port), FixtureState(state_file))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("FIXTURE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("FIXTURE_PORT", "8080")))
    parser.add_argument("--state-file", default=os.getenv("FIXTURE_STATE_FILE", "/data/fixture-state.json"))
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.state_file)
    print(f"fixture listening on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
