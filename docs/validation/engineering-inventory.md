# 当前接入材料与工程投入清单

日期：2026-10-21。本文为 **I26 的输入材料准备，不表示 I26 已完成**。当前费用登记截点为010，I23本轮全部固定阶段已执行结束，I24/I25本轮尚未完成。第1–6节保留I21选择专项前的静态材料快照，不是当前目录总量；I22新增见第8节，I23–I25新增维护面见第9节。它记录四条详细路线的接入材料及公共实验设施，供后续比较修改范围、维护点和补齐成本。真实人工工时、业务交付剩余投入仍未知；尚未做候选版本升级及兼容性实验。

## 1. 这些数字能说明什么

原快照只读统计当时的 `adapters/`、候选打包文件、公共代码和 `external/build-context/<candidate>/LAB_ADAPTER_MANIFEST.json`，没有扫描或加载 `old/`。本次更新只读取仓库材料、专项报告及已有安全JSON摘要，补记已发生的接入范围；没有重新统计全目录、stage、构建镜像、运行测试、启动 Docker、查询SQL或调用模型，也没有改变候选和任务状态。

统计口径如下：

- 物理行数为 `len(file.read_bytes().splitlines())`，包括空行、注释和声明，不等于可执行语句数。
- 代码单列 `.py`、`.mjs`、`.ts`、`.js`；配置、README、依赖锁、bootstrap requirements 和历史差异记录分别统计。
- “当前目录”指四个候选适配目录内的文件，排除 `__pycache__` 与 `.pyc`；“staged 材料”只取对应 adapter manifest 列出的文件，不包含上游源码和第三方依赖。
- manifest、副本和仓库文件不重复相加；历史 diff 与它描述的当前代码也不重复相加。候选 Dockerfile、staging 脚本和候选 YAML 在适配目录之外，另列。
- 快照核对时，全部 staged 文件与对应仓库文件、manifest 中的哈希一致；四个 Dockerfile 与 staged 副本一致，四份 source manifest 文件哈希也与 adapter manifest 引用一致。本次更新没有重新核验这些旧快照、全部上游源码文件或实际镜像标签；专项使用各自独立上下文及固定镜像，不能拿旧清单替代其来源、构建和容器证据。

这些数字只说明原快照材料规模。不同语言、框架边界、诊断要求和历史改造方式都会影响行数，不能据此把候选按开发难度直接排序。仓库尚未记录可靠的逐项人工时间，也没有从归档基线计算净新增代码量；构建、下载或模型任务耗时不能代替研发时间。

## 2. 四条路线的原快照规模

| 候选 | 当前适配目录：文件 / 总行 | 其中代码：文件 / 行 | staged 材料：文件 / 总行 |
| --- | ---: | ---: | ---: |
| DSH | 9 / 1327 | 6 / 1064 | 8 / 1110 |
| OpenClaw | 12 / 1767 | 7 / 1130 | 12 / 1767 |
| Deep Agents / LangGraph | 12 / 2782 | 7 / 1138 | 12 / 2782 |
| OpenAI Agents SDK | 13 / 2310 | 9 / 1395 | 13 / 2310 |
| 合计 | 46 / 8186 | 29 / 4727 | 45 / 7969 |

DSH 的 README 为217行，没有列入其 staged 材料；其他三条路线的当前文件全部列入各自 manifest。完整路径、大小及 SHA-256 以这四份清单为准，本文不复制整份清单或上游源码目录：

| 候选 | adapter manifest | export SHA-256 前缀 |
| --- | --- | --- |
| DSH | [DSH 清单](/Users/hanbowen01/repo/puff/external/build-context/deepseek_harness/LAB_ADAPTER_MANIFEST.json) | `0b2bc9379bf5…` |
| OpenClaw | [OpenClaw 清单](/Users/hanbowen01/repo/puff/external/build-context/openclaw/LAB_ADAPTER_MANIFEST.json) | `3163a626178f…` |
| Deep Agents / LangGraph | [Deep Agents 清单](/Users/hanbowen01/repo/puff/external/build-context/deepagents_langgraph/LAB_ADAPTER_MANIFEST.json) | `9a3eb6a5c33c…` |
| OpenAI Agents SDK | [Agents SDK 清单](/Users/hanbowen01/repo/puff/external/build-context/openai_agents_sdk/LAB_ADAPTER_MANIFEST.json) | `b57d1de9087b…` |

非代码部分如下，不能把自动生成锁文件的行数记成业务研发代码：

| 候选 | 配置：文件 / 行 | 文档：文件 / 行 | 锁与 bootstrap：文件 / 行 | 历史差异：文件 / 行 |
| --- | ---: | ---: | ---: | ---: |
| DSH | 2 / 46 | 1 / 217 | 0 / 0 | 0 / 0 |
| OpenClaw | 3 / 21 | 1 / 165 | 0 / 0 | 1 / 451 |
| Deep Agents / LangGraph | 2 / 92 | 1 / 138 | 2 / 1414 | 0 / 0 |
| OpenAI Agents SDK | 1 / 19 | 1 / 167 | 2 / 729 | 0 / 0 |

