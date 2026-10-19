# 云端 Agent Harness 本地验证实施方案

更新日期：2026-10-19。状态：M0/M1/M2 已完成；DeepSeek Harness 的 M3 适配器已完成离线接入，远程模型实验因本地未配置凭证暂缓。

关联需求文档：[REQUIREMENTS.md](./REQUIREMENTS.md)

## 1. 目标与边界

本地验证的目标是用统一的容器、远程模型 API、工具集合和任务集，比较候选框架实现云端 Harness 所需能力的差异，识别可直接复用的部分、需要在适配层补齐的部分和需要自研的部分。验证结果用于选型和下一阶段原型设计，不代表生产部署结论。

首轮只验证本地可运行的单机或少量容器实例，不实现生产集群、多租户产品后端或完整安全边界。当前没有具体业务场景，因此先使用可重复的合成任务；真实业务场景确定后，再追加针对性验证。

浏览器优先采用容器内易复现的 Playwright/Chromium 方式。候选若需要其他浏览器连接方式，应记录接入差异和额外成本，不把本地便利性直接当作生产安全结论。

## 2. 统一本地拓扑

计划用 Docker Compose 或等价的本地容器编排方式提供以下组件：

| 组件 | 作用 | 验证要求 |
| --- | --- | --- |
| 本地 Controller / Runner | 接收任务、调用候选适配器、提供统一生命周期接口 | 不把候选框架内部状态直接当作统一任务状态；保留候选原始事件 |
| Candidate Adapter | 将各候选接入统一协议，并记录适配代码和补齐工作 | 每个候选独立版本、依赖和容器，避免依赖串扰 |
| Execution Container | 执行工具、文件、Shell 和浏览器操作 | 使用同一基础镜像、工作区挂载和资源限制；权限差异单独记录 |
| 状态与事件存储 | 保存任务状态、checkpoint、事件和运行元数据 | MVP 固定使用本地 SQLite + JSONL 挂载卷；只有验证需要时再评估 Postgres/Redis |
| 产物目录 | 保存工作区快照、结果、日志、截图和报告索引 | 按 `run_id` 隔离，敏感值脱敏，凭证只从环境变量注入 |
| 远程模型 API | 提供统一模型和参数配置 | 固定模型、系统提示、采样参数、超时、重试和预算，记录配置哈希 |
| Candidate Adapter Service | 启动候选适配器并通过 RPC 与 Runner 通信 | 每个 P0 候选独立进程或容器；适配器异常不应使 Runner 失去任务记录 |
| 本地 Fixture 服务 | 提供可重复的 HTTP、业务数据和幂等副作用场景 | 用于验证工具调用、浏览器任务、失败重试和重复副作用 |

首轮不为了模拟完整云平台而引入复杂基础设施。只有在验证暂停恢复、状态持久化或 Runner 重启时，才按需要增加 Postgres、Redis 或对象目录等组件，并把这些组件的建设成本单独记录，避免将平台复杂度误计为框架能力。

适配器至少提供以下协议：

```text
submit(task_spec) -> run_id
stream_events(run_id, after_seq=None) -> cursor, events
send_input(run_id, message)
approve(run_id, approval_id, decision)
pause(run_id) / resume(run_id) / cancel(run_id)
snapshot(run_id) -> checkpoint_id
restore(run_id, checkpoint_id)
result(run_id) / artifacts(run_id)
```

`task_spec` 至少包含 `task_id`、`tenant_scope`、`session_scope`、任务提示、工具清单、浏览器配置、超时、预算和 checkpoint 策略。事件流必须有单调递增的 `event_seq`、游标和去重语义，支持断线后按 `after_seq` 重连；事件至少带有 `run_id`、候选及版本、事件类型、前后状态、父事件、工具或模型调用摘要、时间和错误分类。

