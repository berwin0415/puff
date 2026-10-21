# 暂停与换容器恢复：接口核对和最小探针

日期：2026-10-20。本文整理固定源码的接口线索及后续实验设计，不能据此认定暂停恢复通过。实际Docker结果另见[详细矩阵](detailed.md)：DSH取消已实测并暴露原生结束事件写入缺口；旧冷读003失败保留，改用公开`tool/result`元数据后的extension冒烟002和冷读004已通过；OpenClaw冷读001也已通过。两者均只证明原生状态可在新容器读回，I22尚未开始。已有初筛、浏览器或工具任务结果不能替代暂停恢复探针。

先做 I16 选定候选的共同详细实验，在 I21 根据证据选出 2–3 条路线，再为 I22 接入适合各自原生机制的控制入口。不为了统一接口或做出通过结果，提前建设一套通用任务状态机。

## 1. 本文区分哪些能力

| 能力 | 所需证据 | 不足以证明它的材料 |
| --- | --- | --- |
| 持久会话冷启动 | 新进程从候选原生存储加载同一 session、历史、身份和连续 seq | 重新创建同名 session，或把旧消息重新发一遍 |
| 同一任务继续 | 候选自己的执行状态或持久目标在新进程保留，继续未完成工作，原有写入和产物可核对 | 只有历史、原始 prompt 或外层 paused 标签 |
| 运行中请求暂停 | 外部在真实执行期间提出请求，候选到安全点、落盘并确认，之后才能移除 Worker | 预设第 N 步停点、直接 kill、等待整项任务自然完成 |
| 故障恢复 | 突然退出或丢回执后，区分已完成、未执行和结果不确定，核对实际副作用 | 先主动 flush 后退出的受控路径 |

I17 的最小会话探针用于排除材料和加载入口问题；I22 仍执行 [暂停任务协议](../../configs/tasks/pause-resume.yaml)：正常基线、运行中外部请求、安全点确认、删除旧容器、等待 600 秒、新容器继续、同链预算累计、关键路径三次。浏览器现场不要求保留，可重建并重新登录。

## 2. DSH：固定源码与三个不同层次

DSH 材料来自 `deepseek-ai/deepseek-harness`，固定 commit 为 `dd6322d604e00eec1ba5e0c8541159906a21094a`，源码版本 `0.1.2-alpha.3`。来源配置见 [deepseek_harness.yaml](../../configs/candidates/deepseek_harness.yaml)，当前接入说明见 [adapter README](../../adapters/deepseek_harness/README.md)。以下源码链接指向已准备的安全导出 `external/build-context/deepseek_harness/sources/main/`，不引用归档实验代码，也不执行外部候选 SDK。

| 层次 | 已核对的公开入口 | 源码能够证明什么 | 当前缺口或边界 |
| --- | --- | --- | --- |
| 高层 SDK / stdio server | `DeepSeekHarness`、`initialize`、`session/prompt`、`shutdown` | 运行任务和关闭整个 runtime；同进程按 sessionId 保存 Agent handle | wire 没有 session pause/cancel/resume；新进程的 `createSession()` 调用 `agents.create()`，不会因同名 session 自动读取旧 JSONL |
| direct scoped runtime | `runProfile`、`ctx.agents.create({setup})`、`ctx.agents.resume({resumeSessionId,setup})`、`Agent.cancel/whenIdle`、`ctx.sessions.flush` | 可重建原生持久 Session，在发布前重挂可信工具/guard/MCP；有取消并等待活动收束的公开入口 | resume 后是 idle，未自动续跑旧 turn；003自定义事件冷读失败保留，公开元数据方案的004冷读已通过；取消实测缺结束事件；尚无任务暂停协议 |
| 官方持久 Goal 及 round driver | `ctx.goals.create/get/pause/resume/complete`、`@deepseek-ai/dsh-goal-round-driver` | 目标、phase、revision、已启动 round 计数来自原生 `goal/change`；明确恢复授权后，官方 driver 根据持久目标发起新 round | 当前 sdk-minimal 未加载；不是旧 turn 指令位置恢复；Goal pause 本身不停止当前 turn |

SDK server 的方法分派与始终 create 的路径见 [server.ts](../../external/build-context/deepseek_harness/sources/main/packages/sdk/server/src/server.ts)。客户端源码也明确说明：请求超时不等于服务端取消，没有 wire-level cancel，任务会继续到 runtime 关闭。见 [client.ts](../../external/build-context/deepseek_harness/sources/main/packages/sdk/client/src/client.ts)。这只是该 SDK 协议的限制，不能扩大成 DSH 框架不支持恢复。

### 2.1 direct runtime 的生命周期与取消

公开 [ResumeAgentOptions / AgentHandle](../../external/build-context/deepseek_harness/sources/main/packages/core/agent/src/index.ts) 和 [Agent runtime API](../../external/build-context/deepseek_harness/sources/main/packages/core/agent/src/runtime-types.ts) 规定：

