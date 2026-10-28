# DeepSeek Harness 容器初筛与详细验证

日期：2026-10-20。对应任务 I08。**I08 已完成：真实模型和多步工具任务连续两次通过，真实候选重试及换容器后的限额补验通过。** 这不是浏览器、暂停恢复或选型结论。

I17 已新增五类任务 × 101/202/303 三组的 **15/15 正式通过**，累计 121 次真实模型请求。浏览器其中两轮用满 20 次预算；原生取消的结束事件缺口仍存在，冷读003已确认本实验必需自定义事件被原生加载拒绝，公开工具元数据新接法已通过Docker公共校准、真实extension任务和23项换容器冷读，受影响两类正式三组6/6通过，I17已完成调研步骤并保留取消缺口。

最终404/505已使用新保存方式镜像完成五类各两轮，**10/10通过、78次真实远程请求、14笔写入**。原候选镜像、保存接法和进程验收有版本差异，原15轮、新保存6轮和最终10轮分组保留，不能拼成同一完整镜像/验收组合的25轮；详见末节。

2026-10-21更新：I24已完成DSH专用接线与相关六文件 **430项容器校准**；前两次单用户基线均在模型调用前因实验接线失败，原件保留。新材料的 `i24-dsh-baseline-101-003` **单用户基线完整通过**：9次模型调用、3笔正确写入、34.596秒。双用户 `i24-dsh-smoke-101-001` 已结束、整组失败：两主任务通过，两取消因模型结果在途/未知未评分，有效工具并发窗口未命中，删除采样证据不足；正向清理通过。正式三种子本轮未完成，不继续修实验台扩跑；状态和固定材料以 [I24执行记录](deepseek-multi-user-execution.md) 为准。

## 用了什么

官方源码 `dd6322d604e00eec1ba5e0c8541159906a21094a`，版本 `0.1.2-alpha.3`。真实 `DeepSeekHarness` SDK 使用官方 `sdk-minimal` 源码启动路径；未修改上游内核。镜像固定 Node 24.22.0 Linux arm64，pnpm 11.7.0 按上游 frozen lock 安装。构建及运行时核对 9044 项源码和 4 项接入文件哈希。

正式镜像：`sha256:002d46f412752aca61ece1eb69bec7f0d5674417a94b64c7703542b3c3bfbf6f`。候选配额 2 GiB、1 CPU；只读根文件系统。候选仅持有本地运行 token，远程模型密钥只在计数代理。工具、Fixture、代理和独立验收均在容器运行。

薄接入包括 SDK 入口、工具插件、Cordis patch 和来源校验。关闭原生 Shell/编辑器，以统一 HTTP 工具进行同题比较；插件只转发参数，不代办筛选、计算或写入。普通操作自动执行，敏感拒绝由公共执行层提供。正式 `sdk` profile 和原生工具变体尚未测。

## 实际做到了什么

Agent 根据目标读取 `request.json`、查询所有分页、自行筛选并计算，提交业务写入，再生成 `report.json`。预期答案由外部验收从种子独立重算，未交给 Agent。

| 运行 | 种子 | 模型请求 | 实际写入 / 写入请求 | SDK 运行耗时 | 外部验收 |
| --- | --- | --- | --- | --- | --- |
| `i08-dsh-tools-001` | 101 | 9 / 上限 20 | 3 / 3 | 26.469 秒 | 12 项全部通过 |
| `i08-dsh-tools-002` | 101 | 5 / 上限 20 | 3 / 3 | 18.159 秒 | 12 项全部通过 |

第一轮候选容器已删除，第二轮从同一镜像创建，容器 ID 分别为 `244c47a6e3c7…`、`84ce290f9d9a…`，使用各自干净状态、工作区与账本。第二轮是删除后重新执行验证，**不是已有任务的恢复实验**。相同种子两次成功不能替代后续三组或五组输入比较。

两轮原生终态均为 `completed`，模型请求全部经持久计数代理，有真实上游 2xx。报告、分页、工具 start/finish 配对、scope、请求和实际写入均由外部核对；不能只依赖 SDK 的 completed。模型金额因缺少可靠单价仍为未知。

