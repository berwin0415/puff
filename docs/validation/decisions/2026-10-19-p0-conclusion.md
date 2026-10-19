# P0 云端 Harness 调研结论与推荐

日期：2026-10-19

这份结论基于三个 P0 候选的固定源码、依赖安装和远程模型最小运行证据。它回答的是“下一步应该沿哪条路线做云端 Harness 原型”，不把尚未完成的暂停恢复、故障恢复和真实业务效果验证描述成已通过。

## 结论

推荐以 **Deep Agents + LangGraph 作为主实现路线**，外接本仓库已有的 Runner、Execution、Fixture 和证据存储。它目前最接近“可控 Agent loop + 可持久化状态”的需求：真实远程模型调用、确定性工具调用、结构化 graph 事件和 SQLite checkpoint 已经通过；源码、许可证和依赖可以固定在仓库外，适配层保持薄。后续主要建设可恢复 Worker、统一浏览器桥接、任务生命周期和多租户作用域。

推荐将 **OpenClaw 保留为现有业务基线和控制平面对照**。它的 Gateway、Agent 工具面、插件和会话能力适合继续支持现有助手产品，也已经通过构建、Gateway health/ready 和远程模型调用。但本轮还没有统一 T1/T2、暂停恢复或事件协议证据，因此暂不把它定为新 Harness 的核心执行状态机。若后续验证证明 Gateway 的事件、会话恢复和自定义工具接入可以低成本映射到统一协议，可以把 OpenClaw 用作接入层或渠道层。

**DeepSeek Harness 暂不作为通用云端 Harness 主路线**。它适合作为 DeepSeek 生态和现有 Harness 实现的参考基线；T1 工具调用已经通过，但当前 `headless` 入口明确没有浏览器 Connection，也没有通过适配器提供暂停、恢复、checkpoint、审批、实时输入和取消。浏览器是首要任务之一，这个能力阻断使它无法直接满足当前目标。只有在 SDK/ACP 提供持久会话、浏览器接入和可恢复生命周期后，才值得重新评估。

## 证据强度

| 路线 | 已确认 | 仍未确认 | 当前定位 |
| --- | --- | --- | --- |
| Deep Agents + LangGraph | MIT 来源；Python 3.12 依赖；远程 API；`fixture_lookup`；9 个结构化事件；SQLite checkpoint 文件 | 统一 Playwright；进程/容器重启恢复；实时 pause/resume/approval；多租户作用域 | 主实现候选 |
| OpenClaw | MIT 来源；OpenClaw 2026.10.5 构建；Gateway health/ready；20 个插件加载；远程 API 返回 `OPENCLAW_API_OK` | 统一工具任务；浏览器任务；事件映射；checkpoint；暂停恢复；多租户边界 | 业务基线/控制平面对照 |
| DeepSeek Harness | MIT 来源；Node CLI；远程 API；T1 bash/curl 和幂等副作用；统一 Playwright 基线 | headless 浏览器原生能力（已阻断）；持久会话；暂停恢复；审批和实时输入 | 专项参考，不作为主路线 |

证据报告：

- [DeepSeek Harness](../reports/2026-10-19-deepseek-harness.md)
- [OpenClaw](../reports/2026-10-19-openclaw.md)
- [Deep Agents + LangGraph](../reports/2026-10-19-deepagents-langgraph.md)
- [能力矩阵](../matrix.md)

## 推荐的目标架构

```text
产品后端（租户、用户、鉴权）
        │ task/session scope + auth context
        ▼
统一 Runner（排队、预算、生命周期、事件和审计）
        │
        ├── Deep Agents + LangGraph adapter（主路线）
        ├── OpenClaw adapter（业务基线/可选控制平面）
        └── DeepSeek adapter（专项能力/对照）
        │
        ▼
Execution worker（工具、Shell、Playwright、工作区）
        │
        ├── SQLite/JSONL：验证阶段
        └── 可替换持久化：进入多租户原型时
```

