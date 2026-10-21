# 暂停后省资源，再换容器继续：I22 接入设计

日期：2026-10-20。本文记录接入设计，不是实验结果。I21已选DeepSeek Harness（DSH）与Deep Agents + LangGraph实施专项；OpenAI Agents SDK方案保留作参考，未实际接入I22。现有详细实验、镜像和评分材料保持原样，I22单独登记接入材料、镜像和结果。

这次要证明的事很具体：一个还没做完的工具任务，收到外部暂停请求后，自己走到能保存的地方；旧 Worker 退出并删除，等十分钟，再由新容器接着完成。恢复靠候选的原生进度，不能把整段任务重新发一遍，再凭最后答案正确就说恢复成功。浏览器现场不要求保留，本专项先使用冻结的 tools 任务。

三条路线的入口不同，不能都套成“保存聊天记录，再说一句继续”。DSH 适合用官方持久 Goal 继续目标；LangGraph 适合从已保存的下一节点继续；Agents SDK 适合保存并重新交回完整 RunState。共同编排只负责控制、保存、换容器、累计预算和独立验收，不替候选决定剩余业务步骤。

## 1. 当前代码有什么，还缺什么

| 候选与固定版本 | 当前已经接上的部分 | I22 必须新增的部分 | 最小成本判断 |
| --- | --- | --- | --- |
| DSH `0.1.2-alpha.3`，`dd6322d604e00eec1ba5e0c8541159906a21094a` | direct runtime；`agents.create/resume({setup})`；原生 JSONL 冷读；可信工具和 guard 重挂；`sessions.flush` | 官方 Goal、Goal driver、Goal 工具、checkpoint policy；公开步边界暂停；Goal create/resume 分支 | 三者中接线较多，会增加目标工具和提示；须用相同 Goal 变体做不暂停基线 |
| Deep Agents `0.7.28` / LangGraph `1.2.16`，提交分别为 `0089e635…` / `98043c37…` | `RunControl.request_drain`、实际 `GraphDrained`、`durability="sync"`、SQLite saver | 区分暂停与取消；可信 resume 分支；同 thread/checkpoint 传 `None` 继续；保存与重建验收 | 最接近现有控制入口；完整换容器续跑仍未验证 |
| OpenAI Agents SDK `0.24.0`，`64cd5dfc5a1579870dcbed0ea42b27aa84033fee` | 原生 `Runner.run_streamed`、SQLiteSession、immediate cancel、RunState 诊断导出 | 独立 `after_turn` 暂停；完整私有 RunState 原子落盘；`from_json` 后交给 Runner；同 Session 续用 | 公开接口齐备，但 after_turn → JSON → 新进程组合尚需实测，不能拿诊断导出代替 |

对应配置为 [DSH](/Users/hanbowen01/repo/puff/configs/candidates/deepseek_harness.yaml)、[Deep Agents](/Users/hanbowen01/repo/puff/configs/candidates/deepagents_langgraph.yaml)、[Agents SDK](/Users/hanbowen01/repo/puff/configs/candidates/openai_agents_sdk.yaml)。接口细节与源码链接继续保留在 [recovery-interfaces.md](/Users/hanbowen01/repo/puff/docs/validation/recovery-interfaces.md)。

有三处现成入口不能直接拿来运行 I22：

- DSH 的 [resume 分支](/Users/hanbowen01/repo/puff/adapters/deepseek_harness/scoped-runtime-entrypoint.ts:373)目前先恢复 idle Session，再选择是否提交一条明确的新 followup；没有 followup 就只检查状态。两者均不是未完成任务自动继续。现 profile 也没有加载上述四项官方插件。
- Deep Agents 的 [入口](/Users/hanbowen01/repo/puff/adapters/deepagents_langgraph/entrypoint.py:251)明确拒绝已有 checkpoint，正常运行始终提交新 user message，并为扩展任务新建 business_state。只删掉拒绝语句会把新 prompt 和空业务状态灌进旧图，属于错误接法。
- Agents SDK 的 [入口](/Users/hanbowen01/repo/puff/adapters/openai_agents_sdk/entrypoint.py:327)拒绝非空 Session；[finally 导出](/Users/hanbowen01/repo/puff/adapters/openai_agents_sdk/entrypoint.py:390)经过 `encoded()` 脱敏，并以普通 `write_text` 保存。此文件是诊断材料，不能直接当作可信、完整、已经 durable 的恢复输入。

