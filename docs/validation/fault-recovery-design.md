# I23 故障验证实施设计

日期：2026-10-20。**I23 已进入无故障基线，尚未确认任何候选具备故障恢复能力。** 本文保留实施设计，并在末节记录基线发现的接线修复；旧镜像、旧结果和旧失败证据均保留。总进度仍以 [实施任务表](../../IMPLEMENTATION_PLAN.md) 为准。

要回答的问题很具体：**模型或工具偶尔失败时，Agent 能不能把事情继续做完；进程突然没了、或者业务已经成功但没收到回执时，它会不会漏做、重复提交，或者把不知道的事说成成功。** 不在这里建设生产调度系统，也不增加审批流。

## 1. 本轮测什么，为什么这样测

沿用 [故障任务](../../configs/tasks/fault-recovery.yaml) 的普通工具任务、固定模型、300 秒累计活跃时限、20 次累计模型请求及原来的最多两次自动重试配置。任务仍是查全商品、按条件选三件、写入、生成报告；故障不会告诉 Agent 正确答案。

| 故障 | 现实中对应什么 | 要分清的问题 |
| --- | --- | --- |
| 第一次模型请求返回 504 | 模型服务暂时不可用 | 原生重试是否发生，是否仍遵守总预算；重试失败能否查清原因 |
| 第一件预期业务操作写入前返回一次 503 | 工具服务偶发失败 | 能否重试并完成；是不是没有写入却报告成功 |
| 第一件预期业务操作发送前，Worker 被杀 | 执行进程突然退出 | 新 Worker 能否从已经落盘的原生进度继续；不能临时补保存点 |
| 第一件预期业务操作已经提交，但连接断开、回执丢失 | 最容易重复扣款、重复下单的窗口 | 是否先查实际记录，再决定下一步；有没有盲目重发 |

每种故障分别跑 **幂等服务** 和 **非幂等服务**。幂等服务收到相同 operation_id 的重复写会返回旧回执；非幂等服务会真的多写一笔。两者都要数请求和真实写入，才不会把服务替它兜底误当成框架不重发。

这里的 `model_timeout` 沿用任务已冻结的“计数代理第一次请求立即返回 504”。它验证收到超时类错误后的处理，**不是实测远程模型长时间不响应、流式响应中断或所有网络故障**。若以后要测这些窗口，另建变体，不能偷偷改变这组输入。

## 2. 现成材料有哪些，缺什么

| 当前材料 | 已有能力 | I23 仍需补齐 |
| --- | --- | --- |
| [计数代理](../../execution/model_proxy.py) | 指定请求序号返回 504；发往模型前记账；数据库跨容器保留 | 将相同故障放进八个明确变体，记录原生重试与代理真实请求的对应关系 |
| [业务服务](../../fixtures/service/server.py)、[faults.py](../../fixtures/service/faults.py) | admin 指定 operation 的 `fail_once`、`drop_receipt_once`；故障消费与写入在 SQLite 事务中持久化；两种写入模式 | 没有“杀 Worker 前已经到达准确写前窗口”的接线 |
| [工具服务](../../execution/tools/server.py) | 每个实际工具调用的可信 start/finish；写后断连接会返回 `502 + side_effect_state=unknown`；不会替候选重试或查询回执 | 故障前发送栅栏、跨 attempt 的可信证据及统计 |
| [公共验收](../../runner/grading.py) | `grade_tools` 独立算预期，核对报告、分页、账本、作用域、请求和实际写入 | 故障命中验证、原生恢复路径、重复请求与结果不确定的独立分类 |
| [详细编排](../../scripts/run_detailed.py)、[准备与评分](../../runner/detailed.py) | 一次任务的容器、镜像、预算、资源与评分记录 | 当前每次 prepare 都新建场景、token 和预算；不能直接调用两次冒充同任务故障恢复 |

`fail_once` 确实在写入前返回 503，没有业务写入；`drop_receipt_once` 则在事务提交之后关闭连接。后者账本中该次写请求的 status 为 0，**不是业务没有发生**。工具服务之后给候选的 502 也只是“回执不可得”，不能改写成“写入失败，可以放心再写”。

现有工具描述和任务提示已要求“回执不确定先核对实际记录”。这是共同业务约束，保留给所有候选。候选是否遵守，要看真实调用；不能由适配层捕获 502 后自动查询 receipt、代替它决定是否重写。