## 3. 每条路线具体维护什么

### DSH

适配目录为 [adapters/deepseek_harness/](/Users/hanbowen01/repo/puff/adapters/deepseek_harness)。

| 代码文件 | 行数 | 作用 |
| --- | ---: | --- |
| `entrypoint.ts` | 137 | 请求校验、原 SDK 入口及详细变体分派 |
| `scoped-runtime-entrypoint.ts` | 446 | 实际 runtime 生命周期、工具挂载、原生事件与控制证据、Session 加载 |
| `tools-plugin.mjs` | 86 | 将统一 HTTP 工具接到候选工具接口 |
| `business-extension.mjs` | 209 | 查询工具、数量规则、业务观察及原生 projection 接线 |
| `detailed-options.mjs` | 125 | 详细任务配置、scope、浏览器凭证与下载证据检查 |
| `verify-source.mjs` | 61 | 候选来源与适配材料核对 |

候选提供 `runProfile`、`agents.create/resume`、Agent、工具 guard、Session projection/persistence 和官方 MCP client。工具 HTTP 转发、实验业务状态结构、规则、下载导出、身份绑定和独立诊断由本仓库提供。

当前详细变体按固定源码路径导入 CLI profile boot 和多个 runtime 模块。没有修改这些上游模块，但未来升级仍须核对源码路径、插件服务契约及 Session 格式。两份 `cordis.patch.yml` 共46行，是官方支持的配置覆盖；它们关闭或配置插件，不是修改上游源码的 diff。当前取消及暂停续跑是否满足要求仍取实际实验结论，不能由接口存在或材料行数判定。

### OpenClaw

适配目录为 [adapters/openclaw/](/Users/hanbowen01/repo/puff/adapters/openclaw)。

| 代码文件 | 行数 | 作用 |
| --- | ---: | --- |
| `entrypoint.mjs` | 364 | Gateway 启停、HTTP 任务接入、可信绑定与诊断 |
| `native-control.mjs` | 245 | 原 HTTP 传输断连、原生运行观测及取消证据 |
| `verify-source.mjs` | 61 | 来源与适配材料核对 |
| `tools-plugin/index.mjs` | 86 | 原生工具注册、hook、数量规则及 HTTP 工具接入 |
| `tools-plugin/browser-client.mjs` | 122 | 官方 MCP client 接线、传输观测及实际下载导出 |
| `tools-plugin/detailed-options.mjs` | 126 | 详细配置、凭证、路径和证据处理 |
| `tools-plugin/business-state.mjs` | 126 | 原生 Session 扩展状态的写入、读取及证据投影 |

候选提供 Gateway HTTP、`agent.wait`、plugin hooks/V2 factory、`session.state`、公开 session facade 和 `openclaw/plugin-sdk/session-store-runtime`。本仓库维护 Gateway 生命周期、任务与 Session 绑定、HTTP/MCP 桥及控制请求和原生终态之间的证据链。浏览器操作使用官方 MCP client 接统一浏览器，不能把本仓库这段桥接算成候选已经自带当前整套实验接入。

构建采用官方完整 recipe，并显式为 tsdown 配置串行参数；还需处理 Linux arm64 依赖、fs-safe native 和 SQLite 验收。这些是当前打包与接入维护点，不代表已经测过版本升级。

### Deep Agents / LangGraph

适配目录为 [adapters/deepagents_langgraph/](/Users/hanbowen01/repo/puff/adapters/deepagents_langgraph)。

| 代码文件 | 行数 | 作用 |
| --- | ---: | --- |
| `entrypoint.py` | 401 | Graph/模型/SQLite saver 创建、执行、冷读和原生诊断 |
| `http_tools.py` | 139 | 统一 HTTP 工具及候选工具调用接线 |
| `browser_tools.py` | 110 | 官方 MCP client、浏览器基础动作和下载证据 |
| `business_extension.py` | 104 | 业务状态 channel/reducer、数量规则和原始回执观察 |
| `run_control.py` | 96 | 公开 RunControl drain 请求与实际 GraphDrained 证据 |
| `detailed_support.py` | 179 | 详细配置、scope、凭证、终态和证据处理 |
| `verify_source.py` | 109 | 来源、安装分发与适配材料核对 |

候选及其官方配套提供 `create_deep_agent`、HarnessProfile、ChatOpenAI、`astream/aget_state`、AsyncSqliteSaver、ToolRuntime/Command、RunControl/GraphDrained。HTTP/MCP 桥、业务 reducer、观察结构和实验控制证据属于本仓库接入；drain 的合作停止语义应与立即取消分开评价。

独立锁共85包；[LOCK_I19_CHANGES.json](/Users/hanbowen01/repo/puff/adapters/deepagents_langgraph/LOCK_I19_CHANGES.json) 记录浏览器接入时由70增至85包，已有包版本未改变、增加15包。这是一次已记录的依赖材料调整，不是候选框架升级兼容性实验。未来还需维护 Deep Agents、LangGraph、LangChain、SQLite saver 和 MCP 配套版本之间的关系。

### OpenAI Agents SDK