## 2. 一条实验链怎样运行

先为每条路线准备一条干净的不暂停基线，再执行暂停链。两者使用同一种子、冻结 tools 任务、模型、工具契约和预算；各自使用独立业务 scope 和目录。DSH 的基线也必须加载同一组 Goal 插件，否则额外工具、提示和 round 成本没有可比对象。

暂停链固定为以下流程：

1. **只准备一次。** 建立逻辑任务 `chain_id`，生成一次场景、operation_prefix、工具绑定和模型预算。固定候选源码、接入摘要、镜像 ID、bench ID、工具定义和模型配置。
2. **Worker A 执行真实任务。** 宿主从原生事件和独立工具审计确认任务正在执行，再发送属于该 chain/attempt 的暂停请求。请求不能预埋在“第 N 步自动停止”的候选逻辑里。
3. **候选到安全点。** 等当前允许完成的工作收束、保存原生进度，给出可核对的原生证据。暂停请求返回只代表收到请求，不代表可以删容器。若已经自然完成、出错、OOM 或超时，保留实际分类。
4. **正常关闭，再删除 A。** 退出 saver/Session/runtime，登记退出、`OOMKilled`、状态文件清单和 hash；独立核对保存标记与关闭后的材料一致。关闭失败或状态不完整时，不继续宣布安全暂停。确认旧容器已删除，也没有旧候选进程继续运行。
5. **等待至少 600 秒。** 每 2 秒记录保留服务的资源与模型计数；没有候选 Worker。工具服务、fixture、模型代理等常驻资源单独记账。至少一条完整链在恢复前重建模型代理，用原预算 DB/token 验证预算不依赖代理内存。
6. **Worker B 先检查，再恢复。** 同一候选镜像、新 container ID，回挂同一 state/workspace/预算及业务 scope。先核对原生进度及完整性，不先调用模型。工具连接与凭证由可信配置重建，不能从任务文本中恢复。
7. **用原生入口继续。** DSH `goals.resume`、LangGraph `astream(None, ...)`、SDK `Runner.run_streamed(agent, restored_state, ...)`。记录实际传入类型；恢复分支不提交原 prompt 或新“继续”消息。
8. **验收最终业务和恢复过程。** 同时核对最终产物、完整业务账本、A/B 两段审计、已有写入是否重发、累计模型请求和活跃时间。完整链保留失败，不能只展示最终成功的一段。

正式关键路径按冻结种子 101/202/303 各执行一条完整链，共三次。先用 101 贯通入口，不能把这条预备运行自动算成三次验证。每条链的基线与暂停任务都须通过各自独立验收；三次材料与汇总分开保存。

为了真正考察重复写，正式触发条件应包含：**独立审计已经确认至少一笔业务写入完成，任务仍未完成，且原生还有活动**。恢复后检查这些已完成 operation_id 没有再次发出 write；同一任务剩余工作仍由模型决定。可先用早期模型活动做控制冒烟，但 0 笔写入的暂停不能支持“已写部分不会重做”。任务太快、请求落在自然完成之后时，记为未命中暂停窗口并保留证据，另用新 chain 重试；不重置原链预算，不延长或改简单任务来凑停点。

## 3. DSH：保存 Goal，在新进程继续目标

保留 direct scoped runtime 和可信 `setup`，新增一个明确的 Goal 变体；当前 sdk-minimal/I17 的结果不回填。新增官方插件 `dsh-goal`、`dsh-goal-round-driver`、`dsh-tool-goal`、`dsh-session-checkpoint-policy`，不改上游 Agent loop。配置和工具列表变化先冻结，再做该变体基线。

**开始。** 可信控制方 `goals.create(agent, {objective, maxGoalRounds})` 创建原任务目标，官方 driver 生成 round 输入。`maxGoalRounds` 在运行前固定，恢复不得增加；round 上限也不能代替总共 20 次 API 请求上限。候选通过官方目标工具自行报告 complete/blocked，外层验收只核对业务，不替它调用 complete。

**暂停。** 外部请求只设置本轮暂停意图。公开 `agent/pre-step` hook 在真正进入下一步前检查；调用下游异步 hook 后再次检查，防止等待期间收到的请求被漏掉。可在当前已开始的模型响应/工具完成后停，不用 `agent.cancel` 或进程信号强杀替代这个步边界。

