# DeepSeek Harness：I24 两用户最小接线方案

日期：2026-10-21。本文是接线设计，**没有新增实现、构建、模型调用或 I24 能力通过结论**。总进度仍以 [IMPLEMENTATION_PLAN.md](../../IMPLEMENTATION_PLAN.md) 为准，两用户通用契约与现有 Deep Agents 结果见 [multi-user-design.md](multi-user-design.md)。

实施追加：已按本文编写源码与离线测试，当前仅完成静态接线，尚未执行；变更清单、冻结文件与后续容器校准流程见 [实施交接](deepseek-multi-user-implementation.md)。本文开头及第6节的“本次只新增本文”描述设计提交当时的范围，不代表后续源码已经验证。

建议复用现有模拟产品后端的身份、归属检查和固定 Worker 路由，在独立变体中增加 DSH 专用 Worker 与原生证据判定，再补少量候选选择接点。无需另建 Agent 循环、恢复引擎、租户系统、队列或资源池。**当前 I24 入口写死 Deep Agents，是实验设施尚未接入 DSH，不是 DSH 多用户能力失败。** 接通后仍须真实验证，不能由 I22 或源码机制推导 I24 通过。

## 1. 采用哪份材料，已有证据能复用到哪里

测试对象沿用 MIT 开源 `deepseek-ai/deepseek-harness`，固定提交 `dd6322d604e00eec1ba5e0c8541159906a21094a`、包版本 `0.1.2-alpha.3`。继续使用 I22 的 `goal-runtime` 入口和官方 Goal 插件，不改用 I23 的 `fault-round-order` 入口，也不升级或修补上游。来源及入口分层见 [候选附录](../report/candidates/deepseek_harness.md)。