适配目录为 [adapters/openai_agents_sdk/](/Users/hanbowen01/repo/puff/adapters/openai_agents_sdk)。

| 代码文件 | 行数 | 作用 |
| --- | ---: | --- |
| `entrypoint.py` | 463 | Agent/Runner/Session 创建、流执行、冷读与原生诊断 |
| `http_tools.py` | 140 | FunctionTool 与 HTTP 工具接线、规则与业务观察 |
| `browser_tools.py` | 109 | 官方 MCP client、浏览器基础动作和下载证据 |
| `business_extension.py` | 98 | mapping context 业务结构及公开 RunState roundtrip |
| `cancel_control.py` | 111 | 公开 immediate cancel、活动 hook 与原流收束证据 |
| `tool_definitions.py` | 85 | 已认证工具定义缓存及重建时的绑定核对 |
| `model_transport.py` | 102 | 原模型 HTTP/SSE 观察与截断终态检查 |
| `detailed_support.py` | 193 | 详细配置、scope、凭证和证据处理 |
| `verify_source.py` | 94 | 来源、安装分发与适配材料核对 |

候选提供 Agent/Runner、FunctionTool、RunHooks、SQLiteSession、RunState、公开 cancel 和 OpenAIChatCompletionsModel。本仓库提供 HTTP/MCP 桥、身份绑定、完整诊断、工具缓存和实际模型传输观察；独立锁共44包。httpx2、原生流终态、缓存绑定、RunState 重建和 Session 一致性都是当前接入需要维护的部分。

现有入口保存的 RunState 诊断、离线反序列化和 immediate cancel 不能直接代表暂停续跑完成。该路线未进入本轮I22；若后续展开，仍需单独接 after_turn、完整私有快照持久化及新容器 Runner 恢复，投入不可漏算，也不能把计划代码算成已实现。

## 4. 适配目录之外的候选专用材料

每条路线还各有一个 Dockerfile、staging 脚本及候选配置，下面行数没有计入前述适配目录总数：

| 候选 | Dockerfile | staging 脚本 | 候选 YAML |
| --- | ---: | ---: | ---: |
| DSH | 34 | 71 | 28 |
| OpenClaw | 71 | 63 | 27 |
| Deep Agents / LangGraph | 38 | 83 | 35 |
| OpenAI Agents SDK | 34 | 69 | 30 |

文件分别位于 [compose/candidates/](/Users/hanbowen01/repo/puff/compose/candidates)、[scripts/](/Users/hanbowen01/repo/puff/scripts) 的 `stage_<candidate>.py`、[configs/candidates/](/Users/hanbowen01/repo/puff/configs/candidates)。

还有四个候选专用的实验判定/触发文件：Deep Agents 的 `runner/deepagents_control.py` 213行、`scripts/native_triggers.py` 56行；OpenClaw 的 `runner/openclaw_control.py` 366行、`scripts/openclaw_trigger.py` 65行，合计700行。这些是外部实验验证与触发，不是框架运行循环。DSH/SDK 部分专用判定和触发仍写在公共 `runner/detailed.py`、`scripts/run_detailed.py` 内，本次没有强行拆分或把整份公共文件归给某个候选。

## 5. 哪些是公共建设

下面各组互不重复；候选适配目录、四个候选的打包文件及上节专用判定/触发文件不重复计入。公共入口同时支持其他初筛候选，不能把全部建设量分摊为四条详细路线的专属成本。

| 范围 | 文件 / 行 | 谁提供、用来做什么 |
| --- | ---: | --- |
| `execution/` | 5 / 667 | 本仓库：模型计数代理、工具 HTTP 接入、可信 scope 约束及基础工具 |
| `fixtures/` 代码 | 8 / 726 | 本仓库实验设施：模拟业务、网站、种子数据生成与故障行为 |
| 公共 `runner/` | 13 / 3485 | 本仓库实验设施：配置、准备、判定、原生状态检查、独立进程/资源/作用域证据；不含两份候选专用 control 文件 |
| 公共宿主编排 | 6 / 1615 | 本仓库：`lab.py`、`prepare_candidate_source.py`、`probe_candidate_budget.py`、`probe_native_state.py`、`run_detailed.py`、`run_detailed_batch.py` |
| 浏览器启动与基础验收代码 | 2 / 63 | 本仓库：`compose/browser/launcher.mjs`、`acceptance.mjs`；底层浏览器与 MCP server 为官方配套 |
| 公共打包配置 | 9 / 589 | 三个基础 Dockerfile、Compose/详细 override、浏览器 package/lock、两份公共 requirements |
| 实验任务与种子配置 | 11 / 467 | `configs/experiment.yaml`、九份任务 YAML、`fixtures/data/seeds.json` |
| 开发环境配置 | 3 / 201 | 根目录 `pyproject.toml`、`uv.lock`、`pytest.ini` |
| 当前校准测试 | 20 / 4453 | 本仓库实验验证；包含候选专用测试，本次只统计文件、未运行测试 |

前五组代码合计34文件/6556行。`runner/resource_budget.py`、`scope_observation.py` 等包含后续专项的判定准备，不能据其文件存在声称已有真实多用户后端或 I22–I25 已通过。

