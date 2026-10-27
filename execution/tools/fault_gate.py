"""显式启用的 I23 写入前栅栏，不保存、读取或执行业务任务。

候选在 POST /call 之前报告。目标报告永远不会获得许可。
放弃会撤销旧尝试并使等待者失败；不存在放行目标的接口。
管理员凭证与目标只由验收程序持有，状态存于私有 SQLite。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading


SCOPE_FIELDS = {"experiment_id", "variant", "user_id", "session_id"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
ATTEMPTS = {"worker-a", "worker-b"}
TOKEN_HEADER = "X-Harness-Prewrite-Token"
PERMIT_HEADER = "X-Harness-Prewrite-Permit"
CALL_HEADER = "X-Harness-Prewrite-Call-Id"
SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate (
 slot INTEGER PRIMARY KEY CHECK(slot=1), gate_id TEXT NOT NULL UNIQUE,
 target_attempt TEXT NOT NULL, target_operation TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('armed','hit','abandoned')),
 first_hit_event INTEGER
);
CREATE TABLE IF NOT EXISTS attempts (
 attempt_id TEXT PRIMARY KEY, credential_sha256 TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('active','revoked'))
);
CREATE TABLE IF NOT EXISTS reports (
 attempt_id TEXT NOT NULL, bridge_call_id TEXT NOT NULL,
 native_call_id TEXT, operation_id TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('blocked','permitted','consumed','abandoned','revoked')),
 permit_sha256 TEXT UNIQUE, report_event_id INTEGER NOT NULL,
 PRIMARY KEY(attempt_id, bridge_call_id)
);
CREATE TABLE IF NOT EXISTS events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatches (
 server_call_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, tool TEXT NOT NULL,
 arguments_sha256 TEXT NOT NULL, admission_event_id INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('admitted','finished','unknown')),
 terminal_event_id INTEGER
);
"""


class GateError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value) or value.lower() == "old":
        raise GateError(400, "实验标识无效")
    return value