- `agents.resume({resumeSessionId, agentOptions, setup})` 先读取 persistence，再 mint 新 Agent scope，await setup 后发布。setup 可重建当前作用域工具和执行规则；不能沿用旧进程闭包、HTTP/MCP 连接或测试凭证对象。
- create/resume 的 `signal` 是创建阶段取消信号，返回 handle 前会脱离，不能把它当运行中暂停接口。运行阶段须使用 Agent 的公开取消入口或公开扩展点。
- `agent.cancel(cause, {keepInbox:true})` 中止活动 turn 并保留未消费 inbox；默认不传 keepInbox 会清空 inbox。取消不是协作暂停，也不撤销外部已提交写入。
- `await agent.whenIdle()` 等待整 Agent 活动收束，不代表某条消息完成或业务成功。
- `await ctx.sessions.flush(agent.session)` 等待该 live Session 的持久化监听器全部成功；返回值应确认确有监听器参与，不能忽略写盘异常后仍确认 paused。

具体实现见 [ReactLoopAgent](../../external/build-context/deepseek_harness/sources/main/packages/core/agent-loop/src/agent.ts)、[resume/setup/teardown](../../external/build-context/deepseek_harness/sources/main/packages/core/agent-loop/src/index.ts)、[SessionStore.flush](../../external/build-context/deepseek_harness/sources/main/packages/core/session/src/index.ts)。

### 2.2 JSONL 提供持久事实，不恢复任意执行位置

JSONL 后端保存 header 和连续事件；追加后会调用文件 `sync()`，首次发布也有文件与目录持久化处理。事件写入有缓冲，不能把同步 `session.append()` 返回或观察到 `session/event`，直接当作该事件已经 fsync。暂停确认前必须 await flush。见 [JSONL 后端](../../external/build-context/deepseek_harness/sources/main/packages/session/session-persistence-jsonl/src/index.ts)、[PersistenceCoordinator](../../external/build-context/deepseek_harness/sources/main/packages/session/session-persistence/src/coordinator.ts)。

冷加载会保留完整已提交前缀，舍弃撕裂的末记录，并为未闭合工具、step、turn 补齐恢复事件。未记录开始的调用标为 `TOOL_NOT_STARTED`；已记录开始、没有 durable 结果的调用标为 `TOOL_OUTCOME_UNKNOWN`；原 turn 用 `interrupted` 关闭。随后 Agent 从新回合工作，不能声称回到旧工具的指令位置。见 [修复算法](../../external/build-context/deepseek_harness/sources/main/packages/core/session/src/repair.ts) 和 [持久服务契约](../../external/build-context/deepseek_harness/sources/main/packages/session/session-persistence/src/index.ts)。

`Inbox` 确有原生 `agent/inbox/spliced` 事件和重建投影；但保留 pending 内容与自动唤醒是两回事。上游 [resume 测试](../../external/build-context/deepseek_harness/sources/main/packages/core/agent-loop/tests/resume.spec.ts) 的 pending inject 场景，仍须新 followup 才进入下一回合。不能据此宣布任务自动续跑。

### 2.3 官方 Goal 可以提供任务目标状态

固定源码已有 [GoalService](../../external/build-context/deepseek_harness/sources/main/packages/goal/goal/src/index.ts) 和 [goal-round-driver](../../external/build-context/deepseek_harness/sources/main/packages/goal/goal-round-driver/src/index.ts)：

- `goals.create(agent,{objective,maxGoalRounds})` 建立目标；`get(agent)` 读取当前完整 view。
- `pause/resume/complete(agent,{id,revision})` 采用当前 CAS ref；旧 revision 不能覆盖新状态。
- 目标 phase 为 `active/paused/blocked/complete`，目标、revision、roundsStarted 等写入原生 `goal/change`；是否允许自动继续的 activation 是进程内状态。
- 每次 `agent/session-start` 都 disarm。换进程后不会擅自恢复自动运行；可信控制方显式 `goals.resume` 后才重臂。
- 官方 driver 在 whole-agent idle 且目标 active/armed、有剩余 round 时，先处理持久化义务，再创建归属于该目标的新 round。它不重建或重发整个旧会话，也不从旧 turn 中间接回。
- `goals.pause()` 抑制后续 round，不取消当前活动 turn；仅加载 GoalService 也不负责调度。

这是原生持久目标机制，比只有 transcript 更接近本次任务恢复要求，但仍不是与外部业务事务共同提交的工作流。上游源码和 [driver 测试](../../external/build-context/deepseek_harness/sources/main/packages/goal/goal-round-driver/tests/goal-round-driver.spec.ts) 可支持实验设计，不能替代本仓库真实换容器验收。

## 3. I17 最小会话冷启动探针

目的仅是确认 direct runtime 能正确打开本轮原生 JSONL，以及可信 setup 能在新容器恢复，不提前完成 I22。