生产接入仍需要有对应的工具实现、身份/任务路由、状态存储、模型预算与生命周期处理，但不一定保留这些模拟服务和实验程序。种子数据、假业务、独立评分、哈希核对、故障注入与资源采样属于本轮研究设施，不能全部计作未来产品必需的运行代码，也不能把它们评价为候选原生能力。

## 6. 三种“patch”不要混在一起

### 本仓库的历史接入 diff

[OpenClaw BUSINESS_EXTENSION.patch](/Users/hanbowen01/repo/puff/adapters/openclaw/BUSINESS_EXTENSION.patch) 共451物理行，新增238行、删除32行，目标为本仓库的适配入口、插件、README、Dockerfile 和 staging 脚本共8处，没有上游源码目标。它保存当时的接入改造差异，不是候选上游核心 fork，也不是当前全部改造的累计差异。描述过的代码已经计入现有文件，不能再把238行加到当前代码量里。

### 官方配置覆盖

DSH 的两份 `cordis.patch.yml` 使用官方 profile 配置机制；官方源码启动还提供 source compatibility 配置。它们与 Git diff、依赖补丁的含义不同。当前 staging/Dockerfile 没有发现本仓库自研上游源码补丁应用；未修改上游不等于未来没有接口升级成本。

### 上游官方依赖补丁

固定上游自身的 workspace 和 pnpm lock 声明下列补丁。实际 patch 文件字节哈希逐项与其官方 lock 一致；它们属于上游依赖维护面，不计入本仓库自研代码。

| 固定上游 | 官方 patched dependency | patch 物理行 |
| --- | --- | ---: |
| DSH | `node-pty@1.2.0-beta.15` | 32 |
| OpenClaw | `@novnc/novnc@1.7.0` | 19 |
| OpenClaw | `baileys@7.0.0-rc14` | 68 |
| OpenClaw | `chrome-devtools-mcp@1.10.1` | 562 |
| OpenClaw | `ghostty-web@0.4.0` | 82 |
| OpenClaw | `matrix-js-sdk@43.0.0` | 199 |
| OpenClaw | `vitest@5.0.3` | 758 |

DSH 合计1项/32行，OpenClaw 合计6项/1688行。依据为各 staged 上游根目录的 [DSH workspace](/Users/hanbowen01/repo/puff/external/build-context/deepseek_harness/sources/main/pnpm-workspace.yaml)、[DSH lock](/Users/hanbowen01/repo/puff/external/build-context/deepseek_harness/sources/main/pnpm-lock.yaml)、[OpenClaw workspace](/Users/hanbowen01/repo/puff/external/build-context/openclaw/sources/main/pnpm-workspace.yaml) 和 [OpenClaw lock](/Users/hanbowen01/repo/puff/external/build-context/openclaw/sources/main/pnpm-lock.yaml)，patch 原文件保留在各自 `patches/` 目录。它们列在完整安装图中，不代表当前业务任务运行了所有相关功能。

Deep Agents 和 Agents SDK 使用已核验的固定源码及独立 Python 锁构建 wheel；当前接入未发现另行应用的自研上游核心补丁。这次统计也没有对全部第三方 wheel 做补丁或源码差异审计。

## 7. I26 还缺哪些依据

后续汇总应把现有接入代码、公共实验设施、候选原生能力及尚需补齐的能力分别写清。至少还需要：

1. 结合实际实验结果，判断哪些接入已经可用、哪些只是诊断或尚未完成；I22–I25 新增接线另记，不从本文推定完成。
2. 保留实际修改次数、调试失败、构建与自动运行等待记录；没有可靠人工计时的阅读、开发、调试和维护投入继续写 unknown，不用日历跨度或自动运行秒数换算。
3. 明确升级实验尚未开展。要评价升级成本，需另选版本，在同样任务下核对依赖、接口、状态格式及迁移行为；本次的固定版本重建和锁文件调整不能代替。
4. 在业务场景确定后，评估产品所需的最小接入及剩余研发量。当前实验目录行数和公共设施规模不足以推导生产交付工期。

真实能力、资源和模型用量仍以各候选实验报告及原始证据为准： [DSH](/Users/hanbowen01/repo/puff/docs/validation/deepseek_harness.md)、[OpenClaw](/Users/hanbowen01/repo/puff/docs/validation/openclaw.md)、[Deep Agents](/Users/hanbowen01/repo/puff/docs/validation/deepagents_langgraph.md)、[Agents SDK](/Users/hanbowen01/repo/puff/docs/validation/openai_agents_sdk.md)。本文不新增执行进度入口，实际状态继续回写根目录实施计划。

## 8. I22实施中的新增维护面

这里登记暂停专项材料。I22调研步骤已结束：两路线各三条原生暂停恢复成立，DSH完整业务3/3，Deep Agents完整业务1/3，另两条分页失败保留；不据此估算生产工期。下表行数是跨时钟修复前的静态快照，不是当前持续更新的总量；其后单调时钟、恢复会话头和离线全链复核增加的代码未逐行累计。专项使用独立镜像/构建上下文，不能把上一节的目录快照与下面文件机械相加；完整来源摘要以各正式链的 `trusted/pins.json` 为准。