暂停采用“请求—安全点确认”语义：Runner 发出 pause 请求后，适配器在当前模型/工具调用结束或可中断点创建 checkpoint，再返回 `paused`；in-flight 调用的处理方式、重试次数和副作用策略必须写入事件。恢复使用 checkpoint 加适配器重建，不假定原进程仍存在。所有有副作用的 Fixture 调用必须带幂等键，并明确该步骤是至少一次还是至多一次。

## 3. 候选顺序

P0 固定按以下顺序验证：

1. DeepSeek Harness：优先理解已有 Harness 实现、工具/浏览器执行、状态和容器边界。
2. OpenClaw：作为 Hogee 现有业务基线，重点验证流程透明度、任务恢复和自定义成本。
3. Deep Agents/LangGraph：作为可控 Agent loop 和持久化工作流的自建主线。

P0 在最小任务或暂停恢复硬门槛上阻断时，先记录证据和原因，再决定是否投入 P1。P1 包括 OpenAI Agents SDK、Google ADK、Pi Agent Core、OpenAI Codex SDK；P2 为 OpenHands。P1/P2 只有在能回答 P0 未解决的问题时才进入验证。

每个候选都要明确记录其覆盖的层次：Agent 框架、Harness/运行时、执行环境/沙箱和云端平台。候选原生能力、配套组件、适配层补齐和自研部分必须分开统计。

## 4. 分阶段实施

| 阶段 | 主要工作 | 退出条件 |
| --- | --- | --- |
| Phase 0：基线准备 | 锁定候选源码版本和许可证；确定容器基础镜像、远程模型 API、工具 schema、浏览器方式、超时/预算和结果验收规则；确定 SQLite+JSONL、适配器 RPC 和暂停安全点 | 统一任务协议、事件字段和产物目录确定；可以独立启动本地验证拓扑 |
| Phase 1：候选冒烟 | 按 P0 顺序接入最薄适配器；分别完成模型调用、一个确定性工具、一个浏览器动作和一个自定义工具/Skill | 每个 P0 候选有可重复的最小运行记录，并记录首次可运行时间、适配代码量和阻断项 |
| Phase 2：能力矩阵 | 使用统一任务集跑工具、浏览器、人工介入、长任务、暂停恢复、失败重试和新增工具/Skill 等任务 | 每个必测任务有成功/失败、耗时、模型调用、产物和失败原因 |
| Phase 3：生命周期与故障 | 在模型等待、工具执行前后、审批等待和长命令期间注入暂停、取消、Runner 重启、容器重启和模型超时；Worker 更换作为 P0 之后的扩展项 | 能区分会话继续、任务恢复和无副作用重放；记录恢复点、恢复耗时和重复副作用 |
| Phase 4：透明度与定制 | 检查 Agent loop、状态变化、工具路由、事件关联、终态原因、回放及调试；新增一个工具、Skill、工作流或审批节点，记录修改范围 | 形成流程透明度和自定义成本证据；框架原生缺口与适配层补偿分开记录 |
| Phase 5：成本与结论 | 汇总首次接入、定制、升级和维护工时/代码量，估算模型、容器、浏览器、存储和恢复成本；决定是否进入 P1/P2 | 形成候选矩阵、缺口清单、适用条件、阻断项和下一步建议 |

## 5. 统一任务集与验收

在尚无具体业务场景时，先使用可重复的合成任务，避免比较结果被业务不确定性主导：

| 编号 | 任务类别 | 重点观察 |
| --- | --- | --- |
| T1 | 确定性工具链：文件、HTTP、计算和结果校验 | 工具 schema、路由、参数校验、结果回传和事件完整性 |
| T2 | 浏览器访问本地测试站点并抽取或填写数据 | 浏览器接入、状态保存、截图/产物和失败定位 |
| T3 | 多步骤任务，包含人工确认或追加指令 | 流式进度、审批、用户介入、取消和上下文连续性 |
| T4 | 长任务中主动暂停，等待后恢复 | checkpoint、工作区、消息上下文、恢复时限和结果一致性 |
| T5 | 模型/工具超时、进程退出、容器重启和重复提交 | 重试策略、幂等性、外部副作用和故障后恢复 |
| T6 | 新增工具、Skill、工作流或状态节点 | 自定义入口、改动量、调试难度、升级影响和维护成本 |