当前实际结果：旧`i17-dsh-native-state-003`在删除旧Worker、同镜像新容器加载时，拒绝未知必需自定义事件`harness-lab/business-state`，该失败保留。新方案由公开`output.presentationMeta`将完整业务态交给官方调度器保存到真实`tool/result.data.meta.business_state`，再由公开projection重建；新镜像为`sha256:dc5238ff6e83883e223619d149c4b784e55f491c86b3fa7be90dc99ce29336ed`。`i17-dsh-extension-smoke-002`已真实19/19项通过，10次模型请求、3笔写入/3次请求；来源该run的`i17-dsh-native-state-004`已真实23/23项通过。旧Worker已删除，新容器ID不同，原生日志前缀保留，仅追加正常加载边界；完整业务态和真实receipt来源一致，无新模型/业务调用。DSH加载会追加原生边界，state为受校验的rw挂载，不能称全程只读；这仍是冷读，不是未完成任务续跑。001/002编排失败、003加载失败和新004成功分别保留，见[DSH报告](deepseek_harness.md)和[详细矩阵](detailed.md)。

1. 容器 A 从现有 direct scoped runtime 创建 Session，执行一个真实普通工具回合。保留模型代理、原生事件、工具审计和账本。
2. 回合结束后 `await agent.whenIdle()`，确认原生终态，再 `await ctx.sessions.flush(session)`。通过 `ctx.sessionPersistence.locate(session.header)` 记录实际原生文件路径，保存 sessionId、cwd、末尾 seq、文件 SHA256 和产物；不得由 adapter 重造 Session 日志。
3. A 正常退出并删除容器；保留本轮专属 `/state`、`/workspace`、模型累计预算和可信后端绑定。
4. 新容器 B 使用同一固定镜像和挂载，启动相同 profile，改走 `agents.resume({resumeSessionId,agentOptions,setup})`。setup 复用现有工具/guard/MCP 接线，注入本轮有效凭证；不要走 SDK createSession 或重新 agents.create。
5. 先不调用模型，检查原 Session header、历史前缀、seq、cwd、恢复后的工具表和可信身份。B 自行追加的原生 `session/end-seed` 边界应保留，不能为了字节相同而删除正常原生事件。
6. 如需验证上下文可继续，再发送一个短的只读 followup；核对 seq 连续、原历史保留及 guard 仍有效。

结论应写“持久会话在新容器加载成功/失败；新回合上下文继续成功/失败”。第 6 步不是旧任务自动恢复；不用它满足 I22 的未完成任务继续条件。

增量成本为一个明确的 create/resume 入口分支、原生状态导出和 A/B 容器编排。无需新任务数据库或通用 checkpoint 格式。若 native load、setup 或写盘失败，保存具体异常和原生文件，记为该入口的探针缺口，不扩大成整个框架无恢复能力。

## 4. I22 建议路线：官方 Goal + 公开步边界

只有 I21 选中 DSH 后实施。优先增加四项现有官方插件，保持固定源码和上游锁：

| 官方插件 | 用途 | 当前 sdk-minimal |
| --- | --- | --- |
| `@deepseek-ai/dsh-goal` | 持久目标、phase、revision、round 进度 | 未加载 |
| `@deepseek-ai/dsh-goal-round-driver` | 明确恢复授权后用持久目标开启下一 round | 未加载 |
| `@deepseek-ai/dsh-session-checkpoint-policy` | 模型请求和顶层工具副作用前 flush 原生日志 | 未加载；该插件在官方 base profile 中存在 |
| `@deepseek-ai/dsh-tool-goal` | 向模型开放官方 `get_goal/create_goal/update_goal`，由候选自己判定并报告complete/blocked | 未加载 |

第三项的公开实现见 [session-checkpoint-policy](../../external/build-context/deepseek_harness/sources/main/packages/session/session-checkpoint-policy/src/index.ts)，当前 profile 见 [sdk-minimal composition](../../external/build-context/deepseek_harness/sources/main/packages/bundle/sdk-minimal/cordis.patch.yml)。不能把 base profile 的配置当作当前变体已经启用。

driver自身依赖为agents/goals/sessions，但其提示要求模型读目标并在完成时更新目标。为验证候选自己管理任务，应同时加载官方[tool-goal](../../external/build-context/deepseek_harness/sources/main/packages/goal/tool-goal/src/index.ts)。可信控制端只负责获得授权的create/pause/resume和预算停止，模型经官方工具判定complete/blocked；不能由外层grader替候选调用complete来凑成功。官方工具会核对顶层人类指令或当前goal round的精确归属、id/revision，见[authority](../../external/build-context/deepseek_harness/sources/main/packages/goal/tool-goal/src/authority.ts)。增加三项目标工具和官方提示会改变工具面及token成本，须作为独立Goal变体与普通任务基线分别记录，不能回填当前变体已具备该能力。

### 4.1 开始与外部请求

