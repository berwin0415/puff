"""Local HTTP upstream proves model budget enforcement and byte-preserving relay."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from threading import Lock, Thread
import time

import httpx
import pytest

from execution.model_proxy import ProxyConfig, create_server


TOKEN = "local-run-token-for-test"
UPSTREAM_KEY = "synthetic-upstream-secret"
TOOL_BODY = b'{"choices":[{"message":{"tool_calls":[{"id":"call-1","type":"function","function":{"name":"query","arguments":"{\\"page\\":2}"}}]}}],"usage":{"total_tokens":7}}'
SSE_BODY = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"query","arguments":"{\\"page\\":"}}]}}]}\n\ndata: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"2}"}}]}}]}\n\ndata: [DONE]\n\n'


@contextmanager
def running(server):
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def upstream():
    requests = []
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            with lock:
                requests.append({"path": self.path, "body": body,
                                 "authorization": self.headers.get("Authorization")})
            value = json.loads(body)
            stream = value.get("stream", False)
            response = SSE_BODY if stream else TOOL_BODY
            status = value.get("test_status", 200)
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            if stream:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            if stream:
                for part in [response[:30], response[30:100], response[100:]]:
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                self.wfile.write(response)

    with running(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as url:
        yield url, requests


def config(tmp_path: Path, url: str, **kwargs):
    return ProxyConfig(upstream_base_url=url + "/v1", upstream_api_key=UPSTREAM_KEY,
                       run_token=TOKEN, database=tmp_path / "calls.sqlite", **kwargs)


def post(url, body=None, *, token=TOKEN, path="/v1/chat/completions"):
    with httpx.Client(trust_env=False) as client:
        return client.post(url + path, content=body or b'{"model":"fixed-model"}',
                           headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})


@contextmanager
def running_cli(tmp_path: Path, upstream_url: str):
    """Launch the real module entrypoint; all network traffic stays on loopback."""
    token_file = tmp_path / "run-token"
    token_file.write_text(TOKEN)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {**os.environ, "MODEL_API_BASE_URL": upstream_url + "/v1",
                   "MODEL_API_KEY": UPSTREAM_KEY, "MODEL_PROXY_TIMEOUT_AT": "",
                   "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
                   "http_proxy": "", "https_proxy": "", "all_proxy": "",
                   "NO_PROXY": "127.0.0.1,localhost"}
    process = subprocess.Popen(
        [sys.executable, "-m", "execution.model_proxy", "--database", str(tmp_path / "calls.sqlite"),
         "--run-token-file", str(token_file), "--cap", "2", "--host", "127.0.0.1", "--port", str(port)],
        cwd=Path(__file__).resolve().parents[2], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        with httpx.Client(trust_env=False, timeout=0.2) as client:
            while True:
                if process.poll() is not None:
                    raise AssertionError("Model proxy CLI exited before listening")
                try:
                    if client.get(url + "/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("Model proxy CLI did not become ready")
                time.sleep(0.02)
        yield url, process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_real_cli_process_restart_preserves_budget(tmp_path, upstream):
    upstream_url, requests = upstream
    with running_cli(tmp_path, upstream_url) as (url, first):
        assert post(url).status_code == 200
    assert first.returncode is not None  # Process terminated, not just a new in-memory server.
    with running_cli(tmp_path, upstream_url) as (url, second):
        assert second.pid != first.pid
        assert post(url).status_code == 200
        blocked = post(url)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["type"] == "call_limit_exceeded"
    assert len(requests) == 2
    assert all(row["authorization"] == "Bearer " + UPSTREAM_KEY for row in requests)
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2


def test_limit_blocks_before_upstream_and_survives_proxy_recreation(tmp_path, upstream):
    upstream_url, requests = upstream
    settings = config(tmp_path, upstream_url, cap=2)
    with running(create_server(settings, "127.0.0.1", 0)) as url:
        assert post(url).status_code == 200
    with running(create_server(settings, "127.0.0.1", 0)) as url:
        assert post(url).status_code == 200
        blocked = post(url)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["type"] == "call_limit_exceeded"
    assert len(requests) == 2
    assert all(x["authorization"] == "Bearer " + UPSTREAM_KEY for x in requests)
    with sqlite3.connect(settings.database) as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2


def test_concurrent_calls_cannot_exceed_cap(tmp_path, upstream):
    upstream_url, requests = upstream
    with running(create_server(config(tmp_path, upstream_url, cap=3), "127.0.0.1", 0)) as url:
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: post(url).status_code, range(8)))
    assert statuses.count(200) == 3
    assert statuses.count(429) == 5
    assert len(requests) == 3


def test_json_tools_sse_request_and_error_status_are_preserved(tmp_path, upstream):
    upstream_url, requests = upstream
    with running(create_server(config(tmp_path, upstream_url), "127.0.0.1", 0)) as url:
        body = b'{"model":"fixed-model", "messages":[{"role":"tool","tool_call_id":"a","content":"value"}]}'
        response = post(url, body)
        assert response.status_code == 200 and response.content == TOOL_BODY
        stream_body = b'{"model":"fixed-model","stream":true,"tools":[{"type":"function","function":{"name":"query"}}]}'
        response = post(url, stream_body)
        assert response.headers["content-type"] == "text/event-stream"
        assert response.content == SSE_BODY
        failed = post(url, b'{"model":"fixed-model","test_status":503}', path="/responses")
        assert failed.status_code == 503 and failed.content == TOOL_BODY
    assert requests[0]["body"] == body
    assert requests[1]["body"] == stream_body
    assert [r["path"] for r in requests] == ["/v1/chat/completions", "/v1/chat/completions", "/v1/responses"]


def test_bad_token_and_unapproved_path_never_reach_upstream(tmp_path, upstream):
    upstream_url, requests = upstream
    settings = config(tmp_path, upstream_url, cap=1)
    with running(create_server(settings, "127.0.0.1", 0)) as url:
        assert post(url, token="other-user-token").status_code == 401
        for path in ["/v1/models", "/v1/responses?upstream=https://example.invalid", "/%2e%2e/responses", "/https://example.invalid/responses"]:
            assert post(url, path=path).status_code == 404
        with httpx.Client(trust_env=False) as client:
            health = client.get(url + "/health")
        assert health.json() == {"status": "ok"}
        assert UPSTREAM_KEY not in health.text and TOKEN not in health.text
        assert post(url).status_code == 200  # Rejections did not consume the valid budget.
    assert len(requests) == 1


def test_injected_timeout_consumes_count_and_can_be_retried(tmp_path, upstream):
    upstream_url, requests = upstream
    settings = config(tmp_path, upstream_url, cap=2, inject_timeout_at=frozenset({1}))
    with running(create_server(settings, "127.0.0.1", 0)) as url:
        timed_out = post(url)
        assert timed_out.status_code == 504
        assert UPSTREAM_KEY not in timed_out.text and TOKEN not in timed_out.text
        assert post(url).status_code == 200
        assert post(url).status_code == 429
    assert len(requests) == 1
    with sqlite3.connect(settings.database) as db:
        assert db.execute("SELECT outcome FROM calls ORDER BY id").fetchall() == [("injected_timeout",), ("forwarded",)]


def test_run_binding_and_cap_cannot_be_changed_on_restart(tmp_path, upstream):
    upstream_url, _ = upstream
    settings = config(tmp_path, upstream_url, cap=2)
    server = create_server(settings, "127.0.0.1", 0)
    server.server_close()
    with pytest.raises(ValueError, match="different run or call limit"):
        create_server(replace(settings, run_token="other-run"), "127.0.0.1", 0)
    with pytest.raises(ValueError, match="different run or call limit"):
        create_server(replace(settings, cap=20), "127.0.0.1", 0)


def test_unreachable_upstream_is_counted_without_secret_in_error(tmp_path):
    settings = config(tmp_path, "http://127.0.0.1:1", cap=1)
    with running(create_server(settings, "127.0.0.1", 0)) as url:
        failed = post(url)
        assert failed.status_code == 502
        assert UPSTREAM_KEY not in failed.text and TOKEN not in failed.text
        assert post(url).status_code == 429
