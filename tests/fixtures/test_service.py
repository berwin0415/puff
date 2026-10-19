from __future__ import annotations

import json
import threading
from tempfile import TemporaryDirectory
from urllib import request

from fixtures.service import create_server


def _call(url: str, method: str = "GET", body: dict | None = None, key: str | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    if key:
        headers["Idempotency-Key"] = key
    req = request.Request(url, method=method, data=data, headers=headers)
    try:
        with request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except Exception as exc:
        if hasattr(exc, "code"):
            return exc.code, json.loads(exc.read())
        raise


def _server(tmp_path):
    server = create_server(state_file=tmp_path / "state.json")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_health_and_items_are_deterministic(tmp_path):
    server, thread = _server(tmp_path)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert _call(base + "/healthz") == (200, {"status": "ok", "service": "harness-lab-fixture"})
        status, value = _call(base + "/api/items?limit=2")
        assert status == 200
        assert [item["id"] for item in value["items"]] == ["item-1", "item-2"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_side_effect_retries_are_idempotent_and_conflicts_are_visible(tmp_path):
    server, thread = _server(tmp_path)
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        payload = {"operation": "charge", "payload": {"amount": 10}}
        status, first = _call(base + "/api/side-effects", "POST", payload, "run-1-step-1")
        assert status == 201 and first["status"] == "created"
        status, replay = _call(base + "/api/side-effects", "POST", payload, "run-1-step-1")
        assert status == 200 and replay["status"] == "replayed"
        assert replay["record"]["effect_id"] == first["record"]["effect_id"]
        status, conflict = _call(
            base + "/api/side-effects", "POST", {"operation": "charge", "payload": {"amount": 20}}, "run-1-step-1"
        )
        assert status == 409 and conflict["error"] == "idempotency_key_conflict"
        _, effects = _call(base + "/api/side-effects")
        assert effects["count"] == 1
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_state_survives_server_recreation(tmp_path):
    server, thread = _server(tmp_path)
    base = f"http://127.0.0.1:{server.server_port}"
    assert _call(base + "/api/side-effects", "POST", {"payload": {"x": 1}}, "persisted")[0] == 201
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()

    second, thread = _server(tmp_path)
    try:
        status, value = _call(second and f"http://127.0.0.1:{second.server_port}/api/side-effects")
        assert status == 200 and value["count"] == 1
    finally:
        second.shutdown()
        thread.join(timeout=2)
        second.server_close()