## 3. 最小运行流程

只对 I21 选中的 2–3 条专项路线执行。候选串行运行；全部 Agent、工具、模拟业务、故障服务和评分都在本地 Docker 镜像中。宿主只控制 Docker 生命周期、发送实验控制请求和保存 Docker 观测，不在宿主运行候选或评分。

1. **一次准备一条链。** 为 candidate/fault/write_mode/seed 创建新的 chain，冻结任务、镜像、模型、工具 schema、预算、触发条件和原生恢复入口。只 prepare 一次，生成一个业务 scope、一份工作区、一份原生状态和一份累计模型预算。
2. **可信 bench 设置故障。** 从 seed 独立计算任务应该选择的第一件商品及 operation_id，只交给可信故障设置端。Agent 仅得到原目标、request.json 和原工具，不得到预期商品清单、故障种类、触发 operation 或验收答案。
3. **运行 Worker A。** 模型超时、工具 503、写后丢回执三类先观察候选在原进程中如何处理。提交前退出类由外部控制器在精确窗口杀 A，不先调用暂停或保存接口。
4. **需要换 Worker 时只创建 B。** 同镜像、同原生存储、同 scope、同工作区、同预算，使用已经查清的原生 crash/recovery 入口。不能重新 prepare、重发原始任务提示、注入“接着下单”、重置业务状态，或把控制器算出的剩余步骤交给 B。没有这种入口就记录恢复缺口，不偷偷退化成重跑。
5. **停止执行，独立验收。** 先停止付费模型请求，再收集完整工具审计、业务账本、原生事件、原生状态及容器状态；bench 输出故障是否命中、是否完成、请求/写入次数和结果分类。

故障等待和重启时间计入同链 300 秒活跃预算，A/B 共用同一 calls.sqlite、run.token 和 cap=20；I23 没有 I22 的 600 秒等待。原来的模型客户端自动重试上限不提高，不叠加外层整任务重跑或自研工具自动重试。模型自行选择再次调用 write 时，另记业务请求次数，不伪称它是 SDK 传输重试。所有重试导致的模型调用仍计入 20 次。

配套服务与临时 bench 的配额都计入 6 GiB／3 CPU，至多两个活跃任务；本专项默认一次只跑一条链。正式运行期间不重建相关镜像、不移动标签、不改适配材料和评分规则。

## 4. 提交前退出，必须先把窗口钉准

**不能看到一条 tool start 就立刻 kill，然后宣布“肯定还没写”。** 公共工具服务在独立容器中，候选死了以后它仍可能把在途 HTTP 请求提交给业务服务。只看 kill 时间和日志时间也不足以排除竞争。

最小拟接法是在**候选容器自己的实际 write HTTP 发送前**加一个实验栅栏（barrier）。它只制造故障窗口，不保存业务进度、不计算下一步，也不参与正常业务恢复。

- 可信 bench 在工具服务私有绑定中设置本 attempt 的目标 operation、一次性栅栏与凭证。目标不进入工具 schema 或模型提示。
- 候选的薄 HTTP 桥在调用 `POST /call` 之前，向固定工具服务的实验 prewrite 端点报到。非目标调用立即继续；目标调用被服务阻塞，候选的同一调用栈尚未发送原 write。
- 栅栏服务先持久记录完整 scope、chain/attempt、实际参数摘要、桥接调用 ID、命中时间和一次性状态。控制器收到可信命中后，重新核对运行中的精确 Worker ID/镜像，再 `SIGKILL` 该 Worker。
- 控制器独立保存 kill 请求和 Docker 结果：A 已退出、PID=0、不是 OOM、ID 正确。确认 A 不再运行并删除后，废弃 A 的栅栏等待；**不向仍活着的 A 放行**。旧调用栈已经不存在，所以不会由它稍后发出 write。
- 使用独立业务账本和公共工具审计证明：目标 operation 在 A 段没有 `POST /call` write，没有业务 write 请求，也没有真实写入。其他 operation 是否已发生，逐项保留，不能假设并行派发从未发生。
- A 退出后，独立工具服务可能仍在处理其他 operation。B 启动前先确认这些实际请求已经收束并保存恢复前账本；无法确认时不启动 B，记录在途结果未知，不能只按 A 已退出推断全链已经静止。
- B 沿用原状态，A 的栅栏不得再次触发；B 是否执行这笔尚未提交的操作由候选原生恢复机制决定。