原生 `assistant/message.usage` 按消息各计一次（不再累加 chunk 中的重复 usage）：两轮 `totalTokens` 分别为 26490、14043，`outputTokens` 为 2291、1702；原始缓存等字段保留在 `native-usage-summary.json`，不重复加入 total。小样本的调用和 token 差异只能作观察，尚不能据此推算生产成本。

证据在 `runs/2026-10-20/deepseek_harness/tools/<run_id>/`：`manifest.json`、`input.json`、`model-calls.json`、`ledger.json`、`tool-events.jsonl`、`verdict.json`、资源采样，以及 `candidate/` 内的原始通知、SDK 完整结果和来源信息。候选只能挂载其输出子目录，不能改工具审计。

复现：

```bash
uv run --frozen python scripts/lab.py build --candidate deepseek_harness
uv run --frozen python scripts/lab.py run --candidate deepseek_harness --case tools --seed 101 --run-id UNIQUE_ID
```

## 边界和投入记录

首次依赖安装约 4 分 33 秒，分层后重建安装约 4 分 27 秒；它们是自动构建时间，不能当作研发工时。完整上游 workspace 安装有不需要的附带依赖，后续资源比较应单列镜像和构建开销。原生通知、工具调用和usage可查；I17的实际缺文件、504与取消错误已用于评价诊断边界，结果见下文。

开跑前发现 Compose 的 profile 默认过滤了模型服务，导致编排读取模型配置失败；未发出模型请求、未创建该轮数据。已显式启用 model profile 后重跑。该问题属于实验编排，不算 DSH 能力失败。

初筛阶段未覆盖浏览器、运行中控制、已有任务恢复、故障一致性、后端鉴权和共享实例成本；后续详细实验单列如下。已有会话文件不等于任务恢复通过，所选入口的缺口也不能扩大为整个 DSH 不支持。


## 重试和累计限额补验

`i08-dsh-budget-001` 使用同一 DSH 镜像和真实 Agent 循环，模型上游替换为容器内固定 503 的模拟服务，单独内部网络禁止访问公网，未注入真实 API 密钥。代理上限 2 次；首轮记录两次原生 `llm/retry` 并命中限额，累计只有两次请求到达模拟上游。

删除首轮候选与计数代理后，分别创建新容器，保留同一预算数据库再执行。候选再次命中限额，上游请求仍为两次；候选和代理的容器 ID 均不同，账本原样保留。独立验收 **17 项全部通过**。两轮候选都明确以错误终态退出，没有伪装成业务完成。

证据与上述工具运行同级，目录 `i08-dsh-budget-001/` 内有两轮原始事件、`snapshot.json`、容器/镜像记录、模拟上游账本与 `budget-probe-verdict.json`。此补验验证调用拦截，不代表任务暂停或恢复；计数能力由实验代理提供，属于自研公共运行层。

```bash
uv run --frozen python scripts/probe_candidate_budget.py --run-id UNIQUE_BUDGET_PROBE_ID
```

## I17 详细实验

详细入口改用官方 `runProfile` 与 `agents.create/resume({setup})`，按任务作用域挂载工具、规则及浏览器连接。未修改上游内核。下列原批次的业务扩展使用公开 `Session.append` 写自定义必需事件、`sessionProjections.register/stateOf` 和 `sessions.flush`；新增 `query_order_status` 只转发单次原有 receipt 查询，数量上限通过公开 `tools.guard` 在 HTTP 写入前执行。原生状态的精确行、哈希、业务回执和独立审计均需对应，不能从写入响应猜测“已确认”。该自定义事件方案的冷读兼容缺口及后续源码调整另见下文，不改写原批次证据。