在可用边界，按最新 Goal `{id, revision}` 调 `goals.pause`，阻止后续自动 round，再返回 `{kind:"reject"}`。原生 turn 结束原因应保留为 `blocked`，Goal 为 `paused/disarmed`，不能把原生事件改写成 paused。随后 `whenIdle()`、确认工具在途调用均已结束、inbox 无 pending，再在 live Session 上 await `sessions.flush` 成功。缺少真实 turn/end、flush 失败或发生已知的结束事件缺口时，都不能凭 idle 标签确认暂停。

**这里有一个必须收窄的范围。** [pre-step 实现](/Users/hanbowen01/repo/puff/external/build-context/deepseek_harness/sources/main/packages/core/agent-loop/src/agent.ts:234)调用 hook 前已经 claim 输入。最小路径只接受该 hook 参数中原始 claimed `messages` 为空、没有并发 followup/steer/inject、inbox 为空的单任务；这里检查的是原始 claim，不把下游追加的 runtime context 算作新用户输入。否则 reject 可能丢掉刚 claim 的输入。若命中的是另一条 Goal round 或普通消息，明确记“该停点不在已验证范围”，不能静默丢弃。后续 dispose 默认还会清空 inbox，即便之前 cancel 使用 keepInbox，也不构成队列保存证明。

**保存和恢复。** A 记录 Goal id/objective/revision/phase/roundsStarted、Session header/seq、工作区清单、最后原生事件与 flush 来源；正常关闭后保留完整原生文件。B 用 `agents.resume({resumeSessionId, agentOptions, setup})`，在发布前重挂相同工具、guard 和投影，核对 Goal 与原 Session 一致、activation 为 disarmed。可信恢复请求再调用 `goals.resume`，由官方 driver 开启后续 round。恢复输入由该原生机制产生，不另调用原任务的 `followup`。

这一条验证的是“持久目标 + 步边界协作暂停 + 新进程继续目标”，比恢复旧 turn 的指令位置更宽。模型可能重读文件或查询回执，额外请求和 token 必须记录。checkpoint policy 在模型和顶层工具派发前 flush，也没有把业务提交和 JSONL 组成一个事务，不能声称 exactly-once。官方 [Goal driver](/Users/hanbowen01/repo/puff/external/build-context/deepseek_harness/sources/main/packages/goal/goal-round-driver/src/index.ts:137)和 [checkpoint policy](/Users/hanbowen01/repo/puff/external/build-context/deepseek_harness/sources/main/packages/session/session-checkpoint-policy/src/index.ts:63)继续承担这些原生行为。

## 4. Deep Agents：drain 后从下一节点继续

沿用现有 [NativeRunControl](/Users/hanbowen01/repo/puff/adapters/deepagents_langgraph/run_control.py:39) 和 SQLite saver，新增独立的 pause action/reason，不能把当前 `experiment_cancel` 记录事后改名。暂停请求仍由所属事件循环调用公开 `request_drain("experiment_pause")`；不取消 asyncio 主任务，不中断 HTTP 工具来制造保存点。

**A 的安全点。** 原生当前 superstep 完成，下一 superstep 前实际抛出 `GraphDrained`；`durability="sync"` 完成 checkpoint 保存。读取真实 snapshot，记录 thread_id、checkpoint_id、values 摘要、`next`、tasks/errors/interrupts，以及流中已开始但未结束的调用。需要保留明确的下一节点，否则可能是已经自然完成；异常、interrupt 或超时单独分类。退出 SQLite saver 后再登记完整 state 目录，不在连接仍打开时只复制主 SQLite 文件而漏掉 WAL。

**B 的最小分支。** 在 [现有拒绝旧 checkpoint 的位置](/Users/hanbowen01/repo/puff/adapters/deepagents_langgraph/entrypoint.py:251)增加可信 new/resume 判断。new 分支仍要求空状态；resume 分支要求相同 graph/tool/schema/model、同 thread、namespace 与保存的 checkpoint。持有原 chain 独占执行权，先用带指定 `checkpoint_id` 的 `aget_state` 精确核对保存材料，再用不带 ID 的配置读取当前 head，确认它仍等于保存的 ID、内容与暂停状态；不接受“历史 checkpoint 还存在”作为当前恢复依据。新建 RunControl，实际执行 `graph.astream(None, config={"configurable":{"thread_id":原thread,"checkpoint_ns":原namespace}, ...}, durability="sync", control=新control)`，执行配置不携带 `checkpoint_id`。固定版本的 [_loop.py](/Users/hanbowen01/repo/puff/external/build-context/deepagents_langgraph/sources/langgraph/libs/langgraph/langgraph/pregel/_loop.py:952)会将显式 ID 加 `None` 识别为 time travel，可能走 fork/replay；不能把带 ID 的 snapshot.config 直接用于执行，也不设置私有内部标志绕过。核查到执行期间继续持有 chain 独占，防止 head 被另一执行者推进。不再生成 user message，不再初始化 business_state，不调用 `update_state` 改节点。