栅栏的 setup、查询和废弃端点只给可信 bench/control；候选只能使用绑定当前 attempt 的报到凭证，没有设置故障、解锁别人或查询预期答案的权限。栅栏状态与事件在候选不可写位置。不能仅靠候选自己输出“已到写前”作为命中证据。

这是“业务请求发送前的 Worker 突然退出”，不覆盖业务服务接收请求后任意微秒内的所有崩溃。那个更危险的“已提交但客户端不知情”窗口由写后丢回执一项单独覆盖。

拟接入位置如下，均在正常参数/身份检查及执行规则之后、真实 HTTP 发送之前：

| 候选 | 实验接线位置 | 需要保留的事实 |
| --- | --- | --- |
| DSH | [tools-plugin.mjs](../../adapters/deepseek_harness/tools-plugin.mjs) 的 execute、fetch `/call` 前 | 公开 execution.callId 与实际 tool/call 来源；不为这次 kill 额外 flush 原生 session |
| OpenClaw | [工具插件 execute](../../adapters/openclaw/tools-plugin/index.mjs) 的 fetch 前 | 官方 callId、agent/session 绑定、实际 before_tool_call；不调用 agent.cancel 或正常 Gateway shutdown 代替突然退出 |
| Deep Agents | [HttpTools.forward](../../adapters/deepagents_langgraph/http_tools.py) 的 client.post 前 | 原生 ToolNode/工具调用与桥接请求分别保留；当前基础路径的 uuid 是桥接 ID，不能冒充原生 tool_call_id |
| Agents SDK | [HttpTools._forward](../../adapters/openai_agents_sdk/http_tools.py) 的 client.post 前 | 实际 context.tool_call_id 及原生 hook；没有原生 ID 时明确记录缺失，不伪造对应关系 |

新栅栏只出现在 I23 镜像变体中，正常基线使用同一镜像、栅栏关闭。其额外 HTTP 请求和少量时间单列为实验开销，不算业务工具调用，也不说成框架能力。

## 5. 突然退出后，各候选先查什么

**I22 的安全暂停成功，不等于 I23 的突然退出也能恢复。** I22 可以主动收束并保存；本项只能使用 kill 之前自然持久化的东西。不能在 SIGKILL 后找到一个更早安全快照，就不说明地改成“从精确故障点接回”。

| 路线 | 当前已知材料与边界 | I23 最小验证 |
| --- | --- | --- |
| Deep Agents | SQLite saver、sync durability 会自然产生 checkpoint；当前入口拒绝旧 checkpoint，尚未接真实续跑 | 复用 I22 的可信 resume 分支，但读 kill 时实际留下的 checkpoint，保留其 next/tasks/pending writes。新 graph 用原 thread/checkpoint、输入 None；不改节点和消息。观察原生重放了哪些操作，不能预设不重放 |
| DSH | 已有 agents.resume 冷读；已落盘 session、Goal driver 的继续语义与原 turn 中断不是同一件事 | 核对实际 profile、原 Goal/session、未结束 turn/tool、已 claim 输入。只按锁定版本公开恢复语义运行；若只能继续持久目标，报告该粒度及可能重做，不声称恢复任意执行位置 |
| Agents SDK | 当前 RunState 是诊断材料；拟 I22 的 after_turn 完整保存依赖活着的进程 | kill 前若没有可执行的完整原生 RunState，就不能在 B 用 SQLiteSession 聊天历史或脱敏 JSON 充当未完成运行恢复。记录 crash durability 缺口；若补周期性完整保存，另登记业务接入成本和变体，并重跑基线 |
| OpenClaw | HTTP 任务入口、原生 session 存储和恢复相关 API；尚无已证实的运行中任务 durable 暂停/恢复链 | 针对锁定入口查明崩溃后哪些内容能重建、是否真正继续原任务。重新发送 HTTP prompt 或只读出历史不算自动恢复；没有可用入口时如实结束这一格 |

这张表是待验证方向，不是对框架支持或不支持的最终判定。I21 如只选其中部分，只实施实际选中路线。

## 6. 怎么判“没重复”，怎么判“结果不确定”

每个 operation 单独保留：A/B 各自 write 工具调用数、业务服务实际 write 请求数、真实 write_count、receipt/ledger 查询次数与顺序、原生调用/终态来源。模型请求另由计数代理核对。共同工具服务和适配桥都不加自动重试，所以它们不能悄悄制造或消除重复请求。