| 冒烟运行（种子101） | 外部验收 | 模型请求 | 原生终态 / 入口耗时 |
| --- | --- | --- | --- |
| `i17-dsh-tools-smoke-001` | 16项通过，3次请求/3次写入 | 6 | completed / 22.792秒 |
| `i17-dsh-sensitive-smoke-001` | 11项通过，真实guard拒绝、0写入 | 2 | completed / 4.022秒 |
| `i17-dsh-extension-smoke-001` | 19项通过，3笔真实查询回执及原生业务态一致 | 7 | completed / 24.891秒 |
| `i17-dsh-extension-denied-smoke-001` | 11项通过，数量4前拒绝，工具HTTP及业务均无write | 2 | completed / 5.125秒 |
| `i17-dsh-browser-smoke-001` | 13项通过，登录、3页、表单、1笔写入及真实CSV下载 | 18 | completed / 49.794秒 |

前四项镜像 `sha256:0f51f3e4e7ceeed24b030f31db63f62a8d170ab7f93a5d9978c9c9053d136199`；浏览器使用新镜像 `sha256:35fa3a70349add94194f8c66547e1c856ab4d1d5ec6f57dca54108b23041a2c6`，接入材料hash `4840c21833e069b85b3e1de18d009e591c41314bd1c79ce64302bf97e2ed7315`。新镜像增加公开取消钩子，并将下载识别限定为官方MCP返回的独立 Events 小节，排除页面正文或代码块中的伪下载说明。构建证据在同候选 `runtime/i17-dsh-build-002/`。

浏览器采用容器内统一 Playwright MCP，DSH通过原生 Connection 逐步选择基础动作；没有HTTP业务工具代办。接入层仅核对MCP实际下载记录并拷贝已存在的CSV，独立验收核对字节与账本。此冒烟说明 hsi 浏览器连接迁移方案能在本地 Docker 完成此任务，浏览器门槛在这个入口上已有通过证据；当时为18/20次请求，正式多组结果另列如下。

`control-policy.yaml` 在早期敏感冒烟之后补充了诊断/控制定义：旧run保留当时input hash，不用新任务文件倒评旧run；正式三组使用最终版本。原始证据见每个 `<case>/<run_id>/` 的manifest、input、独立verdict、原生事件及账本，不在报告复制原始推理文本。

### 正式三组：15/15 通过，浏览器预算余量很小

批次索引：i17-dsh-formal-batch-001/batch.json（`runtime/i17-dsh-formal-batch-001`）。已逐轮交叉核对 manifest、input、verdict、原生 result、模型调用及资源指标；批次汇总与各轮证据一致。15轮均为不同候选容器 ID，使用同一镜像 `sha256:3c4da6e39b8c237a3df6a9bfcaf291050c6ed922bbba43dd9d1b372aa16c8504`、同一接入材料 `f9058d67826f632d7f177ce0ee4c02a646526689089003f29982ea1fc68f9f95`；bench 镜像为 `sha256:f5031a02d411a650a72dc7a768f61ce6332a831518b3fa7c31cb9eae2e35ad8a`。任务文件 SHA256 与批次冻结记录及当前定义相符。

| 用例 | 101 / 202 / 303 模型请求 | 各轮验收 | 各轮实际业务写入 | 三轮入口耗时范围 |
| --- | --- | --- | --- | --- |
| tools | 8 / 10 / 7 | 各16项全部通过 | 各3笔，写入请求各3次 | 25.422–28.456秒 |
| browser | 20 / 20 / 16 | 各13项全部通过 | 各1笔、访问3页、真实CSV下载 | 39.333–58.178秒 |
| sensitive | 2 / 2 / 2 | 各11项全部通过 | 0笔；真实候选执行guard拒绝 | 4.433–6.080秒 |
| extension | 9 / 7 / 11 | 各19项全部通过 | 各3笔，写入请求各3次；逐笔查询回执及原生业务态一致 | 22.633–31.262秒 |
| extension-denied | 3 / 2 / 2 | 各11项全部通过 | 0笔；数量4被执行前拒绝，0次工具HTTP write | 4.587–8.172秒 |

运行 ID 统一为 `i17-dsh-<case>-seed-<101|202|303>-001`，各自位于同候选对应 case 目录。15轮原生 `status/native_status/stop_reason` 均为 completed，候选和独立验收退出码均为0；121次代理请求均实际转发并取得上游200，没有以注入错误或限额拒绝充数。合计21笔业务写入来自tools和extension各9笔、browser共3笔；拒绝任务均无业务写入。

