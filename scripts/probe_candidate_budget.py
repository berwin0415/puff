"""通过宿主编排 Docker，使用模拟 API 探测真实候选重试。

请使用新的 --run-id。添加 runner/model_probe.py 后需重建 bench 镜像。所有准备、模拟 HTTP 处理、SQLite 检查和判定均在 Docker 内执行。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import subprocess
from zoneinfo import ZoneInfo

from lab import CANDIDATES, Lab, ROOT
from runner.bench import identifier


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def run_probe(run_id: str, seed=101, candidate='deepseek_harness'):
    lab = Lab()
    candidate_service = CANDIDATES[candidate][0]
    if any(row["state"]["Running"] for row in lab.containers()):
        raise RuntimeError('请先结束正在运行的 harness-lab 实验')
    lab.config.initialize_layout()
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    paths = lab.config.paths_for("screening", run_id, candidate, "alice", "main", run_id, case="tools", date=day)
    if any(path.exists() for path in (paths.run, paths.fixture, paths.state, paths.budget, paths.workspace)):
        raise RuntimeError('运行 ID 已存在；请为预算探针选择新 ID')
    project = "harness-lab-budget-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]
    paths.fixture.mkdir(parents=True, exist_ok=False)
    override = paths.fixture / "budget-probe.compose.yaml"
    # --env-file /dev/null 阻止 Compose 自动加载根目录 .env；
    # !reset 在解析前移除代理显式配置的 env_file。
    override.write_text("""services:
  model-proxy:
    env_file: !reset []
    environment:
      MODEL_API_BASE_URL: http://model-stub:8090/v1
      MODEL_API_KEY: fake-probe-upstream-key
      MODEL_PROXY_TIMEOUT_AT: ''
      HTTP_PROXY: ''
      HTTPS_PROXY: ''
      ALL_PROXY: ''
      NO_PROXY: model-stub,localhost,127.0.0.1
  deepseek-harness:
    volumes:
      - ${LAB_PROBE_STATE_HOST}:/state
      - ${LAB_PROBE_CANDIDATE_OUTPUT_HOST}:/evidence
  model-stub:
    image: harness-lab-bench:local
    platform: linux/arm64
    entrypoint: [python]
    command: [-m, runner.model_probe, serve, --database, /probe/upstream.sqlite, --port, '8090']
    read_only: true
    tmpfs: [/tmp:size=16m]
    mem_limit: 128m
    cpus: 0.125
    pids_limit: 64
    volumes:
      - ${LAB_EVIDENCE_HOST}/model-stub:/probe
    networks: [lab]
networks:
  lab:
    internal: true