1. A 使用同一 direct scoped runtime 和可信 setup 创建 Agent；调用 `goals.create(agent,{objective,maxGoalRounds})`，由官方 driver 驱动真实任务。round cap、模型请求总限额和活动时间分别约束，不将 round 数当 API 调用数。
2. 控制器从真实模型/工具开始证据确认任务已经运行，再向所属入口发送暂停请求。入口仅记录本轮 `pauseRequested` 和请求时间，不靠预设第 N 步自动暂停冒充外部控制。
3. 请求不立即强杀或取消工具。公开 `agent/pre-step` hook 在下一步开始前检查暂停意图；当前模型响应和已启动工具先正常结算。请求若刚好晚于一个边界，可允许当前一步结束后再停，真实记录延迟。

### 4.2 安全点、持久化与退出

在公开 pre-step hook 确认暂停意图后，用最新 Goal view 的 `{id,revision}` 调 `goals.pause` 并返回 `{kind:"reject"}`，使本次 turn 收束。hook 应在下游异步决策之后再次检查暂停标志，避免把早先检查当作后续永久准入。

原生 loop 会把被拒绝步骤的 turn 记为 `blocked`；Goal 则持久为 `paused`，两者必须分别保留，不能改写原生事件成 paused。随后等待 `whenIdle()`，确认没有活动工具及后续 round，再 await `sessions.flush` 成功；保存实际 Goal view、原生末尾 seq、日志和工作区哈希后才确认“已保存、已暂停”。任务已经自然完成、出现异常或达到停止期限时，分别记录 completed/failed/timeout，不能统一改写为暂停成功。

这个最小探针限定一个任务、没有并发输入，并要求停点的 claimed messages 和 pending inbox 均为空。`pre-step` 在调用 hook 前已经 claim 输入，直接 reject 可能丢弃刚被 claim 的消息；非空场景不纳入最小通过范围，需单列后续队列保存成本。

完成原生收尾后删除旧 Worker，记录旧 container ID、退出及资源释放。等待 600 秒期间不保持候选 Worker；公共 proxy/工具/Gateway/浏览器等保留资源单独采样，不把它们的常驻成本藏在 Worker 之外。

### 4.3 新容器继续

1. B 使用同一固定镜像、新 container ID、同一链状态/工作区/预算启动；通过 `agents.resume({resumeSessionId,agentOptions,setup})` 重建。
2. 在发起任何新模型调用前核对原生 Goal 的 id、objective、revision、phase 和 roundsStarted，并确认 activation 为 disarmed。新凭证、HTTP/MCP 连接、工具和敏感 guard 必须重挂。
3. 可信恢复请求调用 `goals.resume(agent,{id,revision})`。官方 driver 根据持久目标和原工作区进入新 round；不要重新提交原始任务 prompt 或把旧消息复制成新 Session。
4. 候选通过官方`update_goal`自行报告complete/blocked；独立tools grader再用真实operation_id/receipt/ledger和产物判定业务是否正确。目标已complete但实际任务未完成，应判失败；grader不能替候选完成目标。
5. 核对暂停后释放资源、恢复延迟、额外模型/工具调用、已完成写入没有重复，以及累计 300 秒活跃时间/20 次模型请求/最多两次重试没有因换容器清零。按冻结协议重复关键路径三次。

该路径能验证的是“原生持久 Goal + 步边界协作暂停 + 新进程继续目标”。它不承诺恢复旧 turn 指令位置、自动复活被撤销的身份或 exactly-once 业务执行。

### 4.4 增量投入与停止条件

新增工作限于四项官方插件配置、一个公开 hook、简单可信控制通道、create/resume/Goal 生命周期分支和 A/B 证据编排。模型循环、Goal状态及模型目标工具、round driver、JSONL、repair和副作用前checkpoint继续使用官方实现。

本阶段不自建任务数据库、通用状态机或业务事务协调器。若唯一任务/空 inbox 的最小路径也无法可靠收束或冷启动，则保存失败入口、日志与影响，记录补齐范围和投入；不通过重写上游循环、重放整个提示或隐藏失败来做出通过结论。

## 5. 两个不能省略的限制

### 5.1 dispose 会清空 inbox

公开 AgentHandle 的 disposer 最终调用 `machine.cancel({kind:"disposed"})`，没有传 keepInbox，再等待 whenIdle 和释放 scope。默认 cancel 会清空 inbox，即使之前调用过 `cancel(...,{keepInbox:true})`，后续 dispose 也仍会清空。见 [AgentLoop teardown](../../external/build-context/deepseek_harness/sources/main/packages/core/agent-loop/src/index.ts) 和 [cancel 实现](../../external/build-context/deepseek_harness/sources/main/packages/core/agent-loop/src/agent.ts)。

因此不能只 flush pending inbox 后照常 dispose，就宣称这些 pending 工作原样保留。最小 Goal 探针要求没有并发输入/待消费上下文；非空时不得确认该范围的安全暂停。未来若要支持并发队列，需单独验证所有权移交和待处理输入保留，计为额外研发成本。