| 观测 | 可以说什么 | 不能说什么 |
| --- | --- | --- |
| 丢回执后查询 receipt/ledger，核对真实记录，没有再次请求该 operation 的 write，最后任务正确完成 | 这个变体在这个窗口中先核对结果，避免了已提交操作的重发；说明决策来自原生机制还是模型读工具结果 | 对任意故障提供 exactly-once 保证 |
| 幂等模式同 operation 请求两次、实际写一笔，第二次返回 deduplicated | 业务最终防重有效，防重来自服务；候选发生了重发 | 框架保证只提交了一次 |
| 非幂等模式同 operation 请求两次、实际写两笔 | 已复现重复业务副作用；即使最终报告只列一笔仍然失败 | 用报告正确或平均成功率掩盖重复 |
| fail_once 后同 operation 两次请求、一次 503 未写、一次成功写入 | 正常故障恢复重试，不是重复提交已成功操作 | 所有“请求两次”都是错误；或者把这类重试算成服务防重 |
| 写前 kill，A 的目标操作零请求零写，B 请求一次并正确完成 | 此精确写前窗口的换 Worker 恢复成立 | 所有写入中的崩溃都可恢复 |
| 候选明确停止并指明哪些操作结果未知，没有继续提交；独立账本可核对实际状态 | 明确转为待确认，记录由候选还是薄业务接入层提供该状态 | 自动恢复成功；或者业务一定没发生 |
| 进程崩溃、预算用完、没有产物，也没有明确的不确定信息 | 未完成或错误，记录已知副作用与缺证据 | 自动转人工；或者把沉默当成安全确认 |

写后丢回执项还要检查查询的**内容和时序**。只调用一次 receipt，但忽略其已成功结果又重写，不算处理正确。允许使用 ledger 核对，但需能从可信返回内容定位同 scope、同 operation 和真实写入，不能仅凭工具名称认定已确认。

独立评分输出分轴结果，避免用一个 passed 混淆：

1. `injection_valid`：故障确实命中规定窗口，来源与时序完整。未命中是本次实验无效，不是候选通过或失败。
2. `task_result`：完整复用 `grade_tools`，正确报告、查全分页、正确且恰好一次的真实写入与规则都要核对。故障后缺报告或漏分页仍然不能算任务完整通过。
3. `handling_outcome`：`automatic_completed`、`explicit_failed`、`needs_confirmation`、`unsafe_duplicate`、`incomplete` 或 `evidence_invalid`。每项带原始证据来源，不能只从候选自述分类。
4. `replay_behavior`：哪些 operation 在哪一段发过几次；已提交之后的重发、已知未提交后的合理重试分开；每项标防重由候选、模型、业务接入还是服务提供。
5. `budget_and_process`：总模型请求/累计活跃时间、真实 Docker 状态、OOM、清理和错误。外部制造的 SIGKILL 单独验收为故障触发，不能改普通进程健康判定器让 137 变成健康退出；B 仍按正常流程核查。

“结果待确认”不是审批：不建设人工操作页面，不发送消息；只留可由产品后端展示的状态、原因及 operation 清单。若框架没有这种原生状态、由接入层根据可信失败事件生成，明确记为业务接入；接入层不能替它查询、重写、完成剩余任务然后冒充原生恢复。

## 7. 最少新增或修改哪些文件

以下是实施位置。写前栅栏和独立候选桥已开始落盘，runner/host与原生crash入口正在接线；尚未执行本专项的Docker校准或真实故障任务。优先复用 I22 的一次 prepare、chain/attempt、累计预算和 Docker 生命周期；不再造一套通用控制平台。

