"""独立判定可信宿主机留存的 Docker Worker 观测。

只检查进程证据，不判断原生任务完成或工具结果。
调用方必须提供可信 WorkerExpectation，不能从候选框架可控的任意结果复制预期身份。
调用方必须记录哪些字段在执行前固定、哪些在执行后观测；
运行结束时的 Docker ID 只能用于归属，不能替代独立的启动前 ID 固定记录。
兼容现有详细清单和 orchestration-status.json，无需改写。
CLI 在 bench 容器中运行，仅写入新的补充审计。"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_DOCKER_ID = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
_NAME = re.compile(r"/?[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_MAX_JSON_BYTES = 8 * 1024 * 1024
_CANDIDATES = {
    "deepseek_harness": ("deepseek-harness", "dsh"), "openclaw": ("openclaw", "openclaw"),
    "deepagents_langgraph": ("deepagents-langgraph", "deepagents"),
    "pi_agent_core": ("pi-agent-core", "pi"), "google_adk": ("google-adk", "adk"),
    "openai_agents_sdk": ("openai-agents-sdk", "agents-sdk"),
    "openhands": ("openhands", "openhands"), "codex_sdk": ("codex-sdk", "codex"),
}


def expected_exit_code(candidate: str, case: str) -> int:
    """当前详细验证策略：普通退出码为 0，取消为 1，但原生图 drain 为 0。"""
    return 1 if case == "cancel" and candidate != "deepagents_langgraph" else 0


def _matches(pattern: re.Pattern, value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _name(value: Any) -> str | None:
    return value.removeprefix("/") if _matches(_NAME, value) else None


@dataclass(frozen=True)
class WorkerExpectation:
    run_id: str
    candidate: str
    case: str
    container_name: str
    container_id: str
    image_id: str
    image_tag: str
    expected_exit_code: int = 0
    memory_limit: int | None = None
    cpu_nano: int | None = None

    def __post_init__(self):
        if not all(_matches(_ID, value) for value in (self.run_id, self.candidate, self.case)):
            raise ValueError("预期 run、candidate 和 case 必须是明确有效的标识")
        if not _matches(_NAME, self.container_name) or not _matches(_DOCKER_ID, self.container_id):
            raise ValueError("预期 Worker 必须有精确名称和完整 Docker ID")
        if not _matches(_IMAGE_ID, self.image_id):
            raise ValueError("预期镜像必须是不可变的 sha256 Docker 镜像 ID")
        if (not isinstance(self.image_tag, str) or not self.image_tag
                or len(self.image_tag) > 512 or any(char.isspace() for char in self.image_tag)):
            raise ValueError("必须提供预期镜像标签")
        if type(self.expected_exit_code) is not int or not 0 <= self.expected_exit_code <= 255:
            raise ValueError("预期退出码必须是整数 Docker 退出码")
        if self.expected_exit_code != expected_exit_code(self.candidate, self.case):
            raise ValueError("预期退出码不匹配当前任务策略；信号退出不能算健康退出")
        for value in (self.memory_limit, self.cpu_nano):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("预期资源上限必须是正整数")


def grade_process_observation(manifest: Any, host_observation: Any,
                              expected: WorkerExpectation) -> dict:
    """缺失或含糊的观测一律拒绝，不判定服务进程退出。

    清单和宿主机观测必须来自候选框架可写输出挂载之外的可信宿主机证据。
    此检查不是签名验证，也不会实时检查 Docker。服务可能仍运行或清理时退出 137；
    仅评估唯一固定的候选 Worker 的进程健康。"""
    if not isinstance(expected, WorkerExpectation):
        raise TypeError("必须提供可信 WorkerExpectation")
    manifest = manifest if isinstance(manifest, dict) else {}
    host = host_observation if isinstance(host_observation, dict) else {}
    identity = {key: getattr(expected, key) for key in ("run_id", "candidate", "case")}
    inventory = manifest.get("containers")
    inventory_ok = isinstance(inventory, list) and bool(inventory)
    if inventory_ok:
        inventory_ok = all(isinstance(row, dict) and _matches(_DOCKER_ID, row.get("id"))
                           and _name(row.get("name")) is not None for row in inventory)
    if inventory_ok:
        inventory_ok = (len({row["id"] for row in inventory}) == len(inventory)
                        and len({_name(row["name"]) for row in inventory}) == len(inventory))
    rows = inventory if isinstance(inventory, list) else []
    workers = [row for row in rows if isinstance(row, dict) and (
        row.get("id") == expected.container_id
        or _name(row.get("name")) == _name(expected.container_name)
        or row.get("image_id") == expected.image_id
        or row.get("image_tag") == expected.image_tag)]
    unique = inventory_ok and len(workers) == 1
    worker = workers[0] if unique else {}
    state = worker.get("state") if isinstance(worker.get("state"), dict) else {}
    exit_codes = (state.get("ExitCode"), manifest.get("candidate_exit_code"), host.get("candidate_exit_code"))
    checks = [
        ("manifest_identity", all(manifest.get(key) == value for key, value in identity.items()),
         "可信清单标识预期 run、candidate 和 case"),
        ("host_identity", all(host.get(key) == value for key, value in identity.items()),
         "可信宿主机进程观测标识同一次执行"),
        ("valid_unique_inventory", bool(inventory_ok), "留存 Docker 清单具有完整且不同的 ID 和名称"),
        ("exactly_one_candidate_worker", bool(unique), "名称、ID、镜像 ID 或标签必须恰好标识一个 Worker"),
        ("worker_identity", bool(unique) and worker.get("id") == expected.container_id
         and _name(worker.get("name")) == _name(expected.container_name)
         and worker.get("image_id") == expected.image_id and worker.get("image_tag") == expected.image_tag,
         "唯一观测 Worker 匹配全部可信预期身份字段；固定时间由调用方记录"),
        ("worker_exited", bool(unique) and state.get("Status") == "exited"
         and type(state.get("Pid")) is int and state["Pid"] == 0,
         "Docker 报告 Worker 已退出且无运行中 PID"),
        ("exit_codes_consistent", bool(unique) and all(type(code) is int
         and code == expected.expected_exit_code for code in exit_codes),
         "Docker、清单和宿主机退出码必须精确匹配预期退出码，包含整数类型"),
        ("worker_no_oom", bool(unique) and state.get("OOMKilled") is False,
         "Docker 必须明确报告 OOMKilled=false；任务成功不能覆盖 OOM 证据"),
        ("worker_inactive", bool(unique) and all(state.get(key) is False
         for key in ("Dead", "Running", "Paused", "Restarting")),
         "全部非活跃状态标志均明确为 false"),
        ("worker_no_docker_error", bool(unique) and state.get("Error") == "",
         "Docker 明确报告空 Error 字符串"),
        ("worker_resource_limits", bool(unique) and all(type(worker.get(key)) is int
         and worker[key] > 0 and (pin is None or worker[key] == pin)
         for key, pin in (("memory_limit", expected.memory_limit), ("cpu_nano", expected.cpu_nano))),
         "观测内存和 CPU 上限必须是正整数，并匹配所有已提供固定值"),
    ]
    return {"schema_version": 1, "audit_type": "candidate_docker_process_observation", **identity,
            "passed": all(passed for _, passed, _ in checks),
            "checks": [{"name": name, "passed": passed, "detail": detail} for name, passed, detail in checks],
            "expectation": asdict(expected),
            "observed_worker": {key: worker.get(key) for key in (
                "id", "name", "image_id", "image_tag", "state", "memory_limit", "cpu_nano")} if unique else None,
            "claim": "仅为留存的可信宿主机 Worker 进程证据；不对任务、恢复、资源效率或生产健康作结论"}


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 对象键")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON 数值不是有限数")
    return number


def read_evidence(path: Path) -> tuple[dict, dict]:
    """读取普通、非符号链接的 JSON 对象，并保留原始字节哈希。"""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("证据路径必须为绝对路径且不含路径穿越")
    if any(parent.is_symlink() for parent in (path, *path.parents)) or not path.is_file():
        raise ValueError("证据必须为普通文件，祖先目录不得含符号链接")
    if path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError("证据 JSON 超出大小限制")
    data = path.read_bytes()
    value = json.loads(data, object_pairs_hook=_object_pairs, parse_float=_finite_float,
                       parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("JSON 数值不是有限数")))
    if not isinstance(value, dict):
        raise ValueError("证据 JSON 必须是对象")
    return value, {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def audit_files(manifest_path: Path, host_path: Path, expectation_path: Path,
                original_verdict_path: Path | None = None) -> dict:
    """离线补充审计，不修改或重新解释旧判定。"""
    manifest, manifest_source = read_evidence(manifest_path)
    host, host_source = read_evidence(host_path)
    expected, expected_source = read_evidence(expectation_path)
    result = grade_process_observation(manifest, host, WorkerExpectation(**expected))
    result["sources"] = {"manifest": manifest_source, "host_observation": host_source,
                         "expectation": expected_source}
    if original_verdict_path is not None:
        _, source = read_evidence(original_verdict_path)
        result["sources"]["original_verdict_unmodified"] = source
    return result


def _status(checks: list[dict]) -> str:
    if any(check["passed"] is False for check in checks):
        return "failed"
    return "unknown" if any(check["passed"] is None for check in checks) else "passed"


def snapshot_health(worker: dict, host_exit: Any, expected_exit: int | None) -> dict:
    """快照字段缺失记为未知；明确的 OOM/故障证据判失败。

    宿主机退出码可来自同一可信清单，但这明确弱于独立身份固定证据。
    不能因 ID 未知而掩盖 OOM。"""
    state = worker.get("state") if isinstance(worker.get("state"), dict) else {}
    checks = []

    def check(name, keys, predicate):
        known = all(key in state for key in keys)
        checks.append({"name": name, "passed": bool(predicate()) if known else None})

    check("docker_exited", ["Status"], lambda: state["Status"] == "exited")
    check("docker_pid_zero", ["Pid"], lambda: type(state["Pid"]) is int and state["Pid"] == 0)
    check("docker_no_oom", ["OOMKilled"], lambda: state["OOMKilled"] is False)
    for key in ("Dead", "Running", "Paused", "Restarting"):
        check("docker_" + key.lower() + "_false", [key], lambda key=key: state[key] is False)
    check("docker_no_error", ["Error"], lambda: state["Error"] == "")
    known_exit = "ExitCode" in state and host_exit is not None and expected_exit is not None
    checks.append({"name": "docker_host_expected_exit", "passed": (
        type(state["ExitCode"]) is int and type(host_exit) is int
        and state["ExitCode"] == host_exit == expected_exit) if known_exit else None})
    return {"status": _status(checks), "checks": checks,
            "exit_source": "同一可信宿主机清单中的 candidate_exit_code"}


def audit_manifest(manifest_path: Path, *, expectation_path: Path | None = None,
                   host_path: Path | None = None) -> dict:
    """补充旧证据，不把未固定的身份提升为健康。

    完整宿主机快照可独立通过 snapshot_health。没有单独提供的 Worker
    身份固定证据时，独立验证仍为未知。原任务判定保持原样，包括伴随 OOM 的通过结果。"""
    manifest, source = read_evidence(manifest_path)
    candidate, run_id, case = (manifest.get(key) for key in ("candidate", "run_id", "case"))
    known_identity = (candidate in _CANDIDATES if isinstance(candidate, str) else False)
    known_identity = known_identity and _matches(_ID, run_id)
    service, prefix = _CANDIDATES[candidate] if known_identity else (None, None)
    names = {f"harness-lab-{prefix}-{run_id}", f"harness-lab-detailed-{prefix}-{run_id}"} if known_identity else set()
    tag = f"harness-lab-{service}:local" if known_identity else None
    inventory = manifest.get("containers")
    rows = inventory if isinstance(inventory, list) else []
    workers = [row for row in rows if isinstance(row, dict) and known_identity
               and (_name(row.get("name")) in names or row.get("image_tag") == tag)]
    expected_exit = expected_exit_code(candidate, case) if _matches(_ID, case) else None
    health = (snapshot_health(workers[0], manifest.get("candidate_exit_code"), expected_exit)
              if len(workers) == 1 else {"status": "failed" if len(workers) > 1 else "unknown", "checks": [
                  {"name": "unique_candidate_snapshot", "passed": False if len(workers) > 1 else None}]})
    result = {"schema_version": 1, "audit_type": "supplemental_candidate_process_audit",
              "run_id": run_id, "candidate": candidate, "case": case,
              "snapshot_health": health, "independent_identity_check": {"status": "unknown", "checks": []},
              "sources": {"manifest": source},
              "claim": "离线审计留存的可信宿主机快照，不替换原任务判定"}
    original_path = manifest_path.parent / "verdict.json"
    if original_path.exists() or original_path.is_symlink():
        _, original_source = read_evidence(original_path)
        result["sources"]["original_verdict_unmodified"] = original_source
    else:
        result["sources"]["original_verdict_unmodified"] = None
    if expectation_path is not None:
        expectation, pin_source = read_evidence(expectation_path)
        expected = WorkerExpectation(**expectation)
        if host_path is None:
            host = {key: manifest.get(key) for key in ("run_id", "candidate", "case", "candidate_exit_code")}
        else:
            host, host_source = read_evidence(host_path)
            result["sources"]["host_observation"] = host_source
        strict = grade_process_observation(manifest, host, expected)
        result["sources"]["expectation"] = pin_source
        result["independent_identity_check"] = {"status": "passed" if strict["passed"] else "failed",
                                                  "checks": strict["checks"], "expectation": asdict(expected),
                                                  "host_exit_source": "单独的宿主机观测" if host_path else "同一宿主机清单"}
    # 即使独立身份未知，明确的快照故障也优先判失败。
    statuses = (health["status"], result["independent_identity_check"]["status"])
    result["status"] = "failed" if "failed" in statuses else "passed" if all(x == "passed" for x in statuses) else "unknown"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--run-dir", type=Path, action="append", default=[])
    parser.add_argument("--host-observation", type=Path)
    parser.add_argument("--expectation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    from runner.bench import environment
    info = environment()  # 真实验收仅在 Docker 内运行。
    if info["model_credentials_injected"]:
        raise RuntimeError("离线进程审计不得接收模型凭证")
    manifests = args.manifest + [path / "manifest.json" for path in args.run_dir]
    if not manifests or len(set(manifests)) != len(manifests):
        raise ValueError("提供不同且明确的清单路径或运行目录；不扫描数据根目录")
    if len(manifests) != 1 and (args.expectation is not None or args.host_observation is not None):
        raise ValueError("每次调用的独立身份固定值只适用于一次运行")
    if args.host_observation is not None and args.expectation is None:
        raise ValueError("单独宿主机观测必须提供明确 Worker 身份固定值")
    if args.output.name != "process-audit.json" or not args.output.is_absolute():
        raise ValueError("使用新的补充 process-audit.json 输出")
    if ".." in args.output.parts or any(parent.is_symlink() for parent in args.output.parents):
        raise ValueError("审计输出不能穿越符号链接目录")
    runs = [audit_manifest(path, expectation_path=args.expectation, host_path=args.host_observation) for path in manifests]
    result = {"schema_version": 1, "audit_type": "supplemental_candidate_process_audit_batch",
              "runs": runs, "environment": info,
              "passed": all(run["status"] == "passed" for run in runs)}
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    for run in runs:
        print(json.dumps({"run_id": run["run_id"], "status": run["status"],
                          "snapshot_health": run["snapshot_health"]["status"],
                          "independent_identity_check": run["independent_identity_check"]["status"],
                          "failed_checks": sorted({check["name"] for part in ("snapshot_health", "independent_identity_check")
                                                   for check in run[part]["checks"] if check["passed"] is False})}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