要区分两种“pending”：checkpoint 的 `next` 和未来 task 可以存在，它们正是要继续的工作；当前在途 task、未结束的工具/模型请求必须为零。不能要求 snapshot 没有任何下一任务，也不能只看 snapshot 有下一任务就推断旧进程已经安全。真实动态 interrupt 才使用 `Command(resume=...)`，drain 恢复不伪造审批答案。

最低证据链为真实暂停请求 → 对应 reason 的 GraphDrained → durable checkpoint → saver 正常关闭 → A 删除 → B 在调用模型前读到同 checkpoint → 输入 `None` → 下一节点实际执行 → 最终 tools 验收。图状态保存与业务服务仍不是共同事务；服务端幂等不能替代“没有重复发出已经完成的 write”的专项检查。

## 5. Agents SDK：after_turn 后保存完整 RunState

新增与现有 SIGUSR1/immediate cancel 区分的 pause 通道，调用公开 `result.cancel(mode="after_turn")`。公开接口允许当前模型响应完成并执行该响应排队的工具、保存 Session 后再停；所以请求之后仍可能发生写入，不能把这些正常收束工作说成违规。必须继续消费同一个 `stream_events()`，直到原 stream 正常收尾、run loop 结束并重新抛出任何错误；取消 stream 消费任务会走 immediate 路径，不能这样暂停。

**何时可以保存。** 收到 after_turn 请求、原流已收束、没有 loop exception、没有未结束的模型/工具调用，且原生状态保留继续步骤。已有原生自然完成终态时应记自然完成，即使 final output 为空也不能改称暂停；出现审批 interruptions、异常或不可恢复标记时单独记录，不统一改标 paused。`is_complete=true` 只表示这次 stream 结束，不代表业务已完成或一定可恢复。

调用公开 `result.to_state().to_json(strict_context=True, include_tracing_api_key=False)`，将完整、未经语义改写的 JSON 保存在私有 state：同目录临时文件写入、flush/fsync、原子替换、目录 fsync，然后才确认保存。Session 正常关闭后登记 SQLite 及完整 state 清单。可展示的证据副本再脱敏；B 使用私有快照，不能使用当前 `encoded()` 生成的诊断副本。若 context 不是 mapping，需明确 serializer/deserializer，不偷偷丢弃内容。

**B 的最小分支。** 重建同名 Agent、工具 callable/schema/路由、模型与同一 SQLiteSession；先检查完整快照归属、hash、chain/scope、版本与配置，再 `await RunState.from_json(agent, full_state, strict_context=True)`。把返回 state 交给 `Runner.run_streamed(agent, state, session=原Session, hooks=新观测hooks, run_config=相同配置)`，不传新的原始 prompt，不用新 attempt 的 context 覆盖旧业务 context。RunState 内的 current_turn/max_turns 和已保存项目计数由原生 Runner 延续，不能手工清零。

RunState 保存的是可重建的执行状态，不保存活的 Python 闭包、HTTP 连接和凭证。它的 [from_json 契约](/Users/hanbowen01/repo/puff/external/build-context/openai_agents_sdk/sources/main/src/agents/run_state.py:2423)要求应用核对完整快照的可信来源；strict_context 不负责身份认证。原生拒绝 `terminal_unrecoverable` 或不兼容状态时保留失败，不删标记，不修改私有 current_step 绕过。当前桥接使用 FunctionTool；若以后改为原生 MCP，需额外保持 pending 调用的 recipient bindings 和原服务器配置。

OpenAI Docs 的 [Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents)也区分结束流、下一轮对话与从 state 继续未完成运行。该在线文档只作概念参照；after_turn、序列化和跨进程的实际行为以仓库锁定版本的 [cancel 实现](/Users/hanbowen01/repo/puff/external/build-context/openai_agents_sdk/sources/main/src/agents/result.py:941)、RunState 和本专项实测为准，不能因文档有 state 就宣布组合已经通过。

