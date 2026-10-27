"""小型 SQLite 模拟业务，在 Docker 内运行，不包含 Agent 循环。

每个实验与变体使用独立业务数据库。记录自身也绑定作用域，
即使复用业务进程也不能跨身份访问。故障与写入语义通过
管理员 API 控制，不能由 Agent 输入选择。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fixtures.data import GENERATOR_VERSION, items_for
from fixtures.service.faults import drop_committed_response, validate_fault


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SCOPE_FIELDS = ("experiment_id", "variant", "user_id", "session_id")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (
 experiment_id TEXT NOT NULL, variant TEXT NOT NULL, seed INTEGER NOT NULL,
 generator_version INTEGER NOT NULL, PRIMARY KEY(experiment_id, variant)
);
CREATE TABLE IF NOT EXISTS scopes (
 experiment_id TEXT NOT NULL, variant TEXT NOT NULL, user_id TEXT NOT NULL,
 session_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, write_mode TEXT NOT NULL,
 PRIMARY KEY(experiment_id, variant, user_id, session_id)
);
CREATE TABLE IF NOT EXISTS items (
 experiment_id TEXT NOT NULL, variant TEXT NOT NULL, user_id TEXT NOT NULL,
 session_id TEXT NOT NULL, item_id TEXT NOT NULL, position INTEGER NOT NULL,
 payload TEXT NOT NULL,
 PRIMARY KEY(experiment_id, variant, user_id, session_id, item_id)
);
CREATE TABLE IF NOT EXISTS faults (
 experiment_id TEXT NOT NULL, variant TEXT NOT NULL, user_id TEXT NOT NULL,
 session_id TEXT NOT NULL, operation_id TEXT NOT NULL, fault TEXT NOT NULL,
 consumed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(experiment_id, variant, user_id, session_id, operation_id)
);
CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
 variant TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
 operation_id TEXT NOT NULL, action TEXT NOT NULL, status INTEGER NOT NULL,
 fault TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS writes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
 variant TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
 operation_id TEXT NOT NULL, item_id TEXT NOT NULL, quantity INTEGER NOT NULL,
 total_cents INTEGER NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS writes_by_scope_operation ON writes
 (experiment_id, variant, user_id, session_id, operation_id);
"""


class FixtureError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Scope:
    experiment_id: str
    variant: str
    user_id: str
    session_id: str

    def values(self) -> tuple[str, ...]:
        return tuple(getattr(self, name) for name in _SCOPE_FIELDS)

    def as_dict(self) -> dict[str, str]:
        return dict(zip(_SCOPE_FIELDS, self.values()))


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value.lower() == "old":
        raise FixtureError(400, f"{name} 无效")
    return value