browser的101、202轮正好在第20次模型请求后正常完成，并未发起第21次，也不是超限后改判成功。但两轮已无剩余调用额度，303轮也用了16次。当前证据仅说明三组固定页面任务在本轮预算内完成，不能据此认为对页面变化、重试或更复杂任务有足够余量；不调高预算掩盖这个结果。原批次每种输入只运行一次；后来404/505的不同材料组见末节，仍不能换算成生产成功率。

`resource-metrics.json` 均已保留，15轮合计226条资源样本、7次采样错误。指标包含本轮同时运行的候选与配套容器，不能当成候选独占内存或每用户增量；采样也不是完整连续轨迹。121次仅为这一正式批次，不含先前冒烟、取消失败、构建和诊断投入；价格未知，金额仍为null，失败支出和研发投入后续在I26汇总。此批次没有验证跨用户隔离、共享Worker、暂停续跑或生产实例成本。

### 诊断与取消

`i17-dsh-diagnostic-smoke-001` 读取不存在的文件，真实工具返回400；11项验收通过、模型2次，原生completed，工作区和业务均未写入。这覆盖工具执行错误，尚非无效schema参数测试。

`i17-dsh-diagnostic-timeout-smoke-001` 第一请求由代理注入HTTP504、不向远程模型发送；后续9次请求正常转发，3笔写入/请求，17项验收通过。原生seq9的 `llm/retry` 与seq10的 `llm/retry-started` 分别记录计划和实际重试。固定DSH把504归为SERVER；这验证临时504后的恢复，不能称为真实网络空闲超时或provider watchdog验证。

取消使用公开 `Agent.cancel({kind:'hook',reason:'experiment_cancel'})`，宿主只在观察到实际turn/start后发SIGUSR1。`i17-dsh-cancel-smoke-001/002` 均未通过严格验收：每轮1次模型请求、0业务写入，API执行时running，随后whenIdle返回且idle/noPending，但原生Session与通知中均缺 `turn/end`，结果保留incomplete/missing_turn_end。未用强杀或completed掩盖失败。

002仅新增公开 `agent/error` 观察，镜像 `sha256:3c4da6e39b8c237a3df6a9bfcaf291050c6ed922bbba43dd9d1b372aa16c8504`，接入材料hash `f9058d67826f632d7f177ce0ee4c02a646526689089003f29982ea1fc68f9f95`；`native-errors.jsonl` 实际报错为 `session event "turn/end" carries non-JSON-serializable data`，关联最后原生step/end（raw第9行）。固定源码 `packages/core/agent-loop/src/agent.ts` 的turn finally会append结束事件，但kick捕获异常后仍设idle；本次记录证明结束事件写入失败，排除了仅接入观察者漏收或未flush。Node fetch修改可变abort cause与严格JSON快照的冲突是源码/stdlib复现支持的原因线索，尚未修改上游确认唯一根因。

影响：当前固定版本与Node24.22组合能停止这次执行，但取消终态及审计链不完整，业务接入不能只认idle。补齐至少需要修正/升级原生事件写入，并重验模型和工具在途停点。此缺口不等于所有DSH入口都无法取消，也不证明已有任务可暂停续跑。

### 冷读003：必需自定义事件的兼容缺口与重新设计

`i17-dsh-native-state-003` 对正式 `i17-dsh-extension-seed-101-001` 的原生状态做真实换容器冷读。旧候选已删除，新容器ID不同且镜像相同；未启动模型代理、未注入上游凭证、没有新增模型或业务工具调用。候选公开 `agents.resume` 返回 `failed/runtime_error`，报错为 `session "main" contains event type "harness-lab/business-state" (seq 924) unknown to this harness and not marked ignorable`。详见原生失败结果（`i17-dsh-native-state-003/candidate`）及失败verdict（`native-state/i17-dsh-native-state-003`）。因加载失败而没有业务态导出文件，验收随后报告 `Native evidence must be a regular file`；该文件缺失是结果，不是原生拒绝的原因。原始state、工作区、预算、HTTP审计及正式批次证据均保留。