""".replace('  deepseek-harness:', '  ' + candidate_service + ':'))
    lab.prefix = ["docker", "compose", "--env-file", "/dev/null", "--project-name", project,
                  "-f", str(ROOT / "compose/docker-compose.yml"), "-f", str(override)]
    for name in ("MODEL_API_KEY", "MODEL_API_BASE_URL", "MODEL_NAME"):
        lab.env.pop(name, None)
    lab.env.update({"LAB_EXPERIMENT_ID": "screening", "LAB_VARIANT": run_id,
                    "LAB_EVIDENCE_HOST": str(paths.run), "LAB_WORKSPACE_HOST": str(paths.workspace),
                    "LAB_STATE_HOST": str(paths.state), "LAB_BUDGET_HOST": str(paths.budget),
                    "LAB_FIXTURE_HOST": str(paths.fixture), "LAB_MODEL_NAME": "probe-model",
                    "LAB_MAX_MODEL_CALLS": "2", "LAB_PROBE_STATE_HOST": str(paths.state / "first"),
                    "LAB_PROBE_CANDIDATE_OUTPUT_HOST": str(paths.run / "first/candidate")})
    names = [project + "-candidate-first", project + "-candidate-second"]
    manifest = {"run_id": run_id, "candidate": candidate, "model_name": "probe-model",
                "real_model_called": False, "mock_only_internal_network": False,
                "rounds": [], "proxy_ids": [], "first_candidate_deleted": False, "errors": []}
    outcome = 1
    container_run = lab.config.for_environment("container").paths_for(
        "screening", run_id, candidate, "alice", "main", run_id, case="tools", date=day)

    def bench(*args, **kwargs):
        return lab.compose("run", "--rm", "--entrypoint", "python", "bench", *map(str, args), **kwargs)

    def inspect_candidate(name):
        template = '{"id":{{json .Id}},"image_id":{{json .Image}},"exit_code":{{json .State.ExitCode}},"compose_service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
        return json.loads(lab.call(["docker", "inspect", "--format", template, name], capture=True, timeout=10).stdout)

    try:
        resolved = json.loads(lab.compose("--profile", "model", "--profile", candidate_service,
                                          "config", "--format", "json", capture=True, timeout=30).stdout)
        proxy = resolved["services"]["model-proxy"]
        if (proxy.get("env_file") or proxy["environment"].get("MODEL_API_BASE_URL") != "http://model-stub:8090/v1"
                or proxy["environment"].get("MODEL_API_KEY") != "fake-probe-upstream-key"
                or resolved["networks"]["lab"].get("internal") is not True):
            raise RuntimeError('未应用仅使用模拟服务的 Compose 配置')
        expected_image = lab.call(["docker", "image", "inspect", resolved["services"][candidate_service]["image"],
                                   "--format", "{{.Id}}"], capture=True, timeout=10).stdout.strip()
        manifest.update({"expected_candidate_service": candidate_service, "expected_candidate_image_id": expected_image})
        del resolved, proxy
        manifest["mock_only_internal_network"] = True
        lab.compose("up", "-d", "--no-build", "fixtures")
        lab.wait_health("fixtures", 8080)
        bench("-m", "runner.experiment", "prepare", "--run-id", run_id, "--candidate", candidate,
              "--seed", seed, "--date", day)
        bench("-m", "runner.model_probe", "prepare", "--run-root", container_run.run, "--state-root", container_run.state)
        (paths.run / "budget-probe.compose.yaml").write_text(override.read_text())
        lab.compose("up", "-d", "--no-build", "tools", "model-stub", "model-proxy")
        for health_service, port in (("tools", 8081), ("model-stub", 8090), ("model-proxy", 8082)):
            lab.wait_health(health_service, port)
        for index, label in enumerate(("first", "second")):
            lab.env.update({"LAB_PROBE_STATE_HOST": str(paths.state / label),
                            "LAB_PROBE_CANDIDATE_OUTPUT_HOST": str(paths.run / label / "candidate")})
            manifest["proxy_ids"].append(lab.compose("ps", "-q", "model-proxy", capture=True, timeout=10).stdout.strip())
            try:
                result = lab.compose("run", "--no-deps", "--name", names[index], candidate_service,
                                     capture=True, check=False, timeout=lab.config.budget.active_timeout_seconds + 30)
                command_exit = result.returncode
            except subprocess.TimeoutExpired:
                lab.halt(names=(names[index],))
                command_exit = 124
            record = inspect_candidate(names[index])
            record["command_exit"] = command_exit
            manifest["rounds"].append(record)
            if record["compose_service"] != candidate_service or record["image_id"] != expected_image:
                raise RuntimeError('候选服务或镜像不符合预期；拒绝生成候选判定')
            log = lab.call(["docker", "logs", names[index]], capture=True, check=False, timeout=10)
            (paths.run / label / "candidate.log").write_text(log.stdout + log.stderr)
            bench("-m", "runner.model_probe", "snapshot", "--budget", container_run.budget / "calls.sqlite",
                  "--upstream", container_run.run / "model-stub/upstream.sqlite",
                  "--candidate-dir", container_run.run / label / "candidate",
                  "--output", container_run.run / label / "snapshot.json")
            if index == 0:
                lab.call(["docker", "rm", "-f", names[0]], capture=True, timeout=10)
                gone = lab.call(["docker", "inspect", "--format", "{{.Id}}", names[0]], capture=True, check=False, timeout=10)
                manifest["first_candidate_deleted"] = gone.returncode != 0
                lab.compose("stop", "--timeout", "3", "model-proxy", capture=True, timeout=15)
                lab.compose("rm", "-f", "model-proxy", capture=True, timeout=15)
                lab.compose("up", "-d", "--no-build", "model-proxy")
                lab.wait_health("model-proxy", 8082)
    except Exception as exc:
        manifest["errors"].append(type(exc).__name__ + '：预算探针命令失败；请检查保留的证据')
    finally:
        # 在证据 I/O 或检查前，停止候选与全部模型出口。
        lab.halt(names=names, services=("model-proxy", "model-stub"))
        try:
            if paths.run.exists():
                save(paths.run / "budget-probe-manifest.json", manifest)
                for service in ("fixtures", "tools", "model-proxy", "model-stub"):
                    try:
                        log = lab.compose("logs", "--no-color", service, capture=True, check=False, timeout=10)
                        (paths.run / (service + ".log")).write_text(log.stdout + log.stderr)
                    except Exception:
                        pass
                if len(manifest["rounds"]) == 2 and not manifest["errors"]:
                    outcome = bench("-m", "runner.model_probe", "verify", "--run-root", container_run.run,
                                    check=False, timeout=30).returncode
                else:
                    save(paths.run / "budget-probe-verdict.json", {"passed": False, "real_model_called": False,
                                                                 "errors": manifest["errors"] or ['轮次未完整结束']})
        finally:
            lab.cleanup(("model-proxy", "model-stub", "tools", "fixtures"), names=names)
            # 本项目及内部网络仅属于本探针；
            # 不使用 --volumes 时，down 后仍保留绑定挂载的证据和状态。
            lab.compose("down", "--remove-orphans", capture=True, check=False, timeout=20)
    print(json.dumps({"run_id": run_id, "passed": outcome == 0, "real_model_called": False, "evidence": str(paths.run)}))
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=identifier)
    parser.add_argument("--candidate", choices=tuple(CANDIDATES), default='deepseek_harness')
    parser.add_argument("--seed", type=int, choices=(101, 202, 303, 404, 505), default=101)
    args = parser.parse_args()
    return run_probe(args.run_id, args.seed, args.candidate)


if __name__ == "__main__":
    raise SystemExit(main())
