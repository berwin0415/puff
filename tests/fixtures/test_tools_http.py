"""真实绑定工具进程不能切换凭证、工作区或作用域。"""

from contextlib import contextmanager
import json
from pathlib import Path
import selectors
import subprocess
import sys

import httpx


class ToolsProcess:
    def __init__(self, directory, fixture_process, scope):
        self.directory = directory.resolve()
        self.binding = {
            "fixture_url": fixture_process.base_url,
            "fixture_token": scope["token"],
            "workspace": str(self.directory / "workspace-a"),
            "scope": {field: scope[field] for field in ("experiment_id", "variant", "user_id", "session_id")},
            "events_path": str(self.directory / "evidence" / "tool-events.jsonl"),
        }
        self.path = self.directory / "tool-binding.json"
        self.path.write_text(json.dumps(self.binding), encoding="utf-8")
        self.process = None

    def start(self):
        self.process = subprocess.Popen([sys.executable, "-m", "execution.tools.server", "--host", "127.0.0.1", "--port", "0", "--binding-file", str(self.path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(10):
                self.stop()
                raise RuntimeError("工具 CLI 未就绪")
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("工具 CLI 失败：" + self.process.stderr.read())
            information = json.loads(line)
            assert self.binding["fixture_token"] not in line
            self.base_url = f"http://127.0.0.1:{information['port']}"
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

    def call(self, name, arguments, token=None):
        with httpx.Client(trust_env=False, timeout=5) as client:
            return client.post(self.base_url + "/call", headers={"Authorization": "Bearer " + (token or self.binding["fixture_token"])}, json={"name": name, "arguments": arguments})

    def events(self):
        return [json.loads(line) for line in Path(self.binding["events_path"]).read_text(encoding="utf-8").splitlines()]


@contextmanager
def running_tools(directory, fixture_process, scope):
    process = ToolsProcess(directory, fixture_process, scope)
    try:
        process.start()
        yield process
    finally:
        process.stop()


def test_bound_http_tools_do_real_business_and_files_but_reject_scope_replacement(tmp_path, fixture_process):
    scopes = fixture_process.prepare()
    a, b = scopes["user-a"], scopes["user-b"]
    with running_tools(tmp_path, fixture_process, a) as process:
        items = process.call("list_items", {"page": 1, "page_size": 2}).json()["result"]["items"]
        assert len(items) == 2
        result = process.call("calculate_totals", {"items": [{"unit_price_cents": item["unit_price_cents"], "quantity": 1} for item in items]})
        assert result.json()["result"]["total_cents"] == sum(item["unit_price_cents"] for item in items)
        assert process.call("write_file", {"path": "report/result.txt", "content": "agent-generated result"}).status_code == 200
        assert process.call("read_file", {"path": "report/result.txt"}).json()["result"]["content"] == "agent-generated result"
        assert process.call("write", {"operation_id": "ordinary", "item_id": items[0]["item_id"], "quantity": 1}).status_code == 200
        ledger = fixture_process.request("GET", "/ledger", token=a["token"]).json()
        assert ledger["write_count"] == 1
        assert process.call("ledger", {}, token=b["token"]).status_code == 403
        for arguments in ({"user_id": "user-b"}, {"fixture_token": b["token"]}, {"workspace": "/tmp/user-b"}):
            assert process.call("ledger", arguments).status_code == 400
        assert process.call("write_file", {"path": "../evidence/tool-events.jsonl", "content": "erase audit"}).status_code == 400
        assert fixture_process.request("GET", "/ledger", token=b["token"]).json()["write_count"] == 0
        events_text = Path(process.binding["events_path"]).read_text(encoding="utf-8")
        assert a["token"] not in events_text and b["token"] not in events_text
        assert {event["scope"]["user_id"] for event in process.events()} == {"user-a"}
        assert any(event["phase"] == "start" and event["tool"] == "write" for event in process.events())
        assert any(event["phase"] == "finish" and event["tool"] == "write" and event["status"] == 200 for event in process.events())


def test_tool_process_leaves_lost_receipt_uncertain_without_retrying_or_bypassing_policy(tmp_path, fixture_process):
    scope = fixture_process.prepare(write_mode="non_idempotent")["user-a"]
    fixture_process.fault(scope, "lost-reply", "drop_receipt_once")
    with running_tools(tmp_path, fixture_process, scope) as process:
        item = process.call("list_items", {}).json()["result"]["items"][0]
        response = process.call("write", {"operation_id": "lost-reply", "item_id": item["item_id"], "quantity": 1})
        assert response.status_code == 502
        assert response.json()["side_effect_state"] == "unknown"
        ledger = fixture_process.request("GET", "/ledger", token=scope["token"]).json()
        assert (ledger["request_count"], ledger["write_count"]) == (1, 1)
        assert process.call("receipt", {"operation_id": "lost-reply"}).json()["result"]["write_count"] == 1
        assert process.call("sensitive_export", {"operation_id": "blocked"}).status_code == 403
        old_pid = process.process.pid
        original_events = len(process.events())
        process.stop()
        process.start()
        assert process.process.pid != old_pid
        assert process.call("sensitive_export", {"operation_id": "blocked"}).status_code == 403
        assert len(process.events()) > original_events
        ledger = process.call("ledger", {}).json()["result"]
        assert ledger["write_count"] == 1
        assert sum(row["action"] == "sensitive_export" and row["status"] == 403 for row in ledger["requests"]) == 2


def test_bad_backend_binding_fails_before_listening_or_creating_workspace(tmp_path, fixture_process):
    scopes = fixture_process.prepare()
    process = ToolsProcess(tmp_path, fixture_process, scopes["user-a"])
    process.binding["scope"]["user_id"] = "user-b"
    process.path.write_text(json.dumps(process.binding), encoding="utf-8")
    completed = subprocess.run([sys.executable, "-m", "execution.tools.server", "--host", "127.0.0.1", "--port", "0", "--binding-file", str(process.path)], capture_output=True, text=True, timeout=10)
    assert completed.returncode != 0
    assert '不属于' in completed.stderr
    assert not Path(process.binding["workspace"]).exists()
    assert process.binding["fixture_token"] not in completed.stdout + completed.stderr