flush 应在 Session 仍 live 时完成；dispose 后再调用 `sessions.flush` 不属于有效 live Session 路径。退出前后的持久文件变化也要记录，避免最后一轮 teardown 改变已确认状态却不留痕。

### 5.2 外部副作用与候选日志不是同一事务

安全步边界允许已经开始的一步完成，暂停请求后仍可能产生已排队的实际写入。并行工具可能同时在途；HTTP 请求被取消，不代表服务端尚未提交。

即便加载官方 checkpoint policy，仍存在“工具服务已提交、客户端尚未收到回执或结果尚未 durable”的窗口。JSONL 的 `TOOL_OUTCOME_UNKNOWN` 能提示不确定性，不证明 exactly-once；当前 HTTP 接线确实传递 execution.signal，但不能据此撤销提交。业务 receipt/operation_id 查询和现有真实账本才是核对依据。

受控安全暂停与突然退出要分开实验。后者按 [故障任务](../../configs/tasks/fault-recovery.yaml) 记录请求数、真实写入数、回执查询与最终分类；允许明确失败或需人工确认，不能把这类结果当作自动恢复成功。服务端幂等防重也不算框架独有保证。

## 6. 其他候选的原生线索：供 I21 选入口

下表只指向已固定源码/adapter 文档中的可行入口，不增加候选能力通过结论，也不因为当前 one-shot 入口没实现而写成框架不支持。

| 候选 | 已记录的原生线索 | 后续最小核验与限制 |
| --- | --- | --- |
| Deep Agents + LangGraph | `RunControl.request_drain()`、`GraphDrained`；同 AsyncSqliteSaver/thread_id 重建 graph，`astream(None,...)` 继续；真实动态 interrupt 才使用 `Command(resume=...)` | 当前superstep完成、下一superstep前停；新RunControl、同图/工具和可信依赖。当前入口拒绝已有checkpoint，原生协作停止接线不等于任务续跑；I22须另接可信resume分支并实测 |
| OpenAI Agents SDK | `RunResultStreaming.cancel(mode="after_turn")`，消费结束流后 `to_state()`；`RunState.to_json/from_json`，再交给 Runner | 完成本轮模型、待执行工具及Session保存后停；当前入口拒绝非空Session，取消使用immediate。after_turn与同任务续跑须另接并实测；SQLite历史、离线业务态读取或脱敏诊断快照不等于可续跑状态 |
| OpenClaw | 整 Gateway 的 `gateway.suspend.prepare/status/resume/handoff`，以及符合条件的已接纳主会话原生 restart recovery | 核对本实验 `openai-user` 入口的恢复资格、持久 authority、Gateway host lifecycle/退出所有权和累计预算。整 Gateway 暂停会影响其承载的其他任务，不能默认是单任务暂停；显式 Stop/cancel 属终止 |

详细依据分别见 [Deep Agents README](../../adapters/deepagents_langgraph/README.md)、[Agents SDK README](../../adapters/openai_agents_sdk/README.md)、[OpenClaw README](../../adapters/openclaw/README.md)。

### 6.1 Deep Agents + LangGraph：在执行步边界停，原生checkpoint继续

本次只读核对使用 DeepAgents `0.7.28` / commit `0089e635dce525055272883b53bc222f385beac1`，LangGraph `1.2.16` / commit `98043c37abc60374030f960a03a6e0d4c0112643`，来源见[候选配置](../../configs/candidates/deepagents_langgraph.yaml)。以下为源码事实和I22接入建议，未运行本仓库任务续跑实验。

- 公开 [RunControl](../../external/build-context/deepagents_langgraph/sources/langgraph/libs/langgraph/langgraph/runtime.py) 的 `request_drain(reason)` 只记录协作停止请求，不取消Python任务或在途HTTP；每次运行须新建control，不能把已drain的对象复用到恢复运行。
- [原生执行循环](../../external/build-context/deepagents_langgraph/sources/langgraph/libs/langgraph/langgraph/pregel/_loop.py) 先判断有无下一任务，再检查drain。当前superstep先完成，下一superstep开始前才停止；如果任务自然完成，应保留completed，不能改标paused。工具节点可能有多个并行调用，需要等本步全部收束。
- [astream实现](../../external/build-context/deepagents_langgraph/sources/langgraph/libs/langgraph/langgraph/pregel/main.py) 在 `durability="sync"` 时等待checkpoint保存；原生循环确实进入draining后才抛 `GraphDrained`。[SQLite saver](../../external/build-context/deepagents_langgraph/sources/langgraph/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py) 的 `aput/aput_writes` 均等待commit。不能仅凭请求API返回就确认保存完成。
- [官方test_runtime](../../external/build-context/deepagents_langgraph/sources/langgraph/libs/langgraph/tests/test_runtime.py) 的 `test_drain_with_exit_durability_persists_resume_checkpoint` 验证了同checkpointer/thread在drain后使用 `invoke(None, config)`继续下一节点。这支持接口选择，仍不能替代本实验的SQLite、Docker换容器与真实模型验收。

