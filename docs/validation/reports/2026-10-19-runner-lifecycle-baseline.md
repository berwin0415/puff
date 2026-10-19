# 统一 Runner 生命周期与故障恢复基线

日期：2026-10-19

本报告记录本仓库统一 Runner 控制面的 T3-T6 场景建设、T4/T5 运行证据，以及
云 Chromium/CDP 冒烟问题的修复。报告只覆盖 Runner/协议层，不代表任何候选框架
已经具备原生暂停恢复能力。

## 本轮完成内容

### T3-T6 任务与场景

- 新增 `configs/tasks/t3-approval.yaml`、`t4-pause-resume.yaml`、
  `t5-fault-recovery.yaml`、`t6-extension.yaml`，补齐 T1-T6 的统一任务定义。
- 新增对应的 `fixtures/scenarios/t3_approval.json`、`t4_pause_resume.json`、
  `t5_fault_recovery.json`、`t6_extension.json`，并为 T1/T2 场景补充
  `config_task_id` 和可执行步骤，使六个任务使用同一套装载入口
  `runner/scenarios.py`。
- `t6-extension` 的度量项固定为 `changed_files`、`first_run_seconds`、
  `event_coverage`、`debug_path`，仍需要候选侧实际扩展后人工填写。

### Runner 级生命周期与故障测试

- `tests/scenarios/test_lifecycle_scenarios.py`：校验 T1-T6 配置/场景完整性，
  覆盖 T3 的 `send_input`/`approve`、T4 的 pause → Runner 重建 → resume、
  事件序号连续性和 manifest/checkpoint 产物。
- `tests/fault_injection/test_runner_recovery.py`：覆盖 fail-once 的 503→200、
  Fixture 重启后的幂等重放、Runner 重建后的 checkpoint 恢复、重复提交的独立
  run_id。
- `scripts/run_lifecycle_scenario.py`：可重复执行 T3/T4/T5，并把恢复指标写入
  `runs/<run_id>/metrics.json`。

### Deep Agents checkpoint 离线检查

- `adapters/deepagents_langgraph/worker.py` 新增 `--mode inspect`：只读取
  LangGraph SQLite checkpointer，不调用模型、不读取 API key，返回
  `checkpoint_id`、checkpoint 数量、元数据和裁剪后的状态摘要。
- `DeepAgentsLangGraphAdapter` 新增 `inspect_checkpoint()` 和 `snapshot()`；
  pause/resume/cancel 仍明确抛出 `NotImplementedError`，因为当前 Worker 仍是
  one-shot，不能用 snapshot 冒充任务恢复。
- `tests/adapters/test_deepagents_snapshot.py` 使用外部 Deep Agents venv 创建
  一个不依赖模型的 LangGraph checkpoint，再通过 adapter 读取；外部环境缺失时
  自动跳过。

### CDP 浏览器冒烟修复

原失败现象：`persisted_after_restart=False`。

根因：对 CDP 连接的浏览器调用 Playwright `Browser.close()` 只断开客户端连接，
不会关闭 Chromium 进程；随后立即 SIGTERM 会使 Chromium 尚未提交的
localStorage/profile 写入丢失。

修复：通过 CDP 会话发送 `Browser.close`，等待进程优雅退出（必要时再
SIGTERM），再启动第二个 Chromium 进程验证 profile 持久化。修复后
`tests/execution/test_cdp_browser.py` 通过。

## 运行证据

2026-10-19 使用本地 Fixture 服务执行：

| 任务 | run_id | 状态 | 恢复结果 |
| --- | --- | --- | --- |
| T3 审批/追加输入 | `800fd5d5-6831-4ec4-9e7c-45cbeb614b52` | completed | 输入和审批事件均入同一 run |
| T4 暂停/重建/恢复 | `c537d77a-511d-4fc2-99d3-e96add6808e2` | completed | `resume_after_restart=true`，重复副作用 0 |
| T5 故障/重试 | `50c58428-65c7-4005-83ee-f64ab9110130` | completed | fail-once 503→200，副作用 201→replayed，重复副作用 0 |

T4/T5 的 `recovery_latency_seconds` 约为 0.004 秒，仅代表本地 Runner 重建耗时，
不能外推为候选框架的恢复时延。原始 `manifest.json`、`events.jsonl`、
`metrics.json` 位于仓库外 `~/harness-lab-data/runs/runs/<run_id>/`。

全量测试：`39 passed`（包含 CDP、Fixture、Runner、场景和 adapter 测试）。

## 仍未验证

- 候选原生 pause/resume/cancel/approval：三个 P0 候选都还没有长连接或可恢复
  Worker 的同口径证据。
- Deep Agents 的真实 resume：当前只验证了 checkpoint 可读；需要在线 Worker
  在模型等待或工具调用安全点暂停，并在新进程中用同一 `thread_id` 继续。
- OpenClaw 统一 T1/T2 适配器、统一 Playwright 接入。
- 云 Chromium 上 `@playwright/mcp` 的端到端验证；当前 CDP 冒烟只验证了
  Chromium + CDP + profile 持久化，不含 Playwright MCP 工具层。
- T6 的实际改动量、首次可运行时间和维护成本。
