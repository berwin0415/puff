"""在测试容器中以子进程启动真实模拟业务 CLI。"""

import json
from pathlib import Path
import selectors
import subprocess
import sys

import httpx
import pytest


class FixtureProcess:
    def __init__(self, directory: Path):
        self.db = directory.resolve() / "ledger.sqlite"
        self.process = None
        self.start()

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "fixtures.service.server", "--host", "127.0.0.1", "--port", "0", "--db", str(self.db)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(10):
                self.stop()
                raise RuntimeError("Fixture CLI 未就绪")
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Fixture CLI 失败：" + self.process.stderr.read())
            self.startup_line = line
            information = json.loads(line)
            self.base_url = f"http://127.0.0.1:{information['port']}"
            self.admin_token = Path(information["admin_token_file"]).read_text(encoding="utf-8").strip()
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

    def restart(self):
        self.stop()
        self.start()

    def request(self, method, path, token=None, **kwargs):
        headers = {"Authorization": "Bearer " + (token or self.admin_token)}
        with httpx.Client(trust_env=False, timeout=5) as client:
            return client.request(method, self.base_url + path, headers=headers, **kwargs)

    def prepare(self, seed=101, variant="baseline", write_mode="idempotent", experiment_id="tools", users=None):
        response = self.request("POST", "/admin/scenarios", json={
            "experiment_id": experiment_id, "variant": variant, "seed": seed,
            "write_mode": write_mode,
            "users": users or [{"user_id": "user-a", "session_id": "session-a"}, {"user_id": "user-b", "session_id": "session-b"}],
        })
        assert response.status_code == 200, response.text
        return {scope["user_id"]: scope for scope in response.json()["scopes"]}

    def fault(self, scope, operation_id, fault):
        response = self.request("POST", "/admin/faults", json={
            **{name: scope[name] for name in ("experiment_id", "variant", "user_id", "session_id")},
            "operation_id": operation_id, "fault": fault,
        })
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def fixture_process(tmp_path):
    process = FixtureProcess(tmp_path)
    try:
        yield process
    finally:
        process.stop()