| 文件 | 最小作用 |
| --- | --- |
| `configs/tasks/fault-recovery.yaml` | 保留四类故障、两种模式和共同种子；实现完成后登记真实入口与判定器，不调整既有预算 |
| `scripts/run_fault_recovery.py`（拟新增） | 宿主 Docker 编排：固定镜像/容器、启动 A、接收栅栏命中、kill 精确 A、确认删除、必要时启动 B；只提出控制请求，不判业务答案 |
| `runner/fault_recovery.py`（拟新增） | bench 中一次 prepare、admin 故障设置、可信证据收集、原生恢复证据与 `grade_tools` 合并验收、上述分轴分类 |
| `execution/tools/fault_gate.py`（拟新增）及 `execution/tools/server.py` | 容器内一次性写前栅栏与可信事件；私有绑定、固定目标、报到等待；不发送业务请求、不保存候选进度、不代查询/重试 |
| `fixtures/service/server.py`、`faults.py` | 优先不改既有 fail/drop 语义；复用 admin 设置与事务持久化。实际需要新增只读观测时单独记录，不能改变故障窗口 |
| `compose/fault-recovery.override.yml`（拟新增） | 同链 state/workspace/budget、按 attempt 分输出、候选只读控制绑定；隐藏 admin、目标 operation 和预期答案；不新起额外常驻平台 |
| 选中候选的上述 HTTP 桥、options 与入口 | 仅 I23 prewrite 接线和已经证明可用的原生 crash/resume 分支；固定到新镜像，来源摘要随之更新 |
| `tests/fixtures/test_fault_gate.py`、`tests/execution/test_fault_recovery.py`（拟新增） | Docker 校准：故障未命中、错 scope/attempt、栅栏提前放行、伪 kill、预算重置、幂等拦重冒充不重发、非幂等重复、结果未知冒充成功等反例 |

证据留在配置的 `data_root` 下，用 `paths_for("fault-recovery", chain_id, candidate, "alice", "main", chain_id, case="fault-recovery")` 生成路径。fault、write_mode、seed 写入唯一 chain_id 和私有 chain 配置；不同格子绝不复用状态、账本或预算。

```text
runs/<date>/<candidate>/fault-recovery/<chain>/
  chain.json                         冻结的任务、模式、故障、镜像和身份绑定，不含 key
  attempts/worker-a/candidate/        原始事件、结果；被 kill 时允许无正常 result
  attempts/worker-b/candidate/        只有真实恢复才出现；新目录，不覆盖 A
  trusted/fault-setup.json            可信设置及目标；不挂给候选
  trusted/prewrite-gate.jsonl         栅栏命中、废弃与 attempt 来源
  trusted/worker-lifecycle.jsonl      精确容器身份、kill、退出删除、B 启动
  trusted/tool-events.jsonl           可信公共工具审计，全链保留
  trusted/ledger-before-recovery.json B 启动前的真实副作用快照
  trusted/ledger-final.json           最终请求与实际写入，保留故障消费状态
  model-calls.json                    外部预算 DB 导出的全链请求记录
  operation-analysis.json             逐 operation 的请求、写入、查询和重复来源
  verdict.json                        五轴结果、原始证据引用和缺口
```

state、workspace、fixture 和 budget 使用相同 chain 的现有路径布局。候选只可写自己的 state/workspace/candidate 输出，不可修改 trusted、独立账本、预期和 verdict。凭证、完整 Docker Env、展开的 Compose 和原始 reasoning 不进入公开报告。

## 8. 运行表与验收顺序

先基础校准，再冒烟，再冻结正式矩阵。原有 `diagnostic-timeout` 可以作为接线参考，不能直接算 I23 全矩阵通过；同种子旧详细实验只有镜像、任务和变体确实一致时才可作明确标注的辅助对照，不代替改变后的 I23 基线。

