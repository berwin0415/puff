# 验证证据留存

本目录保存跨候选的验证规则、指标矩阵和脱敏报告。一次运行使用唯一
`run_id`，运行元数据写入 `manifest.json`，标准化事件追加到 `events.jsonl`，
checkpoint 和产物分别放入 `state/`、`artifacts/`。

当前运行证据根目录由 `HARNESS_LAB_ARTIFACT_ROOT` 指定，默认使用仓库外的
`~/harness-lab-data/runs`；真实 API key、Cookie、Authorization header、业务数据、原始大日志
和截图不得提交到 Git。报告只引用 `run_id` 及外部证据路径或哈希。

运行状态使用 `planned`、`running`、`blocked`、`completed`、`aborted`。任何候选版本、
任务定义或配置变化都创建新的 `run_id`，不覆盖历史记录。报告应分开写出事实证据、
推断、未知项、框架原生缺口和适配层补偿。

统一留存流程和目录约定见 [LOCAL_VALIDATION_PLAN.md](../../LOCAL_VALIDATION_PLAN.md)。

当前候选报告：

- [DeepSeek Harness](reports/2026-10-19-deepseek-harness.md)
- [hsi-desktop 浏览器迁移评估](reports/2026-10-19-hsi-desktop-browser-migration.md)
- [OpenClaw](reports/2026-10-19-openclaw.md)
- [Deep Agents + LangGraph](reports/2026-10-19-deepagents-langgraph.md)
- [统一 Runner 生命周期与故障恢复基线](reports/2026-10-19-runner-lifecycle-baseline.md)
- [中断恢复、OpenClaw 适配器与云浏览器验证](reports/2026-10-19-interrupt-resume-and-cloud-browser.md)
- [Deep Agents T2、进程级恢复与 T6 扩展成本](reports/2026-10-19-deepagents-t2-t6.md)

阶段性结论：[P0 云端 Harness 调研结论与推荐](decisions/2026-10-19-p0-conclusion.md)。

P0 收口：[P0 云端 Harness 调研收口](decisions/2026-10-19-p0-closeout.md)。
