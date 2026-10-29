"""按 token 计量的接入代理：转发模型请求、记录 usage、执行 token 上限。

与 `execution/model_proxy.py` 的区别：本模块不只看调用次数，还解析供应商返回的
input/output/cache/reasoning token，并写入逐请求 JSONL 台账。它只为接入实验服务，
不改变模型 API 协议，也不保存提示、响应正文或凭证。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit

import httpx


ALLOWED_PATHS = {
    "/chat/completions": "/chat/completions",
    "/v1/chat/completions": "/chat/completions",
    "/responses": "/responses",
    "/v1/responses": "/responses",
    "/messages": "/messages",
    "/v1/messages": "/messages",
}
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
USAGE_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _nested(mapping: dict, *keys: str) -> int | None:
    value = mapping
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _int(value)


def _merge_usage(target: dict, values: dict) -> None:
    for key in USAGE_FIELDS:
        value = _int(values.get(key))
        if value is None:
            continue
        if target.get(key) is not None and target[key] != value:
            target["_conflict"] = True
            continue
        target[key] = value


def usage_from_openai_chat(payload: dict) -> dict:
    usage = _dict(payload.get("usage"))
    return {
        "input_tokens": _int(usage.get("prompt_tokens")),
        "output_tokens": _int(usage.get("completion_tokens")),
        "total_tokens": _int(usage.get("total_tokens")),
        "cache_read_tokens": _nested(usage, "prompt_tokens_details", "cached_tokens"),
        "cache_write_tokens": None,
        "reasoning_tokens": _nested(usage, "completion_tokens_details", "reasoning_tokens"),
    }


def usage_from_openai_responses(payload: dict) -> dict:
    usage = _dict(payload.get("usage"))
    return {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "total_tokens": _int(usage.get("total_tokens")),
        "cache_read_tokens": _nested(usage, "input_tokens_details", "cached_tokens"),
        "cache_write_tokens": None,
        "reasoning_tokens": _nested(usage, "output_tokens_details", "reasoning_tokens"),
    }


def usage_from_anthropic(payload: dict) -> dict:
    usage = _dict(payload.get("usage"))
    if not usage:
        usage = _dict(_dict(payload.get("message")).get("usage"))
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    total_tokens = _int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
        if cache_read is not None:
            total_tokens += cache_read
        if cache_write is not None:
            total_tokens += cache_write
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": _nested(usage, "output_tokens_details", "reasoning_tokens"),
    }


def usage_for_payload(provider: str, payload: dict) -> dict:
    if provider == "anthropic":
        return usage_from_anthropic(payload)
    if provider == "openai_responses":
        nested = _dict(payload.get("response"))
        if nested.get("usage"):
            return usage_from_openai_responses(nested)
        return usage_from_openai_responses(payload)
    return usage_from_openai_chat(payload)


def effective_total(provider: str, values: dict) -> int:
    total = _int(values.get("total_tokens"))
    if total is not None:
        return total
    input_tokens = _int(values.get("input_tokens"))
    output_tokens = _int(values.get("output_tokens"))
    if provider == "anthropic":
        if input_tokens is None and output_tokens is None:
            return 0
        return (
            (input_tokens or 0)
            + (output_tokens or 0)
            + (_int(values.get("cache_read_tokens")) or 0)
            + (_int(values.get("cache_write_tokens")) or 0)
        )
    if input_tokens is not None and output_tokens is not None:
        return input_tokens + output_tokens
    return 0


class Usage:
    """跨流式事件合并 usage；缺失保持 None，冲突保留标记。"""

    def __init__(self) -> None:
        self.values = {key: None for key in USAGE_FIELDS}
        self.conflict = False

    def merge(self, values: dict) -> None:
        before = dict(self.values)
        _merge_usage(self.values, values)
        if self.values.get("_conflict") or before.get("_conflict"):
            self.conflict = True
        self.values.pop("_conflict", None)

    def status(self) -> str:
        if self.conflict:
            return "conflict"
        if self.values["total_tokens"] is not None:
            return "reported"
        if self.values["input_tokens"] is not None and self.values["output_tokens"] is not None:
            return "reported"
        return "unknown"

    def effective_total(self) -> int:
        if self.values["total_tokens"] is not None:
            return self.values["total_tokens"]
        if self.values["input_tokens"] is not None and self.values["output_tokens"] is not None:
            return self.values["input_tokens"] + self.values["output_tokens"]
        return 0


class TokenBudget:
    """SQLite token 预算；每个数据库永久绑定一条 session。"""

    def __init__(self, database: Path, run_token: str, token_cap: int, call_cap: int):
        if token_cap < 1 or call_cap < 1:
            raise ValueError("token 预算配置无效")
        if not run_token or "\n" in run_token or "\r" in run_token:
            raise ValueError("session token 无效")
        self.database = Path(database)
        self.token_cap = token_cap
        self.call_cap = call_cap
        self.database.parent.mkdir(parents=True, exist_ok=True)
        binding = hashlib.sha256(run_token.encode()).hexdigest()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS budget ("
                "id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT NOT NULL, "
                "token_cap INTEGER NOT NULL, call_cap INTEGER NOT NULL, total_tokens INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, path TEXT NOT NULL, "
                "outcome TEXT NOT NULL, upstream_status INTEGER, input_tokens INTEGER, "
                "output_tokens INTEGER, total_tokens INTEGER)"
            )
            db.execute(
                "INSERT OR IGNORE INTO budget VALUES(1, ?, ?, ?, 0)",
                (binding, token_cap, call_cap),
            )
            saved = db.execute("SELECT token_hash, token_cap, call_cap FROM budget WHERE id=1").fetchone()
            if saved != (binding, token_cap, call_cap):
                raise ValueError("预算数据库对应其他 session 或上限")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=30)

    def reserve(self, path: str) -> tuple[int | None, str]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT total_tokens, token_cap, call_cap FROM budget WHERE id=1").fetchone()
            total, token_cap, call_cap = row
            count = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            if total >= token_cap:
                return None, "token_limit"
            if count >= call_cap:
                return None, "call_limit"
            cursor = db.execute(
                "INSERT INTO calls(started_at,path,outcome) VALUES(?,?,'reserved')",
                (_now(), path),
            )
            return int(cursor.lastrowid), "ok"

    def finish(self, call_id: int, outcome: str, status: int | None, values: dict, total: int) -> int:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE calls SET outcome=?, upstream_status=?, input_tokens=?, "
                "output_tokens=?, total_tokens=? WHERE id=?",
                (
                    outcome, status, values.get("input_tokens"),
                    values.get("output_tokens"), total, call_id,
                ),
            )
            db.execute("UPDATE budget SET total_tokens=total_tokens+? WHERE id=1", (total,))
            return int(db.execute("SELECT total_tokens FROM budget WHERE id=1").fetchone()[0])

    def snapshot(self) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT total_tokens, token_cap, call_cap FROM budget WHERE id=1").fetchone()
            count = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        return {
            "total_tokens": int(row[0]),
            "token_cap": int(row[1]),
            "call_cap": int(row[2]),
            "calls": int(count),
        }


class TokenLedger:
    """逐请求 JSONL 台账；只写数字/标识，不写正文和凭证。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def append(self, row: dict) -> None:
        line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