暂停恢复至少验证以下过程：

1. 保存 Agent 状态、消息和工具上下文、工作区及所需浏览器状态。
2. 杀掉 Runner 或容器后，从 checkpoint 恢复同一个任务。
3. 对比恢复结果与未暂停基线，确认状态和结果一致。
4. 使用带幂等键的 Fixture 服务确认没有重复外部副作用。

结果记录 `pause_point`、`checkpoint_id`、`resume_after_restart`、`duplicate_side_effects`、`recovery_latency` 和 `recovery_failure_reason`。

浏览器恢复单独区分两种能力：一是浏览器上下文本身可持久化并恢复，二是只保存语义 checkpoint 后重新创建浏览器上下文。报告中不得把二者合并为同一种恢复能力。主比较轨道默认所有候选调用统一的 Execution Playwright 服务；候选自带浏览器仅作为单独变体记录，避免执行环境差异混入框架比较。

每个候选至少记录以下指标：任务成功率和验收质量、端到端耗时、模型调用次数与 token/费用、暂停恢复成功率和时延、事件覆盖率、失败可复现性、适配代码量、首次可运行时间以及人工维护工时。

能力状态统一标为“原生提供、配套组件提供、适配层补齐、自行实现、尚未验证”。硬门槛为远程模型 API 接入、工具和浏览器执行、暂停后恢复、任务/会话作用域以及基础事件可追踪。

## 6. 验证文档留存流程

验证开始后，以 `REQUIREMENTS.md` 作为需求入口，以本文件作为实施流程入口；运行证据和候选结论按以下结构留存：

```text
docs/validation/
  README.md                         # 留存规则、状态定义和脱敏要求
  matrix.md                         # 跨候选能力矩阵和统一指标
  <candidate>/plan.md               # 候选版本、接入范围和已知限制
  runs/<UTC-date>/<candidate>/<task>/<run_id>/
    manifest.json                   # 版本、配置、模型、时间、状态和产物索引
    events.jsonl                    # 原始事件流
    metrics.json                    # 成功率、耗时、token、费用、恢复等
    state/                           # checkpoint 和状态快照
    artifacts/                       # 结果、工作区、日志、截图等
  reports/<date>-<candidate>.md     # 基于 run_id 的候选报告
  decisions/                        # 选型决定、阻断项和问题复盘
```

当前只记录目录和流程约定，不立即创建实验目录或实现脚本。验证真正开始时，每次运行先生成唯一 `run_id`，并在 `manifest.json` 登记：

- 候选源码 commit/版本、容器镜像 digest 和依赖锁定信息；
- 配置哈希、模型和 API 引用名、任务版本、超时/预算；
- 开始/结束时间、最终状态和证据路径。

运行中追加原始事件、标准输出/错误、状态快照、浏览器截图和工作区归档；结束后生成脱敏摘要，报告只引用 `run_id` 和证据索引，不复制未经验证的结论。

运行状态使用 `planned`、`running`、`blocked`、`completed`、`aborted`。记录采用追加方式，禁止覆盖历史运行；候选版本、任务定义或配置发生变化时，生成新 `run_id` 并注明变更原因。报告需分别写出事实证据、推断、未知项、框架缺口和适配层补偿；重大选型结论再同步到 `REQUIREMENTS.md` 或 `decisions/`。

提交版本库的内容限于计划、任务定义、适配器协议、汇总报告和脱敏样例 manifest。原始大日志、截图、工作区和含业务数据的产物放在受控本地目录或仓库外，并在文档中保留路径或哈希；API key、Cookie、Authorization header 和真实业务数据不得进入 Git。

