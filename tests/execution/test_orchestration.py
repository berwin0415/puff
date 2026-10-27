"""使用 Docker 传输替身测试真实宿主编排控制流程。

测试在验收容器内运行，不启动 Docker 进程、容器、候选或远程模型请求。
"""

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from scripts.lab import Lab


class FakeDockerLab(Lab):
    """保留编排流程，只替换 Docker 输入输出和资源采样。"""

    def __init__(self, root: Path):
        super().__init__()
        self.config = replace(self.config, host_data_root=root,
                              sampling=replace(self.config.sampling, interval_seconds=0.001))
        self.env = {"HARNESS_LAB_DATA_ROOT": str(root)}
        self.commands = []
        self.failures = set()

    def call(self, args, *, capture=False, check=True, timeout=None):
        command = tuple(args)
        self.commands.append(command)
        if command in self.failures:
            raise subprocess.TimeoutExpired(args, timeout or 1)
        tail = args[len(self.prefix):] if args[:len(self.prefix)] == self.prefix else None
        output = ""
        if tail == ["--profile", "model", "config", "--format", "json"]:
            output = json.dumps({"services": {"model-proxy": {"environment": {"MODEL_NAME": "test-model"}}}})
        elif tail and "runner.experiment" in tail and "prepare" in tail:
            # 真实 prepare 操作会生成这些挂载目录。
            for key in ("LAB_EVIDENCE_HOST", "LAB_WORKSPACE_HOST", "LAB_STATE_HOST", "LAB_BUDGET_HOST"):
                Path(self.env[key]).mkdir(parents=True, exist_ok=False)
        return subprocess.CompletedProcess(args, returncode=0, stdout=output, stderr="")

    def sample(self, event, samples, evidence):
        # 采样与清理无关，不能和模拟磁盘故障竞争。
        return


def test_evidence_write_failure_halts_candidate_and_still_cleans_every_service(tmp_path, monkeypatch):
    lab = FakeDockerLab(tmp_path / "data")
    run_id = "save-failure"
    name = "harness-lab-dsh-" + run_id
    original_write = Path.write_text

    def fail_evidence(path, content, *args, **kwargs):
        if path.name == "fixtures.log":
            lab.commands.append(("evidence_write_failed", str(path)))
            raise OSError("注入的证据磁盘故障")
        return original_write(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_evidence)
    with pytest.raises(OSError, match="注入的证据磁盘故障"):
        lab.run_candidate("deepseek_harness", run_id, 101)

    failure = next(i for i, command in enumerate(lab.commands) if command[0] == "evidence_write_failed")
    assert lab.commands.index(("docker", "kill", name)) < failure
    proxy_stop = tuple(lab.prefix + ["stop", "--timeout", "3", "model-proxy"])
    assert lab.commands.index(proxy_stop) < failure
    after_failure = lab.commands[failure + 1:]
    assert ("docker", "rm", "-f", name) in after_failure
    for service in ("tools", "model-proxy", "fixtures"):
        assert tuple(lab.prefix + ["stop", "--timeout", "3", service]) in after_failure
        assert tuple(lab.prefix + ["rm", "-f", service]) in after_failure


def test_cleanup_transport_failures_do_not_skip_other_resources(tmp_path, capsys):
    lab = FakeDockerLab(tmp_path / "data")
    name = "harness-lab-dsh-cleanup-failure"
    lab.failures = {("docker", "rm", "-f", name),
                    tuple(lab.prefix + ["stop", "--timeout", "3", "tools"])}

    lab.cleanup(("tools", "model-proxy", "fixtures"), names=(name,))

    # 单次容器删除或服务停止失败，不能中断其余清理。
    for service in ("tools", "model-proxy", "fixtures"):
        assert tuple(lab.prefix + ["stop", "--timeout", "3", service]) in lab.commands
        assert tuple(lab.prefix + ["rm", "-f", service]) in lab.commands
    reported = capsys.readouterr().err
    assert name in reported and "tools" in reported
