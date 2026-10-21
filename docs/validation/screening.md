# 八候选初筛记录

日期：2026-10-20。**八项均已完成材料、接口和容器初筛调研。** 七项有两个新容器完整工具任务通过，Pi为1/2。八者模拟限额检查均通过；初筛不是恢复、浏览器或多租户验收，不据此推荐赢家。来源、完整 commit 和归档校验见 [sources.md](sources.md)，机器可读配置见 `configs/candidates/`。

## 先说结论

八条路线都保留，优先 DeepSeek Harness，OpenClaw 作为业务对照，Deep Agents 作为另一重点路线；Pi、ADK、OpenAI Agents SDK 中至少选一条进入后续同题比较。Codex SDK 和 OpenHands 也要做容器初筛，不能因为偏代码任务或需要补运行管理而预先移除。

本次发现应立即修正的材料假设：Pi 官方仓库和包名已变化；OpenHands 实际后台已分到独立 SDK 仓库；OpenClaw 当前版本的 Node 要求超过旧 Node 22 环境；Codex SDK 的本地运行程序需要 Responses 协议。它们会影响镜像和 API 接入方案，不等于能力已验证失败。

## 候选适合从哪里接

| 候选 | 本轮后台入口和模型接法 | 状态、实例和隔离的源码线索 | 当前需要验证的缺口 |
| --- | --- | --- | --- |
| DeepSeek Harness | I08 使用官方 `DeepSeekHarness` SDK 的 `sdk-minimal` 源码启动入口与统一工具插件；正式 `sdk` 和原生工具变体尚未测。通过计数代理映射 exact model/base URL/token | SDK 每 `sessionId` 创建 Agent；会话事件及 persistence 插件落盘；同一 runtime 可有多会话。SDK 通知是 runtime 全量、客户端过滤 | 真实工具任务与限额已通过；浏览器需把 hsi 接法迁到 Docker。SDK wire 未发现 cancel/session-close，不扩大为所有入口缺失；暂停恢复、可信作用域与共享成本待实测 |
| OpenClaw | Official Gateway + Agent/session 请求；按 custom `models.providers` 配 exact model/base URL/key/API shape | 固定版本文档支持一个 Gateway 进程内多个 Agent，各有 workspace/agentDir/SQLite 会话。Agent 不必然等于独立容器 | 同 Gateway 的权限和工具/沙箱隔离是否满足业务；按需启停/共享方案能否降空闲成本。不能把源码新能力说成 Hogee 已有现网能力 |
| Deep Agents + LangGraph | Python `create_deep_agent` + graph 流式执行；传入配置好的 LangChain 模型对象 | checkpointer/store 及 thread scope 是原生机制；图调用可共享应用进程，工作区和可信身份绑定仍需接入 | 容器依赖锁、运行中暂停、换容器继续、故障后业务副作用；已有预设停点不是外部暂停验收 |
| Pi Agent Core | `Agent` 库对象的 prompt/subscribe/abort；Pi AI 配 openai-completions、baseUrl、key、确切模型 | 核心维护内存中的 state/messages，in-process 多对象可共享进程；工具、持久化和部署交给上层 | 自研完整 durable 状态/控制/作用域的投入。coding-agent session manager 或 durable 包作为附加变体单列，不能默认算给 Core |
| Google ADK | `Agent/LlmAgent` + `Runner.run_async`；官方 `LiteLlm` 适配接选定模型；基础依赖另加 LiteLLM，不装全部 extensions | SessionService 按 app/user/session 存储，存在 SQLite 实现；Runner 是执行引擎，可处理多个会话 | LiteLLM 实际依赖与协议工具兼容、任务状态而不只是历史记录的恢复；user_id 不替代后端鉴权 |
| OpenAI Agents SDK | Python `Agent/Runner`；`AsyncOpenAI(base_url, api_key)` + `OpenAIChatCompletionsModel`，不启用托管 tracing | SQLiteSession 保存历史；支持 RunState 序列化的中断续跑；应用管理进程、工具、存储及用户身份 | 任意运行中暂停与原生审批中断的区别；本轮普通自动执行、敏感任务直接拒绝；持久存储不等于租户授权 |
| Codex SDK | TypeScript SDK 包装 `codex exec`。安装 SDK/CLI `0.163.0` 对；设置 baseUrl/apiKey/model。app-server 单列变体 | 持久 thread/session，可用 thread id 继续；SDK 启动 CLI 子进程，线程/进程/容器关系需量测 | 选定 API 是否支持 Responses；当前源码拒绝 chat wire API；原生 Responses 与统一MCP工具已实测通过，不能靠协议转换或换模型掩盖。输出token无公开上限，恢复尚未验证 |
| OpenHands | `software-agent-sdk` 的 `LLM/Agent/Conversation`，需要服务控制时用 Agent Server REST/WS；Canvas 界面可省 | `persistence_dir` + conversation_id；官方已有 pause、persistence 示例；Agent 定义和 Conversation 执行状态分开 | Docker 内实际恢复/中断、workspace 隔离和空闲成本；代码任务假设、工具/沙箱/可选后台组件的额外成本 |

