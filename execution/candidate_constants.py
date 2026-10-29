"""候选启动常量：不依赖 worker/profile，避免循环导入，作为唯一值来源。"""

from __future__ import annotations


MODEL_BASE_URL = "http://model-proxy:8082/v1"

DA_CANDIDATE = "deepagents_langgraph"
DA_ARGV = (
    "/opt/venv/bin/python",
    "/opt/materials/harness-lab/entrypoint.py",
    "--request",
    "/config/request.json",
)
DA_CWD = "/workspace"
DA_ENVIRONMENT = frozenset({
    "MODEL_NAME",
    "MODEL_API_BASE_URL",
    "MODEL_RUN_TOKEN_FILE",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING_V2",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
})
DA_MODEL_TOKEN_FILE = "/config/model-run.token"
DA_PAUSE_SEMANTICS = "cooperative_drain_at_superstep_boundary"
DA_CANCEL_SEMANTICS = "drain_stop_no_new_dispatch_not_native_abort"
DA_WORKER_MODULE = "execution.worker_control"

DSH_CANDIDATE = "deepseek_harness"
DSH_ARGV = (
    "/usr/local/bin/node",
    "--import",
    "tsx/esm",
    "/opt/materials/harness-lab/goal-runtime-entrypoint.ts",
    "--request",
    "/config/request.json",
)
DSH_CWD = "/opt/materials/sources/main"
DSH_TOKEN = "/budget/run.token"
DSH_ENVIRONMENT = frozenset({
    "MODEL_API_BASE_URL",
    "MODEL_NAME",
    "MODEL_RUN_TOKEN_FILE",
    "HARNESS_LIFECYCLE_IMAGE_ID",
    "TSX_TSCONFIG_PATH",
    "DSH_TELEMETRY_DISABLED",
})
DSH_SEMANTICS = {
    "pause": "native_goal_pause_at_empty_step_boundary",
    "cancel": "public_Agent_cancel_immediate_terminal_requires_native_evidence",
}
DSH_WORKER_MODULE = "execution.worker_control_dsh"