## 6. 同一任务，预算和写入也必须接得上

**逻辑任务 ID 与尝试 ID 分开。** `chain_id` 固定为 scope 的 variant，session_id 不变，预算路径也不变；attempt_id 仅区分 worker-a/worker-b 的容器与证据目录，不进入业务 operation_prefix，不成为新预算。当前 [Detailed prepare](/Users/hanbowen01/repo/puff/runner/detailed.py:73)每次都要求新目录、创建场景并生成新 token，不能在 B 再跑一次。新专项只能在 chain 创建时 prepare，一次恢复不得重置场景、账本或工作区。

**请求也分 new 和 resume。** 只有 new 请求可提交原 prompt；resume 请求只接受原 chain 的绑定与原生状态引用，不接收新的任务文本，不能在恢复失败时回退到 new。暂停使用独立通道，例如绑定本 attempt 的 SIGUSR2 → pause intent，保留当前 SIGUSR1 的取消含义；拒绝启动前、已结束、重复或不属于本 attempt 的请求，不把它们排队影响未来任务。

**复用原模型预算。** A/B 使用同一个 calls.sqlite、run.token、cap=20；[代理](/Users/hanbowen01/repo/puff/execution/model_proxy.py:39)已经将 DB 与 token hash/cap 绑定，并在上游请求前原子 reserve。B 只重新挂载/启动，不创建新的预算文件；用预算前后行号/数量和实际请求验证延续。失败和模型内部重试也计入 20 次。`max_retries=2` 保持原配置，不额外加整任务重试或工具自动重试；恢复本身不是重新发放一份 20 次额度。SDK max_turns、DSH round 数均不能替代 API 计数。

**累计活跃时间。** 当前入口的进程内 monotonic deadline 会在 B 重新开始。专项由可信编排追加记录 A/B 的候选准备、执行、安全保存和退出时间，累计不得超过 300 秒；600 秒暂停等待与独立评分不算候选活跃时间，恢复冷启动算。B 仅获得剩余额度，同时由宿主硬期限约束；没有剩余时间就拒绝恢复。用 chain 私有记录即可，无需另建通用任务状态数据库。

**防止两个 Worker 同时接同一任务。** 启动 B 前独立确认 A 已退出删除；同 chain 同时只允许一个执行者。恢复标记包含原镜像、scope、checkpoint/Session/RunState hash 和已用预算，检查失败即拒绝发起模型。hash 本身不认证来源，归属来自可信编排和私有存储；候选自写的 `paused.json` 仅作索引，不能单独作为通过证据。

**独立核对副作用。** 把 A/B 两段工具审计合并，保持不同来源，不覆盖旧文件；共享工具服务账本贯穿整条 chain。最终继续复用 `grade_tools` 重算结果，另加恢复前已完成 operation_id 在 B 不得再次发出 write 的检查。仅最终 write_count 没增加还不够：幂等服务可能拦住了重复请求。允许候选读 receipt/ledger 来核对；接入层不替它查询和决定下一笔业务操作。

若服务已提交但回执或本地结果没有保存，属于 I23 的不确定窗口。I22 的受控安全暂停先要求已派发调用全部收束，不把未知结果强行算安全保存；I23 再分别测试幂等和非幂等服务、自动恢复和转人工。三条路线都不凭这次专项声称 exactly-once。

## 7. 文件放哪里，哪些代码最少要改

以下接线代码已开始落盘，正在进行无模型校准及镜像核对；尚无真实暂停续跑通过结论。优先复用已有公共工具、判定器和 Docker 编排；候选原生状态保持各自格式。