Runner 必须持有租户和会话作用域、预算、超时、幂等键和统一事件序号；候选框架只负责 Agent loop 和候选原始事件。工具、浏览器和工作区由 Execution worker 管理，避免每个候选维护一套不可比较的执行环境。

## 阻断条件

以下任一条件在目标任务上无法满足时，应暂停该路线投入：

1. **恢复硬门槛**：模型等待、工具执行和长命令期间无法在安全点创建 checkpoint；杀掉 Runner 或执行容器后不能恢复同一任务，或恢复会重复产生不可接受的外部副作用。
2. **浏览器硬门槛**：通用 Agent 无法访问统一 Playwright 轨道，或浏览器上下文/语义状态无法按约定保存与重建。DeepSeek Harness 当前已触发此项，因为 headless profile 没有 browser Connection。
3. **作用域硬门槛**：任务、会话、租户和 artifact 无法明确隔离，或者候选事件/工具调用无法关联到统一 `run_id`，导致无法审计和排查跨租户问题。
4. **模型链路硬门槛**：选定远程 API 无法稳定完成工具调用，或超时、重试和预算控制无法被 Runner 接管。
5. **工程成本硬门槛**：在业务场景确认后的有限研发时间内，补齐上述能力所需的适配代码和长期维护成本明显高于主路线，且没有可复用的现成组件。

生产级网络、凭证、沙箱和资源隔离属于后续安全设计；本地验证阶段仍需记录实际权限姿态，但不以生产安全方案尚未完成作为当前淘汰条件。

## 成本判断

目前只有接入工作量的结构性证据，还没有足够的真实业务任务样本来给出每任务费用或人日承诺。现阶段可以先做以下排序：

| 项目 | 当前判断 | 依据与限制 |
| --- | --- | --- |
| 最小 API 冒烟 | OpenClaw、DeepSeek 和 Deep Agents/LangGraph 均已通过 | 只代表模型调用，不代表云端 Harness 完整成本 |
| 首次适配代码 | DeepSeek 和 Deep Agents/LangGraph 已有薄适配器；OpenClaw尚无统一适配器 | 代码量是代理指标，不能代替工程人日 |
| 状态/恢复建设 | Deep Agents/LangGraph 已有 checkpoint 后端，仍需可恢复 Worker；OpenClaw、DeepSeek 尚未形成同口径证据 | 这是当前研发成本最大的未知项 |
| 运行成本 | 远程模型 token、执行容器、浏览器和 checkpoint 存储均需按 T3-T5 测量 | 当前 API 冒烟的 token 数不能外推到业务任务 |

因此，有限研发时间下优先投入 Deep Agents/LangGraph 的恢复 Worker 和统一浏览器桥接；如果这两项成本超出时间窗口，再评估复用 OpenClaw Gateway 的范围。DeepSeek 只有在补齐浏览器和长生命周期入口后才重新进入成本比较。

## 下一步的决策门

先只推进主路线的生命周期专项：

- 用同一套 T3-T5 任务验证 pause → checkpoint → Runner/容器重启 → resume；确认消息、工具上下文、工作区和幂等副作用一致。
- 为主路线增加长连接或可重建 Worker，再接入统一 Playwright；把浏览器持久化上下文和语义 checkpoint 分开记录。
- 用 T6 增加一个工具、一个审批节点和一个状态节点，记录改动文件数、首次可运行时间、事件覆盖率和调试路径。
- 只有主路线触发恢复或作用域阻断时，才进入 P1 对照（OpenAI Agents SDK、Google ADK、Pi Agent Core、OpenAI Codex SDK、OpenHands）。
- OpenClaw 仅在统一事件、恢复和自定义工具验证通过后，才升级为可选执行路线。

在这些决策门通过前，结论是“推荐 Deep Agents + LangGraph 进入下一阶段原型”，不是“已经完成生产选型”。
