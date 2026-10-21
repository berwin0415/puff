# Pi Agent Core 候选附录

资料截点：**2026-10-21 11:49:45，Asia/Shanghai**。本候选停留在已完成的 I11 初筛，未进入同题详细与后续专项。比较口径见[分组结果](../02-results.md)，原件见[证据索引](../evidence/index.md)。

## 测试对象、版本与入口

对象是 MIT 开源 `earendil-works/pi`，固定提交 `73f8de80bc87021375f8b525292ac07492253002`；实际使用 `@earendil-works/pi-agent-core`、pi-ai 与 pi-telemetry `1.3.0`。仓库与包名已经变化，本轮不按旧 `badlogic/pi-mono` / `@mariozechner` 名称假设安装内容。Pi Coding Agent、Pi Durable 是不同上层变体，未计入 Core 的能力。

入口直接调用进程内 `Agent.prompt/subscribe`。由于源码归档缺少生成后的模型目录，本轮从公开 Agent 与官方 `openai-completions.streamSimple` 编译 source bundle，使用固定模型而不做模型自动发现；上游 Agent 循环、工具参数校验和响应处理没有修改。详情见[接入说明](../../../../../adapters/pi_agent_core/README.md)。

## 接入与能力来源

Agent 循环、工具执行和内存 state/messages 来自 Pi。HTTP 工具桥、身份绑定、容器运行与独立证据由本仓库提供，桥不预写业务步骤。`observed-state.json` 和状态快照仅是应用对内存的观察，不是官方 durable checkpoint；本入口发现已有观察状态即拒绝新任务。

## 分组实测与失败

| 初筛运行 | 模型请求 | 实际写入/请求 | 既有结果 |
| --- | ---: | --- | --- |
| tools-001，seed101 | 6 | 3/3 | 12/12，完整通过 |
| tools-002，seed101 | 5 | 3/3 | 11/12，整体失败 |

第二轮一次调用 `list_items(page=1,page_size=100)`，公共 schema 虽允许该参数，实验任务要求每页最多 5 条，因此过程检查失败。报告和三笔写入均正确，原生也正常结束，但不能以此覆盖失败。两次是不同新容器、空状态下重做同一种子任务，不是同一任务恢复，也不足以认定 Pi 整体可靠性差。模拟重试及跨容器累计限额 18 项通过。详见[初筛报告](../../../../validation/pi_agent_core.md)。

## 维护投入、适用条件与未知项

实际接入处理了源码编译、TypeBox 和各 workspace 的依赖解析、bundle 位置、原生事件与严格终态。早期构建失败属于实验编译接线，不是模型业务失败。人工工时未可靠记录，不能由运行秒数推算。

该对象适合继续评估“把最小 Agent 循环嵌入自有应用”的路线；持久状态、工具/凭证隔离、产品控制和部署仍需应用承担，或另测官方 durable/coding-agent 包。浏览器、取消暂停、换 Worker 继续、故障一致性、多用户与实例成本均未实测。一个 Agent 是进程内对象，不意味着每用户必须常驻一个容器，也不能据此证明共享安全。暂缓原因是本轮投入集中，不是判定这些能力不支持，见[初筛安排](../../../../validation/screening.md)。
