# 验证证据留存

本目录保存跨候选的验证规则、指标矩阵和脱敏报告。一次运行使用唯一
`run_id`，运行元数据写入 `manifest.json`，标准化事件追加到 `events.jsonl`，
checkpoint 和产物分别放入 `state/`、`artifacts/`。

当前运行证据根目录由 `HARNESS_LAB_ARTIFACT_ROOT` 指定，默认使用仓库外的
`var/artifacts`；真实 API key、Cookie、Authorization header、业务数据、原始大日志
和截图不得提交到 Git。报告只引用 `run_id` 及外部证据路径或哈希。

运行状态使用 `planned`、`running`、`blocked`、`completed`、`aborted`。任何候选版本、
任务定义或配置变化都创建新的 `run_id`，不覆盖历史记录。报告应分开写出事实证据、
推断、未知项、框架原生缺口和适配层补偿。

统一留存流程和目录约定见 [LOCAL_VALIDATION_PLAN.md](../../LOCAL_VALIDATION_PLAN.md)。