此项准确归因为实验业务接入与固定版本的事件兼容契约不匹配：源码 `known-event-types.ts` 的事件目录为构建时生成，持久化 `prepare` 仅接受已知事件或明确可忽略的外部信息事件；`sessionProjections.register` 注册的是投影，不会把事件类型加入该目录。公开 `agents.resume` 的加载也先于 Agent setup，且固定源码明确未提供外部事件名早期注册机制，因此简单前移投影注册无法解决。业务态为必需数据，不能改标 `ignorable` 来绕过拒绝。该结果不说明所有DSH原生状态都不可恢复，也不否定原15/15任务在原容器内的完成证据。

已实现最小公开接口重新设计：实际 write/receipt 工具的 canonical output 携带完整业务状态，公开纯函数 `output.presentationMeta` 将其投影为 `tool/result.data.meta.business_state`，由官方调度器在真实工具完成时追加原生 `tool/result`；业务投影只从该真实元数据和实际写入调用重建状态，不抢写或伪装内建事件。给模型渲染的仍是原HTTP工具结果，`query_order_status` 仍仅发送一次原receipt请求，数量上限guard与拒绝前不转发HTTP的规则不变。真实 `tool/call(write)` 会使旧确认失效，并在实际转发写入前flush该调用；导出仍保留原生日志精确行与字节哈希，状态pointer改为 `/data/meta/business_state`。

当前工具定义未声明 `isConcurrencySafe`。固定公开工具分类器默认返回 `exclusive`；调度器等待单个工具结果完成并追加原生metadata后，才启动下一个调用，下一次 `stateOf` 会补算到最新Session cursor。即使模型一次返回多个工具调用，当前whole-state快照也按该串行顺序累积。若以后显式开启业务工具并行，多个调用可能从同一个旧状态生成快照，按顺序提交结果也不能避免覆盖；需要重新设计状态合并，不能靠外层补账掩盖。这是本接入方式的扩展限制。

改造涉及3个候选接入文件及原生状态验收/校准2个文件，未改上游、旧日志或旧失败结果，真实人工工时未知。新来源校准增加2个正例及12个反例，覆盖原生callId、工具别名/参数、operation、receipt来源、原生结果内容、pointer和过期状态；新增校准已在Docker的`i17-metadata-check-001`通过，全套364项通过（21.45秒），bench镜像`sha256:d4489e54d827e324df6274b08599ab782da05fc21722ed190d3dc87e82bf9c0b`。随后新镜像、新任务及冷读验证如下；数量拒绝及受影响正式三组也已通过。旧15/15、冷读001/002接线失败及003原生加载失败均原样保留。

### 新保存方式补测与 I17 收尾

正式101/202/303的五类任务原批次已完成。新保存方式的`i17-dsh-extension-smoke-002`真实19项全过，10次模型请求、3笔写入/3次请求，原生completed。新镜像为`sha256:dc5238ff6e83883e223619d149c4b784e55f491c86b3fa7be90dc99ce29336ed`，adapter export为`0b2bc9379bf53d47edbfe8f4206a81e1ed547ed8673b069f18bcdd1822872be7`，来源清单hash`209ab1e04aa4897d91c67991b1d2269bf4ef3e31aea1b3f3ed7793494c67ece4`，构建材料留在`runtime/i17-dsh-build-004/`。

该新run的冷读004（`native-state/i17-dsh-native-state-004`）真实23项全过：旧Worker确已删除，同镜像新容器ID，经公开原生加载和投影读回相同完整业务状态；原日志前缀保留，仅允许原生加载边界追加，工作区/预算/HTTP审计/原candidate证据不变。没有启动模型代理、注入真凭证或新增业务工具调用，独有探针资源已清理。DSH加载需要写入原生边界，因此state为受校验的rw挂载，不能写成全程只读。此结果只证明换容器可重建业务态，不是暂停或任务继续。冷读001/002接线失败和003自定义事件兼容失败保留，不改旧日志，也不把新方案倒套到旧正式批次。

