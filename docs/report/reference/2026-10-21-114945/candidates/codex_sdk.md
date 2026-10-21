# Codex SDK 候选附录

资料截点：**2026-10-21 11:49:45，Asia/Shanghai**。本候选已完成 I14 初筛，未进入同题详细与后续专项。比较口径见[分组结果](../02-results.md)，运行原件见[证据索引](../evidence/index.md)。

## 测试对象、版本与入口

对象为 Apache-2.0 开源 `openai/codex` 的 TypeScript SDK 与本地 CLI，固定 tag `rust-v0.163.0` / commit `83086188ae0614bd882818ff1aa23f3194962012`。SDK、CLI 和 Linux arm64 平台发布包版本均固定到对应 `0.163.0`，实际执行官方二进制，没有宣称在镜像内重编 Rust，也不包含托管 Codex。

SDK 通过 `startThread().runStreamed()` 启动 CLI，保持该版本原生 Responses 协议；app-server 是不同变体。本轮选定端点实际支持 Responses，没有靠更换模型或协议转换取得通过。材料完整性及 provenance 的 subject/workflow/tag/commit 对应已核对，未宣称独立完成 Sigstore 证书链验证。见[接入说明](../../../../../adapters/codex_sdk/README.md)。

## 接入与能力来源

SDK/CLI 循环、thread/session 和官方 MCP 配置属于候选；本仓库新增 stdio MCP 到公共 HTTP 工具的桥，只转发单次原名/原参数调用，不重试写入或预写任务流程。内建 Shell、Goal、图片、搜索等公开开关已关闭，但仍存在 MCP 资源元数据接口，不能声称模型可见面严格只有八工具。

固定版本 SDK/CLI 没有找到可生效的公开 `max_output_tokens` 设置，这是本轮明确的输出限制缺口。公共代理限制请求次数，不等于远端输出 token 已被硬限额。

## 分组实测与失败

| 阶段 | 结果及其含义 |
| --- | --- |
| 仅协议准入 | 10 次真实 Responses 请求成功，原生 turn.completed；未接工具，报告与写入均失败 |
| 首次 MCP 工具任务 | `auto` 因工具缺 annotations 仍要求审批，15 次请求、0 写入，整体失败 |
| 显式 `approve` 修订后 | 两个新容器任务各 12/12，模型 7/8 次，各 3 笔写入，原生 turn.completed |
| 最终镜像模拟探针 | 累计限额 18 项通过；早期 429 正文缺失及 home 软链接清点接线失败保留 |

普通授权工具的显式 approve 是公开配置修正；没有等待用户审批或篡改模型行为。两次成功仍只是 seed101 的新任务复现，不是 thread 恢复。协议可用、工具可用与业务完成三种结论必须分开。详见[初筛报告](../../../../validation/codex_sdk.md)。

## 维护投入、适用条件与未知项

实际工作包括发布包/平台核验、SDK 到 CLI 的子进程与 home 管理、MCP 桥、审批模式、残留工具面和严格终态观察；桥是需要维护的传输代码。调用数和原生 usage 已留存，模型金额及人工工时未知。

当应用愿意接受本地 CLI 进程边界、Responses 端点和 MCP 接入时，本轮证明这条路线能完成共同业务工具任务。浏览器、运行中暂停、新 Worker 续跑、租户隔离、线程/进程/容器并发关系与成本均未实测；输出硬限额缺口仍在。没有因 Coding 定位预先淘汰，也不能把本轮暂缓解释成模型或 MCP 不兼容，见[初筛安排](../../../../validation/screening.md)。