| 已有材料 | 已验证内容 | 本次可复用与不能外推的部分 |
| --- | --- | --- |
| [I22 暂停报告](pause-recovery.md)；上下文 `external/build-context/deepseek_harness-pause` | 101/202/303 原生暂停恢复 3/3、完整业务 3/3；101 采用保留原判定的追加离线全链复核 | 复用公开 Goal 暂停、关闭、原 Session/Goal 无提示恢复机制；不重复研发持久化或续跑算法 |
| I22 三条真实链 `i22-deepseek-pause-seed-101-002`、`...-202-001`、`...-303-001` | 已写入且任务仍活动 → 安全暂停 → 正常关闭 → 删除旧 A → 等待 600 秒并重建代理 → 新 B 原目标继续；已提交写入未重发，预算未清零 | 证明既有固定任务的换容器续跑；不证明两用户并发、后端授权、同 Worker 换用户或 0.75 CPU 下的表现 |
| I22 独立工具面校准 | 真实空 Agent 暴露 11 个工具，包含 8 个 HTTP 工具和 3 个 Goal 工具；模型调用为 0 | 复用校准流程；新 Worker 镜像、接入摘要及 scope 改变后，必须生成对应的新校准，不能直接复制旧通过记录 |
| [I17 取消诊断](deepseek_harness.md#诊断与取消) | 公开 `Agent.cancel` 后 idle/noPending，但 Session/通知缺原生 `turn/end`；记录到非 JSON 可序列化结束事件错误 | 作为已知取消缺口保留；Goal 暂停成功没有修复立即取消，不能用 idle 或容器停止代替取消终态 |

新变体以 I22 已验证上下文的 Goal 接入文件和其依赖字节为起点，逐文件核对来源和摘要。不能默认当前仓库同名文件与 I22 材料相同。新增 Python 控制包形成新的接入 manifest、Worker 镜像与 trial；旧 I22、I23 代码、上下文、镜像、pins、原始证据和 verdict 均不覆盖。

## 2. 可以直接复用的产品边界

| 组件 | 复用内容 | 保持的约束 |
| --- | --- | --- |
| [runner/shared_backend.py](../../runner/shared_backend.py) | Bearer 凭证摘要映射 principal、task → owner、归属检查、六类 API、保护侧 journal | 先认证和检查归属，再读取或派发；A 访问真实存在的 B 任务返回固定 403；body 不能覆盖身份、路径或目标进程 |
| [runner/shared_supervisor.py](../../runner/shared_supervisor.py) | 不可变 task/attempt、互不重叠的目录、原始事件游标、实际文件读取、保留的 Popen 与 Linux PID/start_ticks 绑定 | 已接受 `deepseek_harness`；每个 Worker 仍只持有一个固定 attempt；不开放任意 argv、环境或控制目标 |
| [execution/control_backend.py](../../execution/control_backend.py) 的 HTTP 与路由层 | 固定私网 Worker URL、禁止重定向、Worker 身份与 request ID 核对、真实旧连接 EOF 和新连接记录 | 后端无 Docker socket，不接收客户端 Worker 地址；start/resume 响应丢失不自动重发；202 仅表示派发尝试 |
| [scripts/run_multi_user.py](../../scripts/run_multi_user.py) 的编排框架 | 双用户独立网络/工具/模型代理、实际镜像 ID 与配额核对、资源采样、A 退出删除后创建 B、确切 ID 清理 | 模型上游密钥只在代理，Worker 只拿本任务 run token；宿主不运行候选 SDK、不计算业务答案 |
| [runner/multi_user.py](../../runner/multi_user.py) 的公共验收部分 | 真实 HTTP 客户端、归属正反例、游标与重连、Fixture 账本、写入不重发、累计预算与资源/清理核对 | 主任务与取消子任务分开；原生/业务/边界/资源结果分列；不为接入 DSH 放宽既有来源要求 |

Worker 私有 journal 与候选子进程仍处于同 UID、同容器，只是应用侧佐证，不构成操作系统级安全边界。生产租户来自独立产品后端，本轮继续用 A/B 模拟身份验证接入契约。

## 3. 为什么还不能只增加一个 Worker 文件

当前公共层包含下列 DA 专用假设。它们需要小范围、显式的候选分派，不能把 DSH 事件重命名成 DA 事件，也不能将固定策略放宽成任意命令执行器。

| 当前落点 | DA 专用假设 | DSH 最小接点 |
| --- | --- | --- |
| `execution/worker_control.py`：`DA_ARGV`、`SingleAttemptWorker`、`verify_resume_materials` | `/opt/venv/bin/python`、DA 入口/环境、SQLite checkpoint、`pause-index.json`、superstep drain | 新增专用 DSH Worker 和恢复材料校验；复用共用读取/进程身份原语，保留 DA 固定契约 |
| `execution/control_backend.py`：`_materials`、`state`、`_signal`、`resume` | description 的 candidate 固定 DA；原生材料文件表、信号语义和恢复校验均固定 DA | 从可信 task/attempt 选取候选白名单策略，分别选择材料表、ACK 契约与恢复校验；原授权/固定路由不变 |
| `scripts/run_multi_user.py`：`paths_for`、`create`、`wait_port`、`start_workers`、bench/collector | 路径候选为 DA，控制服务/探针用 DA Python，预算 token 挂载和 worker module 固定 | 增加可信候选 profile，明确 DSH Python、模块、环境和逐文件挂载；共用配额、采样和清理代码 |
| `runner/multi_user.py`：`CANDIDATE`、`ARGV`、prepare/bind、activity/overlap、baseline/cancel | DA 环境、tasks stream、checkpoint 来源、活动与取消判定 | 增加具名候选接点，调用 DSH 专用原生活动、恢复、基线和取消判定；HTTP/业务/资源部分继续共用 |

建议采用少量具名策略函数/静态 profile，不复制整份 1400 行 runner，也不运行时改写模块全局常量。候选选择只能来自启动时固定的可信材料，不能通过公共 HTTP 请求切换。

## 4. DSH Worker 的固定启动与恢复绑定

### 4.1 启动和打包

独立上下文建议为 `external/build-context/deepseek_harness-multi-user`。新 stager 只接受该类新目录，发现已有接入 manifest、Dockerfile 或 `harness-lab` 时拒绝覆盖，避免现有 DSH stager 的替换行为误碰 I22/I23。

现有 [DSH Dockerfile](../../compose/candidates/deepseek_harness/Dockerfile) 已安装系统 `python3`，无需引入 Python 第三方依赖或 `/opt/venv`。Worker HTTP 包只使用标准库，连同 `execution/__init__.py`、`runner/__init__.py`、`shared_backend.py`、`shared_supervisor.py` 随新 manifest 一起复制。Python 模块搜索路径需明确包含 `/opt/materials/harness-lab`；不能假定 DA 镜像配置会在 Node 镜像自动存在。

候选子进程拟固定为以下参数；实际可执行文件位置须在新镜像无模型校准中核实并固定：

```text
/usr/local/bin/node --import tsx/esm /opt/materials/harness-lab/goal-runtime-entrypoint.ts --request /config/request.json
```

启动目录固定为 `/opt/materials/sources/main`，延续 I22 镜像的包解析位置，确保 `tsx/esm` 可解析；不能照搬 DA Worker 的 `cwd=/workspace`。原生 Session 的业务 cwd 仍由可信请求固定为 `/workspace`。不增加宿主 node_modules 或源码挂载。

子进程使用重新构造的白名单环境，至少明确：

| 环境项 | 固定值/来源 |
| --- | --- |
| `MODEL_API_BASE_URL` | `http://model-proxy:8082/v1` |
| `MODEL_NAME` | 启动前固定的同一模型 |
| `MODEL_RUN_TOKEN_FILE` | `/budget/run.token`；不是 DA 的 `/config/model-run.token` |
| `HARNESS_LIFECYCLE_IMAGE_ID` | 宿主核实的本次完整 Worker 镜像 ID |
| `TSX_TSCONFIG_PATH` | `/opt/materials/sources/main/tsconfig.json`，显式传给子进程，不依赖继承镜像环境 |
| `DSH_TELEMETRY_DISABLED` | `1` |

不能继承后端控制凭证、远程模型密钥或无关宿主环境。新 A/B 继续分别记录空输出目录、真实 PID/start_ticks、固定 argv、scope/chain/attempt 和实际退出；readiness 只证明 handler 已安装，不证明暂停或取消完成。

### 4.2 挂载和恢复前的拒绝条件

沿用 state/workspace/evidence/private 分离、只读 policy/token/request/lifecycle/scope。DSH 另需：

- 原预算 token 只读挂载为 `/budget/run.token`；预算库仍由原代理持有，A/B 共用同一 `calls.sqlite` 与 token、20 次模型上限。
- A/B 各自只读挂载 `/config/lifecycle-state.json`，覆盖完整 state/workspace 文件及目录清单。
- B 另只读挂载 `/config/lifecycle-pause.json`，只接受保护侧独立验证的原 Session/Goal 暂停索引。

后端和 Worker 启动前都要核对候选、来源摘要、工具/模型配置、实际镜像绑定、完整 scope/session/chain/attempt、token/预算标识、状态清单及剩余活跃时间。拒绝异用户/异目标、文件变化、过期 attempt、预算重置和带 prompt/followup 的恢复。镜像环境中的自报 ID 只能作为声明，实际 Docker Image ID 仍由宿主独立核对。

先观察 A 正常退出，再删除原 Worker，验证原生保存点后为 B 生成只读绑定。B 只执行公开 `agents.resume({setup})`、核对完整原 Session 前缀与合法 `session/end-seed`，再恢复同一个 Goal；不重发任务提示、不加“继续”消息，不新建等价目标。

可复用 [runner/pause_recovery.py](../../runner/pause_recovery.py) 的 `validate_trigger`、`validate_dsh_pause`、`dsh_contract`、DSH `bind` 和 `final_native` 分支，以及 [runner/dsh_goal.py](../../runner/dsh_goal.py) 的物理事件独立重放。持久 GoalSpec 与进程内 GoalView 分开验，不要求持久事件含进程内 activation。

I24 当前从 DA pause index 读取 `total_active_seconds`，DSH 索引没有这个字段。应沿用 I22 的保护侧实际计时及关闭观察，累计已用活跃时间并与候选记录核对，保守扣除预算；不能编造字段、按恢复次数重新发 300 秒或把不明时间补零。

## 5. 观测和取消必须保留候选自己的语义

### 5.1 原生活动与状态读取

DSH 的 `raw-events.jsonl` 是 `session.event` 通知，原生事件按 `params.sessionId`、`params.event` 读取；不是 DA 的 `stream_mode=tasks`。专用解析器应定位真实 Session、turn/start/end、step 与实际 model/tool 派发，再与同一观察端读取的工具/代理前缀核对。

有效并发仍要求两任务实际活动区间重叠，且重叠内双方均有有效调用或进展。容器同时存在、模型请求开始后一直等待、两份 synthetic 事件均不够。跨用户拒绝前后自然业务进展，沿用通用设计的真实只读等待窗口和独立快照，未命中窗口就记录未命中。

state API 返回实际进程观测与可用的 DSH 原始文件引用/摘要，例如 `session-after-idle.json`、`lifecycle-pause-candidate.json`、`native-state-index-after-close.json` 和 `result.json`。文件不存在时保持缺失/阶段未到，不能先写一份“paused”缓存。events 保留 task/attempt 游标，恢复后仍能读取原 attempt；artifacts 对照实际 `report.json` 和本用户内容。

### 5.2 立即取消的已知缺口

DSH 暂停走 `SIGUSR2` → 步边界 → `goals.pause` → blocked turn/end → whenIdle/flush/正常关闭。取消走 `SIGUSR1` → 公开 `Agent.cancel({kind:'hook', reason:'experiment_cancel'})`，两条路径必须分列，不能把暂停改名为立即取消。

后端/Worker ACK 只能说明固定进程的信号派发尝试。DSH 的暂停与立即取消分别采用对应的候选语义声明，不能复用 DA 的 `cooperative_drain_at_superstep_boundary` 来解释 `Agent.cancel`；任何 ACK 都不得先宣称 native confirmed。

取消子试验至少分开报告：

1. **授权和路由：** A 对真实 B-cancel 的请求收到 403，无错误原生派发；owner 请求只落到本 task/attempt 的确切 PID。
2. **公开 API 调用：** 实际活动中的 handler 记录、API attempt/returned/error 及原始事件来源。
3. **原生取消完成：** 同一个 turn 出现匹配 cause 的 aborted 结束事件并进入 idle；缺终态仍记缺失，不能由 API 返回或 noPending 推定成功。
4. **副作用：** 保留完整工具调用、实际账本和在途结果；只在原生取消完成已证实时检查确认后无新写入派发。若确认缺失，该子项不能偷换成已通过。

I17 两轮仅覆盖模型活动时发取消、0 次业务写入，没有验证工具写入在途取消。I24 如未实际命中某停点，应标“未覆盖”。Goal round driver 源码对取消后的暂停/收束处理只是机制线索，不是该新变体的实测证明。

现有 POSIX 信号不携带后端 UUID；DSH handler 生成自己的 cancel request ID。最小接线通过“一 attempt 只接受一次控制”、完整 scope、固定 PID/start_ticks、保护侧前后原生前缀建立关联，明确记录两种 ID，不伪称它们端到端相同。如果要求携带同一控制 ID 的本地传输，那是额外开发范围，不是本轮先决条件。

取消诊断允许记录候选真实 failed/incomplete、退出 1 或超时，同时收集已发生的边界证据；不能套用主任务 pause 必须正常退出 0 的要求而在收集前丢失材料。异常退出/强制清理仍如实登记，不能改为健康完成。取消缺口保留时，可以得出某些授权/暂停子项通过，**不能写成 DSH I24 全项通过**。

## 6. 最小文件清单与自研归因

以下是后续实施清单，文件名为拟定；本次只新增本文。

| 类型 | 文件/材料 | 责任 |
| --- | --- | --- |
| 新增 | `execution/worker_control_dsh.py` | 固定 DSH 子进程、严格绑定、专用恢复材料验证、两类信号 ACK；标准库 HTTP 和现有进程身份原语 |
| 新增 | `runner/multi_user_dsh.py` | DSH 原生活动/重叠、基线来源、暂停索引、取消终态与缺口的专用证据判定；不复制公共 runner |
| 新增 | `scripts/stage_deepseek_runtime.py` | 独立不可覆盖的 multi-user 上下文、I22 基础材料核对、Python 包覆盖层和 manifest |
| 新增 | DSH Worker / multi-user 的离线测试文件 | 错误绑定拒绝、物理事件解析、控制与恢复来源、缺失/冲突终态反例；必要边界测试，不模拟业务通过 |
| 小改 | `execution/control_backend.py` | 白名单候选策略，description、材料表、ACK 语义与恢复 verifier；认证/归属顺序不变 |
| 小改 | `scripts/run_multi_user.py` | 可信 candidate profile、Python/模块、环境与挂载、工具面校准接点；复用原资源/生命周期实现 |
| 小改 | `runner/multi_user.py` | 显式调用候选策略；DSH 绑定/计时及专用原生判定；保留 DA 默认与原验收要求 |
| 新建/重固定 | Worker、services、bench 镜像及 pins/工具面记录 | 每份新字节对应新摘要与实际镜像 ID，独立 0.75 CPU 基线与新 trial |

[compose/Dockerfile.multi-user-services](../../compose/Dockerfile.multi-user-services) 已复制 `execution`、`runner`，通常只需重建/固定镜像，不需在后端装 DSH SDK。DSH 原生恢复 verifier 若新增可复用标准库 helper，应单独列入 manifest，避免通过导入 DA Worker 隐式继承其专用契约。

官方仍负责 Agent 循环、Goal/Session、工具调度、原生持久化与恢复。**HTTP 认证/路由、Worker 控制及不可变恢复绑定、模型计数代理、采集和验收均属于自研接入/实验层**，不能算作 DSH 原生多租户产品。候选专用包装、事件格式/Goal 契约适配和版本升级重验是持续维护项；故障注入、离线判定、资源采样属于研究设施，产品是否保留另评。

## 7. 验收顺序：0.75 CPU 基线 → 101 接线 → 三种子

实施必须与其他真实实验遵守现有独占锁和资源上限。当前 I23 冻结材料与任务不因本文变化；真实容器/模型验证等运行窗口释放后串行安排。本次不运行下列步骤。

| 顺序 | 工作 | 通过门禁与留存 |
| --- | --- | --- |
| 1 | 独立材料与离线边界校准 | 确认无旧上下文覆盖；固定 command/cwd/env 可用；拒绝错误候选/argv/scope/session/Goal/image/hash/token、缺文件、旧 attempt、prompt 恢复和预算重置；验证响应丢失不重发、错误 Worker/重定向拒绝 |
| 2 | 新镜像固定与零模型校准 | 固定完整镜像 ID 和 manifests；实际 Python 模块/tsx 解析、describe/handler/进程读取可用；按新镜像和完整任务 scope 独立校准 11 工具面，模型调用前后均 0；控制服务不持有远程密钥 |
| 3 | **同入口 seed101 不暂停基线，Worker 2 GiB / 0.75 CPU** | 真实 Goal/Session 启动并完成，核对原生来源、冻结业务任务、累计预算、实际配额、采样、健康退出与正向清理；不能借 I22 的 1 CPU 基线或旧镜像通过数 |
| 4 | **seed101 双用户接线冒烟** | 两个主任务及两个独立取消子任务按下述完整矩阵执行；原始材料与按维度 verdict 全留存，先识别接线/采集问题，再决定正式三种子，不挑成功重跑 |
| 5 | **固定新材料后正式 101/202/303** | 每组独立 scope、state、workspace、预算和 trial；保持模型、提示、工具、配额和验收标准；业务失败、取消缺口和基础设施失败分别报告，原件不覆盖 |

双用户活跃阶段继续使用现有 I24 总配额：两个 Worker 各 2 GiB/0.75 CPU，两套 tools/proxy 加 Fixture/后端各 256 MiB/0.25 CPU，总计 5.5 GiB/3 CPU。bench 不与两个活跃 Worker 同时启动；构建、重建及附带服务同样纳入已有资源规则，不挪用 I23 预算。

基线如只有真实业务规则失败，保留整轮失败；在来源、原生运行、预算和基础设施完整的前提下，可按现有 I24 规则继续边界探索，不靠重跑挑一条业务成功。来源/预算/采样/配额/生命周期完整性失败先停止并修正设施，换新材料与 trial 后再验证，不能把其结果算作候选能力成败。

seed101 及正式三组分别覆盖：

- **主任务：** 两个真实任务有效重叠；owner state/events/artifacts 对照原生数据和实际文件；A 对 B 六类操作实测 403，其中运行态 pause/cancel 与合法暂停后的 resume 分别验；原任务完成冻结业务要求。
- **暂停续跑：** owner 请求后有真实原生 Goal pause/blocked turn/end、flush 和正常退出；实际删旧 A，再由不同容器/进程 B 沿原 Session/Goal 无 prompt 继续；旧事件可查、已提交写入不重发、原 token/预算/剩余活跃时间连续。I24 无需再次等待 600 秒，该等待能力沿引 I22，不用缩短等待来重判旧实验。
- **重连：** 产品 API 客户端真实 EOF、新连接重新认证、同一个原 task/游标可读，并在该新连接再次实测访问 B 被拒。产品客户端断线不替换候选上游连接。
- **取消子任务：** 双方实际活动、跨用户取消无派发、owner 公开 API、真实原生终态和全部副作用；缺 `turn/end` 时按第5节保留缺口，不用 Goal pause 或 137 强杀补通过。
- **独立验收与清理：** 保护侧原始来源/账本/预算核对、逐容器配额与完整采样、确切 ID 生命周期、正向 cleanup 原件和最终同 trial 容器/网络为空。业务、授权、原生续跑、取消及基础设施分列。

本变体继续记录 `worker_reuse_supported=false`，仅验证换容器继续；浏览器身份/cookie 隔离和同 Worker 跨用户复用均“未验证”。I25 常驻/按需成本不在本接线范围，也不由 I22 的 Worker 内存样本推算生产节省比例。

## 8. 投入是否适合当前短期验证

在“复用 I22 原生 Goal 链、增加薄 Worker 与专用证据策略、允许留下取消缺口”的范围内，值得做一轮有界接线。它是数个关联文件的集成工作，不能估成仅改配置；主要不确定性在并发观测窗口、完整恢复绑定及取消终态，而不是重建身份平台。

没有可靠的人工工时记录，不报实测人日或升级成本；实施时按 [工程投入清单](engineering-inventory.md) 记录新增/修改文件、公共设施与候选专用接入、失败轮次和模型支出。已有 I22 成果减少了重复研发，但不能抵消本次新包装、来源校准和维护成本。

遇到固定公开 API 无法给出可信取消终态、恢复物理材料无法匹配、预算连续性或同配额验证不成立，应留存可复现缺口并停止扩大范围。修补/升级 DSH 核心、重写错误序列化、建设通用长期多会话服务、同进程资源池、浏览器恢复或副作用事务协调器，均应另立产品研发任务，不为填满 I24 表格在本轮展开。