| 部分 | 材料与本次物理行数 | 作用及归因 |
| --- | --- | --- |
| Deep Agents运行接入 | `entrypoint.py`451行（前快照401行）；新增 `pause_recovery.py`386行 | 原生drain、同步saver和None续跑由官方提供；本仓库补请求绑定、私有索引、关闭后发布和完整性校验 |
| DSH运行接入 | 新增 `goal-runtime-entrypoint.ts`31行、`goal-runtime.ts`264行、`goal-binding.mjs`132行及16行profile覆盖 | 官方Goal、round-driver、tool-goal和checkpoint插件；本仓库补步边界、空inbox/零在途检查、durable关闭与原Goal续跑绑定 |
| 公共实验编排与验收 | `scripts/run_pause_recovery.py`692行；`runner/pause_recovery.py`1088行、`pause_recovery_review.py`78行、`dsh_goal.py`110行 | 真实Docker生命周期、600秒监控、预算连续、物理状态及业务独立验收；主要是研究设施，不全是产品运行必需代码 |

已发生的接入调试包括DSH符号链接复制、GoalSpec/GoalView判定混淆、Docker删除错误文本、PregelTask wire格式、短容器采样竞态，以及代理替换造成采样空档。每次失败和后续校准均留在[暂停报告](pause-recovery.md)及外部原始证据中。这些反映真实集成与验证投入；不能归为模型任务耗时，也不能全部归因于候选框架缺陷。人工工时、版本升级工作量及生产剩余工期仍为unknown。

I23–I25使用与上述固定I22镜像分开的材料。其后已发生Docker校准和真实候选实验，当前范围与限制见下节；不能沿用“仅代码准备”，也不能把新入口存在写成专项全部通过。

## 9. I23–I25新增接线与维护范围

本节不重新报累计行数。它记录实现及调试实际碰到的契约，人工开发、阅读、排查和维护工时仍为unknown。每次失败、修订镜像和新run ID分别留存，不能以最终代码规模倒推这些投入，也不把实验台缺陷归为候选框架缺陷。

### I23：故障恢复与写入不确定窗口

| 部分 | 主要材料 | 本仓库维护什么，候选提供什么 |
| --- | --- | --- |
| DSH故障入口 | `adapters/deepseek_harness/fault-goal-runtime.ts`、`fault-goal-binding.mjs`、两个fault入口、fault profile及`fault/`工具桥 | 官方提供Session持久化、`agents.resume`的中断日志修复及原Goal继续；本仓库绑定原会话/Goal/剩余预算、核对实际恢复前缀和工具派发。修正官方round-driver先排下一轮、后推进`roundsStarted`的事件时序识别，并识别tool-goal的收尾notice；不是改上游执行循环 |
| Deep Agents故障入口 | `adapters/deepagents_langgraph/fault_entrypoint.py`、`fault_recovery.py`、`fault/`工具桥 | 官方提供Graph、saver和同checkpoint空输入继续；本仓库保存诊断、绑定原状态与剩余预算、核对真实工具派发。`Send`仅投影到可审计JSON，不改saver、Graph状态或恢复输入；原生task path中的bool按固定官方格式核对 |
| 独立物理审计与过程判定 | `runner/fault_recovery.py`、`fault_outcome.py`、`tests/test_fault_native.py`及故障系列测试 | bench只读重建DeltaChannel祖先链、pending writes和固定serializer的Send形态，与实际Graph/saver观察逐值比较；DSH独立核原日志提交前缀及原生修复。格式、来源或完整性不符仍停止，不由候选自报恢复成功 |
| 注入、编排和材料冻结 | `scripts/run_fault_recovery.py`、`run_fault_batch.py`、`stage_fault_bridges.py`、`execution/tools/fault_gate.py` | 公共实验设施负责四故障、真实kill/删A/新B、幂等和非幂等账本、预算连续、清理、冻结材料；这些不是框架原生运行服务，也不必全部进入产品 |

当前DSH `fault-round-order`新两模式基线2/2、四故障非幂等冒烟4/4；Deep Agents `fault-send`新两模式基线0/2、四故障冒烟2/4，未通过部分为业务分页失败。两路线真实kill换B原生恢复已有证据，DA kill链11项外层检查全过；旧Delta/Send投影中止保留。[DSH旧正式24格批](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/runtime/i23-dsh-round-formal-001/batch.json)首格通过、第二格因`fault_resource_sampling_gap`完整性停止，仅1/24有效，第二格不计候选成败。旧证据没有逐命令耗时，不能倒推具体慢点或预判候选缺陷，也不用业务通过豁免资源证据。