| 阶段 | 每条选中路线的运行内容 | 验收及停止条件 | 状态 |
| --- | --- | --- | --- |
| F00 | 核对故障源码、当前入口和本设计 | 四个窗口、来源边界、突然退出与安全暂停的区别说明完整 | 已完成，只读设计 |
| F01 | I21 选定路线，复用 I22 链编排并冻结 I23 变体 | 镜像、任务、模型、schema、来源、配额、原生恢复入口和能力来源清楚；没有恢复入口的格子预先标待核实，不能伪恢复 | 已完成材料冻结：两条专属context/source/overlay及新镜像固定，见fault-recovery-execution.md |
| F02 | 实现一次 prepare、故障设置、prewrite 栅栏和分轴评分 | admin 不给候选；目标不泄漏；B 不重建场景或预算；无候选外业务执行循环 | 已完成接线与容器校准；真实原生crash恢复仍待实际故障链 |
| F03 | Docker 内无付费模型的公共正反例校准 | 四种故障及两模式可确定复现；栅栏不会留下晚到写入；错来源/重复副作用/缺失回执证据不能通过 | 旧材料已完成公共242项、DSH边界25项；新增Goal收尾通知关联正反例尚未运行，须在新镜像重新校准；真实注入命中另验 |
| F04 | 同 I23 镜像先跑 101 的两种无故障基线；再对四种故障各跑一个非幂等冒烟 | 基础任务正常；四故障真的命中；不能归咎于评分/接线。未命中或基础设施坏了先修；真实候选缺口保留，不无限修上游 | 进行中：DA baseline-101两模式已启动，冒烟尚未开始 |
| F05 | 正式：四故障 × 两模式 × 101/202/303，共 24 格 | 每格单独新链，证据齐全且有分类；没有原生续跑的格子可用充分缺口证据结束，不能把没跑写为实测失败 | 未开始 |
| F06 | 补齐同镜像两种无故障模式的 202/303，共 4 格 | 与 F04 两格合为每模式三种子基线；能区分任务原有波动与故障新增问题 | 未开始 |
| F07 | 对最终路线的写前 kill、写后丢回执补 404/505，两模式共 8 格 | 关键结论补到五种子；若投入上限不足，标本轮未完成，不改成功口径 | 未开始 |
| F08 | 回写候选报告、矩阵、I23 状态和投入记录 | 分开报告自动完成、明确失败、待确认、重复副作用、无效与未完成；单列服务幂等和自研接线成本 | 未开始 |

F04 的冒烟与 F05 正式 seed 101 使用不同 chain，全部保留；不拿失败冒烟改名覆盖。F05 不以“必须修到通过”为目标，完成规定实验、证据充分即可结束调研步骤。普通基线不通过时先说明相同输入的波动与投入上限，不能从故障格子的失败直接推出框架故障恢复不行。

最终并排给出三件事：**出错后有多少格自动完成；有多少格诚实停在明确失败或待确认；有多少格漏写、重复写或错误宣称成功。** 再附额外模型/工具请求、恢复时间、工程改动与能力来源。不会用一个总分把这些不同风险混在一起。

## 9. DSH 无故障基线发现的官方收尾通知误判

旧链 `i23-dsh-001-baseline-101-none-id-101` 使用了8次模型请求。其原生Goal已经是complete/disarmed，最后turn/end为completed，原生错误数为0；包装层却把 `concurrent_input` 标为true，最终输出incomplete并退出1。这是本仓库对官方通知的误分类。旧链及其失败结果保留，不改写成修复后通过。

固定官方源码 `packages/goal/tool-goal/src/index.ts` 在同Goal自动轮次调用公开 `update_goal complete/blocked` 后，会通过 `exec.deferContext` 投递一条 `source={kind:plugin, plugin:tool-goal, form:notice}` 的收尾上下文，要求模型给用户写结束说明。它沿用原Goal，不是外部新任务；原来只允许 `source.kind=goal` 的检查误拦了它。

本次仅在I23专用 [fault-goal-runtime.ts](../../adapters/deepseek_harness/fault-goal-runtime.ts) 增加公开观测关联：先记录实际 `update_goal` 的callId、原Goal id/revision及当前轮次；在公开 `tools/result` 看到成功值与同Goal的新终态一致，并核对唯一通知的id、source和官方纯renderer输出；等对应的原生 `tool/result` 按同callId、turn/step、sourceEventSeqs及内容提交后，才允许完全相同的通知进入inbox一次。接纳记录只保存身份、hash和原生事件引用。没有改写工具结果或原生状态，没有调用followup、补发prompt或产生新通知。

通知格式通过固定源码的真实 `boundContextSummary`、`renderWrapupContext` 核对。工具结果的观察和提交顺序分别按官方 `core/tools` 的 `tools/result` 通知、`core/agent-loop/tool-calls.ts` 的 `appendToolResult -> acceptContext`，以及 `core/agent/inbox.ts` 的实际插入通知核对。其他plugin、外部followup、错Goal/revision、伪造内容、错call来源、提前或重复投递继续触发并发输入门禁。blocked收尾即便合法，也仍是原生未完成，不会改成任务完成。

[Node边界反例](../../tests/execution/deepseek_fault_recovery.test.mjs) 新增complete/blocked两项正例及22项错误关联反例；测试使用真实官方消息工厂和纯renderer，Agent/工具回调为隔离的合成fixture，不启动模型或官方运行时。**新增实现和反例尚未执行。** 后续由新context、新镜像先做容器内无模型校准，再用新链重跑基线；旧context不覆盖，未校准前不继续宣布故障专项通过。
