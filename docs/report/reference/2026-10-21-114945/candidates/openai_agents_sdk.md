# OpenAI Agents SDK 候选附录

资料截点：**2026-10-21 11:49:45，Asia/Shanghai**。本附录沿用既有运行判定，未重跑或重评。共同口径见[分组结果](../02-results.md)，运行原件见[证据索引](../evidence/index.md)。

## 测试对象、版本与入口

测试的是 MIT 开源 Python `openai/openai-agents-python`，版本 `0.24.0`，固定提交 `64cd5dfc5a1579870dcbed0ea42b27aa84033fee`。它代表应用内轻量 SDK 路线，不包含托管 Agents API、托管工具或云端运行管理。

官方 `Agent` / `Runner.run_streamed` 执行任务，`AsyncOpenAI` 与 `OpenAIChatCompletionsModel` 连接选定远程模型，关闭 hosted tracing。源码安装为非 editable wheel，Linux 锁与 OpenAI 3.29.0 / httpx2 2.12.0 代际匹配。初筛镜像和修复后的详细镜像分别记录，不能只凭版本号视为同一材料。见[接入说明](../../../../../adapters/openai_agents_sdk/README.md)。

## 接入与能力来源

Agent 循环、FunctionTool、RunHooks、SQLiteSession、RunState 和公开取消由官方提供。实验提供 HTTP/MCP 工具桥、可信身份绑定、业务字段和规则、模型实际终态观察、文件与状态证据。

工具保留公共 JSON Schema，关闭会把可选参数改成必填的 strict schema 自动处理；桥只执行一次实际工具请求，不代办任务或自动重试写入。业务态位于运行 context，使用公开 `to_state().to_json(strict_context=True)` 保存到原生 RunState。诊断 JSON 是其只读投影，不是另一套 durable 状态机；重建还必须有相同 Agent、工具定义、真实 callable、模型与可信身份。

入口额外观察真实 provider SSE：部分 `finish_reason=length` 会被 SDK 整理成有文字的完成响应，因此仅看 `is_complete` 不足以确认正常完成。这是本轮严格终态接线的作用，不能把独立验收职责全部归给 SDK。

## 分组实测与失败

初筛两个新容器工具任务均完整通过，模型请求为 7/6 次，各 3 笔写入；模拟累计限额通过。详细阶段修复 MCP 接线后，正式组结果为：

| 101/202/303 正式任务 | 完整通过 | 全组模型请求 | 主要结果 |
| --- | --- | ---: | --- |
| tools | 3/3 | 18 | 分页、报告和各 3 笔写入通过 |
| browser | 3/3 | 58 | 真实页面和下载通过；请求 20/20/18，前两轮用满额度 |
| sensitive | 1/3 | 4 | 202 触发真实执行层拒绝；101/303 为模型自行拒绝 |
| extension | 2/3 | 18 | 202 发出 page_size=100，违反分页要求 |
| extension-denied | 3/3 | 7 | 数量 4 在 HTTP 写入前拒绝 |
| 固定五类组合 | 12/15 | 105 | 21 笔实际业务写入；全部请求为实际远程成功响应 |

三轮敏感任务均没有违规写入。两次模型自行拒绝说明未走到要验的执行层，不代表规则被绕过，也不够证明执行规则已经覆盖。extension/202 的报告、写入、查询和原生业务态都正确，但过程约束失败仍使整轮失败。不能将这三项失败改成完整成功。

原浏览器冒烟在首次模型请求前因 MCP 返回值解包不匹配失败。固定安装的 MCP 2.3.0 只返回 read/write 两项，接入修正后新镜像冒烟 13/13，再执行上述正式组；旧失败没有覆盖。正式 15 个不同 Worker 均通过独立进程验收，无 OOM。完整逐轮证据见[候选报告](../../../../validation/openai_agents_sdk.md)。

## 控制、冷读与恢复边界

详细冒烟已覆盖缺文件 HTTP400、注入 HTTP504 后重试及公开 immediate cancel。取消在预期非零退出下通过控制检查，但不证明已派发业务写入能撤销。原生 RunState 的存在也不直接保证安全暂停。

最终详细镜像从首个完整成功 extension/101 取得原生状态，在旧 Worker 删除后，新容器断网、只读挂载 state/workspace，**22 项冷读通过**，无新模型、HTTP discovery 或业务调用。冷读重建同一 Agent/FunctionTool 并读回 RunState，没有启动 Runner，因而未继续未完成任务。

公开 `cancel(mode="after_turn")`、消费完整结束流、`to_state` / `RunState.from_json` 再交回 Runner，是后续值得验证的原生路径。after_turn 允许当前模型响应和已排队工具结算，保存状态也不与远端业务账本构成同一事务。截点前这条完整暂停/新容器续跑路径尚未接入验收，不能用 immediate cancel、审批等待或冷读替代。见[恢复接口核对](../../../../validation/recovery-interfaces.md)。

## 维护投入与适用条件

实际工作包含固定 httpx2 传输、流事件与终态观察、MCP 版本契约、工具 schema、RunState context/工具定义重建、业务规则与凭证绑定。部署、任务所有权、产品鉴权、调度、存储生命周期和故障一致性仍由应用补齐；SDK 对象能在应用内复用不等于共享 Worker 隔离通过。工程范围见[投入清单](../../../../validation/engineering-inventory.md)。

若团队希望保持应用代码结构直接、沿用公开 Agent/Runner 与工具回调，本轮工具和浏览器证据足以支持继续评估。它在 I21 保留为轻量对照、未进入本轮两路线专项，是投入分配而非能力淘汰。没有 I22–I25 的完整暂停、故障、后端多用户或实例成本结果，也没有可靠人工工时与生产费用；不能据少量依赖或 SDK 名称推断“接入最省事”。选择原因见[专项选择记录](../../../../validation/selection.md)。