当前[entrypoint](../../adapters/deepagents_langgraph/entrypoint.py) 读取已有checkpoint后直接拒绝，正常入口始终提交新用户消息；GraphDrained分支只导出实际snapshot并记录drained。I21选中后，最小改造为可信的new/resume分支：重建相同graph、工具/schema、模型和checkpointer，核对原thread/checkpoint/作用域；resume向 `astream` 传 `None`，使用新RunControl及同步持久化。不重发原prompt、不重置业务状态、不用 `update_state` 或手工更改下一节点制造继续执行。

保留候选完整state目录和workspace，正常退出saver后再清点、删除旧容器；不能在数据库仍打开时只复制主SQLite文件而遗漏WAL等持久材料。暂停确认需有实际GraphDrained、可读checkpoint ID、下一节点/任务与落盘证据。恢复前后的进度与业务结果由独立判定核对，不靠外层paused标签。

### 6.2 OpenAI Agents SDK：完成当前轮后停，完整RunState继续

本次只读核对使用 Agents SDK `0.24.0` / commit `64cd5dfc5a1579870dcbed0ea42b27aa84033fee`，来源见[候选配置](../../configs/candidates/openai_agents_sdk.yaml)。本节未执行after_turn、JSON恢复或新容器任务续跑实验。

公开 [RunResultStreaming.cancel](../../external/build-context/openai_agents_sdk/sources/main/src/agents/result.py) 区分immediate和after_turn。`after_turn`允许当前模型响应完成、执行该响应已经决定的待执行工具、保存Session并计量用量，再停在下一轮前；调用后必须继续消费原 `stream_events()`。不能取消消费任务或Python主任务来代替安全暂停。原stream正常消费收尾会等待实际run loop并重新检查异常；`is_complete`只说明本次stream结束，不代表业务自然完成。

[run_loop](../../external/build-context/openai_agents_sdk/sources/main/src/agents/run_internal/run_loop.py) 的RunAgain分支先保存原生下一步及Session，再检查after_turn。公开 `result.to_state()` 会保留原始输入、已生成/Session项目、当前Agent、context、模型响应、当前轮数、下一步、已保存项目计数和待落盘Session写入；[RunState.to_json/from_json](../../external/build-context/openai_agents_sdk/sources/main/src/agents/run_state.py) 提供完整序列化/重建。[Runner恢复入口](../../external/build-context/openai_agents_sdk/sources/main/src/agents/run.py) 沿用state的max_turns/current_turn及保存计数，后者用于避免重复追加历史。上述源码提供组合所需入口；未找到直接覆盖after_turn→JSON→新进程的完整上游用例，组合后能否正确续跑仍须本仓库实测。

当前[entrypoint](../../adapters/openai_agents_sdk/entrypoint.py) 拒绝非空SQLiteSession，SIGUSR1调用immediate；finally中的 `run-state.json` 经 `encoded()` 脱敏并使用普通 `write_text` 保存，明确只是诊断材料。离线业务状态roundtrip也没有进入Runner。I21选中后的最小接入为：

1. 新增与immediate取消分开的可信after_turn暂停请求；继续消费原stream，区分安全停、自然final output、异常和不可恢复终态。不能把所有stream关闭都当暂停成功。
2. 安全收束后，用公开 `to_state().to_json(strict_context=True, include_tracing_api_key=False)` 导出完整原生JSON；在私有state保存未经语义改写的可执行快照，临时文件flush/fsync、原子替换完成后才确认暂停。原始快照留在可信私有存储，供报告读取的证据副本再脱敏；不能把替换了任务内容的脱敏副本当恢复输入。
3. 新容器重建同名、同定义的Agent、工具callable/schema及模型，核对完整快照的归属、完整性和同链scope，再使用公开 `RunState.from_json`，把state交给 `Runner.run_streamed(agent, state, session=同一SQLiteSession)`。不追加原prompt或新“Continue”指令；不以新attempt context覆盖原业务context。

RunState不是存活闭包、HTTP/MCP连接或凭证对象的快照；这些由可信配置重建。`strict_context`不校验快照所有权，SDK文档要求应用从可信存储加载并核对完整快照。`terminal_unrecoverable`标记存在时原生Runner会拒绝继续，不能手工删除标记或修改私有current_step绕过该边界。仅SQLiteSession历史不足以代替完整RunState。

### 6.3 两条路线在故障窗口和预算上的共同限制

