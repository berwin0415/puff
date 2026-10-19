# P0 候选能力矩阵（2026-10-19）

本矩阵只记录已经完成的最小本地验证，不替代完整 T3-T6 生命周期、透明度和成本专项。
“通过”表示有运行证据；“未验证”表示尚未形成同口径证据；“阻断”表示候选当前
能力或入口明确无法满足该任务。

| 能力 | DeepSeek Harness | OpenClaw | Deep Agents + LangGraph |
| --- | --- | --- | --- |
| 开源来源与许可证 | MIT，pinned commit，已核验 | MIT，pinned commit，已核验 | MIT，两个来源和 archive hash 已核验 |
| 依赖/运行环境 | Node 24.22 + pnpm 11.7，CLI 通过 | Node 24.22 + pnpm 12.11，安装和 build 通过 | Python 3.12，Deep Agents 0.7.28 + LangGraph 1.2.16 |
| 远程模型 API | 通过，`API_OK` | 通过，`OPENCLAW_API_OK` | 通过，返回 `fixture-alpha` |
| 确定性工具 | T1 通过，Fixture GET/POST 和幂等副作用有证据 | 统一 T1 adapter 已接入；`agent exec` 调用 curl 返回 `60` | `fixture_lookup` 通过，事件可见 |
| 统一 Playwright 浏览器 | Execution 基线通过 | T2 被阻断：浏览器依赖扩展 relay，240s 超时 | 统一 Playwright 轨道尚未接入 |
| 候选原生浏览器 | `headless` 直连无 browser Connection；hsi-desktop MCP/CDP 轨道已迁移验证 | deferred browser 工具依赖扩展 relay；无头环境不可用 | 未验证 |
| 云 Chromium + Playwright MCP | 不适用（独立基础设施验证） | 不适用 | 不适用 |
| 事件/游标归一化 | adapter 已实现，T1/T2 artifact 已留存 | `agent exec --json` envelope 已映射为统一事件 | JSONL worker 事件和游标已实现 |
| SQLite/checkpoint | 候选 headless 入口未提供 | CLI 会话状态存在，checkpoint/resume 未验证 | SQLite checkpointer + adapter 离线 snapshot 已通过 |
| 暂停/恢复/取消/审批 | 适配器入口明确未支持 | one-shot CLI，未验证 | 非审批模型安全点 pause（`interrupt_after=["model"]`）+ 跨进程 resume 已通过；审批中断为可选能力；即时抢占、cancel、live input 未验证 |
| 多租户边界 | 需外围 Runner/Execution 实现 | Gateway/agent scope 需适配到租户后端 | 需外围 Runner/持久化/隔离层实现 |
| 适配/自研成本 | 已有薄适配器；SDK/ACP 接入待评估 | `agent exec` adapter 已落地；Gateway 控制平面适配待评估 | 薄 worker/adapter 已实现；统一浏览器桥接待补齐 |

证据索引：

- [DeepSeek Harness 报告](reports/2026-10-19-deepseek-harness.md)
- [hsi-desktop 浏览器迁移评估](reports/2026-10-19-hsi-desktop-browser-migration.md)
- [OpenClaw 报告](reports/2026-10-19-openclaw.md)
- [Deep Agents + LangGraph 报告](reports/2026-10-19-deepagents-langgraph.md)
- [统一 Runner 生命周期基线](reports/2026-10-19-runner-lifecycle-baseline.md)
- [中断恢复、OpenClaw 适配器与云浏览器验证](reports/2026-10-19-interrupt-resume-and-cloud-browser.md)

## 统一基础设施证据

以下证据验证本仓库的 Runner/协议和共享执行环境，不是候选框架的原生能力：

- `tests/scenarios/` 覆盖 T3 输入/审批、T4 Runner 重建后恢复、T6 扩展度量定义。
- `tests/fault_injection/` 覆盖 fail-once 重试、Fixture 重启后的幂等重放、Runner 重建后事件连续性和 checkpoint 恢复。
- `scripts/run_lifecycle_scenario.py` 生成 `pause_point`、`checkpoint_id`、
  `resume_after_restart`、`duplicate_side_effects`、`recovery_latency_seconds` 和
  `recovery_failure_reason`，写入运行目录的 `metrics.json`。
- `execution/browser/mcp_cdp_smoke.py` 验证无 Electron 的 Chromium + `@playwright/mcp`
  轨道：导航、表单、snapshot、双 tab + `window.open`、截图和重启后 localStorage 恢复。

2026-10-19 的关键 run_id：

| 验证 | run_id / 证据 |
| --- | --- |
| Runner T4 恢复 | `c537d77a-511d-4fc2-99d3-e96add6808e2` |
| Runner T5 故障恢复 | `50c58428-65c7-4005-83ee-f64ab9110130` |
| Deep Agents 非审批暂停恢复 | `deepagents-pause-d25e50a2-caec-4826-9ef3-6a566e6c93cb` |
| Deep Agents 审批中断（可选） | `deepagents-interrupt-8103c501-e33e-44f2-b5b1-e324f1f26628` |
| OpenClaw API 冒烟 | `98ccbdf5-1ea4-42fc-acc3-82cf32384ea5` |
| OpenClaw T1 工具 | `3727278b-24f0-43e9-bcf6-0bf9f0f20731` |
| Playwright MCP + Chromium | `~/harness-lab-data/runs/2026-10-19/browser-mcp/chromium-cdp/` |

## 结论边界

当前可以形成阶段性推荐，但不能据此做生产最终选型。Runner 级通过不能替代候选原生
能力证据；云 Chromium + MCP 通过也不代表候选框架的浏览器任务效果。正式推荐与阻断
条件见 [P0 调研结论](decisions/2026-10-19-p0-conclusion.md)。
