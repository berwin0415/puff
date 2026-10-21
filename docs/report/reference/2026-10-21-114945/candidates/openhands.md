# OpenHands 候选附录

资料截点：**2026-10-21 11:49:45，Asia/Shanghai**。本候选已完成 I15 初筛，后续详细与专项未执行。比较口径见[分组结果](../02-results.md)，运行原件见[证据索引](../evidence/index.md)。

## 测试对象、版本与入口

实际后台是 MIT 开源 `OpenHands/software-agent-sdk`，版本 `1.56.0`，固定提交 `33d8aa29c3078d074799e2fbf5e847aeb2d71360`。本轮仅安装核心 SDK，通过 `LLM → Agent → Conversation.arun()` 和 local workspace 运行。

`OpenHands/OpenHands` 的 Agent Canvas `1.28.1` / commit `8d39305538c1d17a30cbe477e57a0e18fcee7d95` 仅作独立来源固定，没有安装或执行；Agent Server、内置 terminal/browser、远程 Workspace 与托管/企业运维也不计入本入口。125 条 Linux 依赖来自官方锁闭包，不表示启动了同等数量的服务。详情见[接入说明](../../../../../adapters/openhands/README.md)。

## 接入与能力来源

循环、Conversation 状态、动作/observation 和 FinishTool 来自官方。八个公共工具通过官方 ClientTool 与真实 ToolExecutor 接入，模型自行决定步骤；桥保留原始 schema，仅执行实际 HTTP 调用，不用空执行器把“派发”当成成功。

NeverConfirm 使普通已授权业务自动执行，敏感拒绝由公共执行服务提供；初筛没有专门验证敏感拒绝，不能记成 OpenHands 自带规则通过。可信身份、容器编排和独立结果核对属于本仓库接入。

## 分组实测与失败

同镜像、两个新容器的 seed101 工具任务均 **12/12 完整通过**；模型请求 6/7 次，各有 3 次写入请求及 3 笔写入。原生 FINISHED、无 pending action，末次模型为 tool_calls 后真实 FinishTool 成功 observation；没有伪造 finish_reason=stop。外部判定另核对分页、报告和账本。

模拟累计限额 18 项通过。固定 SDK 的 `num_retries` 表示总尝试数，接线按统一重试语义映射，底层传输不叠加重试。早期修正了 SDK 添加 risk/summary schema 与公共工具 schema 不一致的问题，以及继承类自动命名使八个工具都叫 http 的错误；后者发生在 0 模型请求阶段。失败和构建版本均保留，不能归成 Agent 完不成业务。详见[初筛报告](../../../../validation/openhands.md)。

## 维护投入、适用条件与未知项

实际接入维护核心依赖闭包、源码 wheel、ClientTool schema/命名、完成与暂停/异常区分、原生 Conversation 状态及 HTTP 副作用未知返回。模型价格表不能可靠估算当前模型，金额和人工工时保持未知。

该实验表明核心 SDK 可以独立于 Canvas/Agent Server 完成共同业务工具任务。若后续需要 REST/WebSocket 服务控制或远程 Workspace，应另测对应变体并计入常驻与权限成本。公开 pause/persistence 是有价值的后续入口，但当前入口拒绝已有 state，未实测暂停恢复、浏览器、多用户、故障一致性或实例收益。不能因 Coding 定位或依赖条目多就断言不适合通用任务、内存更大；暂缓仅是投入安排，见[初筛安排](../../../../validation/screening.md)。