这些仅为固定版本的接口和机制核对。“存在状态接口”“允许多个会话”均不代表运行中恢复或租户隔离已经通过。

## 运行环境准备约束

| 候选 | 固定源码声明的运行要求 | 容器初筛状态 |
| --- | --- | --- |
| DSH | Node `^22.19.0 || >=24.0.0`，pnpm `11.7.0` | I08 已完成：Linux arm64 固定镜像，两次真实模型/工具任务和候选重试/重启限额补验通过 |
| OpenClaw | Node `>=24.16.0 <25 || >=26.1.0`，pnpm `12.11.1` | I09 已完成：两个新容器真实工具任务各 12 项通过，模拟累计限额 18 项通过 |
| Deep Agents / LangGraph | Python 3.12，源码版本 `0.7.28` / `1.2.16`；独立 Linux 依赖锁及源码 wheel | I10 已完成：同一镜像两个新容器工具任务均通过；模拟重试及重启累计限额 18 项通过 |
| Pi | Node `>=22.19.0`，官方 npm workspace 锁 | I11 已完成：完整任务通过 1/2，另一次分页超限，报告/写入正确；限额 18 项通过 |
| ADK | Python `>=3.11`，非 Google API 需要 LiteLLM 扩展 | I12 已完成：两轮真实工具任务/模拟重试及重建限额通过 |
| Agents SDK | Python `>=3.10`；本轮使用单一选定远程模型 | I13完成：两轮工具任务各12项通过、模型7/6次、写入3/3；模拟限额18项通过。见[报告](openai_agents_sdk.md)；恢复/浏览器/隔离尚未验收 |
| Codex SDK | SDK 要 Node `>=18`；源码 workspace `>=22`；另需正确 Linux arm64 CLI | I14完成：原生Responses+MCP统一工具两轮12项通过、模型7/8次；同镜像模拟限额18项通过，首轮auto审批失败留档。见[报告](codex_sdk.md) |
| OpenHands | SDK/Agent Server Python `>=3.12`；可选 Canvas Node `>=24` | I15完成：核心SDK镜像两轮工具任务各12项通过，模型6/7次、写入3/3，模拟限额18项通过；Canvas/Agent Server未运行。见[报告](openhands.md) |

DSH 和 Deep Agents 的依赖、镜像、真实模型/工具请求、计数拦截点及更换容器后的累计限额已有证据，见 [deepseek_harness.md](deepseek_harness.md)、[deepagents_langgraph.md](deepagents_langgraph.md)。两次相同种子的成功不替代多种子详细比较；新建空状态重新运行也不代表已有任务恢复通过。所有初筛候选均已有独立报告。每项初筛按实施计划约两小时有效排查投入记录；下载/自动构建等待单列。

## 进入详细比较的决定方式

I08–I15 已留下真实容器运行与失败材料，I16 现固定下列名单。优先顺序不是预设推荐。至少一条轻量路线与 DSH、OpenClaw、Deep Agents 用相同任务、数据、模型、工具粒度、预算和总资源做比较；初筛受阻要区分机器、下载、API 协议和候选缺口。

后续普通业务任务自动放行；固定敏感任务由执行规则直接拒绝，不做审批产品。浏览器可重开、重新登录，不要求恢复网页现场。重点比较任务状态、过程能否看清、业务改造成本，以及两个活跃任务下空闲用户增多的资源增量。