新增分阶段耗时诊断的第一版在review发现IO异常可能静默结束采样线程，随后修订材料经279项容器校准和四容器无模型probe，再执行独立[新正式矩阵](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/runtime/i23-dsh-timing-formal-001/batch.json)。前18格有效结束，17格完整通过，工具首次失败/幂等/303保留分页业务失败；第19格kill/幂等/101因采样inspect返回1产生`fault_resource_sample_error`而完整性停止。其原业务和原生恢复通过不能抵消观测错误，第19格不计候选成败，余5格未运行、不补失败或零消耗。batch判定指针null不能当成实际原verdict缺失。该问题属于实验观测维护面，5秒门槛不变。删除与清单互斥修订经287项容器校准、20次真实无模型删除探针后，DA首次正式24格已全部结束，完整10/24；14格仅分页失败，6条kill原生恢复全部通过，24格已提交操作重发均为0，原外层检查、host与清理均有效。

**I23本轮两路线的formal、baseline-rest及final固定阶段现已全部结束，业务失败保留。** DSH删除修订后的独立正式24格完整23/24，唯一失败为模型超时/非幂等/101的分页检查；正常基线rest为DSH4/4、DA2/4，最终404/505关键故障为DSH8/8、DA4/8，未完整通过均仅`all_pages_read`失败。早先101普通基线DSH2/2、DA0/2仍保留，两套bench分层，不拼为同一完整镜像组合批次，也不合并旧停止批成功率。两路线正式各6条kill及最终各4条kill均原生换容器恢复，所有阶段已提交操作重发均为0、外层检查有效、清理剩余为空。这完成本轮固定故障验证，不证明所有业务或生产故障窗口都已覆盖；人工阅读、接入、调试和维护投入仍未知。固定材料、原判定及调试过程见[故障报告](fault-recovery-execution.md)。

### I24：外置模拟产品后端及真实HTTP控制

| 部分 | 主要材料 | 已实现的职责及维护边界 |
| --- | --- | --- |
| 模拟产品边界 | `execution/control_backend.py`、`runner/shared_backend.py`、`shared_supervisor.py` | 从实际认证凭证绑定principal、任务、attempt和固定Worker地址，授权后路由state/events/artifacts/pause/resume/cancel，记录真实连接；不持Docker socket。这是本仓库外置应用接入，不能算Deep Agents自带多租户后端，也未接现有产品租户系统 |
| 单次Worker控制服务 | `execution/worker_control.py`、`compose/Dockerfile.multi-user-services`及独立multi-user候选上下文 | 一个控制服务只管理一个可信绑定的真实候选子进程，信号应答与原生drain/实际终态分开；换Worker仍使用I22原状态及空输入，不由后端提供重做提示词。当前不支持同Worker A→B→A复用 |
| Docker编排与独立HTTP验收 | `scripts/run_multi_user.py`、`runner/multi_user.py`、`tests/test_multi_user.py`、`test_multi_user_host.py` | 负责两主任务与两取消子任务、实际进展重叠、跨用户拒绝前后快照、断线/重认证和各自产物；每任务独立代理/工具属于实验部署开销，不是候选的强制实例模型 |

已发生的接入维护包括真实entrypoint来源核验、长期容器`--init`和143正常关闭、精确ID的stats EOF退出过渡、Mounts顺序规范，以及顺序等待超过15秒后HTTP连接失效。新客户端在控制POST/顺序轮询前换同身份连接，**不重发已接受的POST，不扩大服务idle timeout**；专门重连仍要求新认证、新connection ID及该新连接上的跨用户拒绝。采样豁免必须依原始精确ID/退出/删除证据成立，不把137/OOM或未知错误当健康。

旧`i24-da-fresh-smoke-101-001`完整执行，但两主任务业务和一条`transition_terminal_not_healthy`退出采样失败，原整体false保留。原件定位到已登记精确ID在正常退出、删除过程中被inspect为`removing/Dead`；新分支保留该原始快照，健康终态只能来自先前独立`after_stop`，其余字段必须一致，并额外执行精确ID inspect证明实际不存在。补充inspect后刷新短锁内的该ID登记，避免使用删除完成前的旧快照；Docker调用仍在锁外，原采样时限不放宽。另新增逐对象和网络的正向清理材料，未知错误不能当成已删除。

新bench `sha256:96fe603525839efe1c30a67246a0090c7bdc0c4c96674136ada24a7da6762397`、services `sha256:a68a4e1815cc529ff0ffa3b6c40ffba2e9b3a8dc1eec236326deb2f2567fe06d`的351项Docker断网校准通过，Worker不变；[新removal101轮](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-101-001/scope-verdict.json)已结束：边界/真实重叠/重连及实际资源/生命周期两项通过，A主任务与两取消通过，仅B主任务失败，整体仍false。host passed=true，采样/超额/errors均为空；独立[cleanup-result](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-101-001/cleanup-result.json)正向通过，同trial容器与网络最终均为空。这不覆盖旧fresh失败。

相同pins、独立新状态的[removal202](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-202-001/scope-verdict.json)和[removal303](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-303-001/scope-verdict.json)均已结束，CLI0；每组两个主任务各6次模型、两个取消各4次，共20次。两组scope/host/cleanup均passed=true，host errors、sampling_errors、resource_overages为空，同trial容器及网络清空。新三组产品边界/并发/重连、实际资源生命周期、两取消和正向清理均通过，整组结果2/3，101的B业务失败继续保留；取消仍是superstep边界协作drain，`immediate_cancel=false`。DSH专用接线与真实基线/双用户冒烟已执行，结果另记下文；浏览器使用期隔离与同Worker复用本轮未完成，resident成本入口也不能借本入口结果获得I24通过。见[多用户报告](multi-user-design.md)。