| 文件/目录 | 作用及最小改动 |
| --- | --- |
| `configs/tasks/pause-resume.yaml` | 继续使用现有冻结协议，不为某一候选降低 600 秒、预算或三次关键路径要求 |
| `scripts/run_pause_recovery.py`（拟新增） | 宿主只负责一次 prepare、A/B Docker 生命周期、外部暂停触发、等待采样和硬期限；不运行 Agent 或业务评分 |
| `runner/pause_recovery.py`（拟新增） | 在 bench 容器内 prepare/grade；绑定 chain/attempt、校验候选原生证据、合并审计，并复用 `grade_tools` |
| `compose/pause-recovery.override.yml`（拟新增） | 同链 state/workspace/budget 与分 attempt 输出挂载；可信控制只读；候选看不到预期答案和 host/bench 材料 |
| `adapters/deepseek_harness/` | 增加独立 Goal profile patch 和 pause control；在 direct runtime 中明确 Goal create/resume 分支。登记新变体与材料摘要 |
| `adapters/deepagents_langgraph/entrypoint.py`、`run_control.py` | 明确 new/resume、pause/cancel 分支；resume 输入 None，保留 saver 和 sync durability；不用外层状态覆盖图 |
| `adapters/openai_agents_sdk/entrypoint.py`、新增 pause helper | 独立 after_turn 控制；完整私有快照保存；可信 RunState resume；诊断导出与可执行快照分开 |
| `tests/` 下专项校准 | 校准错误 scope、错镜像、残损快照、缺安全终态、预算重置、重复 write、自然完成冒充暂停、OOM 退出等反例；校准不算候选运行通过 |

数据继续放在 `/Users/hanbowen01/harness-lab-data`，不另起实验根目录。按 `paths_for("pause-recovery", chain_id, candidate, "alice", "main", chain_id, case="pause-resume")` 得到同链目录；attempt 放在该 run 内部，避免改变已有路径函数和历史材料：

```text
state/pause-recovery/<chain>/<candidate>/alice/main/     候选完整原生状态；SDK 私有可执行快照也在这里
workspaces/pause-recovery/<chain>/<candidate>/alice/main/  同一个业务工作区
budgets/pause-recovery/<chain>/<chain>/                同一个模型预算 DB/token
fixtures/pause-recovery/<chain>/                      一次性场景与可信绑定、只读控制文件
runs/<date>/<candidate>/pause-resume/<chain>/
  chain.json                         宿主/bench 所有的配置、绑定和累计时间，不含 key
  attempts/worker-a/candidate/        A 原始事件和结果
  attempts/worker-b/candidate/        B 原始事件和结果，启动前为新空目录
  trusted/                           外部请求、容器信息、工具审计和资源采样
  pause-index.json                   指向原生安全点和 durable 状态的索引
  verdict.json                       整条链独立验收；失败不覆盖
```

实际路径以 [paths_for](/Users/hanbowen01/repo/puff/runner/config.py:144)返回值为准；state/workspace 包含 candidate/user/session，fixture 归属于 experiment/variant，不能另手拼出不同绑定。基线使用另一 chain，按种子关联对照，不共享预算和业务状态。

## 8. 验收怎么判，结果怎么说

| 要检查的能力 | 必须看到的证据 | 不通过或不能支持结论的情况 |
| --- | --- | --- |
| 真正在运行中请求 | 宿主请求、同 chain/attempt 身份、真实未结束活动的原始来源和时序 | 启动前请求、任务已自然完成、只凭 signal 返回 0 |
| 走到候选安全点 | DSH blocked turn + paused Goal + idle/空 inbox + flush；LG GraphDrained + sync checkpoint；SDK after_turn 原流正常收束 + 可继续 RunState | 直接 kill、超时兜底、仅外层 paused 字段、仍有在途模型/工具/后台循环 |
| 状态真实持久 | 原生文件、完整清单/hash、正常关闭、B 启动前读回与暂停标记吻合 | 只有 prompt/聊天记录/业务结果；脱敏文件充当恢复输入；遗漏 SQLite/WAL；保存失败后照常确认 |
| 计算资源已释放 | A 退出并删除、等待 600 秒无旧 Worker；保留服务逐项采样 | 候选进程仍在后台完成任务，或把共享服务成本隐藏掉 |
| 接回原进度 | 新容器同镜像；DSH 原 Goal、LG 原 checkpoint、SDK 完整原 RunState；实际恢复 API/输入记录 | 新 Session、重发 prompt/Continue、新业务前缀、重置业务 context/图节点 |
| 业务结果与副作用正确 | `grade_tools` 通过；全链独立账本、审计、receipt、已有 operation_id 未被 B 重写 | 最终结果正确但重复业务请求被服务幂等掩盖；在途结果未知却宣称成功 |
| 预算和规则延续 | 同预算 DB/token/cap、A/B 总请求、300 秒活跃累计、同工具 guard | 新容器得到新预算、放宽重试、换 scope 绕过权限，或恢复后补建被禁写入 |
| 退出与环境稳定 | 每个候选容器真实 ExitCode/OOMKilled、原生异常、清理结果与容器时间线 | 只按预期退出码判正常；OOMKilled=true 却写稳定通过 |

