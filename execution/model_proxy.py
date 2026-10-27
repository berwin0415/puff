"""计数并转发模型请求，不改变 API 协议。

每个进程服务一个运行凭证和一份持久 SQLite 调用预算。
上游凭证只由本服务持有，候选仅获得本地运行凭证。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlsplit

import httpx


ALLOWED_PATHS = {
    "/chat/completions": "/chat/completions",
    "/v1/chat/completions": "/chat/completions",
    "/responses": "/responses",
    "/v1/responses": "/responses",
}
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


class CallBudget:
    """出网前预占调用额度；每个数据库永久绑定一条运行。"""

    def __init__(self, database: Path, run_token: str, cap: int):
        if cap < 1 or not run_token or "\n" in run_token or "\r" in run_token:
            raise ValueError("模型代理预算配置无效")
        self.database = Path(database)
        self.cap = cap
        self.database.parent.mkdir(parents=True, exist_ok=True)
        binding = hashlib.sha256(run_token.encode()).hexdigest()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT NOT NULL, cap INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, path TEXT NOT NULL, outcome TEXT NOT NULL, upstream_status INTEGER)")
            db.execute("INSERT OR IGNORE INTO budget VALUES(1, ?, ?)", (binding, cap))
            saved = db.execute("SELECT token_hash, cap FROM budget WHERE id=1").fetchone()
            if saved != (binding, cap):
                raise ValueError("预算数据库对应其他运行或调用上限")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=30)

    def reserve(self, path: str) -> int | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            if count >= self.cap:
                return None
            cursor = db.execute(
                "INSERT INTO calls(started_at,path,outcome) VALUES(?,?,'reserved')",
                (datetime.now(timezone.utc).isoformat(), path),
            )
            return int(cursor.lastrowid)

    def finish(self, call_id: int, outcome: str, status: int | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE calls SET outcome=?, upstream_status=? WHERE id=?", (outcome, status, call_id))


@dataclass(frozen=True)
class ProxyConfig:
    upstream_base_url: str
    upstream_api_key: str = field(repr=False)
    run_token: str = field(repr=False)
    database: Path
    cap: int = 20
    timeout_seconds: float = 60
    inject_timeout_at: frozenset[int] = frozenset()
    max_request_bytes: int = 16 * 1024 * 1024

    def __post_init__(self):
        parsed = urlsplit(self.upstream_base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("上游模型 API 基础地址无效")
        if not self.upstream_api_key or any(c in self.upstream_api_key for c in "\r\n"):
            raise ValueError("上游凭证缺失或无效")
        if self.timeout_seconds <= 0 or self.max_request_bytes < 1:
            raise ValueError("模型代理限制无效")


def forwarded_headers(headers, *, request: bool) -> dict[str, str]:
    excluded = HOP_HEADERS | {x.strip().lower() for x in headers.get("connection", "").split(",")}
    if request:
        excluded |= {"host", "authorization", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in excluded}


def create_server(config: ProxyConfig, host: str = "0.0.0.0", port: int = 8081) -> ThreadingHTTPServer:
    budget = CallBudget(config.database, config.run_token, config.cap)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *args):
            # 请求头、路径与 HTTP 客户端异常可能包含秘密。
            pass

        def reply(self, status: int, error_type: str, message: str):
            data = json.dumps({"error": {"type": error_type, "message": message}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def do_GET(self):
            if self.path == "/health":
                data = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.reply(404, "not_found", "未知接口")

        def do_POST(self):
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + config.run_token):
                self.reply(401, "unauthorized", "运行凭证无效")
                return
            suffix = ALLOWED_PATHS.get(self.path)
            if suffix is None:
                self.reply(404, "not_found", "不允许访问该模型接口")
                return
            if self.headers.get("Transfer-Encoding"):
                self.reply(400, "invalid_request", "必须提供 Content-Length")
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > config.max_request_bytes:
                self.reply(413, "invalid_request", "请求体长度无效")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self.reply(400, "invalid_request", "请求体不完整")
                return
            call_id = budget.reserve(suffix)
            if call_id is None:
                self.reply(429, "call_limit_exceeded", "模型请求次数已达上限")
                return
            if call_id in config.inject_timeout_at:
                budget.finish(call_id, "injected_timeout")
                self.reply(504, "model_timeout", "已注入模型超时故障")
                return
            headers = forwarded_headers(self.headers, request=True)
            headers["Authorization"] = "Bearer " + config.upstream_api_key
            url = config.upstream_base_url.rstrip("/") + suffix
            sent_headers = False
            try:
                with httpx.Client(timeout=config.timeout_seconds, follow_redirects=False) as client:
                    with client.stream("POST", url, content=body, headers=headers) as response:
                        self.send_response_only(response.status_code)
                        response_headers = forwarded_headers(response.headers, request=False)
                        chunked = "content-length" not in response_headers
                        for name, value in response_headers.items():
                            self.send_header(name, value)
                        if chunked:
                            self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        sent_headers = True
                        for chunk in response.iter_raw():
                            if chunked:
                                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                            else:
                                self.wfile.write(chunk)
                            self.wfile.flush()
                        if chunked:
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        budget.finish(call_id, "forwarded", response.status_code)
            except httpx.TimeoutException:
                budget.finish(call_id, "upstream_timeout")
                if not sent_headers:
                    self.reply(504, "model_timeout", "上游模型请求超时")
                self.close_connection = True
            except httpx.HTTPError:
                budget.finish(call_id, "upstream_error")
                if not sent_headers:
                    self.reply(502, "upstream_error", "上游模型请求失败")
                self.close_connection = True
            except (BrokenPipeError, ConnectionResetError):
                budget.finish(call_id, "client_disconnected")
                self.close_connection = True

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-token-file", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=20)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--upstream-base-env", default="MODEL_API_BASE_URL")
    parser.add_argument("--upstream-key-env", default="MODEL_API_KEY")
    parser.add_argument("--inject-timeout-at", default=os.environ.get("MODEL_PROXY_TIMEOUT_AT", ""))
    args = parser.parse_args()
    config = ProxyConfig(
        upstream_base_url=os.environ.get(args.upstream_base_env, ""),
        upstream_api_key=os.environ.get(args.upstream_key_env, ""),
        run_token=args.run_token_file.read_text().strip(), database=args.database,
        cap=args.cap, timeout_seconds=args.timeout_seconds,
        inject_timeout_at=frozenset(int(x) for x in args.inject_timeout_at.split(",") if x),
    )
    server = create_server(config, args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