@dataclass(frozen=True)
class TokenProxyConfig:
    upstream_base_url: str
    upstream_api_key: str = field(repr=False)
    run_token: str = field(repr=False)
    database: Path
    ledger: Path
    token_cap: int = 500_000_000
    call_cap: int = 100_000
    timeout_seconds: float = 120
    max_request_bytes: int = 16 * 1024 * 1024
    session_id: str = "integration"
    candidate: str = "unknown"
    phase: str = "unknown"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.upstream_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("上游模型 API 基础地址无效")
        if not self.upstream_api_key or any(c in self.upstream_api_key for c in "\r\n"):
            raise ValueError("上游凭证缺失或无效")
        if self.timeout_seconds <= 0 or self.max_request_bytes < 1:
            raise ValueError("token 代理限制无效")
        if not self.session_id or any(c in self.session_id for c in "\r\n"):
            raise ValueError("session 标识无效")


def _provider_for(path: str) -> str:
    if path.endswith("/messages"):
        return "anthropic"
    if path.endswith("/responses"):
        return "openai_responses"
    return "openai_chat"


def _forwarded_headers(headers, *, request: bool) -> dict[str, str]:
    excluded = HOP_HEADERS | {x.strip().lower() for x in headers.get("connection", "").split(",")}
    if request:
        excluded |= {"host", "authorization", "x-api-key", "content-length"}
    else:
        excluded |= {"content-length", "content-encoding", "transfer-encoding", "connection"}
    return {key: value for key, value in headers.items() if key.lower() not in excluded}