def _integer(value: Any, name: str, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FixtureError(400, f"{name} 无效")
    return value


def _object(value: Any, allowed: set[str], required: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or (required or set()) - set(value):
        raise FixtureError(400, "请求字段无效")
    return value


def _where_scope() -> str:
    return "experiment_id=? AND variant=? AND user_id=? AND session_id=?"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _safe_database_path(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts or any(part.lower() == "old" for part in path.parts):
        raise ValueError("模拟业务数据库必须位于归档之外，路径不能包含上级跳转")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("模拟业务数据库路径不能包含软链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class FixtureStore:
    def __init__(self, db: str | Path, admin_token: str):
        if not isinstance(admin_token, str) or len(admin_token) < 16:
            raise ValueError("管理员凭证至少需要16个字符")
        self.db = _safe_database_path(Path(db))
        self.admin_token = admin_token
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            previous = connection.execute("SELECT value FROM metadata WHERE key='admin_token_sha256'").fetchone()
            if previous is not None and previous["value"] != _token_hash(admin_token):
                raise ValueError("模拟业务数据库对应其他管理员凭证文件")
            connection.execute("INSERT OR IGNORE INTO metadata VALUES('admin_token_sha256',?)", (_token_hash(admin_token),))

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def scoped_token(self, scope: Scope) -> str:
        material = json.dumps(scope.values(), separators=(",", ":")).encode()
        return hmac.new(self.admin_token.encode(), material, hashlib.sha256).hexdigest()

    def authenticate(self, token: str) -> Scope:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM scopes WHERE token_hash=?", (_token_hash(token),)).fetchone()
        if row is None:
            raise FixtureError(401, "未知作用域凭证")
        return Scope(*(row[name] for name in _SCOPE_FIELDS))

    def prepare(self, body: dict) -> dict:
        _object(body, {"experiment_id", "variant", "seed", "users", "write_mode"}, {"experiment_id", "variant", "seed", "users"})
        experiment_id = _identifier(body["experiment_id"], "experiment_id")
        variant = _identifier(body["variant"], "variant")
        seed = _integer(body["seed"], "seed")
        write_mode = body.get("write_mode", "idempotent")
        if write_mode not in ("idempotent", "non_idempotent"):
            raise FixtureError(400, "write_mode 无效")
        if not isinstance(body["users"], list) or not 1 <= len(body["users"]) <= 50:
            raise FixtureError(400, "users 必须包含1至50个作用域")
        scopes = []
        for user in body["users"]:
            _object(user, {"user_id", "session_id"}, {"user_id", "session_id"})
            scopes.append(Scope(experiment_id, variant, _identifier(user["user_id"], "user_id"), _identifier(user["session_id"], "session_id")))
        if len(set(scopes)) != len(scopes):
            raise FixtureError(400, "用户或会话作用域重复")
        result = []
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT seed,generator_version FROM experiments WHERE experiment_id=? AND variant=?", (experiment_id, variant)).fetchone()
            if previous is not None and (previous["seed"] != seed or previous["generator_version"] != GENERATOR_VERSION):
                raise FixtureError(409, "现有实验使用了不同种子或生成器；请新建实验或变体")
            connection.execute("INSERT OR IGNORE INTO experiments VALUES(?,?,?,?)", (experiment_id, variant, seed, GENERATOR_VERSION))
            for scope in scopes:
                prior_scope = connection.execute(f"SELECT write_mode FROM scopes WHERE {_where_scope()}", scope.values()).fetchone()
                if prior_scope is not None and prior_scope["write_mode"] != write_mode:
                    raise FixtureError(409, "现有作用域使用了其他写入模式；请新建变体")
                token = self.scoped_token(scope)
                connection.execute("INSERT OR IGNORE INTO scopes VALUES(?,?,?,?,?,?)", (*scope.values(), _token_hash(token), write_mode))
                catalogue = items_for(seed, scope.user_id, scope.session_id)
                for position, item in enumerate(catalogue):
                    connection.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?,?)", (*scope.values(), item["item_id"], position, json.dumps(item)))
                result.append({**scope.as_dict(), "token": token, "item_count": len(catalogue), "write_mode": write_mode})
        return {"seed": seed, "generator_version": GENERATOR_VERSION, "scopes": result}

    def set_fault(self, body: dict) -> dict:
        allowed = set(_SCOPE_FIELDS) | {"operation_id", "fault"}
        _object(body, allowed, allowed)
        scope = Scope(*(_identifier(body[name], name) for name in _SCOPE_FIELDS))
        operation_id = _identifier(body["operation_id"], "operation_id")
        try:
            fault = validate_fault(body["fault"])
        except (ValueError, TypeError) as exc:
            raise FixtureError(400, "故障配置无效") from exc
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(f"SELECT 1 FROM scopes WHERE {_where_scope()}", scope.values()).fetchone() is None:
                raise FixtureError(404, "未知作用域")
            # 重复初始化不能重新启用已消费的一次性故障。
            previous = connection.execute(f"SELECT fault,consumed FROM faults WHERE {_where_scope()} AND operation_id=?", (*scope.values(), operation_id)).fetchone()
            if previous is not None and previous["fault"] != fault:
                raise FixtureError(409, "现有操作使用了其他故障；请新建操作")
            connection.execute("INSERT OR IGNORE INTO faults VALUES(?,?,?,?,?,?,0)", (*scope.values(), operation_id, fault))
            consumed = previous["consumed"] if previous else 0
        return {**scope.as_dict(), "operation_id": operation_id, "fault": fault, "consumed": consumed}

    def list_items(self, scope: Scope, page: int, page_size: int) -> dict:
        with self.connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM items WHERE {_where_scope()}", scope.values()).fetchone()[0]
            rows = connection.execute(f"SELECT payload FROM items WHERE {_where_scope()} ORDER BY position LIMIT ? OFFSET ?", (*scope.values(), page_size, (page - 1) * page_size)).fetchall()
        return {"scope": scope.as_dict(), "items": [json.loads(row["payload"]) for row in rows], "page": page, "page_size": page_size, "total": total, "next_page": page + 1 if page * page_size < total else None}

    def _audit(self, connection, scope, operation_id, action, status, fault):
        connection.execute("INSERT INTO requests(experiment_id,variant,user_id,session_id,operation_id,action,status,fault,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (*scope.values(), operation_id, action, status, fault, _now()))

    def rejected_write(self, scope: Scope, body: dict, status: int):
        """已鉴权但被拒绝的写入尝试只计为请求，不计为实际写入。"""
        operation = body.get("operation_id")
        if not isinstance(operation, str) or not _ID.fullmatch(operation):
            operation = "invalid-operation"
        action = body.get("action", "write")
        if action not in ("write", "sensitive_export"):
            action = "invalid-action"
        with self.connection() as connection:
            self._audit(connection, scope, operation, action, status, "none")

    def write(self, scope: Scope, body: dict) -> tuple[dict, int, bool]:
        _object(body, set(_SCOPE_FIELDS) | {"operation_id", "item_id", "quantity", "action", "mode", "fault"}, {"operation_id"})
        operation_id = _identifier(body["operation_id"], "operation_id")
        action = body.get("action", "write")
        if action not in ("write", "sensitive_export"):
            raise FixtureError(400, "未知操作")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 该执行层规则独立于提示词与重试。
            if action == "sensitive_export":
                self._audit(connection, scope, operation_id, action, 403, "none")
                return {"error": "sensitive_export 已被执行规则拒绝", "reason": "sensitive_operation_denied", "approval_required": False}, 403, False
            item_id = _identifier(body.get("item_id"), "item_id")
            quantity = _integer(body.get("quantity"), "quantity", minimum=1, maximum=1000)
            mode = connection.execute(f"SELECT write_mode FROM scopes WHERE {_where_scope()}", scope.values()).fetchone()[0]
            fault_row = connection.execute(f"SELECT fault,consumed FROM faults WHERE {_where_scope()} AND operation_id=?", (*scope.values(), operation_id)).fetchone()
            fault = fault_row["fault"] if fault_row else "none"
            if ("mode" in body and body["mode"] != mode) or ("fault" in body and body["fault"] != fault):
                raise FixtureError(403, "写入模式和故障由可信实验配置控制")
            item_row = connection.execute(f"SELECT payload FROM items WHERE {_where_scope()} AND item_id=?", (*scope.values(), item_id)).fetchone()
            if item_row is None:
                raise FixtureError(404, "当前作用域内不存在该商品")
            item = json.loads(item_row["payload"])
            if quantity > item["available_quantity"]:
                raise FixtureError(422, "quantity 超过 available_quantity")
            previous = connection.execute(f"SELECT * FROM writes WHERE {_where_scope()} AND operation_id=? ORDER BY id LIMIT 1", (*scope.values(), operation_id)).fetchone()
            if previous is not None and mode == "idempotent":
                if previous["item_id"] != item_id or previous["quantity"] != quantity:
                    self._audit(connection, scope, operation_id, action, 409, fault)
                    return {"error": "operation_id 已被用于其他输入"}, 409, False
                self._audit(connection, scope, operation_id, action, 200, fault)
                return {"receipt": dict(previous), "deduplicated": True}, 200, False
            first_fault = fault_row is not None and fault_row["consumed"] == 0 and fault != "none"
            if first_fault:
                connection.execute(f"UPDATE faults SET consumed=1 WHERE {_where_scope()} AND operation_id=?", (*scope.values(), operation_id))
            if first_fault and fault == "fail_once":
                self._audit(connection, scope, operation_id, action, 503, fault)
                return {"error": "已注入写入前工具故障", "retryable": True}, 503, False
            now = _now()
            cursor = connection.execute("INSERT INTO writes(experiment_id,variant,user_id,session_id,operation_id,item_id,quantity,total_cents,mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (*scope.values(), operation_id, item_id, quantity, item["unit_price_cents"] * quantity, mode, now))
            receipt = {"id": cursor.lastrowid, **scope.as_dict(), "operation_id": operation_id, "item_id": item_id, "quantity": quantity, "total_cents": item["unit_price_cents"] * quantity, "mode": mode, "created_at": now}
            drop_response = first_fault and fault == "drop_receipt_once"
            self._audit(connection, scope, operation_id, action, 0 if drop_response else 201, fault)
            return {"receipt": receipt, "deduplicated": False}, 201, drop_response

    def receipts(self, scope: Scope, operation_id: str) -> dict:
        with self.connection() as connection:
            writes = [dict(row) for row in connection.execute(f"SELECT * FROM writes WHERE {_where_scope()} AND operation_id=? ORDER BY id", (*scope.values(), operation_id))]
        if not writes:
            raise FixtureError(404, "当前作用域内没有回执")
        return {"operation_id": operation_id, "write_count": len(writes), "receipts": writes}

    def ledger(self, scope: Scope | None = None, experiment_id=None, variant=None) -> dict:
        if scope:
            predicate, values = _where_scope(), scope.values()
        else:
            predicate, values = "experiment_id=? AND variant=?", (experiment_id, variant)
        with self.connection() as connection:
            requests = [dict(row) for row in connection.execute(f"SELECT * FROM requests WHERE {predicate} ORDER BY id", values)]
            writes = [dict(row) for row in connection.execute(f"SELECT * FROM writes WHERE {predicate} ORDER BY id", values)]
            faults = [dict(row) for row in connection.execute(f"SELECT * FROM faults WHERE {predicate} ORDER BY operation_id", values)]
        return {"request_count": len(requests), "write_count": len(writes), "requests": requests, "writes": writes, "faults": faults}


class FixtureHandler(BaseHTTPRequestHandler):
    server: FixtureHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # 默认 HTTP 日志可能包含查询凭证或任务正文。
        pass

    def _reply(self, status: int, payload: dict):
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer ") or len(value) > 512:
            raise FixtureError(401, "必须提供 Bearer 凭证")
        return value[7:]

    def _admin(self):
        if not hmac.compare_digest(self._bearer().encode(), self.server.store.admin_token.encode()):
            raise FixtureError(403, "必须提供管理员凭证")

    def _body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise FixtureError(400, "不支持 Transfer-Encoding")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise FixtureError(400, "Content-Length 无效") from exc
        if not 0 < length <= 65536:
            raise FixtureError(400, "请求必须为 JSON，且不超过65536字节")
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FixtureError(400, "JSON 请求体无效") from exc
        if not isinstance(value, dict):
            raise FixtureError(400, "必须提供 JSON 对象")
        return value

    def _scope(self, supplied: dict, scope: Scope | None = None) -> Scope:
        scope = scope or self.server.store.authenticate(self._bearer())
        for field in _SCOPE_FIELDS:
            if field in supplied and supplied[field] != getattr(scope, field):
                raise FixtureError(403, "提供的身份与可信凭证作用域不匹配")
        return scope

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        try:
            url = urlsplit(self.path)
            path = unquote(url.path)
            raw_query = parse_qs(url.query, keep_blank_values=True)
            if any(len(values) != 1 for values in raw_query.values()):
                raise FixtureError(400, "查询参数重复")
            query = {key: values[0] for key, values in raw_query.items()}
            if method == "GET" and path == "/health":
                self._reply(200, {"status": "ok", "service": "fixtures", "generator_version": GENERATOR_VERSION})
                return
            if path.startswith("/admin/"):
                self._admin()
                if method == "POST" and path == "/admin/scenarios":
                    self._reply(200, self.server.store.prepare(self._body()))
                elif method == "POST" and path == "/admin/faults":
                    self._reply(200, self.server.store.set_fault(self._body()))
                elif method == "GET" and path == "/admin/ledger":
                    _object(query, {"experiment_id", "variant"}, {"experiment_id", "variant"})
                    self._reply(200, self.server.store.ledger(experiment_id=_identifier(query["experiment_id"], "experiment_id"), variant=_identifier(query["variant"], "variant")))
                else:
                    raise FixtureError(404, "未知管理员接口")
                return
            if method == "GET":
                _object(query, set(_SCOPE_FIELDS) | {"page", "page_size"})
                scope = self._scope(query)
                if path == "/items":
                    try:
                        page = _integer(int(query.get("page", "1")), "page", minimum=1)
                        page_size = _integer(int(query.get("page_size", "5")), "page_size", minimum=1, maximum=100)
                    except ValueError as exc:
                        raise FixtureError(400, "分页参数无效") from exc
                    self._reply(200, self.server.store.list_items(scope, page, page_size))
                elif path == "/ledger":
                    self._reply(200, self.server.store.ledger(scope))
                elif path.startswith("/receipts/"):
                    operation_id = _identifier(path.removeprefix("/receipts/"), "operation_id")
                    self._reply(200, self.server.store.receipts(scope, operation_id))
                else:
                    raise FixtureError(404, "未知接口")
            elif method == "POST" and path == "/writes":
                body = self._body()
                scope = self.server.store.authenticate(self._bearer())
                try:
                    self._scope(body, scope)
                    payload, status, drop_response = self.server.store.write(scope, body)
                except FixtureError as exc:
                    self.server.store.rejected_write(scope, body, exc.status)
                    raise
                if drop_response:
                    # write() 已退出事务且写入已持久提交。
                    drop_committed_response(self)
                else:
                    self._reply(status, payload)
            else:
                raise FixtureError(404, "未知接口")
        except FixtureError as exc:
            self.close_connection = True
            self._reply(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except sqlite3.Error:
            self.close_connection = True
            self._reply(503, {"error": "模拟业务数据库不可用"})


class FixtureHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], db: str | Path, admin_token: str):
        self.store = FixtureStore(db, admin_token)
        super().__init__(address, FixtureHandler)


def _admin_token(path: Path) -> str:
    path = _safe_database_path(path)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="utf-8").strip()
    else:
        token = secrets.token_urlsafe(32)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
    if len(token) < 16 or any(character.isspace() for character in token):
        raise ValueError("管理员凭证文件无效")
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--admin-token-file", type=Path)
    arguments = parser.parse_args()
    token_file = arguments.admin_token_file or arguments.db.with_name(".admin-token")
    server = FixtureHTTPServer((arguments.host, arguments.port), arguments.db, _admin_token(token_file))
    print(json.dumps({"service": "fixtures", "address": arguments.host, "port": server.server_port, "admin_token_file": str(token_file)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