| 请求/故障时点 | LangGraph drain的预期停点 | SDK after_turn的预期停点 | 实验须核对什么 |
| --- | --- | --- | --- |
| 模型还在生成时请求暂停 | 完成本模型节点，可停在下一tools节点前 | 完成模型响应及其待执行工具、保存本轮后停 | 实际在途工作、允许继续的工具、真实安全点和时延；两者都不是请求后立即停止 |
| 工具完成后提出暂停 | 到下一superstep开始前；可能已错过当前边界 | 到本轮结束前；也可能已进入下一轮 | 如实记录请求与实际停点，恢复使用原生进度，不由接入层重做整条任务 |
| 服务已写入但回执丢失，或结果还未落盘就退出 | 原生checkpoint与业务账本没有共同事务 | RunState/Session与业务账本没有共同事务 | 区分受控暂停与I23突然故障，按真实operation_id/receipt/账本判断已执行、未执行或未知；不能声称exactly-once |

当前HTTP工具接线把错误/副作用未知交给候选，不自动查询回执或重试整项业务。后续模型是否查询、重试或停止应保留原始证据；接入层不能替候选决定下一业务步骤来制造成功。服务端幂等防重另行归因。

两条路线均应沿用同一逻辑任务的[模型代理预算DB、token及cap](../../execution/model_proxy.py)，该预算在请求上游前原子reserve，包含失败和内部重试；新container/attempt ID不代表新预算。SDK保留max_turns也不等于限制累计API请求数。当前适配器进程内monotonic timeout不会自动跨容器累计，I22需最小可信链级活跃耗时记录：准备、执行和安全收束计入原300秒，600秒等待排除，恢复只获得剩余额度。无需为此建设通用任务状态机。

### 6.4 OpenClaw：保留HTTP任务入口，分别核对会话、Goal和重启恢复

本次只读核对固定OpenClaw `2026.10.5` / commit `b6e0872bdb7228e22fac7608345e0529a35ec46e`。现有[entrypoint](../../adapters/openclaw/entrypoint.mjs)仍通过`POST /v1/chat/completions`向`openclaw/lab-a`提交任务，用`user`及`x-openclaw-session-key`绑定原会话；后续控制入口不重发原prompt、不新建同名会话、不由外层程序安排剩余业务步骤。`i18-openclaw-native-state-001`已真实22/22项通过：旧Worker删除、同镜像新容器在network=none下只读原state/workspace，业务态及receipt来源相同，无模型/业务调用；它只证明冷读，详见[OpenClaw报告](openclaw.md)。以下接口尚未做本仓库暂停续跑验收。

| 机制 | 固定源码提供什么 | 不能据此宣称什么 |
| --- | --- | --- |
| 当前HTTP断连取消 | 已接取消路径，结束当前run | 显式Stop/cancel是终止，不能改名为暂停；同session再聊一轮不等于原任务恢复 |
| 主会话restart recovery | 原生记录未完成turn、来源、恢复claim和历史，新进程在有效权限下启动原生恢复turn | 不是任意指令位置恢复；只有transcript或SQLite不够，来源授权缺失/撤销可阻断 |
| `gateway.suspend.prepare/status/resume/handoff` | 整Gateway停止接新工作、暂停调度、检查持久写入custody并向拥有退出权的host交接 | 不是单session检查点；等待任务自然做完不算未完成任务暂停；resume lease也不等于恢复已终止任务 |
| 原生Session Goal | 持久目标/status/token信息；内置chat/auto-reply正常结束后可由原生队列继续目标 | active目标本身不会在启动后自动唤醒；不能把其他chat路径的自动Goal追踪默认算到当前HTTP入口 |
| `sessions.recover` | 对原生restart tombstone做受权限保护的原地恢复或新session恢复，并发起官方恢复turn | 不是通用resume任意历史；必须有原生恢复状态与生命周期核对，新session模式也不能算原session连续 |
| cron | 原生持久schedule及后续调度 | 已开始的cron occurrence不会由主会话恢复器重放；当前实验cron关闭，未来一次调度不证明原任务接续 |

**当前HTTP入口的授权缺口。** [OpenAI兼容执行路径](../../external/build-context/openclaw/sources/main/src/gateway/openai-compatible-agent-run.ts)创建[synthetic client](../../external/build-context/openclaw/sources/main/src/gateway/server-plugin-runtime-client.ts)，只传profile/actor/scopes，没有继承已有`operatorRunAuthority`。即便实时operator授权成功，[captureGatewayOperatorRecoverySnapshot](../../external/build-context/openclaw/sources/main/src/gateway/operator-run-authority.ts)也明确拒绝`syntheticClient`，还要求authenticatedOperator、authPolicy和有效gatewayAccessGrant；HTTP请求的live access grant不会自动变成该持久snapshot。没有snapshot/channelRecoverySource时，[captureQueuedOperatorRecoverySource](../../external/build-context/openclaw/sources/main/src/gateway/operator-run-recovery-source.ts)返回unavailable；没有operator authority时则返回undefined。因此不能套用“主会话重启会自动恢复”的总述，直接认定当前HTTP任务可自动续跑。`openai-user`会话key本身不被[主会话候选判定](../../external/build-context/openclaw/sources/main/src/config/sessions/restart-recovery-state.ts)排除，但key合格不等于持久授权合格。最小探针须记录真实原生恢复字段/原因，而非自己补写authority或伪造claim。