| 候选 | 材料来源 | 容器模型/工具 | 详细名单决定 |
| --- | --- | --- | --- |
| DSH | 已固定并导出 | 2/2完整工具任务，累计限额通过 | 进入I17；用户优先路线，验证hsi浏览器迁移和原生会话/业务状态 |
| OpenClaw | 已固定并导出 | 2/2完整工具任务，累计限额通过 | 进入I18；用户现有业务对照，测透明度、插件改造、Gateway空闲成本 |
| Deep Agents / LangGraph | 已固定并导出 | 2/2完整工具任务，累计限额通过 | 进入I19；持久图执行路线，验证可控性与任务恢复所需接入 |
| Agents SDK | 已固定并导出 | 2/2完整工具任务，累计限额通过 | 进入I20；轻量应用对照，直接provider、公开RunState/after_turn值得实测 |
| Pi | 已固定并导出 | 完整任务1/2，累计限额通过 | 本轮暂缓；优先四路线投入。一次分页违约不足以认定整体不可靠；Core之外pi-durable仍为独立实验变体 |
| ADK | 已固定并导出 | 2/2完整工具任务，累计限额通过 | 本轮暂缓；四路线已满足比较安排。原生ResumabilityConfig/invocation_id未实测，不能判仅存历史或需Google托管 |
| Codex SDK | 已固定并导出 | 2/2修正后工具任务，累计限额通过；首轮审批接线失败留档 | 本轮暂缓；已有四路线，输出token限制缺口另记。不能说模型或MCP不兼容，恢复未知 |
| OpenHands | 已固定并导出，后台/Canvas分开 | 2/2核心SDK工具任务，累计限额通过 | 本轮暂缓；优先四路线投入。其官方pause/persistence仍值得后续验证，不能因依赖数或Coding定位认定不支持 |

这是I16的投入安排，不是能力排名。依赖锁条目数不等于常驻内存、研发成本或恢复质量；只有详细任务证据足够，才在I21选择专项路线。共同条件及逐轮证据矩阵见[detailed.md](detailed.md)。

## OpenClaw I09 入口和配置预案

以下是固定源码的接口核对及 I09 接入设计；下表为开始前的接入预案；I09已经完成真实Gateway/模型/工具验收，结果以[OpenClaw报告](openclaw.md)为准。初筛容器内由薄入口启动真实 Gateway，等就绪后通过容器 loopback 的可信 HTTP 客户端发送一次任务，最后停止 Gateway。此单任务部署便于先验收接线，不是后续共享实例成本的最终方案。`openclaw agent --local` 即使跑通，也不能代替 Gateway 的状态和资源验证。

| 要做的事 | 已核对的接法 | 本地实施注意点 |
| --- | --- | --- |
| 启动 | `node openclaw.mjs gateway run --port 18789 --bind loopback`；配置 `gateway.mode: local`、token 鉴权 | 不装宿主服务；官方 Gateway 在候选容器中前台运行，由薄入口作为真实子进程管理。HTTP 只供同容器可信客户端使用，无宿主映射端口 |
| 指定配置和状态 | `OPENCLAW_CONFIG_PATH` 指向受控配置文件，`OPENCLAW_STATE_DIR` 指向本 run 状态目录 | 新建空目录，不能挂宿主现有 `~/.openclaw`，也不能复用旧实验认证和历史 |
| 调用 Agent | 启用 `gateway.http.endpoints.chatCompletions.enabled`；POST `/v1/chat/completions`，`model: openclaw/lab-a` | 请求体的 `model` 是 Gateway 的 Agent 路由，真正远程模型在该 Agent 的模型配置里固定；请求不传自选模型覆盖头 |
| 延续会话 | 请求 `user` 使用可信后端生成的每会话稳定值；也有 `x-openclaw-session-key` 显式路由 | 不用账号级值把多会话合成一条；不让 Agent 自己提供用户和目标会话。缺少稳定值时该 HTTP 入口默认每次新会话 |
| 远程模型 | `models.providers.harness` 设置本地模型计数代理的 `baseUrl`、run token、原协议 API shape、唯一确切 model id | chat 接 `openai-completions`；Responses 接 `openai-responses`，由已选 API 决定；不做协议转换、不换模型。只有计数代理拿远程真实 key |
| 禁止隐式换模型 | `models.mode: replace`；`agents.entries.lab-a.model: {primary: harness/<已选model>, fallbacks: []}`；辅助模型同样指向已选模型和计数代理 | 配置需经当前版本 schema 校验；捕获全部实际模型请求，证明包括重试和辅助调用都经过计数代理 |
| Agent 目录 | 当前版本是 `agents.entries.lab-a`，其中分别配置 `workspace` 和 `agentDir` | 不照搬旧 `agents.list` 示例；状态 SQLite 与工作区分开挂载，两个目录都留清单 |
| 普通工具 | 官方插件 `api.registerTool(factory)` 注册八个对应工具，仅把原参数转发到已有统一工具服务 | 可用 `harness_` 前缀避免与内建 `write` 等重名，参数和粒度保持一致；不得在插件里预写查询、筛选、下单流程 |
| 绑定业务作用域 | 工具 factory 上下文提供 `agentId`、`sessionKey`、`sessionId`，还可有发起方附加的 `toolBindings` | 用后端受控映射选择单 scope 工具服务/token；字段缺失或映射不存在直接拒绝。Agent 工具参数里不提供 scope/token/URL 字段 |
| 工具日志 | 原样保留统一工具服务输出的 `call_id/start/finish/scope/arguments/result` | 不能只保存 OpenClaw 的最终回答；grader 需要完整配对日志与独立账本 |
| 避免后台意外请求 | `agents.defaults.heartbeat.every: 0m`、`cron.enabled: false`；不接消息渠道、不启用后台 shell/exec 工具 | 仍通过计数代理观察是否出现自动标题等辅助模型调用；发生了也计入本 run，不从成本里扣除 |