验证完成后只清理临时容器、缓存和可重建 volume，保留最终报告、manifest、关键状态快照和能复现结论的最小证据。

## 7. 仓库文件及目录方案

本仓库采用“需求与方案、统一验证运行时、候选适配器、任务夹具、证据文档”分层组织。当前只记录目标结构，不一次创建大量空目录；目录在有对应内容时再创建，并用 README 或实际文件说明职责。

### 7.1 目标结构

```text
.
├── README.md
├── LICENSE
├── REQUIREMENTS.md
├── LOCAL_VALIDATION_PLAN.md
├── .env.example                         # 仅变量名和非敏感默认值
├── .gitignore
├── contracts/                           # 跨候选的任务、事件和生命周期协议
│   ├── adapter.py                       # Python 参考实现；跨语言以 schema/RPC 为准
│   ├── task.schema.json                 # task_spec 结构
│   └── event.schema.json                # 统一事件结构
├── runner/                              # 统一 Controller/Runner，不承载候选 SDK 逻辑
│   ├── cli.py
│   ├── lifecycle.py                     # 排队、超时、暂停、恢复、取消
│   ├── state.py                         # 任务状态和 checkpoint
│   ├── events.py                        # run_id 和候选原始事件关联
│   ├── storage.py
│   └── observability/                   # 日志、指标和 trace
├── adapters/                            # 每个候选独立依赖和入口
│   ├── common/                          # 事件映射、模型/工具/浏览器桥接
│   ├── deepseek_harness/
│   ├── openclaw/
│   ├── deepagents_langgraph/
│   ├── openai_agents/
│   ├── google_adk/
│   ├── pi_agent_core/
│   ├── codex_sdk/
│   └── openhands/
├── execution/                           # 统一工具、浏览器和工作区执行环境
│   ├── Dockerfile
│   ├── tools/
│   ├── browser/
│   └── workspace/
├── fixtures/                            # 可重复的本地测试依赖
│   ├── service/                         # HTTP/业务副作用 Fixture 服务
│   ├── data/                            # 脱敏、可重复种子数据
│   ├── browser_site/                    # 本地测试站点
│   └── scenarios/                       # T1-T6 输入、断言和幂等键
├── configs/
│   ├── base.yaml                        # 统一超时、预算、事件和产物根目录
│   ├── environments/local.yaml
│   ├── candidates/<candidate>.yaml      # 版本、镜像、入口和能力开关
│   └── tasks/<task>.yaml                # 任务参数和验收规则
├── compose/
│   ├── docker-compose.yml
│   ├── Dockerfile.controller
│   ├── Dockerfile.runner
│   ├── Dockerfile.adapter
│   └── Dockerfile.fixtures
├── scripts/                             # 启动、运行、暂停恢复、报告和清理命令
├── tests/
│   ├── contract/                        # 所有适配器必须通过的协议测试
│   ├── adapters/                        # 候选映射测试
│   ├── scenarios/                       # T1-T6 验收和回归
│   └── fault_injection/                 # 重启、超时、重复提交和恢复
├── docs/
│   ├── architecture/                    # 分层边界、事件/状态/恢复协议
│   ├── candidates/<candidate>.md        # 许可证、版本、缺口和接入成本
│   └── validation/                     # 运行规则、矩阵、报告和决策
├── external/                            # 可选的 gitignored 本地挂载/源码缓存
└── var/                                 # 本地临时状态和缓存，仅 gitignored
```

`docs/validation/` 下的运行证据继续遵循第 6 节的结构；原始运行数据不放入仓库，建议通过 `$HARNESS_LAB_ARTIFACT_ROOT` 指向 `~/harness-lab-data/runs` 或其他受控目录。候选源码默认放在仓库外，例如现有的 `../deepseek-harness`；`external/` 只作为可选挂载点，不表示 vendoring。