新接法的数量拒绝冒烟11项通过、2模型/0写；extension/extension-denied各101/202/303也已完成，6/6通过、31次真实模型请求、9笔写入。三组extension为10/9/6次模型、各3笔写入和实际回执；三组denied均2次模型、0写入。所有原生completed，候选/验收退出码均0，6个不同Worker，同一新候选镜像与bench。批次索引为i17-dsh-metadata-formal-batch-001（`runtime/i17-dsh-metadata-formal-batch-001`）。未涉及业务扩展分支的tools/browser/sensitive原结果仍有效，不无故重复全部任务。

诊断已覆盖真实缺文件400和代理注入504后的重试，尚未覆盖无效schema参数或真实网络空闲超时。取消两轮原生结束事件写入失败的缺口必须保留；现有证据只覆盖模型活动时发取消，不能推断工具写入在途的取消效果。I17允许以明确缺口记录研究结果，不以无限修上游作为完成条件，但应把尚未覆盖的停点和补齐范围写清。本轮薄接入位于scoped-runtime-entrypoint、tools-plugin、business-extension三个候选文件及profile配置；原生循环/Session/调度/重试来自官方，Playwright MCP为官方配套，作用域/规则/回执别名/metadata业务投影为业务接入，Docker编排/预算代理/独立验收为公共自研实验层。新6轮原生入口累计102.207秒；它和构建日志只反映自动时间，真实人工工时未知，I26再按同口径汇总代码和失败支出。五组关键任务、10分钟暂停续跑、故障写入窗口、后端鉴权和实例成本分别留给后续专项，I17已完成约定实验并以明确取消缺口收尾；不等于全部能力通过。

## 最终补充：404/505

final-dsh-batch-001（`runtime/final-dsh-batch-001`）已结束，SHA-256为`5992321cff4b56dc3c73c26a8c66a395ccb3cb5c5007d07a7220d308aaa251b9`。五类各两轮，**10/10完整通过、78次真实远程模型请求、14笔实际写入**。

| case | 404/505完整通过 | 请求数（404/505） | 实际写入（404/505） | 核对结果 |
| --- | --- | --- | --- | --- |
| tools | 2/2 | 7/7 | 3/3 | 各16项通过；分页、筛选、报告、账本正确 |
| browser | 2/2 | 19/18 | 1/1 | 各13项通过；真实三页访问、提交和CSV；剩1/2次模型请求 |
| sensitive | 2/2 | 2/2 | 0/0 | 各11项通过；执行层真实拒绝、无敏感副作用 |
| extension | 2/2 | 11/8 | 3/3 | 各19项通过；逐笔新增查询和原生metadata业务态与实际回执一致 |
| extension-denied | 2/2 | 2/2 | 0/0 | 各11项通过；数量4在HTTP写入前拒绝 |

运行ID为`final-dsh-<case>-seed-<404|505>-001`，batch逐一索引manifest，再指向同目录的input、verdict、模型记录和进程验收。10份verdict的run集合、passed及metrics与batch一致；78条代理记录全部为`forwarded`/上游200，没有本地注入或远程结果未知项。金额仍unknown。

10个不同Worker均经独立进程验收：候选镜像`sha256:dc5238ff6e83883e223619d149c4b784e55f491c86b3fa7be90dc99ce29336ed`、2 GiB/1 CPU、退出0且无OOM；入口`sdk-minimal`、模型`deepseek-v4-flash`、20次/300秒预算。163条资源采样、4次采样错误，包含配套服务，不能换算独占资源或空闲用户成本。

### 材料分组与评价边界

本批候选镜像、adapter manifest `85846c79072632967e5b842e4dbe0a97f971a9e4dc556758c9b12013a58d45bf`、export `0b2bc9379bf53d47edbfe8f4206a81e1ed547ed8673b069f18bcdd1822872be7`及9份任务hash与新保存正式6轮一致。实际工具服务镜像仍为`sha256:b464a184673f27c97aa5ae0b6b1b43085663f876f80b7b0d49f6f9b002117276`，浏览器为`sha256:25170960c9830c43399cfe25e581f673a46afea90bddd7818407871ee65c9694`。