def _upstream_auth(provider: str, api_key: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": api_key}
    return {"Authorization": "Bearer " + api_key}


def _consume_sse_line(line: bytes, provider: str, usage: Usage) -> None:
    if not line.startswith(b"data:"):
        return
    data = line[5:].strip()
    if not data or data == b"[DONE]":
        return
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return
    if provider == "anthropic":
        if payload.get("type") in {"message_start", "message_delta"}:
            usage.merge(usage_from_anthropic(payload))
        return
    if provider == "openai_responses":
        if payload.get("type") == "response.completed":
            usage.merge(usage_for_payload(provider, payload))
        return
    if isinstance(payload.get("usage"), dict):
        usage.merge(usage_from_openai_chat(payload))


def create_server(config: TokenProxyConfig, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    budget = TokenBudget(config.database, config.run_token, config.token_cap, config.call_cap)
    ledger = TokenLedger(config.ledger)

    def record(
        *, call_id: int | None, path: str, provider: str, model: str | None,
        stream: bool, usage: Usage | None, status: str, upstream_status: int | None,
        request_id: str | None, source: str, error: str | None,
        outcome: str = "forwarded",
    ) -> None:
        values = usage.values if usage else {key: None for key in USAGE_FIELDS}
        total = effective_total(provider, values) if usage else 0
        if total and values.get("total_tokens") is None:
            values["total_tokens"] = total
        if call_id is not None:
            budget.finish(call_id, outcome, upstream_status, values, total)
        ledger.append({
            "ts": _now(),
            "session_id": config.session_id,
            "candidate": config.candidate,
            "phase": config.phase,
            "provider": provider,
            "model": model,
            "request_id": request_id,
            "path": path,
            "stream": stream,
            "input_tokens": values.get("input_tokens"),
            "output_tokens": values.get("output_tokens"),
            "total_tokens": values.get("total_tokens"),
            "cache_read_tokens": values.get("cache_read_tokens"),
            "cache_write_tokens": values.get("cache_write_tokens"),
            "reasoning_tokens": values.get("reasoning_tokens"),
            "status": status,
            "upstream_status": upstream_status,
            "source": source,
            "error": error,
        })

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *args):
            # 请求头、路径与客户端异常可能包含秘密。
            pass

        def reply(self, status: int, error_type: str, message: str) -> None:
            data = json.dumps({"error": {"type": error_type, "message": message}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def authorized(self) -> bool:
            bearer = self.headers.get("Authorization", "")
            key = self.headers.get("x-api-key", "")
            expected = "Bearer " + config.run_token
            return secrets.compare_digest(bearer, expected) or secrets.compare_digest(key, config.run_token)

        def do_GET(self) -> None:
            if self.path not in {"/health", "/v1/health"}:
                self.reply(404, "not_found", "未知接口")
                return
            data = json.dumps({
                "status": "ok",
                "session_id": config.session_id,
                "candidate": config.candidate,
                "phase": config.phase,
                **budget.snapshot(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            recorded = False

            def emit(**kwargs) -> None:
                nonlocal recorded
                if recorded:
                    return
                recorded = True
                record(**kwargs)

            if not self.authorized():
                record(
                    call_id=None, path=self.path, provider="unknown", model=None,
                    stream=False, usage=None, status="denied", upstream_status=None,
                    request_id=None, source="proxy", error="unauthorized",
                    outcome="denied",
                )
                self.reply(401, "unauthorized", "session token 无效")
                return
            suffix = ALLOWED_PATHS.get(self.path)
            if suffix is None:
                record(
                    call_id=None, path=self.path, provider="unknown", model=None,
                    stream=False, usage=None, status="denied", upstream_status=None,
                    request_id=None, source="proxy", error="path_not_allowed",
                    outcome="denied",
                )
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
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self.reply(400, "invalid_request", "请求体必须是 JSON 对象")
                return
            provider = _provider_for(suffix)
            stream = payload.get("stream") is True
            if provider == "openai_chat" and stream:
                options = payload.get("stream_options")
                if not isinstance(options, dict):
                    options = {}
                options["include_usage"] = True
                payload["stream_options"] = options
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            model = payload.get("model") if isinstance(payload.get("model"), str) else None
            call_id, reason = budget.reserve(suffix)
            if call_id is None:
                record(
                    call_id=None, path=suffix, provider=provider, model=model,
                    stream=stream, usage=None, status="denied", upstream_status=None,
                    request_id=None, source="proxy", error=reason, outcome="denied",
                )
                self.reply(429, reason, "token/调用预算已用尽")
                return
            headers = _forwarded_headers(self.headers, request=True)
            headers.update(_upstream_auth(provider, config.upstream_api_key))
            if provider == "anthropic" and "anthropic-version" not in {k.lower() for k in headers}:
                headers["anthropic-version"] = "2023-06-01"
            url = config.upstream_base_url.rstrip("/") + suffix
            usage = Usage()
            request_id: str | None = None
            upstream_status: int | None = None
            sent_headers = False
            outcome = "upstream_error"
            error: str | None = None
            stream_actual = stream
            try:
                with httpx.Client(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False) as client:
                    with client.stream("POST", url, content=body, headers=headers) as response:
                        upstream_status = response.status_code
                        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
                        content_type = response.headers.get("content-type", "")
                        stream_actual = "text/event-stream" in content_type.lower()
                        if not stream_actual:
                            content = response.read()
                            try:
                                response_payload = json.loads(content)
                            except (ValueError, UnicodeDecodeError):
                                response_payload = {}
                            if isinstance(response_payload, dict):
                                usage.merge(usage_for_payload(provider, response_payload))
                                if not request_id and isinstance(response_payload.get("id"), str):
                                    request_id = response_payload["id"]
                            outcome = "forwarded"
                            emit(
                                call_id=call_id, path=suffix, provider=provider, model=model,
                                stream=False, usage=usage, status=usage.status(),
                                upstream_status=upstream_status, request_id=request_id,
                                source="proxy_json", error=None, outcome=outcome,
                            )
                            sent_headers = True
                            self.send_response_only(response.status_code)
                            for name, value in _forwarded_headers(response.headers, request=False).items():
                                self.send_header(name, value)
                            self.send_header("Content-Length", str(len(content)))
                            self.end_headers()
                            self.wfile.write(content)
                            return
                        self.send_response_only(response.status_code)
                        for name, value in _forwarded_headers(response.headers, request=False).items():
                            self.send_header(name, value)
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        sent_headers = True
                        buffer = b""
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                            self.wfile.flush()
                            buffer += chunk
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                _consume_sse_line(line.rstrip(b"\r"), provider, usage)
                        if buffer:
                            _consume_sse_line(buffer.rstrip(b"\r"), provider, usage)
                        outcome = "forwarded"
                        emit(
                            call_id=call_id, path=suffix, provider=provider, model=model,
                            stream=True, usage=usage, status=usage.status(),
                            upstream_status=upstream_status, request_id=request_id,
                            source="proxy_stream", error=None, outcome=outcome,
                        )
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
            except httpx.TimeoutException:
                error = "upstream_timeout"
                outcome = "timeout"
                emit(
                    call_id=call_id, path=suffix, provider=provider, model=model,
                    stream=stream_actual, usage=usage, status="unknown",
                    upstream_status=upstream_status, request_id=request_id,
                    source="proxy_stream" if sent_headers else "proxy", error=error,
                    outcome=outcome,
                )
                if not sent_headers:
                    self.reply(504, "model_timeout", "上游模型请求超时")
                self.close_connection = True
            except httpx.HTTPError:
                error = "upstream_error"
                outcome = "upstream_error"
                emit(
                    call_id=call_id, path=suffix, provider=provider, model=model,
                    stream=stream_actual, usage=usage, status="unknown",
                    upstream_status=upstream_status, request_id=request_id,
                    source="proxy_stream" if sent_headers else "proxy", error=error,
                    outcome=outcome,
                )
                if not sent_headers:
                    self.reply(502, "upstream_error", "上游模型请求失败")
                self.close_connection = True
            except (BrokenPipeError, ConnectionResetError):
                error = "client_disconnected"
                outcome = "client_disconnected"
                emit(
                    call_id=call_id, path=suffix, provider=provider, model=model,
                    stream=stream_actual, usage=usage, status=usage.status() if usage else "unknown",
                    upstream_status=upstream_status, request_id=request_id,
                    source="proxy_stream" if sent_headers else "proxy", error=error,
                    outcome=outcome,
                )
                self.close_connection = True

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--run-token-file", type=Path, required=True)
    parser.add_argument("--token-cap", type=int, default=500_000_000)
    parser.add_argument("--call-cap", type=int, default=100_000)
    parser.add_argument("--session-id", default="integration")
    parser.add_argument("--candidate", default="unknown")
    parser.add_argument("--phase", default="unknown")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--upstream-base-env", default="MODEL_API_BASE_URL")
    parser.add_argument("--upstream-key-env", default="MODEL_API_KEY")
    args = parser.parse_args()
    config = TokenProxyConfig(
        upstream_base_url=os.environ.get(args.upstream_base_env, ""),
        upstream_api_key=os.environ.get(args.upstream_key_env, ""),
        run_token=args.run_token_file.read_text().strip(),
        database=args.database,
        ledger=args.ledger,
        token_cap=args.token_cap,
        call_cap=args.call_cap,
        session_id=args.session_id,
        candidate=args.candidate,
        phase=args.phase,
        timeout_seconds=args.timeout_seconds,
    )
    server = create_server(config, args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