Gateway HTTP 的 Bearer 是整个实例的 owner/operator 凭证，文档明确说它不是每用户窄权限令牌。I09 只能由可信 bench 持有。后续多租户仍要经过业务后端鉴权和路由，不能把“传了不同 user 字符串”算成租户隔离通过。这一限制不预先判定其共享部署失败，要由 I24 的越权读写与控制探针验收。

插件要按固定版本同时准备 `package.json` 的 `openclaw.extensions`、`openclaw.plugin.json` 的 `contracts.tools` 和明确的 `activation.onStartup`。加载用 `plugins.load.paths` 指向受控插件目录，`plugins.entries.<id>.enabled: true` 显式启用。保留其实际依赖的 provider 插件；不要仅为减少组件而把所需模型适配器禁掉。注册检查先跑容器内 `openclaw plugins inspect <id> --runtime --json`，再发真实 Agent 任务。

构建前还有两个需要明确处理的来源细节：

1. 安全导出有意排除 `.npmrc`，但官方 Dockerfile 会 `COPY .npmrc`。本轮不能把宿主 npm 用户配置带进去。固定源码中该文件只有三条 `min-release-age` 策略；实施时可在审核后把这些非凭据构建策略写入受版本控制的实验 Dockerfile，并记录与原 Dockerfile 的差异。
2. 官方 Dockerfile 除固定基础镜像和 frozen pnpm lock 外，还做 `apt-get dist-upgrade`、`npm@latest` 和 `npm update`，因此不能照搬后宣称完全锁定。实验镜像要固定运行时和实际依赖清单，记录镜像 ID、Node/pnpm 版本；`pnpm build:docker` 是已存在的正式构建脚本，UI 另跑 `pnpm ui:build`。只测后台时可省 UI，但要明确这是后台变体，不能生成假 UI 资源冒充官方产物。

实际采用已锁 commit 的官方 `build:docker`；该脚本本身包含 Canvas、diffs 等真实插件资产，完整执行并保留通知，不能把“省去单独的 Control UI 构建”说成“无任何 UI 资产”。npm `openclaw@2026.10.5` 的 registry 元数据无 gitHead，其发布 provenance 指向另一 commit `c544916c9070b65c2ec8822de01bdf99041e12cd`，依赖也不同，故不直接用同版本名发布包替换当前源码。构建与入口细节见 [OpenClaw 接入说明](../../adapters/openclaw/README.md)。

I09 建议按“离线配置校验与插件注册 → 本地 mock 模型验证真实工具选择与回传 → 同一模型计数代理下真实远程模型工具任务 → 新会话/继续会话对照 → 完整证据与有效排查耗时”的顺序执行。mock 只验证接线，不计为远程模型能力通过。后续共享 Gateway 实验还要为并行 run 设计独立计数绑定，不能把所有租户共用一个无限增长的模型预算。

依据均在导出的固定来源 `external/build-context/openclaw/sources/main/`：`docs/cli/gateway/running.md`、`docs/gateway/openai-http-api.md`、`docs/gateway/config-tools/custom-providers.md`、`docs/gateway/config-agents/entries-and-multi-agent.md`、`docs/plugins/building-plugins.md`、`src/plugins/tool-types.ts`、`docs/gateway/heartbeat.md`、`package.json` 和 `Dockerfile`。文档中的入口与字段均为源码线索，最终以 I09 容器校验为准。