| 已执行材料组 | 候选镜像 | bench镜像 | 实际任务和结果 |
| --- | --- | --- | --- |
| 原正式F1 | `3c4da6e3…` | `f5031a02…` | 五类101/202/303，15/15、121次；必需自定义事件接法 |
| 新保存正式F2 | `dc5238ff…` | `d4489e54…` | 仅extension/extension-denied的101/202/303，6/6、31次 |
| 最终补充F8 | `dc5238ff…` | `53fad51f…` | 五类404/505，10/10、78次；包含独立完整Worker身份/配额/退出验收 |

本批bench完整ID为`sha256:53fad51f6cd3572a64193bbfc6c00fed7c92749262c4f532d196b98685a97ff5`；F2为`sha256:d4489e54d827e324df6274b08599ab782da05fc21722ed190d3dc87e82bf9c0b`。F2的`runner/detailed.py`来源为`0094073d12a97dde0a8e74d2cc76b1583513e6da9fb31833fa890ffb99ae127a`，F8为`304dbf222c0e83490c0f297dd5768719f144c64c71f7f30098caf3d2e25c9af2`；公共`runner/grading.py`相同。既有verdict显示进程项由“宿主观察正常退出”改为核对完整Worker ID、镜像、配额、退出、非活动及无OOM/error。调度`run_detailed.py`也由`42202cac133c0a7624440c9d0c01835971cf83ae49a239b9d1ff4fd51c6dbd6e`变为`5ab42b7f3ad0d8300ed8cecda85bac6dde72e9d7a3f14239c6da5dacfcaaedff`；其余已列调度配置hash相同。

因此保留三组，不能将原F1的tools/browser/sensitive搬到新镜像，或把不同进程验收拼成同一口径25轮。新保存方式的扩展和数量拒绝已各有五个种子的通过记录，但原三组与新增两组仍按上述验收版本分层。原取消和冷读失败全部保留；本批不验证暂停、故障窗口、多用户或常驻成本，不据10/10给出完整Harness推荐。本次只核对既有安全元数据，未重跑候选或判定器。

## I23 故障正式矩阵的当前结果

2026-10-21。删除与容器清单互斥修订后的i23-dsh-removal-formal-001（`runtime/i23-dsh-removal-formal-001`）已结束：24/24格有效，完整通过23/24。唯一失败是模型超时/非幂等服务/101的业务`all_pages_read`检查；报告、实际写入、scope和审计等其余业务项通过，不能用正确结果覆盖分页要求。

模型超时完整5/6，其余工具失败、写后丢回执和写前强杀均6/6。六条强杀链均从原生状态换容器恢复；24格外层检查、host和清理有效，已提交操作重发均0。本批固定模型、镜像和冻结材料单列在[故障执行记录](fault-recovery-execution.md)，索引SHA-256为`acc141a648168dc40e9dbb0f21d7646f96ad9412ffa1406ebab3c9d445c45ac8`；不与旧两个采样停止批合并成功率。两路线补普通基线及最终关键故障种子均已结束，I23约定调研完成并保留业务失败；本批已纳入010：293条reservation中287条远程200、6条本地注入，不能全部视为远程调用；金额未知。

DSH补普通基线202/303（`runtime/i23-dsh-removal-baseline-rest-001`）已`completed`、完整4/4通过；按幂等202/303、非幂等202/303顺序，原模型计数为9/9/14/9，外层检查及清理有效。索引SHA-256为`a7aa22808d1c6d88d69c30ce9748d5e81d74468d67ef6b2ffd1444b39167951e`。与此前101两模式2/2可描述同候选材料普通基线6/6，但原bench为`8a58c351…`、本批为`4dd8a37e…`，保留分层，不拼成同一完整镜像组合或并入故障矩阵成功率。010 Docker安全分组已确认本补基线41条远程200；这只覆盖明确登记来源，仍不称完整远程账单。

