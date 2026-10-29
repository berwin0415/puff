"""验收 token 代理：JSON/SSE usage、Anthropic 合并、未知、冲突和 token cap。"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Lock, Thread
import time

import httpx
import pytest

from execution.token_proxy import TokenProxyConfig, create_server


TOKEN = "session-token-for-test"
UPSTREAM_KEY = "upstream-secret-for-test"

CHAT_JSON = {
    "id": "chat-1",
    "choices": [{"message": {"content": "ok"}}],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens_details": {"reasoning_tokens": 1},
    },
}
CHAT_STREAM = [
    b'data: {"id":"chat-2","choices":[{"delta":{"content":"ok"}}]}\n\n',
    b'data: {"id":"chat-2","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n',
    b"data: [DONE]\n\n",
]
ANTHROPIC_STREAM = [
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg-1","usage":{"input_tokens":12,"cache_read_input_tokens":3,"cache_creation_input_tokens":1}}}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]
RESPONSES_STREAM = [
    b'data: {"type":"response.completed","response":{"id":"resp-1","usage":{"input_tokens":8,"output_tokens":4,"total_tokens":12,"input_tokens_details":{"cached_tokens":2}}}}\n\n',
]


def send_json(handler, payload: dict) -> None:
    data = json.dumps(payload).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def send_sse(handler, chunks: list[bytes]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    for chunk in chunks:
        # Split once to exercise the proxy's line buffer across chunks.
        for part in (chunk[:11], chunk[11:]):
            if not part:
                continue
            handler.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            handler.wfile.flush()
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


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
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            mode = self.headers.get("X-Test-Mode", "ok")
            with lock:
                requests.append({"path": self.path, "body": body, "mode": mode})
            if self.path.endswith("/messages"):
                if body.get("stream"):
                    send_sse(self, ANTHROPIC_STREAM)
                else:
                    send_json(self, {"id": "msg-1", "usage": {
                        "input_tokens": 12, "output_tokens": 7,
                        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1,
                    }})
                return
            if self.path.endswith("/responses"):
                send_sse(self, RESPONSES_STREAM)
                return
            if mode == "none":
                send_json(self, {"id": "chat-none", "choices": []})
                return
            if mode == "conflict":
                send_sse(self, [
                    b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n',
                    b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":3,"total_tokens":6}}\n\n',
                    b"data: [DONE]\n\n",
                ])
                return
            if body.get("stream"):
                send_sse(self, CHAT_STREAM)
            else:
                send_json(self, CHAT_JSON)

    with running(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as url:
        yield url, requests


@contextmanager
def proxy(tmp_path: Path, upstream_url: str, **kwargs):
    config = TokenProxyConfig(
        upstream_base_url=upstream_url,
        upstream_api_key=UPSTREAM_KEY,
        run_token=TOKEN,
        database=tmp_path / "tokens.sqlite",
        ledger=tmp_path / "ledger.jsonl",
        session_id="session-1",
        candidate="candidate-x",
        phase="adapter",
        **kwargs,
    )
    server = create_server(config, host="127.0.0.1", port=0)
    with running(server) as url:
        yield url, config


def post(url: str, body: dict, *, path: str = "/v1/chat/completions", token: str = TOKEN,
         headers: dict | None = None) -> httpx.Response:
    merged = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    merged.update(headers or {})
    with httpx.Client(trust_env=False, timeout=10) as client:
        return client.post(url + path, content=json.dumps(body).encode(), headers=merged)


def rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_chat_json_records_usage(tmp_path, upstream):
    upstream_url, _ = upstream
    with proxy(tmp_path, upstream_url) as (url, _):
        response = post(url, {"model": "fixed-model", "messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 200
    row = rows(tmp_path)[-1]
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5
    assert row["total_tokens"] == 15
    assert row["cache_read_tokens"] == 2
    assert row["reasoning_tokens"] == 1
    assert row["status"] == "reported"
    assert row["source"] == "proxy_json"
    assert row["request_id"] == "chat-1"
    assert row["model"] == "fixed-model"
    assert "hello" not in (tmp_path / "ledger.jsonl").read_text()


def test_chat_stream_injects_include_usage(tmp_path, upstream):
    upstream_url, requests = upstream
    with proxy(tmp_path, upstream_url) as (url, _):
        response = post(url, {"model": "fixed-model", "messages": [], "stream": True})
        assert response.status_code == 200
    sent = requests[-1]["body"]
    assert sent["stream_options"]["include_usage"] is True
    row = rows(tmp_path)[-1]
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (3, 2, 5)
    assert row["status"] == "reported"
    assert row["source"] == "proxy_stream"


def test_anthropic_stream_merges_input_and_output(tmp_path, upstream):
    upstream_url, _ = upstream
    with proxy(tmp_path, upstream_url) as (url, _):
        response = post(url, {"model": "claude-fixed", "messages": [], "stream": True},
                        path="/v1/messages")
        assert response.status_code == 200
    row = rows(tmp_path)[-1]
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 7
    assert row["cache_read_tokens"] == 3
    assert row["cache_write_tokens"] == 1
    assert row["total_tokens"] == 23
    assert row["status"] == "reported"


def test_responses_completed_usage(tmp_path, upstream):
    upstream_url, _ = upstream
    with proxy(tmp_path, upstream_url) as (url, _):
        response = post(url, {"model": "fixed-model", "input": "hello", "stream": True},
                        path="/v1/responses")
        assert response.status_code == 200
    row = rows(tmp_path)[-1]
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (8, 4, 12)
    assert row["cache_read_tokens"] == 2
    assert row["status"] == "reported"


def test_unknown_usage_is_not_zero(tmp_path, upstream):
    upstream_url, _ = upstream
    with proxy(tmp_path, upstream_url) as (url, _):
        response = post(url, {"model": "fixed-model", "messages": []}, headers={"X-Test-Mode": "none"})
        assert response.status_code == 200
    row = rows(tmp_path)[-1]
    assert row["status"] == "unknown"
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["total_tokens"] is None


def test_token_cap_denies_next_call(tmp_path, upstream):
    upstream_url, requests = upstream
    with proxy(tmp_path, upstream_url, token_cap=10) as (url, config):
        first = post(url, {"model": "fixed-model", "messages": []})
        assert first.status_code == 200
        second = post(url, {"model": "fixed-model", "messages": []})
        assert second.status_code == 429
        assert second.json()["error"]["type"] == "token_limit"
    assert len(requests) == 1
    ledger = rows(tmp_path)
    assert ledger[0]["status"] == "reported"
    assert ledger[0]["total_tokens"] == 15
    assert ledger[1]["status"] == "denied"
    assert ledger[1]["error"] == "token_limit"


def test_wrong_token_is_401_without_upstream(tmp_path, upstream):
    upstream_url, requests = upstream
    with proxy(tmp_path, upstream_url) as (url, _):
        response = post(url, {"model": "fixed-model", "messages": []}, token="wrong")
        assert response.status_code == 401
    assert requests == []
    row = rows(tmp_path)[-1]
    assert row["status"] == "denied"
    assert row["error"] == "unauthorized"


def test_conflicting_usage_marks_conflict(tmp_path, upstream):
    upstream_url, _ = upstream
    with proxy(tmp_path, upstream_url) as (url, _):
        response = post(url, {"model": "fixed-model", "messages": [], "stream": True},
                        headers={"X-Test-Mode": "conflict"})
        assert response.status_code == 200
    row = rows(tmp_path)[-1]
    assert row["status"] == "conflict"
    assert row["total_tokens"] == 5


def test_health_reports_budget(tmp_path, upstream):
    upstream_url, _ = upstream
    with proxy(tmp_path, upstream_url, token_cap=100) as (url, _):
        post(url, {"model": "fixed-model", "messages": []})
        with httpx.Client(trust_env=False) as client:
            health = client.get(url + "/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["total_tokens"] == 15
        assert payload["token_cap"] == 100
        assert payload["calls"] == 1


@contextmanager
def running_cli(tmp_path: Path, upstream_url: str):
    token_file = tmp_path / "run-token"
    token_file.write_text(TOKEN)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        **os.environ,
        "MODEL_API_BASE_URL": upstream_url,
        "MODEL_API_KEY": UPSTREAM_KEY,
        "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
        "http_proxy": "", "https_proxy": "", "all_proxy": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    process = subprocess.Popen(
        [
            sys.executable, "-m", "execution.token_proxy",
            "--database", str(tmp_path / "cli.sqlite"),
            "--ledger", str(tmp_path / "cli-ledger.jsonl"),
            "--run-token-file", str(token_file),
            "--token-cap", "1000",
            "--session-id", "cli-session",
            "--candidate", "cli-candidate",
            "--phase", "adapter",
            "--host", "127.0.0.1",
            "--port", str(port),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        with httpx.Client(trust_env=False, timeout=0.2) as client:
            while True:
                if process.poll() is not None:
                    raise AssertionError("token 代理 CLI 在监听前退出")
                try:
                    if client.get(url + "/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("token 代理 CLI 未就绪")
                time.sleep(0.02)
        yield url
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_cli_process_forwards_and_records(tmp_path, upstream):
    upstream_url, _ = upstream
    with running_cli(tmp_path, upstream_url) as url:
        response = post(url, {"model": "fixed-model", "messages": []})
        assert response.status_code == 200
    rows = [json.loads(line) for line in (tmp_path / "cli-ledger.jsonl").read_text().splitlines()]
    assert rows[-1]["session_id"] == "cli-session"
    assert rows[-1]["status"] == "reported"
    assert rows[-1]["total_tokens"] == 15