### 7.2 职责边界

- `runner/` 只管理统一任务生命周期、作用域、事件、快照、超时和预算，不直接依赖任何候选 SDK。
- `adapters/<candidate>/` 只负责把候选映射到 `contracts/`，同时保留候选原始事件和 `candidate_event_id`；不得把通用调度逻辑藏进适配器。
- 适配器默认以独立进程或容器运行，通过 HTTP、SSE、WebSocket 或 JSONL RPC 与 Runner 通信，以隔离 Python/Node/Rust 等依赖；只有极轻量候选才允许进程内插件。
- `execution/` 负责工具、浏览器和工作区；适配器只请求执行，不各自维护一套执行环境。
- `fixtures/` 只提供可重复的测试服务、数据和幂等副作用，不放真实业务工具和业务数据。
- `configs/` 保存模板、版本和配置哈希所需的非敏感信息；凭证通过环境变量注入。
- 产品租户、用户和鉴权后端不进入本仓库，首轮只用 task/session scope mock 验证作用域接口。

### 7.3 版本库与仓库外边界

提交 Git 的内容包括协议、任务定义、配置模板、适配器源码、脱敏样例 manifest、汇总报告和决策记录。候选源码不 vendoring，使用源码 commit、依赖锁定和镜像 digest 留痕；P1/P2 在进入验证前只保留候选说明，不预建不可运行的空适配器。

以下内容放在仓库外或全部加入 `.gitignore`：API key、Cookie、Authorization header、真实业务数据、原始大日志、截图、工作区归档、状态数据库、容器缓存和可重建 volume。报告只引用 `run_id` 加外部路径或哈希，避免凭证和敏感数据误提交。

### 7.4 落地顺序

实施开始时先建立 `contracts/`、`runner/` 骨架和 `fixtures/`，再接入 DeepSeek Harness、OpenClaw、Deep Agents/LangGraph 三个 P0 适配器；同时补齐 `configs/`、`compose/` 和协议/场景测试。只有 P0 结果无法回答问题时，才新增 P1/P2 适配器。首轮不拆分完整微服务，Compose 只编排 Runner、Execution、Fixture 和必要的状态存储。

## 8. 实施计划

实施按里程碑顺序推进，每个里程碑完成退出条件并留存证据后再进入下一步。首轮只做 P0，不并行铺开 P1/P2。

### 当前实施状态

| 里程碑 | 状态 | 已留存证据 |
| --- | --- | --- |
| M0：实施前收敛 | 已完成 | 候选来源、许可证、源码 commit、基础镜像、协议和 T1/T2 配置已写入 `configs/` 与本文件；环境检查脚本通过 |
| M1：最小骨架 | 已完成 | `contracts/`、`runner/`、SQLite+JSONL、checkpoint、manifest、JSONL AdapterProcess、Compose 拓扑；协议测试通过 |
| M2：统一执行基线 | 已完成 | Fixture 幂等副作用、fail-once、确定性工具、Playwright/Chromium 浏览器轨道；Compose 构建和健康检查通过 |
| M3：DeepSeek Harness | 部分完成/待凭证 | 本地 `pnpm dsh --version/help`、源码预检、headless 命令和事件采集已通过；真实远程模型任务待凭证 |
| M4 及以后 | 未开始 | 等待 P0 候选按顺序推进 |

当前验证只证明本地基础设施和统一执行基线可运行，不能据此推断候选框架的任务效果、恢复能力或成本排名。