每条链输出三个分别的结论：原生暂停/恢复链是否成立、业务是否正确、资源/预算是否符合要求。整条 I22 通过必须三者都满足，并完成三次关键路径；部分通过保留具体缺口，不靠平均分掩盖。DSH 可以如实称“继续同一个持久目标”，LG 称“继续 checkpoint 的下一节点”，SDK 称“继续完整 RunState”；这些都不等于恢复任意内存位置、撤销在途写入或已经解决多租户共享 Worker。

与不暂停基线并排记录：总请求/输入输出 token、额外工具与 receipt 查询、请求到安全点延迟、停止保存耗时、600 秒等待期各服务内存/CPU、B 冷启动到首次继续/完成的延迟、额外业务请求及人工改动。缺可靠模型单价时金额仍记未知。一次性暂停的资源释放只支持这个粒度的结论，多用户共享与实例总成本仍归 I24/I25。

## 9. 后续执行任务表

本表保留I22设计与执行对应关系。两路线三种子已执行结束；原生恢复与完整业务结果分别记录，根目录任务表为唯一进度入口。

| 步骤 | 任务 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| P00 | 核对三条原生接口与当前入口，整理本设计 | 原始 API、现有拒绝/诊断边界、额外接线及伪恢复风险均有源码出处 | 已完成，只读 |
| P01 | I21 选定实际运行路线，冻结对应 I22 变体 | 每条路线的 profile/入口/工具面、源码及镜像构建材料、任务/预算和触发条件有清单；不覆盖 I17–I20 材料 | 已完成：DSH/Deep Agents独立上下文、实际镜像及工具面已固定，见pause-recovery.md |
| P02 | 新增一次 prepare 的 chain/attempt 编排与私有绑定 | B 不创建新场景/预算；错 scope/镜像、双执行者、无剩余时间均在模型前拒绝 | 已完成：一次prepare/原预算/私有绑定经校准，并在六条真实A/B链验收 |
| P03 | 接入每条选定路线的原生暂停与恢复 | 使用上述公开入口；不重发原 prompt，不改私有执行位置；保留原生状态和实际异常 | 已完成：两条公开原生接线及容器校准、六条真实恢复结束；能力边界见pause-recovery.md |
| P04 | 建立 bench 专项判定并校准正反例 | 自然完成冒充暂停、残损状态、只读聊天历史、仍有在途工作、预算清零、重复 write、OOM 等反例不能通过 | 已完成：公共720项、宿主/bench36项和DSH42项容器校准；只确认边界与判定器 |
| P05 | 构建并固定专项实际镜像，先跑无模型校准 | 构建日志、实际镜像内入口校验、材料摘要齐全；校准结果与候选能力分开 | 已完成：DA/DSH固定镜像，DA真实原生无模型新进程续跑和DSH零模型工具面通过；详见pause-recovery.md |
| P06 | 同变体完成 seed 101 不暂停基线与控制冒烟 | 基线工具任务通过；能真实命中未完成任务并保存安全点；不提前宣称三次恢复通过 | 已完成调研步骤：六基线49请求/18写，DA101分页失败，其余五条通过；运行中暂停控制在六条真实链成立，原评分/复核分列 |
| P07 | seed 101 完整执行暂停、删除、600 秒等待、B 继续 | 全链原生、业务、预算和资源四类证据完整；至少已有一笔写入可检查恢复后不重发 | 已完成调研步骤：DA101004原生、资源/预算与不重发通过，但分页规则失败；DSH101002追加离线全链复核通过。所有实施中止与原评分保留，见pause-recovery.md |
| P08 | 完成 202/303，至少一条链重建代理 | 三条正式关键路径逐条验收；同预算跨代理/容器累计，所有失败与未命中请求保留 | 已完成调研步骤：DA202原生通过但分页业务失败，DA303及DSH202/303完整通过；六条均重建代理、原预算连续、无已完成写入重发 |
| P09 | 与基线比较并回写专项报告/实施计划状态 | 说明能力、适用粒度、自研工作量和资源代价；I22 与 I23/I24/I25 边界清楚，未测项仍标未验证 | 已完成：pause-recovery.md、按阶段pause-resource-summary.json及根目录任务表；DSH业务3/3、DA1/3，不推导生产收益 |