DSH专用入口在新bench中完成430项断网容器校准；新基线003真实9次模型、3笔正确写入，业务、原生、预算、0.75CPU配额、host和清理全部通过。双用户smoke已结束但整组失败：两主任务通过，两取消在模型结果在途/未知门禁停止评分，有效工具并发窗口未命中；B旧Worker删除中采样时确切ID仍存在，约21毫秒后才确认删除，不能用未来删除补判。正向清理通过，正式三种子尚未开始。维护面还包括零调用工具面允许合法空预算，以及从可信创建登记绑定启动后补全的完整网络ID；这些修订不改变原失败，也不替代候选能力验证。见[DSH执行记录](deepseek-multi-user-execution.md)。

费用登记另维护`scripts/summarize_research_usage.py`的scope闭合规则：核四条任务scope、固定4个模型代理/6个Worker/2个后端、原挂载与精确ID终态、删除先于评分，封住晚到付费writer或派发路由。旧fresh轮的闭合仅证明付费执行与路由已结束，不声称整个trial清理或退出健康；失败、137/OOM及远程未知记录继续留账。当前有效截点为010：343库/2485条reservation，含2416远程200、4远程未确认、1在途状态未知、36模拟、28注入。它保留009全部295项原对象字节及顺序，追加DSH正式24格、两路线各4格正常基线rest及各8格关键故障final共48库，新增500条计数（494远程200、6注入）；DSH旧停止批保留，旧009外部原件不覆盖。独立token提取器已在断网Docker核307项登记、285目录，2266份观察的输入/输出/总量已知、13份未知，已知总量部分为10,253,403 token；另11个材料单元无可用usage，仍无请求级关联。当前17个未登记库未查询：旧DA I24两库、DSH I24七库及I25 N5八库；后两类的15条确切路径及新格式缺口另见[安全说明](research-usage-unenrolled-formats.json)，不为凑齐总量扩建reader。SQL、token及新增阶段分组各自的断网容器均无模型key、健康退出0并确切清理，费用登记不改变原能力判定。I24/I25本轮及I26尚未完成，金额和人工工时仍未知。维护原生去重、字段换算、安全输出和来源绑定属于本仓库计量设施建设，不能计为候选自带能力，也不据此提高业务成功数。过去009为295库/1985计数、7,769,302已知token的历史截点，保留在[费用历史](costs.md#过去截点记录不作为当前数字)及外部原件中。具体边界见[token计量记录](token-usage-design.md)。

### I25：保留真实对象的入口、观察器及成本编排

| 部分 | 主要材料 | 已实现的职责及维护边界 |
| --- | --- | --- |
| 独立resident入口 | `adapters/deepagents_langgraph/resident_runtime.py`及`deepagents_langgraph-instance-runtime`上下文 | 保留真实模型、HTTP工具、Graph和SQLite saver；每条新业务任务重新绑定独立客户端/工具/图/存储，最近自然结束的任务对象可查询。常驻或关闭由宿主策略选择，不是框架自带休眠服务；命令通道是实验可信编排通道，没有产品认证、任务队列或池化能力，也不提供故障A恢复 |
| 受保护观察与判定 | `runner/instance_cost_full.py`、`tests/test_instance_cost_full.py` | 绑定原生事件/状态、阶段、任务类型和nonce，核对历史冷读、真实并发、配额、回收、唤醒及单Worker故障影响；候选活动与实验观察器的开销分列，不能把空壳存活当作真实框架常驻 |
| 生命周期及资源编排 | `scripts/run_instance_cost_full.py`、`run_instance_cost.py`、`runner/instance_cost.py` | 原生历史准备、60秒空闲、两真实任务、60秒回收、冷读、新任务唤醒和独立故障子轮；按完整Docker ID采集全清单，固定6 GiB/3 CPU。resident入口的I24隔离与真实产品后端开销仍未接入 |
| 全阶段采样补审计与非活跃用户轮换 | `scripts/audit_instance_cost_sampling.py`、`tests/test_instance_cost_sampling_audit.py`及上述实例入口/测试 | 旧trial只读、新审计另存；核所有记录阶段的时钟、间隔及control绑定，缺失序号/首尾/写盘证明保持未知。显式轮换为N5/N10首次唤醒003/004，最多双活跃，独立scope/存储/凭证/预算，核原历史及回收后业务态；属于本仓库实验编排，尚非产品池化服务 |
| 轮换生命周期观测修订 | `scripts/instance_cost_lifecycle.py`、`runner/instance_cost_lifecycle.py`、真实无模型probe及对应测试 | 长等待退出采样锁，按确切ID保留创建/关闭/终态/删除证据，独立核全阶段5秒、配额与缺测；服务实际Init配置也要核验。观测失败停止新付费任务，资源缺测不补0，属于公共实验设施 |

观察器调试是公共实验设施投入：同名控制文件原子替换在实际宿主挂载中出现stat可见而open报`FileNotFoundError`，单descriptor读取校准通过也未解决实际挂载。现改为连续12位序号的不可变新文件，观察器逐号消费，reply绑定序号/phase/kind/nonce；仍要求同descriptor、普通单链接文件、NOFOLLOW和4096字节上限，保留8秒probe限制。348项容器校准及真实挂载63条控制/60探针零错误，随后baseline004观察/采样/配额/状态/关闭/清理通过；其5次模型/3笔写入仍因分页业务失败而整体未通过，旧失败全部保留。

同材料seed101的[两用户按需](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-od-101-001/protected/flow-verdict.json)与[常驻配对](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-resident-101-001/protected/flow-verdict.json)均已完整执行，各五个可评分业务3/5、分别33/34条请求计数；按需active-B、常驻active-A及双方fault-B分页失败，故障A无结果且未恢复。原业务态在新容器冷读或常驻对象保留后核对通过，两新任务唤醒、实际并发及杀A后B原生进展成立；原观察、空闲采样、资源、关闭和清理检查保留，业务和故障复合失败仍保留。

补审计发现旧采样判分只约束空闲窗口节奏，没有要求启动、控制、关闭等全部阶段的间隔不超过5秒。新[按需审计](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-legacy-sampling-audits-001/proof/on-demand.json)在136份样本中发现18处超时，最大相邻开始间隔23.670秒；[常驻审计](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-legacy-sampling-audits-001/proof/resident.json)在140份中发现14处，最大23.598秒。单份采样跨度、时钟顺序、control绑定及完整JSON行均通过，但旧样本无连续序号，采样线程完成/总行数、完整起止和首尾边界无证据，继续为null。两份新审计整体均失败，不补边界0秒，不改旧verdict，也不能从旧均值推完整峰值或覆盖率。

新[313项Docker校准](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/token-rotation-check-001/host.json)已通过，覆盖token用量、实例计划、完整轮换及离线审计四份测试；断网、无模型或远程key，512 MiB/0.5 CPU，实际健康退出且清理通过。它只说明代码及反例边界通过，不能代替真实观测门禁。后续生命周期短锁、确切ID登记及服务Init修订已完成405项断网容器校准；真实无模型probe001因辅助服务实际退出137失败，probe002因评分入口导入失败，原失败保留。helper修正导入后，全新probe003的35份样本、23项独立检查、评分健康和精确清理全部通过，0模型/业务，不等于业务成本通过。按需N5轮换随后已真实执行，结果单列下方。默认旧两用户流程保持不变，显式轮换是新变体，常驻N5/N10仍按完整固定分配预拒绝。

按需N5固定101轮换已执行、独立评分和清理完成，7个可评分业务4/7；此前未活跃用户003/004首次业务均通过。active001、wake001、fault002均只因`page_size=100`违反每页最多5条失败，报告、实际写入和scope仍正确。故障实际只杀A且退出137、非OOM，B之后仍有原生进展；复合故障项因B业务分页失败而false，不能写成误杀或B停止。

N5共148份样本，无sample error，原5秒节奏、确切登记与评分进程检查通过。但回收后空闲第82份样本跨到随后业务态冷读：前清单只有Fixture和observer，后清单多了冷读Worker，缺该Worker本份stats/top。该阶段29份完整、1份不完整，整体均值/峰值保持未知；不改阶段、不删样本、不补0，N10先停。它揭示阶段交接仍有观测维护量，不能据无模型probe通过就宣称业务成本完整有效。

旧两用户探索初始/回收后两个60秒空闲窗口，整套内存均值分别为按需31.41/47.85 MiB、常驻282.10/312.63 MiB，容器数为2对4；保留的是两个已加载的真实Agent对象。两用户新唤醒的实验可信请求至首次原生活动为按需21.08/21.06秒、常驻13.26/13.26秒，包含本实验服务准备，没有产品调度或端到端延迟结论。数字含Fixture及观察器，不包含真实产品后端、宿主/VM或生产存储费用；不能用这组小样本推现网收益。

常驻N=5/10分别保留启动前固定配额拒绝记录，`Docker_started=false`、`observed_RSS=null`，没有执行Docker或实测RSS/OOM。按需N5及未活跃用户轮换已执行但整轮失败，N10因回收后空闲窗口缺测先停。其他种子、故障A恢复、resident同入口I24、完整模拟产品后端开销及DSH成本本轮未完成。允许业务失败后继续独立生命周期探索，不代表完整成本比较成立；计划`formal_i25_eligible=false`及两原判定`i25_complete=false`、`savings_percent=null`均不改。过程和材料见[实例成本报告](instance-cost-flow.md)。

这些实现表明实际维护面涉及官方状态格式、控制语义、HTTP连接、容器生命周期和观测协议；还不能回答投入了多少人日或生产还要多久。未来产品可复用其中必要的绑定、工具、持久化与生命周期接入；独立评分、故障注入、模拟业务和受保护采样主要服务于本次研究，应分别估算。
