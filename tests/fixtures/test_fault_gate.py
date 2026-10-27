"""I23 校准：真实工具、模拟业务 CLI 和 HTTP，不作为 Agent 通过证据。

仅在构建好的本地 Docker 验收镜像中运行。这些测试最初为尚未
执行的草稿。子进程工具桥探针只校准故障窗口，不能代替任一
候选的原生崩溃恢复。
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time

import httpx
import pytest

from execution.tools.fault_gate import GateError, PrewriteGate
from execution.tools.basic import ToolError
from execution.tools.server import ToolsHTTPServer


FIELDS = ("experiment_id", "variant", "user_id", "session_id")
TOKEN_HEADER = "X-Harness-Prewrite-Token"
CALL_HEADER = "X-Harness-Prewrite-Call-Id"
PERMIT_HEADER = "X-Harness-Prewrite-Permit"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class GatedTools:
    def __init__(self, directory, fixture, user, *, enabled=True):
        self.root, self.fixture, self.user = directory.resolve(), fixture, user
        self.scope = {key: user[key] for key in FIELDS}
        self.admin = secrets.token_urlsafe(32)
        self.tokens = {attempt: secrets.token_urlsafe(32) for attempt in ("worker-a", "worker-b")}
        self.process, self.enabled = None, enabled
        self.binding = {"fixture_url": fixture.base_url, "fixture_token": user["token"],
            "workspace": str(self.root / "workspace"), "scope": self.scope,
            "events_path": str(self.root / "trusted/tool-events.jsonl")}
        self.private = {"schema": "harness-lab-prewrite-service/1", "chain_id": self.scope["variant"],
            "scope": self.scope, "database": str(self.root / "trusted/prewrite.sqlite"),
            "admin_token_sha256": digest(self.admin)}
        for name, value in (("tools.json", self.binding), ("gate.json", self.private)):
            path = self.root / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def start(self):
        command = [sys.executable, "-m", "execution.tools.server", "--host", "127.0.0.1", "--port", "0",
            "--binding-file", str(self.root / "tools.json")]
        if self.enabled:
            command += ["--prewrite-binding-file", str(self.root / "gate.json")]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(10), "工具 CLI 未就绪"
            line = self.process.stdout.readline()
            assert line, "工具 CLI 在就绪前退出"
            assert self.admin not in line and all(token not in line for token in self.tokens.values())
            self.base = "http://127.0.0.1:" + str(json.loads(line)["port"])
        finally:
            selector.close()

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process.stdout.close()
            self.process.stderr.close()
            self.process = None

    def request(self, method, path, *, admin=False, headers=None, timeout=5, **kwargs):
        auth = self.admin if admin else self.user["token"]
        with httpx.Client(timeout=timeout, trust_env=False, transport=httpx.HTTPTransport(retries=0)) as client:
            return client.request(method, self.base + path,
                headers={"Authorization": "Bearer " + auth, **(headers or {})}, **kwargs)

    def setup(self, target="secret-first-operation"):
        self.target = target
        return self.request("POST", "/experiment/admin/prewrite/setup", admin=True,
            json={"gate_id": "one-shot", "chain_id": self.scope["variant"], "scope": self.scope,
                "target": {"attempt_id": "worker-a", "operation_id": target},
                "attempts": {key: digest(value) for key, value in self.tokens.items()}})

    def state(self):
        reply = self.request("GET", "/experiment/admin/prewrite", admin=True)
        assert reply.status_code == 200, reply.text
        return reply.json()

    def wait_hit(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.state()
            if state["gate"] and state["gate"]["state"] == "hit":
                return state
            time.sleep(0.05)
        raise AssertionError("真实预写入请求未到达私有栅栏")

    def abandon(self):
        return self.request("POST", "/experiment/admin/prewrite/abandon", admin=True,
            json={"gate_id": "one-shot", "attempt_id": "worker-a"})

    def body(self, attempt, call, arguments):
        return {"chain_id": self.scope["variant"], "scope": self.scope, "attempt_id": attempt,
            "bridge_call_id": call, "native_call_id": None, "tool": "write", "arguments": arguments}

    def report(self, attempt, call, arguments, **kwargs):
        return self.request("POST", "/experiment/prewrite", headers={TOKEN_HEADER: self.tokens[attempt]},
            json=self.body(attempt, call, arguments), **kwargs)

    def call(self, attempt, name, arguments, *, bridge=None, permit=None):
        headers = {TOKEN_HEADER: self.tokens[attempt]}
        if bridge is not None:
            headers[CALL_HEADER] = bridge
        if permit is not None:
            headers[PERMIT_HEADER] = permit
        return self.request("POST", "/call", headers=headers, json={"name": name, "arguments": arguments})

    def item(self):
        reply = self.call("worker-a", "list_items", {"page": 1, "page_size": 1})
        assert reply.status_code == 200, reply.text
        return reply.json()["result"]["items"][0]["item_id"]

    def ledger(self):
        reply = self.fixture.request("GET", "/ledger", token=self.user["token"])
        assert reply.status_code == 200
        return reply.json()

    def audit(self):
        path = Path(self.binding["events_path"])
        return [json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []


@contextmanager
def running(directory, fixture, *, enabled=True):
    user = fixture.prepare(experiment_id="fault-recovery", variant="prewrite-calibration",
        write_mode="non_idempotent")["user-a"]
    tools = GatedTools(directory, fixture, user, enabled=enabled)
    try:
        tools.start()
        yield tools
    finally:
        tools.stop()


def test_target_never_gets_a_permit_and_abandon_cannot_release_a_live_attempt(tmp_path, fixture_process):
    with running(tmp_path, fixture_process) as tools:
        assert tools.setup().status_code == 200
        arguments = {"operation_id": tools.target, "item_id": tools.item(), "quantity": 1}
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(tools.report, "worker-a", "a-write", arguments, timeout=10)
            state = tools.wait_hit()
            assert not blocked.done()
            assert (tools.ledger()["request_count"], tools.ledger()["write_count"]) == (0, 0)
            assert not any(row["tool"] == "write" and row["phase"] == "start" for row in tools.audit())
            assert state["reports"][0]["status"] == "blocked"
            assert state["gate"]["first_hit_event"] == state["reports"][0]["report_event_id"]
            wrong = tools.request("POST", "/experiment/admin/prewrite/abandon", admin=True,
                json={"gate_id": "one-shot", "attempt_id": "worker-b"})
            assert wrong.status_code == 400 and not blocked.done()
            assert tools.abandon().json() == {"abandoned": True, "old_attempt_revoked": True, "released": False}
            refused = blocked.result(timeout=5)
            assert refused.status_code == 410 and "permit" not in refused.json()
        # 这里刻意保持 A 存活：控制器提前放弃仍须拒绝，
        # 不能让待执行调用变成稍后发生的业务写入。
        assert tools.call("worker-a", "write", arguments, bridge="a-write", permit="forged" * 8).status_code == 403
        assert tools.report("worker-a", "a-late", arguments).status_code == 403
        admitted = tools.report("worker-b", "b-write", arguments)
        assert admitted.status_code == 200
        assert tools.call("worker-b", "write", arguments, bridge="b-write", permit=admitted.json()["permit"]).status_code == 200
        assert (tools.ledger()["request_count"], tools.ledger()["write_count"]) == (1, 1)
        assert tools.setup().status_code == 409


def test_permit_binds_actual_attempt_call_arguments_and_is_consumed_once(tmp_path, fixture_process):
    with running(tmp_path, fixture_process) as tools:
        assert tools.setup().status_code == 200
        arguments = {"operation_id": "ordinary-nontarget", "item_id": tools.item(), "quantity": 1}
        permit = tools.report("worker-a", "call-a", arguments).json()["permit"]
        assert tools.call("worker-b", "write", arguments, bridge="call-a", permit=permit).status_code in {403, 409}
        assert tools.call("worker-a", "write", arguments, bridge="other-call", permit=permit).status_code == 403
        assert tools.call("worker-a", "write", {**arguments, "quantity": 2}, bridge="call-a", permit=permit).status_code == 403
        assert tools.call("worker-a", "write", arguments, bridge="call-a", permit=permit).status_code == 200
        assert tools.call("worker-a", "write", arguments, bridge="call-a", permit=permit).status_code == 403
        assert tools.report("worker-a", "call-a", arguments).status_code == 409
        assert (tools.ledger()["request_count"], tools.ledger()["write_count"]) == (1, 1)
        admitted = [row for row in tools.state()["events"] if row["phase"] == "write_dispatch_admitted"]
        starts = [row for row in tools.audit() if row["tool"] == "write" and row["phase"] == "start"]
        assert len(admitted) == len(starts) == 1
        assert starts[0]["prewrite"]["dispatch_event_id"] == admitted[0]["event_id"]
        assert starts[0]["call_id"] == admitted[0]["server_call_id"]


def test_candidate_cannot_query_targets_set_faults_or_use_another_attempt(tmp_path, fixture_process):
    with running(tmp_path, fixture_process) as tools:
        assert tools.setup().status_code == 200
        for method, path in (("GET", "/experiment/admin/prewrite"), ("POST", "/experiment/admin/prewrite/setup"),
                ("POST", "/experiment/admin/prewrite/abandon"), ("POST", "/experiment/admin/prewrite/release")):
            reply = tools.request(method, path, json={}) if method == "POST" else tools.request(method, path)
            assert reply.status_code == 403
            assert tools.target not in reply.text
        arguments = {"operation_id": "nontarget", "item_id": tools.item(), "quantity": 1}
        forged = tools.body("worker-b", "foreign", arguments)
        reply = tools.request("POST", "/experiment/prewrite", headers={TOKEN_HEADER: tools.tokens["worker-a"]}, json=forged)
        assert reply.status_code == 403
        wrong_scope = tools.body("worker-a", "wrong-scope", arguments)
        wrong_scope["scope"] = {**tools.scope, "user_id": "user-b"}
        assert tools.request("POST", "/experiment/prewrite", headers={TOKEN_HEADER: tools.tokens["worker-a"]}, json=wrong_scope).status_code == 400
        assert tools.call("worker-a", "write", arguments).status_code == 400
        assert tools.ledger()["write_count"] == 0
        public = tools.request("GET", "/tools").text
        assert tools.target not in public and "prewrite" not in public
        evidence = json.dumps(tools.state()) + json.dumps(tools.audit())
        assert tools.admin not in evidence and all(token not in evidence for token in tools.tokens.values())


PROBE = r'''
import json, pathlib, sys
import httpx
config = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
def mark(phase):
    with pathlib.Path(config["events"]).open("a") as stream:
        stream.write(json.dumps({"phase": phase}) + "\n")
headers = {"Authorization": "Bearer " + config["scope_token"], "X-Harness-Prewrite-Token": config["attempt_token"]}
with httpx.Client(timeout=30, trust_env=False, transport=httpx.HTTPTransport(retries=0)) as client:
    mark("before_report")
    reply = client.post(config["base"] + "/experiment/prewrite", headers=headers, json=config["report"])
    reply.raise_for_status()
    permit = reply.json()["permit"]
    mark("before_actual_call")
    headers.update({"X-Harness-Prewrite-Call-Id": config["report"]["bridge_call_id"], "X-Harness-Prewrite-Permit": permit})
    result = client.post(config["base"] + "/call", headers=headers, json={"name": "write", "arguments": config["report"]["arguments"]})
    result.raise_for_status()
    mark("actual_call_returned")
'''


def test_real_bridge_process_is_killed_before_actual_write_and_old_gate_never_releases(tmp_path, fixture_process):
    with running(tmp_path, fixture_process) as tools:
        assert tools.setup().status_code == 200
        arguments = {"operation_id": tools.target, "item_id": tools.item(), "quantity": 1}
        config = {"base": tools.base, "scope_token": tools.user["token"], "attempt_token": tools.tokens["worker-a"],
            "report": tools.body("worker-a", "actual-child-bridge", arguments), "events": str(tmp_path / "probe-events.jsonl")}
        path = tmp_path / "probe-private.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        child = subprocess.Popen([sys.executable, "-c", PROBE, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            tools.wait_hit()
            assert child.poll() is None
            assert (tools.ledger()["request_count"], tools.ledger()["write_count"]) == (0, 0)
            child.kill()
            child.wait(timeout=5)
            assert child.returncode == -signal.SIGKILL
            assert tools.abandon().json()["released"] is False
            assert [row["phase"] for row in map(json.loads, Path(config["events"]).read_text().splitlines())] == ["before_report"]
            assert not any(row["tool"] == "write" and row["phase"] == "start" for row in tools.audit())
            assert tools.ledger()["write_count"] == 0
            # 使用原私有数据库重新连接真实服务进程。
            tools.stop()
            tools.start()
            assert tools.state()["gate"]["state"] == "abandoned"
            assert tools.setup().status_code == 409
            assert tools.report("worker-a", "late-after-restart", arguments).status_code == 403
            permit = tools.report("worker-b", "new-process-write", arguments).json()["permit"]
            assert tools.call("worker-b", "write", arguments, bridge="new-process-write", permit=permit).status_code == 200
            assert (tools.ledger()["request_count"], tools.ledger()["write_count"]) == (1, 1)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


def test_no_gate_flag_preserves_original_baseline_without_attempt_or_permit(tmp_path, fixture_process):
    with running(tmp_path, fixture_process, enabled=False) as tools:
        item = tools.request("POST", "/call", json={"name": "list_items", "arguments": {}}).json()["result"]["items"][0]
        result = tools.request("POST", "/call", json={"name": "write", "arguments": {
            "operation_id": "ordinary-baseline", "item_id": item["item_id"], "quantity": 1}})
        assert result.status_code == 200
        assert tools.ledger()["write_count"] == 1
        assert all("prewrite" not in row for row in tools.audit())
        assert tools.request("GET", "/experiment/admin/prewrite", admin=True).status_code == 403
        assert tools.request("GET", "/experiment/admin/prewrite").status_code == 404


def test_private_database_identity_and_workspace_boundary_reject_rebinding(tmp_path):
    scope = {"experiment_id": "fault-recovery", "variant": "chain", "user_id": "alice", "session_id": "main"}
    binding = {"schema": "harness-lab-prewrite-service/1", "chain_id": "chain", "scope": scope,
        "database": str(tmp_path / "private/gate.sqlite"), "admin_token_sha256": digest("admin-token-32-characters-long")}
    original = PrewriteGate(binding, scope, tmp_path / "workspace")
    original.close()
    with pytest.raises(GateError, match='其他私有绑定'):
        PrewriteGate({**binding, "admin_token_sha256": digest("different-admin-token")}, scope, tmp_path / "workspace")
    with pytest.raises(GateError, match='候选工作区之外'):
        PrewriteGate({**binding, "database": str(tmp_path / "workspace/gate.sqlite")}, scope, tmp_path / "workspace")


def test_setup_rejects_scope_token_reuse_and_B_cannot_run_before_abandon(tmp_path, fixture_process):
    with running(tmp_path, fixture_process) as tools:
        original = tools.tokens["worker-b"]
        tools.tokens["worker-b"] = tools.user["token"]
        assert tools.setup().status_code == 400
        assert tools.state()["gate"] is None
        tools.tokens["worker-b"] = original
        assert tools.setup().status_code == 200
        arguments = {"operation_id": "not-target", "item_id": tools.item(), "quantity": 1}
        assert tools.report("worker-b", "premature-b", arguments).status_code == 409
        assert tools.ledger()["request_count"] == 0


def test_consumed_but_not_started_dispatch_stays_unsettled_until_actual_terminal(tmp_path):
    scope = {"experiment_id": "fault-recovery", "variant": "chain", "user_id": "alice", "session_id": "main"}
    gate = PrewriteGate({"schema": "harness-lab-prewrite-service/1", "chain_id": "chain", "scope": scope,
        "database": str(tmp_path / "private/gate.sqlite"), "admin_token_sha256": digest("admin-secret-token-32")},
        scope, tmp_path / "workspace")
    a, b = "a-secret-token-32-characters", "b-secret-token-32-characters"
    gate.setup({"gate_id": "one-shot", "chain_id": "chain", "scope": scope,
        "target": {"attempt_id": "worker-a", "operation_id": "target"},
        "attempts": {"worker-a": digest(a), "worker-b": digest(b)}})
    args = {"operation_id": "other", "item_id": "item", "quantity": 1}
    report = {"chain_id": "chain", "scope": scope, "attempt_id": "worker-a", "bridge_call_id": "bridge",
        "native_call_id": None, "tool": "write", "arguments": args}
    permit = gate.report(a, report)["permit"]
    gate.consume(a, "bridge", permit, args, "admitted-before-tool-start")
    pending = gate.snapshot()["unsettled_dispatches"]
    assert len(pending) == 1 and pending[0]["state"] == "admitted"
    assert pending[0]["server_call_id"] == "admitted-before-tool-start"
    # 只有服务观察到的完成才能关闭计数，A 退出或放弃均不能。
    gate.finish_dispatch("admitted-before-tool-start", known=True, status=200)
    assert gate.snapshot()["unsettled_dispatches"] == []
    gate.close()


def test_real_lost_receipt_remains_unknown_in_dispatch_ledger_without_auto_query(tmp_path, fixture_process):
    with running(tmp_path, fixture_process) as tools:
        assert tools.setup().status_code == 200
        arguments = {"operation_id": "lost-receipt-other", "item_id": tools.item(), "quantity": 1}
        fixture_process.fault(tools.user, arguments["operation_id"], "drop_receipt_once")
        permit = tools.report("worker-a", "lost-reply-call", arguments).json()["permit"]
        reply = tools.call("worker-a", "write", arguments, bridge="lost-reply-call", permit=permit)
        assert reply.status_code == 502 and reply.json()["side_effect_state"] == "unknown"
        unknown = tools.state()["unsettled_dispatches"]
        assert len(unknown) == 1 and unknown[0]["state"] == "unknown"
        assert tools.ledger()["request_count"] == tools.ledger()["write_count"] == 1
        assert not any(row["tool"] in {"receipt", "ledger"} for row in tools.audit())


@contextmanager
def corrupt_committed_reply(fixture, status, body):
    """仅供测试的回环代理：先真实提交，再破坏响应。

    用于校准不确定性分类，正式实现没有此代理。
    独立真实业务账本仍是业务事实依据。
    """
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def forward(self, method):
            length = int(self.headers.get("Content-Length", "0"))
            content = self.rfile.read(length) if length else None
            with httpx.Client(timeout=5, trust_env=False, transport=httpx.HTTPTransport(retries=0)) as client:
                actual = client.request(method, fixture.base_url + self.path, content=content,
                    headers={"Authorization": self.headers.get("Authorization", ""),
                        "Content-Type": "application/json"})
            outgoing_status, outgoing_body = actual.status_code, actual.content
            if method == "POST" and self.path == "/writes" and 200 <= actual.status_code < 300:
                outgoing_status, outgoing_body = status, body
            self.send_response(outgoing_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(outgoing_body)))
            self.end_headers()
            self.wfile.write(outgoing_body)

        def do_GET(self):
            self.forward("GET")

        def do_POST(self):
            self.forward("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def hit_and_abandon(tools, item_id):
    arguments = {"operation_id": tools.target, "item_id": item_id, "quantity": 1}
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(tools.report, "worker-a", "target-after-other-call", arguments, timeout=10)
        tools.wait_hit()
        assert not blocked.done()
        assert tools.abandon().json()["released"] is False
        assert blocked.result(timeout=5).status_code == 410


def assert_unknown_blocks_B(tools, arguments):
    unresolved = tools.state()["unsettled_dispatches"]
    assert len(unresolved) == 1 and unresolved[0]["state"] == "unknown"
    assert unresolved[0]["attempt_id"] == "worker-a" and unresolved[0]["tool"] == "write"
    assert tools.call("worker-b", "list_items", {}).status_code == 409
    assert tools.report("worker-b", "blocked-b", arguments).status_code == 409
    assert tools.call("worker-a", "list_items", {}).status_code == 403
    assert not any(row["tool"] in {"receipt", "ledger"} for row in tools.audit())


@pytest.mark.parametrize("status,body", [
    (200, b"not-json"),
    (200, b'"\xff"'),
    (200, b"[]"),
    (503, b"<html>upstream unavailable</html>"),
    (500, b'{"error":"unexpected failure after commit"}'),
    (503, b'{"error":"Fixture database unavailable"}'),
    (403, b"not-json"),
])
def test_committed_but_invalid_fixture_reply_cannot_finish_A_or_admit_B(tmp_path, fixture_process, status, body):
    user = fixture_process.prepare(experiment_id="fault-recovery", variant="prewrite-calibration",
        write_mode="non_idempotent")["user-a"]
    tools = GatedTools(tmp_path, fixture_process, user)
    with corrupt_committed_reply(fixture_process, status, body) as relay:
        tools.binding["fixture_url"] = relay
        (tools.root / "tools.json").write_text(json.dumps(tools.binding))
        try:
            tools.start()
            assert tools.setup().status_code == 200
            arguments = {"operation_id": "actually-committed", "item_id": tools.item(), "quantity": 1}
            permit = tools.report("worker-a", "corrupted-reply", arguments).json()["permit"]
            reply = tools.call("worker-a", "write", arguments, bridge="corrupted-reply", permit=permit)
            assert reply.status_code == 502 and reply.json()["side_effect_state"] == "unknown"
            assert tools.ledger()["request_count"] == tools.ledger()["write_count"] == 1
            hit_and_abandon(tools, arguments["item_id"])
            assert_unknown_blocks_B(tools, arguments)
            # 撤销与服务重启不能抹去结果不确定的调用。
            tools.stop()
            tools.start()
            assert_unknown_blocks_B(tools, arguments)
            assert tools.ledger()["request_count"] == tools.ledger()["write_count"] == 1
        finally:
            tools.stop()


@pytest.mark.parametrize("status,body,expected", [
    (200, b"not-json", 400),
    (500, b'{"error":"unexpected failure after commit"}', 500),
])
def test_disabled_gate_preserves_original_response_classification(tmp_path, fixture_process, status, body, expected):
    user = fixture_process.prepare(experiment_id="fault-recovery", variant="prewrite-calibration",
        write_mode="non_idempotent")["user-a"]
    tools = GatedTools(tmp_path, fixture_process, user, enabled=False)
    with corrupt_committed_reply(fixture_process, status, body) as relay:
        tools.binding["fixture_url"] = relay
        (tools.root / "tools.json").write_text(json.dumps(tools.binding))
        try:
            tools.start()
            arguments = {"operation_id": "unchanged-default-classification", "item_id": tools.item(), "quantity": 1}
            reply = tools.call("worker-a", "write", arguments)
            assert reply.status_code == expected
            assert "side_effect_state" not in reply.json()
            assert (tools.ledger()["request_count"], tools.ledger()["write_count"]) == (1, 1)
            assert all("prewrite" not in row for row in tools.audit())
            assert not Path(tools.private["database"]).exists()
        finally:
            tools.stop()


def synthetic_post_commit_error(kind):
    if kind == "ToolError":
        return ToolError("模拟提交后工具错误")
    if kind == "UnicodeDecodeError":
        return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "模拟提交后解码错误")
    return ValueError("模拟提交后序列化错误")


@pytest.mark.parametrize("kind", ["ToolError", "ValueError", "UnicodeDecodeError"])
@pytest.mark.parametrize("failure_stage", ["tool", "finish_audit"])
def test_generic_exception_after_actual_commit_remains_unknown(tmp_path, fixture_process, kind, failure_stage):
    user = fixture_process.prepare(experiment_id="fault-recovery", variant="prewrite-calibration",
        write_mode="non_idempotent")["user-a"]
    tools = GatedTools(tmp_path, fixture_process, user)
    # 本测试只修改隔离的测试服务对象；实际 FixtureClient
    # 与 HTTP 处理器仍通过 Docker 回环网络执行。
    server = ToolsHTTPServer(("127.0.0.1", 0), tools.binding, prewrite_binding=tools.private)
    if failure_stage == "tool":
        original = server.tools.call

        def call(name, arguments):
            result = original(name, arguments)
            if name == "write":
                raise synthetic_post_commit_error(kind)
            return result

        server.tools.call = call
    else:
        original = server.tools.event

        def event(record):
            if record.get("tool") == "write" and record.get("phase") == "finish" and record.get("status") == 200:
                raise synthetic_post_commit_error(kind)
            return original(record)

        server.tools.event = event
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tools.base = "http://127.0.0.1:" + str(server.server_port)
    try:
        assert tools.setup().status_code == 200
        arguments = {"operation_id": "commit-before-exception", "item_id": tools.item(), "quantity": 1}
        permit = tools.report("worker-a", "post-commit-error", arguments).json()["permit"]
        reply = tools.call("worker-a", "write", arguments, bridge="post-commit-error", permit=permit)
        assert reply.status_code == 502 and reply.json()["side_effect_state"] == "unknown"
        assert tools.ledger()["request_count"] == tools.ledger()["write_count"] == 1
        hit_and_abandon(tools, arguments["item_id"])
        assert_unknown_blocks_B(tools, arguments)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_source_defined_prewrite_503_is_a_known_finished_rejection(tmp_path, fixture_process):
    with running(tmp_path, fixture_process) as tools:
        assert tools.setup().status_code == 200
        arguments = {"operation_id": "known-before-write-fault", "item_id": tools.item(), "quantity": 1}
        fixture_process.fault(tools.user, arguments["operation_id"], "fail_once")
        permit = tools.report("worker-a", "known-503", arguments).json()["permit"]
        reply = tools.call("worker-a", "write", arguments, bridge="known-503", permit=permit)
        assert reply.status_code == 503 and reply.json()["details"]["retryable"] is True
        assert tools.state()["unsettled_dispatches"] == []
        assert (tools.ledger()["request_count"], tools.ledger()["write_count"]) == (1, 0)
        hit_and_abandon(tools, arguments["item_id"])
        assert tools.call("worker-b", "list_items", {}).status_code == 200


@pytest.mark.parametrize("known", [True, False])
def test_revoke_cannot_admit_B_until_all_admitted_A_work_has_a_known_terminal(tmp_path, fixture_process, known):
    with running(tmp_path, fixture_process) as tools:
        assert tools.setup().status_code == 200
        item_id = tools.item()
        # 独立打开同一私有账本，模拟调用获准但尚未开始的
        # 精确竞态，不虚构业务请求。
        gate = PrewriteGate(tools.private, tools.scope, tools.binding["workspace"],
            forbidden_credential_sha256={digest(tools.user["token"])})
        try:
            gate.admit_other(tools.tokens["worker-a"], "write_file", {"path": "report.txt", "content": "x"},
                "admitted-before-start")
            hit_and_abandon(tools, item_id)
            assert tools.call("worker-b", "list_items", {}).status_code == 409
            gate.finish_dispatch("admitted-before-start", known=known, status=200 if known else 502)
            assert tools.call("worker-b", "list_items", {}).status_code == (200 if known else 409)
            if not known:
                # 后续未经证明的完成不能静默消除 unknown 状态。
                gate.finish_dispatch("admitted-before-start", known=True, status=200)
                assert tools.call("worker-b", "list_items", {}).status_code == 409
                assert gate.snapshot()["unsettled_dispatches"][0]["state"] == "unknown"
        finally:
            gate.close()
