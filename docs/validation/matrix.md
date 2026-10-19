# P0 候选能力矩阵（2026-10-19）

本矩阵只记录已经完成的最小本地验证，不替代 T3-T6 生命周期、透明度和成本专项。
“通过”表示有运行证据；“未验证”表示尚未形成同口径证据；“阻断”表示候选当前
能力或入口明确无法满足该任务。

| 能力 | DeepSeek Harness | OpenClaw | Deep Agents + LangGraph |
| --- | --- | --- | --- |
| 开源来源与许可证 | MIT，pinned commit，已核验 | MIT，pinned commit，已核验 | MIT，两个来源和 archive hash 已核验 |
| 依赖/运行环境 | Node 24.22 + pnpm 11.7，CLI 通过 | Node 24.22 + pnpm 12.11，安装和 build 通过 | Python 3.12，Deep Agents 0.7.28 + LangGraph 1.2.16 |
| 远程模型 API | 通过，`API_OK` | 通过，`OPENCLAW_API_OK` | 通过，返回 `fixture-alpha` |
| 确定性工具 | T1 通过，Fixture GET/POST 和幂等副作用有证据 | 统一 T1 适配器尚未接入 | `fixture_lookup` 通过，事件可见 |
| 统一 Playwright 浏览器 | Execution 基线通过 | 尚未接入统一适配器 | 尚未验证 |
| 候选原生浏览器 | headless 明确阻断 | runtime 暴露 deferred browser 工具，尚未形成 T2 运行证据 | 尚未验证 |
| 事件/游标归一化 | 适配器已实现，T1/T2 artifact 已留存 | Gateway/Agent 事件映射待实现 | JSONL worker 事件和游标已实现 |
| SQLite/checkpoint | 候选 headless 入口未提供 | 原生能力待验证 | SQLite checkpointer 已产生状态文件 |
| 暂停/恢复/取消/审批 | 适配器入口明确未支持 | 尚未验证 | one-shot worker 明确未支持实时生命周期 |
| 多租户边界 | 需外围 Runner/Execution 实现 | Gateway/agent scope 需适配到租户后端 | 需外围 Runner/持久化/隔离层实现 |
| 适配/自研成本 | 已有薄适配器；SDK/ACP 接入待评估 | Gateway 可复用，统一协议适配尚未估算 | 薄 worker/适配器已实现；可恢复 worker、浏览器桥接待补齐 |

证据索引：

- [DeepSeek Harness 报告](reports/2026-10-19-deepseek-harness.md)
- [OpenClaw 报告](reports/2026-10-19-openclaw.md)
- [Deep Agents + LangGraph 报告](reports/2026-10-19-deepagents-langgraph.md)

当前可以形成阶段性推荐，但不能据此做生产最终选型。下一阶段按统一 T3-T6
验证暂停恢复、Runner/容器重启、人工介入、失败重试、新增工具和维护成本；每项
结论继续引用外部 `run_id`。正式推荐与阻断条件见
[P0 调研结论](decisions/2026-10-19-p0-conclusion.md)。