DSH最终关键故障404/505（`runtime/i23-dsh-removal-final-001`）已`completed`且完整8/8通过，写前强杀与写后丢回执各4/4。四条强杀链均原生换容器恢复，八格外层、host及清理有效，已提交操作重发均0；索引SHA-256为`84a57b8942df276f2f8bd05df874e66aa07259675deb82b625baf20b2da689e1`。本轮正式23/24、普通基线6/6（bench分层）、最终8/8分别留存，不合并成功率，也不与旧停止批拼接。I23调研已结束，尚不推出生产可靠性或迁移成本结论；010独立断网Docker SQL/token均已通过，本最终批95条远程200；全研究登记343库/2485条reservation，已知token部分10,253,403，仍有17个未登记来源未查、缺usage、无请求级关联及金额。009历史原件保留，详见[用量台账](costs.md)。

## I24 两用户接线与当前验证状态

2026-10-21。依据 [接线设计](deepseek-multi-user-design.md)，沿用I22官方Goal/Session暂停恢复入口，新增DSH固定Worker和原生证据策略，接入既有模拟产品后端的认证、归属和固定路由。旧入口只接DA属于实验设施尚未支持DSH，不能计为DSH多用户能力失败。新入口已执行同入口、2GiB/0.75CPU的单用户基线和双用户101冒烟；正式三种子本轮未完成。当前仍采用独立Worker，不支持同Worker换用户。

| 运行 | 已观察结果 | 当前判定 |
| --- | --- | --- |
| `i24-dsh-baseline-101-001` | 零模型工具面准备误用非空预算快照，拒绝新建0调用SQLite；远程模型调用前失败，清理通过 | 实验接线失败，原结果保留；仅零模型前后快照允许空历史，仍要求count0及原token/cap绑定 |
| `i24-dsh-baseline-101-002` | Node工具面校准退出0，暴露11个工具；Docker创建态空网络ID在退出态补全，旧整字典比较误拒；未开始业务，清理通过 | 实验接线失败，原结果保留；新校验绑定宿主真实create取得的确切网络ID，并严格核对trial/name/alias及唯一网络 |
| `i24-dsh-baseline-101-003` | 9次模型调用、3笔正确写入、34.596秒；原生completed，业务9项全过；host采样/配额和正向清理通过 | 同入口0.75CPU单用户基线完整通过；不以新轮覆盖001/002失败 |
| `i24-dsh-smoke-101-001` | 两主任务各10/9次模型，独立业务、原状态恢复及累计预算判定通过；两取消因`inflight_or_unknown_model_result`停止评分；有效工具进展未命中原生重叠窗口；B旧Worker删除中出现未证明采样过渡 | 整组及host为false，正向清理通过；取消尚无可采信原生结论，不能改写成再次证实缺结束事件。不能用两主任务通过或两个容器同时存在替代有效并发与整组边界通过 |

003的CLI退出0，host `passed=true` 且errors、sampling_errors、overages均为空；baseline-verdict三项全过，最终容器/网络清单为空。该结果只通过单用户准入门禁，不能为双用户冒烟补通过。冒烟唯一采样错误发生在B旧Worker原终态exit143、非OOM之后：删除已请求，采样时确切ID仍存在，约21毫秒后才确认删除。未来的删除确认不能解释先前样本，原失败保留。

网络修订经独立只读评审；新bench六个相关文件 **430项容器校准通过**，断网、无key、退出0、无OOM且确切容器清理通过，见 校准原件（`runtime/dsh-network-revision-check-001`）。该结果验证接线与拒绝边界，不能代替真实任务、有效并发、后端隔离或换容器续跑。Worker、bench、services和新pins均在 [I24执行记录](deepseek-multi-user-execution.md) 固定，不混用旧I22或失败基线材料。

取消仍单列：ACK不表示原生完成。I17两次取消的结束事件缺口保留；本次I24两取消在原生评分前因预算结果在途/未知停止，不能据此判断原生取消通过或失败，也不能用Goal暂停、idle或强制退出补通过。浏览器使用期隔离、同Worker跨用户复用和常驻成本本轮未完成；DSH专用包装、证据适配与校准属于额外自研投入。