| 里程碑 | 工作内容 | 交付物 | 继续条件 |
| --- | --- | --- | --- |
| M0：实施前收敛 | 固定远程模型/API、候选源码 commit、基础镜像、SQLite+JSONL、适配器 RPC、统一 Playwright 轨道、artifact 根目录、T1-T6 的具体输入/断言/预算 | `configs/` 模板、协议 v0、候选版本清单、任务清单 | 配置和验收规则可复现；凭证只通过环境变量提供 |
| M1：最小骨架 | 创建 `contracts/`、`runner/`、`fixtures/`、`compose/`、`tests/contract/`；实现 `run_id`、事件序号、状态机、artifact manifest 和适配器进程管理 | 可启动的 Runner/Adapter/Fixture Compose；协议测试 | 无候选 SDK 时也能提交任务、写事件、读取状态和生成 manifest |
| M2：统一执行基线 | 建立 Execution 容器、确定性工具、Playwright 测试站点、T1/T2 grader 和成本/耗时采集 | T1/T2 可重复运行记录、事件和产物 | 同一任务连续运行结果可比，失败能定位到工具或框架边界 |
| M3：DeepSeek Harness | 接入最薄适配器，完成远程模型、工具、浏览器、事件映射和最小结果回传 | DeepSeek 冒烟报告和缺口清单 | 通过远程 API、工具/浏览器和基础事件硬门槛 |
| M4：OpenClaw | 以 Hogee 业务基线为对照接入，记录 Gateway、Sandbox、Skill、流程状态和自定义入口 | OpenClaw 冒烟报告、流程透明度和定制成本证据 | 能完成统一任务，或形成可复现阻断项 |
| M5：Deep Agents/LangGraph | 实现可控 Agent loop、状态持久化和工具/浏览器桥接，保持同一执行基线 | LangGraph 冒烟报告和自研补齐清单 | 三个 P0 均有同口径的最小运行证据 |
| M6：生命周期专项 | 在模型等待、工具前后、人工审批和长命令期间执行 pause、restart、resume、cancel；验证至少一次副作用和 checkpoint 重建 | T3-T5 运行记录、恢复指标和故障复盘 | 能区分会话继续、任务恢复和无副作用重放 |
| M7：透明度、定制和成本 | 执行 T6；比较 Agent loop、状态、工具路由、事件关联、改动量、首次运行时间、token/费用和维护工时 | 能力矩阵、成本表、候选报告草稿 | 每项结论都有 `run_id` 或明确标记为未验证 |
| M8：结论与下一步 | 形成框架比较和完整方案比较；判断是否需要 P1/P2 或进入原型建设 | `docs/validation/reports/`、`decisions/` 和本文件更新 | 推荐方案、阻断条件和后续工作边界明确 |

实施前需要在 M0 明确以下口径：

- `stream_events` 的游标、事件序号、重连和去重规则；人工输入使用 `send_input`，审批使用 `approve`。
- pause 的安全点、in-flight 模型/工具调用处理方式、重试次数、幂等键以及至少一次/至多一次语义。
- 主比较轨道统一调用 Execution Playwright；候选原生浏览器作为独立变体，不与主轨道混合评分。
- provider 未提供 token/费用时，使用请求/响应 token 估算并在报告标记估算来源；成本按成功任务归一化。
- 能力硬门槛同时记录“原生能力”和“适配层补齐”；没有原生 pause 的候选不直接判失败，需结合补齐工时、可靠性和维护成本判定。
- 首轮暂不验证横向扩展、Worker 更换、真实浏览器进程持久化和 P1/P2；这些作为 P0 结果后的扩展项。

## 9. 来源与环境验证记录

本节区分“源码来源可达并已锁定”与“已在本地运行验证”。截至 2026-10-19，远程仓库通过 `git ls-remote` 可达，许可证通过 GitHub 仓库元数据核对；这只证明来源和许可证信息可核验，不代表框架已经在本地运行成功。