**整Gateway安全停止的实际范围。** [suspend coordinator](../../external/build-context/openclaw/sources/main/src/infra/gateway-suspend-coordinator.ts)默认只申请idle lease；`drain:true`会封住新准入并让现有工作继续，预算为45秒。状态轮询只报告custody。handoff要求相同pid/processInstanceId、有效lease、host `externalRestart`所有权，commit还检查pending持久写入；它可在清除写入custody后交接剩余工作，不能把handoff返回当作“当前业务工具已到安全点”。[CLI drain](../../external/build-context/openclaw/sources/main/src/cli/gateway-cli/run-loop-drain.ts)及[host run-loop](../../external/build-context/openclaw/sources/main/src/cli/gateway-cli/run-loop.ts)在交接或期限耗尽后会走原生中断/恢复标记。若任务全做完才ready，应记自然完成；若运行中被中断，应归入重启故障恢复，单独核对实际副作用，不能冒充已证明的单任务协作暂停。整Gateway退出会影响所有承载任务，其停机粒度和常驻成本必须进入多租户评估。

**公开的手动原生恢复值得探针，但有前提。** [sessions.recover handler](../../external/build-context/openclaw/sources/main/src/gateway/server-methods/sessions-recover.ts)允许新鲜授权控制方请求恢复；[service](../../external/build-context/openclaw/sources/main/src/gateway/session-recovery-service.ts)的in-place路径要求相同sessionId/recoveryCycleId、原生restart tombstone、没有活动工作，并用原历史和原生`SessionRestartResume`提交继续输入。[官方continuation](../../external/build-context/openclaw/sources/main/src/gateway/server-methods/session-recovery-continuation.ts)由框架自己发出固定恢复提示，随原生命周期patch提交，不由接入层重发整个任务。这是一条可单独验证的“人工明确授权后，由原生机制继续未完成任务”路线；仍须先证实当前HTTP任务在有序退出后确实留下可恢复tombstone，而非cancel终态或丢失任务。它不证明无人干预自动恢复，也不自动满足运行中安全暂停。自定义`harness_*`工具不能靠名称被默认视为replay-safe；官方[工具重放判定](../../external/build-context/openclaw/sources/main/src/agents/tool-replay-safety.ts)和[resume policy](../../external/build-context/openclaw/sources/main/src/agents/main-session-recovery/main-session-restart-recovery-resume-policy.ts)可能限制恢复工具，并让模型基于记录重新决定下一步；不得把副作用工具一律声明安全来绕过。

**Goal不替代入口核验。** 官方[Goal说明](../../external/build-context/openclaw/sources/main/docs/tools/goal.md)和[goal continuation](../../external/build-context/openclaw/sources/main/src/auto-reply/reply/goal-continuation.ts)确认持久目标及正常chat turn后的自主追踪；但自动nudge只由现有队列拥有，不是启动扫描器。公开[sessions.goal.update](../../external/build-context/openclaw/sources/main/src/gateway/server-methods/sessions-goal.ts)的resume经官方chat handler发起目标继续；这是原生公开入口，但会进入chat执行路径，不能声称当前HTTP `agentCommandFromGatewayIngress`已经有同样能力。Goal pause只抑制后续目标回合，不是当前在途工具的安全停点。若I21决定验证此变体，应保留初始HTTP任务入口，另外明确记录原生Goal控制/RPC、目标模型工具和恢复执行路径的变化；无运行证据前不算能力通过。

I21之后最小新增工作应限定为：可信控制RPC、原生恢复资格/状态/来源的观测、new/recover分支、Gateway退出/新容器编排、同链预算与活跃时间累计。当前adapter每次随机生成Gateway token并重写配置，未来若使用持久授权恢复，必须先核对该行为是否使原授权失效，不能直接照搬；凭证由可信控制方重建，不能写进证据。如果原生tombstone/授权或单任务安全边界不满足，就记录当前入口缺口及接入成本；若需改上游授权传递或新增安全边界扩展，单列研发投入，不建设外层任务状态机来替候选补全业务执行。

I21应比较各自最小原生入口的实际效果、透明度、接线成本和资源粒度；未实测项保持未验证。最终建议不以“所有框架都接上一套自研恢复器”作为共同通过前提。

## 2026-10-20 官方文档补核

OpenAI Docs 的 [Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents) 区分同一Session的下一轮对话与从运行state继续未完成的turn，并要求流结束后再判定执行已稳定；[Results and state](https://developers.openai.com/api/docs/guides/agents/results) 区分历史、最终输出与可续跑状态。因此I22不能只读取Session或把脱敏事件另存后重发原提示词。两页是接口语义参考，未给出本实验锁定Python版本的完整after_turn持久化流程；该具体接法仍依固定源码与Docker实测验收，不据文档直接记通过。模型、协议和本地部署口径不变。