def call_id(value, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or any(ord(char) < 32 for char in value):
        raise GateError(400, "工具桥或原生调用标识无效")
    return value


def scope(value):
    if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
        raise GateError(400, "实验作用域无效")
    return {key: identifier(item) for key, item in value.items()}


def write_arguments(value):
    if (not isinstance(value, dict) or set(value) != {"operation_id", "item_id", "quantity"}
            or any(not isinstance(value[key], str) or not 1 <= len(value[key]) <= 4096 for key in ("operation_id", "item_id"))
            or type(value["quantity"]) is not int or not 1 <= value["quantity"] <= 2**31 - 1):
        raise GateError(400, "写入前参数无效")
    return value


def database_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(part.lower() == "old" for part in path.parts):
        raise GateError(400, "栅栏数据库必须使用当前实验的私有绝对路径")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise GateError(400, "栅栏数据库路径不能经过链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise GateError(400, "栅栏数据库必须为普通文件")
    return path


class PrewriteGate:
    def __init__(self, binding, expected_scope, workspace, *, forbidden_credential_sha256=()):
        fields = {"schema", "chain_id", "scope", "database", "admin_token_sha256"}
        if not isinstance(binding, dict) or set(binding) != fields or binding["schema"] != "harness-lab-prewrite-service/1":
            raise GateError(400, "私有写入前服务绑定无效")
        self.scope = scope(binding["scope"])
        self.chain = identifier(binding["chain_id"])
        if (self.scope != expected_scope or self.scope["experiment_id"] != "fault-recovery"
                or self.chain != self.scope["variant"]):
            raise GateError(400, "写入前服务绑定不属于当前 I23 作用域")
        if not isinstance(binding["admin_token_sha256"], str) or not HASH.fullmatch(binding["admin_token_sha256"]):
            raise GateError(400, "独立管理员凭证摘要无效")
        self.admin_digest = binding["admin_token_sha256"]
        self.forbidden_credentials = frozenset(forbidden_credential_sha256) | {self.admin_digest}
        if any(not isinstance(value, str) or not HASH.fullmatch(value) for value in self.forbidden_credentials):
            raise GateError(400, "禁止使用的凭证身份无效")
        self.database = database_path(binding["database"])
        if self.database.is_relative_to(Path(workspace).resolve()):
            raise GateError(400, "栅栏数据库必须位于候选工作区之外")
        self.condition = threading.Condition()
        self.closing = False
        identity = canonical({"binding": binding, "forbidden_credential_sha256": sorted(self.forbidden_credentials)})
        with sqlite3.connect(self.database, timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)
            # 首个进程原子占用数据库身份；并发的其他首启进程
            # 不能把被忽略的 INSERT 当作成功绑定。
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if previous and previous[0] != identity:
                raise GateError(409, "栅栏数据库属于其他私有绑定")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('identity',?)", (identity,))
        self.database.chmod(0o600)

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, phase, **details):
        payload = {"time": datetime.now(timezone.utc).isoformat(), "scope": self.scope,
            "chain_id": self.chain, "phase": phase, **details}
        cursor = db.execute("INSERT INTO events(payload) VALUES(?)", (canonical(payload),))
        return cursor.lastrowid

    def authenticated_admin(self, header):
        return (isinstance(header, str) and header.startswith("Bearer ") and len(header) <= 512
            and hmac.compare_digest(sha(header[7:]), self.admin_digest))

    def _attempt(self, db, credential):
        if not isinstance(credential, str) or not 16 <= len(credential) <= 512 or any(char.isspace() for char in credential):
            raise GateError(403, "必须提供本次尝试的写入前凭证")
        row = db.execute("SELECT * FROM attempts WHERE credential_sha256=?", (sha(credential),)).fetchone()
        if row is None or row["state"] != "active":
            raise GateError(403, "本次尝试的写入前凭证不可用")
        if row["attempt_id"] == "worker-b":
            gate = db.execute("SELECT state FROM gate WHERE slot=1").fetchone()
            unsettled = db.execute("SELECT 1 FROM dispatches WHERE attempt_id='worker-a' AND state<>'finished' LIMIT 1").fetchone()
            if gate is None or gate["state"] != "abandoned" or unsettled is not None:
                raise GateError(409, "恢复尝试尚未获准进入")
        return row["attempt_id"]

    def identify(self, credential):
        with self.connection() as db:
            return self._attempt(db, credential)

    def setup(self, body):
        if (not isinstance(body, dict) or set(body) != {"gate_id", "chain_id", "scope", "target", "attempts"}
                or body["chain_id"] != self.chain or scope(body["scope"]) != self.scope):
            raise GateError(400, "私有栅栏初始化作用域无效")
        gate_id = identifier(body["gate_id"])
        target, attempts = body["target"], body["attempts"]
        if (not isinstance(target, dict) or set(target) != {"attempt_id", "operation_id"}
                or target["attempt_id"] != "worker-a" or not isinstance(target["operation_id"], str)
                or not 1 <= len(target["operation_id"]) <= 4096
                or not isinstance(attempts, dict) or set(attempts) != ATTEMPTS
                or any(not isinstance(item, str) or not HASH.fullmatch(item) for item in attempts.values())
                or len(set(attempts.values())) != 2 or set(attempts.values()) & self.forbidden_credentials):
            raise GateError(400, "一次性目标或独立尝试凭证无效")
        with self.condition, self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM gate").fetchone() is not None:
                raise GateError(409, "栅栏已配置，不能重置或重新启用")
            db.executemany("INSERT INTO attempts VALUES(?,?,'active')", sorted(attempts.items()))
            db.execute("INSERT INTO gate VALUES(1,?,?,?,'armed',NULL)",
                (gate_id, target["attempt_id"], target["operation_id"]))
            self.event(db, "gate_armed", gate_id=gate_id, target=target)
        return {"configured": True, "gate_id": gate_id}

    def report(self, credential, body):
        fields = {"chain_id", "scope", "attempt_id", "bridge_call_id", "native_call_id", "tool", "arguments"}
        if (not isinstance(body, dict) or set(body) != fields or body["chain_id"] != self.chain
                or scope(body["scope"]) != self.scope or body["attempt_id"] not in ATTEMPTS or body["tool"] != "write"):
            raise GateError(400, "写入前报告绑定无效")
        bridge = call_id(body["bridge_call_id"])
        native = call_id(body["native_call_id"], optional=True)
        arguments = write_arguments(body["arguments"])
        arguments_hash = sha(canonical(arguments))
        permit = None
        with self.condition, self.connection(write=True) as db:
            if self.closing:
                raise GateError(410, "写入前尝试不可用")
            attempt = self._attempt(db, credential)
            if attempt != body["attempt_id"]:
                raise GateError(403, "写入前凭证属于其他尝试")
            gate = db.execute("SELECT * FROM gate WHERE slot=1").fetchone()
            if gate is None:
                raise GateError(409, "写入前栅栏尚未配置")
            previous = db.execute("SELECT * FROM reports WHERE attempt_id=? AND bridge_call_id=?", (attempt, bridge)).fetchone()
            target = attempt == gate["target_attempt"] and arguments["operation_id"] == gate["target_operation"]
            if previous is not None:
                if (previous["arguments_sha256"] != arguments_hash or previous["native_call_id"] != native
                        or previous["status"] != "blocked" or not target):
                    raise GateError(409, "工具桥调用已报告，不发放重复执行许可")
                self.event(db, "duplicate_blocked_report", gate_id=gate["gate_id"], attempt_id=attempt,
                    bridge_call_id=bridge, report_event_id=previous["report_event_id"])
            else:
                if db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] >= 1024:
                    raise GateError(429, "写入前校准报告数量已达上限")
                status = "blocked" if target else "permitted"
                permit = None if target else secrets.token_urlsafe(32)
                event_id = self.event(db, "prewrite_hit" if target else "prewrite_permitted", gate_id=gate["gate_id"],
                    attempt_id=attempt, bridge_call_id=bridge, native_call_id=native,
                    native_call_id_origin="bridge_report_unverified" if native else "absent",
                    operation_id=arguments["operation_id"], arguments_sha256=arguments_hash)
                db.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?)", (attempt, bridge, native,
                    arguments["operation_id"], arguments_hash, status, sha(permit) if permit else None, event_id))
                if target and gate["state"] == "armed":
                    db.execute("UPDATE gate SET state='hit', first_hit_event=? WHERE slot=1", (event_id,))
        if permit is not None:
            return {"permit": permit}
        # 目标调用永不放行；放弃、断连、关闭及重启均按拒绝处理。
        # 控制器仅在观察到 A 实际退出或删除后才能放弃，
        # 即使过早放弃，也不能使目标调用获准执行。
        with self.condition:
            while True:
                with self.connection() as db:
                    current = db.execute("SELECT state FROM attempts WHERE attempt_id=?", (body["attempt_id"],)).fetchone()
                    gate = db.execute("SELECT state FROM gate WHERE slot=1").fetchone()
                if self.closing or current is None or current["state"] != "active" or gate is None or gate["state"] == "abandoned":
                    raise GateError(410, "写入前尝试不可用")
                self.condition.wait(timeout=0.5)

    def consume(self, credential, bridge, permit, arguments, server_call_id):
        bridge = call_id(bridge)
        if not isinstance(permit, str) or not 16 <= len(permit) <= 512:
            raise GateError(403, "必须提供一次性写入前许可")
        arguments_hash = sha(canonical(write_arguments(arguments)))
        with self.condition, self.connection(write=True) as db:
            if self.closing:
                raise GateError(410, "写入前尝试不可用")
            attempt = self._attempt(db, credential)
            gate = db.execute("SELECT * FROM gate WHERE slot=1").fetchone()
            report = db.execute("SELECT * FROM reports WHERE attempt_id=? AND bridge_call_id=?", (attempt, bridge)).fetchone()
            if (gate is None or report is None or report["status"] != "permitted"
                    or report["arguments_sha256"] != arguments_hash
                    or not hmac.compare_digest(report["permit_sha256"] or "", sha(permit))
                    or attempt == gate["target_attempt"] and arguments["operation_id"] == gate["target_operation"]):
                raise GateError(403, "写入前许可不允许本次实际写入")
            db.execute("UPDATE reports SET status='consumed' WHERE attempt_id=? AND bridge_call_id=?", (attempt, bridge))
            event_id = self.event(db, "write_dispatch_admitted", gate_id=gate["gate_id"], attempt_id=attempt,
                bridge_call_id=bridge, native_call_id=report["native_call_id"],
                arguments_sha256=arguments_hash, server_call_id=server_call_id,
                report_event_id=report["report_event_id"])
            db.execute("INSERT INTO dispatches VALUES(?,?,?, ?,?,'admitted',NULL)",
                (server_call_id, attempt, "write", arguments_hash, event_id))
        return {"chain_id": self.chain, "attempt_id": attempt, "bridge_call_id": bridge,
            "native_call_id": report["native_call_id"], "report_event_id": report["report_event_id"],
            "dispatch_event_id": event_id}

    def admit_other(self, credential, tool, arguments, server_call_id):
        """观察服务已接纳的全部工作，包括工作区写入。

        这里只保存私有在途账本，不选择、调用、保存、取消、重试或恢复工具。
        """
        with self.condition, self.connection(write=True) as db:
            if self.closing:
                raise GateError(410, "写入前尝试不可用")
            attempt = self._attempt(db, credential)
            arguments_hash = sha(canonical(arguments))
            event_id = self.event(db, "tool_dispatch_admitted", attempt_id=attempt,
                tool=tool, server_call_id=server_call_id, arguments_sha256=arguments_hash)
            db.execute("INSERT INTO dispatches VALUES(?,?,?,?,?,'admitted',NULL)",
                (server_call_id, attempt, tool, arguments_hash, event_id))
        return {"chain_id": self.chain, "attempt_id": attempt, "dispatch_event_id": event_id}

    def finish_dispatch(self, server_call_id, *, known, status):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM dispatches WHERE server_call_id=?", (server_call_id,)).fetchone()
            if row is None or row["state"] != "admitted":
                return
            state = "finished" if known else "unknown"
            event_id = self.event(db, "tool_dispatch_terminal", attempt_id=row["attempt_id"],
                tool=row["tool"], server_call_id=server_call_id,
                admission_event_id=row["admission_event_id"], state=state, status=status)
            db.execute("UPDATE dispatches SET state=?,terminal_event_id=? WHERE server_call_id=?",
                (state, event_id, server_call_id))

    def abandon(self, body):
        if not isinstance(body, dict) or set(body) != {"gate_id", "attempt_id"} or body["attempt_id"] != "worker-a":
            raise GateError(400, "放弃操作必须指定原目标栅栏和尝试")
        with self.condition, self.connection(write=True) as db:
            gate = db.execute("SELECT * FROM gate WHERE slot=1").fetchone()
            if (gate is None or gate["gate_id"] != body["gate_id"] or gate["target_attempt"] != body["attempt_id"]
                    or gate["state"] not in {"hit", "abandoned"}):
                raise GateError(409, "没有匹配且已命中的栅栏可供放弃")
            if gate["state"] != "abandoned":
                db.execute("UPDATE gate SET state='abandoned' WHERE slot=1")
                db.execute("UPDATE attempts SET state='revoked' WHERE attempt_id=?", (body["attempt_id"],))
                db.execute("UPDATE reports SET status=CASE WHEN status='blocked' THEN 'abandoned' ELSE 'revoked' END "
                    "WHERE attempt_id=? AND status IN ('blocked','permitted')", (body["attempt_id"],))
                self.event(db, "gate_abandoned_without_release", gate_id=gate["gate_id"], attempt_id=body["attempt_id"],
                    target_report_event_id=gate["first_hit_event"], worker_death_verified_by_service=False)
        with self.condition:
            self.condition.notify_all()
        return {"abandoned": True, "old_attempt_revoked": True, "released": False}

    def snapshot(self):
        with self.connection() as db:
            gate = db.execute("SELECT * FROM gate WHERE slot=1").fetchone()
            reports = [dict(row) for row in db.execute("SELECT attempt_id,bridge_call_id,native_call_id,operation_id,"
                "arguments_sha256,status,report_event_id FROM reports ORDER BY report_event_id")]
            events = [{"event_id": row["event_id"], **json.loads(row["payload"])}
                for row in db.execute("SELECT * FROM events ORDER BY event_id")]
            attempts = [dict(row) for row in db.execute("SELECT attempt_id,state FROM attempts ORDER BY attempt_id")]
            dispatches = [dict(row) for row in db.execute("SELECT * FROM dispatches ORDER BY admission_event_id")]
        return {"schema": "harness-lab-prewrite-status/1", "chain_id": self.chain, "scope": self.scope,
            "gate": dict(gate) if gate else None, "attempts": attempts, "reports": reports,
            "dispatches": dispatches, "events": events,
            "unsettled_dispatches": [row for row in dispatches if row["state"] != "finished"]}

    def close(self):
        with self.condition:
            self.closing = True
            self.condition.notify_all()