| 材料 | 官方来源 | 当前本地状态 | 已核对版本/许可证 | 运行验证状态 |
| --- | --- | --- | --- | --- |
| DeepSeek Harness | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | `../deepseek-harness` 已存在；工作区干净 | 本地 commit `dd6322d604e00eec1ba5e0c8541159906a21094a`；本地 LICENSE 为 MIT；远程 HEAD `687f282e8625c6b8d11ff58211a90524be2e4317` | Node `24.22.0`、pnpm `11.7.0`；`pnpm dsh --version/help` 和适配器 help subprocess 冒烟通过；远程模型 API 凭证未配置，模型/工具/浏览器任务待运行 |
| OpenClaw | [openclaw/openclaw](https://github.com/openclaw/openclaw) | 当前工作区未发现 OpenClaw 源码 checkout；Hogee 仅作为业务基线 | 远程 HEAD `b6e0872bdb7228e22fac7608345e0529a35ec46e`；仓库许可证元数据为 MIT | 尚未运行；M0 需在仓库外 checkout 并锁定 commit |
| Deep Agents | [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents) | 未 checkout | 远程 HEAD `0089e635dce525055272883b53bc222f385beac1`；许可证元数据为 MIT | 尚未运行 |
| LangGraph | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | 未 checkout | 远程 HEAD `98043c37abc60374030f960a03a6e0d4c0112643`；许可证元数据为 MIT | 尚未运行 |
| P1 候选 | OpenAI Agents SDK、Google ADK、Pi Agent Core、OpenAI Codex SDK、OpenHands 的官方 GitHub 仓库 | 均未 checkout | 远程仓库可达；许可证元数据分别为 MIT、Apache-2.0、MIT、Apache-2.0、MIT；具体 commit 进入对应阶段再锁定 | 尚未运行，暂不进入 P0 |
| Hogee 业务基线 | `/Users/hanbowen01/workspace/baidu/hogee/hogee-full` | 已存在；当前分支有预先存在的修改和未跟踪文件，只读参考 | 当前 commit `19faf627243e5ab2d043c7bc974830e9e55cb061`；不是候选框架来源 | 不作为候选运行，不修改该仓库 |

当前主机环境检查结果：Colima 已启动，Docker context 为 `colima`，Docker server `29.5.2`，分配 4 CPU、约 8 GiB 内存和 100 GiB 磁盘；Docker Compose `5.6.0` 可用，`hello-world` 容器验证通过。仓库 `.venv` 使用 Python `3.12.13`，已安装 Pydantic、HTTPX、PyYAML、pytest、Docker SDK 和 Playwright；Chromium 已下载，页面启动冒烟通过。Node `24.22.0` 可用。`./scripts/check-environment.sh` 通过；本仓库全量测试当前为 11 passed；Compose 配置校验、镜像构建、四服务启动和 Fixture `/healthz`、`/api/items` 访问均已通过。

DeepSeek Harness 的本地 CLI 入口和首个适配器已完成版本、帮助信息、源码预检及事件采集冒烟，证明本地 checkout 的 Node 依赖可加载；这不等同于远程模型 API、工具调用、浏览器执行或暂停恢复已验证。详细记录见 [`docs/validation/reports/2026-10-19-deepseek-harness.md`](docs/validation/reports/2026-10-19-deepseek-harness.md)。

M0/M1/M2 的本地条件已经满足。进入 M3 的远程模型任务前仍需完成 API 凭证的环境变量注入、候选源码 commit 写入运行 manifest、适配器事件映射和 T1/T2 的真实运行记录。来源确认、依赖安装和首次运行都必须各自留存证据，不能用源码存在替代运行成功。

## 10. 结论判定

每个候选最终输出以下四类结论：

- 可直接复用的能力和组件；
- 需要适配层或自研补齐的能力；
- 已验证的阻断项和不适用条件；
- 首次接入、场景定制、升级和运行维护成本。

首轮先形成证据矩阵，再进行框架比较和完整方案比较：

- **框架比较**：固定模型、任务、工具、环境和预算，分析框架差异。
- **完整方案比较**：允许采用各方案适合的配置，比较最终效果和总成本，并明确配置差异。

只有在同一任务和可追溯证据下，才对候选进行通过、部分通过或失败判定；不依据文档印象直接排名。
