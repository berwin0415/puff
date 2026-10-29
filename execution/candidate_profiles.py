"""候选运行 profile：把 worker control 里散落的候选常量收成静态描述。

本模块只描述“某个候选在当前实验入口里怎么启动、环境是什么、状态在哪里”，
不启动容器、不读取任务材料、不改变现有 worker 行为。M1 先用它消除 DA/DSH
两套入口的隐式差异；后续 worker control 再按 profile 分派。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from execution.candidate_constants import (
    DA_ARGV,
    DA_CANCEL_SEMANTICS,
    DA_CWD,
    DA_ENVIRONMENT,
    DA_MODEL_TOKEN_FILE,
    DA_PAUSE_SEMANTICS,
    DA_WORKER_MODULE,
    DSH_ARGV,
    DSH_CWD,
    DSH_ENVIRONMENT,
    DSH_SEMANTICS,
    DSH_TOKEN,
    DSH_WORKER_MODULE,
    MODEL_BASE_URL,
)


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    supported: bool
    argv: tuple[str, ...]
    cwd: str | None
    environment: frozenset[str]
    config_dir: Path
    state_dir: Path
    workspace: Path
    evidence_dir: Path
    model_base_url: str
    model_token_file: str
    pause_semantics: str
    cancel_semantics: str
    worker_module: str
    note: str = ""


PROFILES = {
    "deepagents_langgraph": CandidateProfile(
        name="deepagents_langgraph",
        supported=True,
        argv=tuple(DA_ARGV),
        cwd=DA_CWD,
        environment=DA_ENVIRONMENT,
        config_dir=Path("/config"),
        state_dir=Path("/state"),
        workspace=Path("/workspace"),
        evidence_dir=Path("/evidence"),
        model_base_url=MODEL_BASE_URL,
        model_token_file=DA_MODEL_TOKEN_FILE,
        pause_semantics=DA_PAUSE_SEMANTICS,
        cancel_semantics=DA_CANCEL_SEMANTICS,
        worker_module=DA_WORKER_MODULE,
    ),
    "deepseek_harness": CandidateProfile(
        name="deepseek_harness",
        supported=True,
        argv=tuple(DSH_ARGV),
        cwd=DSH_CWD,
        environment=DSH_ENVIRONMENT,
        config_dir=Path("/config"),
        state_dir=Path("/state"),
        workspace=Path("/workspace"),
        evidence_dir=Path("/evidence"),
        model_base_url=MODEL_BASE_URL,
        model_token_file=DSH_TOKEN,
        pause_semantics=DSH_SEMANTICS["pause"],
        cancel_semantics=DSH_SEMANTICS["cancel"],
        worker_module=DSH_WORKER_MODULE,
    ),
    "openclaw": CandidateProfile(
        name="openclaw",
        supported=False,
        argv=(),
        cwd=None,
        environment=frozenset(),
        config_dir=Path("/config"),
        state_dir=Path("/state"),
        workspace=Path("/workspace"),
        evidence_dir=Path("/evidence"),
        model_base_url=MODEL_BASE_URL,
        model_token_file="/config/model-run.token",
        pause_semantics="gateway_http_transport_disconnect",
        cancel_semantics="gateway_http_transport_disconnect",
        worker_module="",
        note="Gateway worker profile not implemented; I24 只验证过 DA/DSH。",
    ),
}


def get_profile(candidate: str) -> CandidateProfile:
    try:
        return PROFILES[candidate]
    except KeyError:
        raise KeyError("unknown candidate profile: " + str(candidate)) from None


def profile_names() -> tuple[str, ...]:
    return tuple(sorted(PROFILES))


def main() -> None:
    print(json.dumps(
        {name: {
            "supported": profile.supported,
            "argv": list(profile.argv),
            "cwd": profile.cwd,
            "environment": sorted(profile.environment),
            "model_base_url": profile.model_base_url,
            "model_token_file": profile.model_token_file,
            "pause_semantics": profile.pause_semantics,
            "cancel_semantics": profile.cancel_semantics,
            "worker_module": profile.worker_module,
            "note": profile.note,
        } for name, profile in sorted(PROFILES.items())},
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
