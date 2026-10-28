# Deep Agents + LangGraph：容器初筛与详细验证

2026-10-20，I10 已完成；I19已完成调研步骤：八类真实冒烟通过，正式五类101/202/303三组12/15完整通过、105次模型请求，新镜像冷读22项通过。最终补充404/505已结束，9/10完整通过、70次请求；两批验收镜像的边界和五种子汇总见末节。分页上限及错误商品选择失败、取消的协作drain语义均保留。下文保留初筛与详细阶段各自证据。固定镜像在两个全新容器内，使用已配置的同一远程模型完成公共工具任务；每次 12 项外部判定通过。真实候选的模拟重试及更换容器后累计限额也已通过。这里只证明接入和基础任务可复现，尚未得出选型结论。

## 材料与运行方式

- Deep Agents `0.7.28`，源码 `0089e635dce525055272883b53bc222f385beac1`；LangGraph `1.2.16`，源码 `98043c37abc60374030f960a03a6e0d4c0112643`。
- 独立 Linux arm64 依赖锁，镜像内从六个固定源码包构建普通 wheel。2516 个来源文件、适配文件及实际安装来源均核验；不挂宿主虚拟环境、不使用 editable 安装。
- 镜像 ID：`sha256:c6dc3fd634a3e8bf68eaf0b04cb7c9dfa08cd8870f8f46c8fa0bed15b3c83904`；Linux arm64，候选配额 2 GiB / 1 CPU，只读根目录。完整构建日志保存在当天 `deepagents_langgraph/runtime/i10-deepagents-build-001/`。
- 官方 `create_deep_agent` 决定任务步骤，`ChatOpenAI` 通过统一计数代理透传 Chat Completions；真实 key 仅在代理内。HTTP 工具保留原参数和粒度，使用 `lab_` 前缀区分被排除的框架内置工具，不预写业务流程。
- 官方 `AsyncSqliteSaver`、`durability="sync"` 保存 checkpoint；保存图的 updates/messages/tasks/checkpoints 原始事件及真实状态快照。当前入口明确只接新会话，不能据此声称已有任务恢复通过。

## 实际工具任务

| run_id | 种子 | 模型请求 | 实际写入 / 写入请求 | 外部判定 |
| --- | --- | --- | --- | --- |
| i10-deepagents-tools-001 | 101 | 6 | 3 / 3 | 12 项全部通过 |
| i10-deepagents-tools-002 | 101 | 6 | 3 / 3 | 12 项全部通过 |

候选自己选择读取文件、翻页查询、计算、提交业务写入和生成报告的步骤。外部验收从种子重算，检查分页、报告、作用域、完整工具审计、独立业务账本及真实模型请求。两轮原生图均结束，无待执行节点，最终 assistant 的 `finish_reason` 均为 `stop`。

第一轮结束并删除容器后，第二轮从同一镜像创建新容器，各自使用新的工作区、状态、预算和账本。**这是重新执行复现，不是任务恢复。** 原始证据位于 `runs/2026-10-20/deepagents_langgraph/tools/` 下对应 run 目录，含镜像/容器/挂载清单、原始事件、状态快照、模型计数、工具审计、业务账本、资源采样和判定。

## 限额与失败记录

`i10-deepagents-budget-002` 使用真实候选镜像和内网固定 503 模拟上游，18 项独立检查全部通过。第一新候选触发两次真实请求及重试，第三次被 cap=2 拦住；删除候选并更换 model-proxy 后，第二新候选仍立即被相同累计限额拦截。上游总共仅收到两次，两个候选均以原生错误和非零退出结束，无假完成。使用 fake key，无收费请求；临时容器及独立网络已删除。

之前的 `i10-deepagents-budget-001` 因实验编排变量覆盖，误启动代理而非候选。该轮已停止清理、保留失败，不计作框架失败；修正后额外核对实际服务名与预期镜像 ID，再重新执行 `002`，没有覆盖原记录。

## 能力来源及边界

本节记录I10初筛当时的边界。后续I19详细任务、I22暂停恢复和I23故障专项均已结束，当前结果见下文及[暂停报告](pause-recovery.md)、[故障报告](fault-recovery-execution.md)；I24/I25按实际缺口标为本轮未完成。

Agent 循环、图事件与 SQLite checkpoint 由框架和官方配套提供。HTTP 工具桥、可信身份绑定、请求计数代理、Docker 编排和独立判定是本仓库接入与实验代码，需要计入方案建设投入。当前图可以在进程中创建多个会话，不等于租户隔离或共享 Worker 已验收。

浏览器、不同种子的详细任务、后续输入、运行中暂停、换容器继续、故障写入窗口、任务控制授权和空闲用户实例成本仍待 I19/I22–I25。模型费用缺少可靠单价，金额保持未知；不得从两次相同输入成功推算生产可靠性。


## I19 详细验证

详细镜像为`sha256:38015e9816f1e186f1b278dce381c8b33404c23dea671c9bdf1a05d9d111251b`，adapter export为`3921e70dd94b42fc8d5affa5f5a3a73c9a617290b7d9ce94f9a0705bb30355ec`；源码清单hash为`9121d68efebbdf462cfd01fe7af06b587988e2027c6ebe4cc388989b7229205b`。新85条目依赖锁hash为`7b80b63a93cf3932babfa737f69137fe6b0b0f3f09c71c3c40603ad8cacd0e8f`，包含浏览器接入所需的官方MCP SDK。2516个来源文件、锁与stage材料已逐一核对，新镜像继续使用固定源码及普通wheel。自动构建13.463秒，只反映缓存下构建耗时，不是研发工时；构建记录（`runtime/i19-deepagents-build-001`）。

`i19-deepagents-budget-001`同详细镜像18项通过：原生重试实际到达模拟上游，删除旧候选并重建proxy后累计cap仍为2，模拟上游总共两次请求，没有远程模型调用。两轮预期错误退出，不是任务完成。

| 真实运行 | 外部验收 | 模型请求 | 写入/请求 | 原生终态与入口耗时 |
| --- | --- | --- | --- | --- |
| i19-deepagents-tools-smoke-001（`tools/i19-deepagents-tools-smoke-001`） | 16项全过；分页、报告、审计和账本一致 | 7 | 3/3 | completed/graph_end，27.157秒，候选及验收退出0 |

八类详细冒烟均已实测通过，最小原生状态冷读001在验收准备时失败，正式101/202/303待跑，不能借用I10或公共测试结果。本入口取消采用公开`RunControl.request_drain`并等待真实`GraphDrained`和sync checkpoint，属于当前superstep完成后的协作停止，成功时退出0；它不是立即取消，不能由代码接入推断已通过，更不能替代I22暂停后新Worker继续。

## I19 八类冒烟与冷读001

七项批次（`runtime/i19-deepagents-smoke-batch-001`）与前述tools合计8/8通过，共49次代理请求，其中1次注入504未到远程，其余48次真实远程请求，10笔业务写入。七项均经过新的独立进程验收，无OOM、正常退出；tools的旧快照无OOM，额外身份pin缺失边界见追加进程审计。

| case | 验收 | 模型请求 | 实际写入 | 关键观察 |
| --- | --- | --- | --- | --- |
| browser | 13/13 | 19 | 1 | 三页、真实提交与CSV；19/20调用，余量有限；i19-deepagents-browser-smoke-001（`browser/i19-deepagents-browser-smoke-001`） |
| sensitive | 11/11 | 2 | 0 | 执行层真实拒绝；i19-deepagents-sensitive-smoke-001（`sensitive/i19-deepagents-sensitive-smoke-001`） |
| extension | 19/19 | 7 | 3 | 原生业务态和逐笔回执一致；i19-deepagents-extension-smoke-001（`extension/i19-deepagents-extension-smoke-001`） |
| extension-denied | 11/11 | 4 | 0 | 数量4在HTTP write前真实拒绝；i19-deepagents-extension-denied-smoke-001（`extension-denied/i19-deepagents-extension-denied-smoke-001`） |
| diagnostic | 11/11 | 2 | 0 | 缺文件400可追溯；i19-deepagents-diagnostic-smoke-001（`diagnostic/i19-deepagents-diagnostic-smoke-001`） |
| diagnostic-timeout | 17/17 | 7 | 3 | 一次注入504后原生重试完成；i19-deepagents-diagnostic-timeout-smoke-001（`diagnostic-timeout/i19-deepagents-diagnostic-timeout-smoke-001`） |
| cancel | 9/9 | 1 | 0 | 公开request_drain；实际GraphDrained，非立即取消；i19-deepagents-cancel-smoke-001（`cancel/i19-deepagents-cancel-smoke-001`） |

取消的GraphDrained保留真实checkpoint和待执行tools节点，1次模型请求、0写入、正常退出，无OOM；这只证明superstep边界协作停止，没有运行恢复。

`i19-deepagents-native-state-001`在bench prepare读取原SQLite时失败，尚未启动候选冷读。原Worker已删除，state只有完整checkpoints.sqlite、没有WAL/SHM；只读SQLite打开仍尝试建立WAL辅助文件，挂载拒绝写入。此处为冷读接入缺口，尚不能算能力通过，也不能认定原状态丢失。保留失败与原状态，准备显式核对无未合并journal/WAL后用immutable只读模式；新镜像和新来源状态重验，不覆盖001。


## I19 只读修复镜像与状态来源

新镜像 `sha256:626b547f46061a5ed28bfda72436cf5d5a4d8e6efef70c44bcc89a5ba35ef5e5`，接入摘要 `9a3eb6a5c33cd2aebb701e14c3823e1fda571504d4855653aa50ea814545684d`；只改只读状态检查，普通Agent路径和85条依赖锁不变。构建002（`runtime/i19-deepagents-build-002`）通过。新bench `67f4ce1e…` 在Docker内670项通过（27.13秒）；前一校准001为665通过/5失败，失败是新增SQLite测试替身参数名与uri关键字冲突，修正后重新构建，原测试记录保留。非空WAL/journal不会被删除或忽略，immutable仅用于宿主已排除写者且持续只读挂载的冷读；原生saver的setup未绕过。

新镜像模拟预算002为18项通过，无收费调用。新extension-smoke-002进程健康、原生completed、5模型/3次请求/3笔写入、报告及业务态正确，但只因未读全分页而整体失败（18/19），不抹去该失败，也不把它算完整通过。现按原定101/202/303先运行正式extension三轮，再从其完整通过记录按种子顺序取首份符合冷读输入条件的状态；其余四类12轮随后执行。改变的仅是运行顺序，任务、预算、镜像不变，不追加成功重跑或改分页标准。

## I19 正式扩展与冷读002

扩展正式批次（`runtime/i19-deepagents-extension-formal-batch-001`）三组完整通过1/3：101通过；202先请求page_size=100，后补齐每页5条的全部分页，仍违反冻结的每页≤5要求；303仅一次100条查询，并实际购买14、15、07（后两件为travel），应选14、18、06；实际总额3812分，应为4798分。三笔实际购买均通过新增查询工具确认并保存到原生业务态；扩展检查失败源于实际操作集合不等于应选集合，不能归因为工具未接上或状态丢失。`all_pages_read`同时核对覆盖与每次page_size，不能只解释成漏页。模型请求6/7/8次，实际各3笔写入，21次模型、9笔写入全部计入；候选均正常退出，同一626b547f…镜像，不重复挑成功。独立工具审计、账本与原生投影已交叉核对：框架正常graph end，未见工具桥篡改参数；属于选定模型与当前材料组合的任务遵循/商品选择失败，不扩大为框架整体能力结论。

冷读002（`native-state/i19-deepagents-native-state-002`）以种子顺序首个完整通过的101为来源，22/22通过。实际删除旧Worker后，新容器0499784f…从同一镜像只读加载SQLite，业务态、原生来源、预算、工作区与审计不变；network none、无模型凭证、无新模型/业务调用，无OOM、退出0，本次临时容器清理成功。它证明业务状态可重建，I22运行中暂停后继续仍未验证。其余四类12轮正式已开始。

## Deep Agents 正式三组收尾

五类 × 三组共 **12/15完整通过，105次模型请求、21笔实际业务写入**；21笔包含303错误选择的真实写入，不能当成21笔正确业务。两批次索引分别为扩展三组（`runtime/i19-deepagents-extension-formal-batch-001`）、其余四类（`runtime/i19-deepagents-core-formal-batch-001`）。15个不同Worker，同一候选镜像626b547f…、bench67f4ce1e…，15份新进程验收均通过、正常退出且无OOM。

| case | seed | 模型请求 | 实际写入 | 完整结果 |
| --- | --- | ---: | ---: | --- |
| tools | 101 | 5 | 3 | 未通过：all_pages_read（`tools/i19-deepagents-tools-seed-101-001`） |
| tools | 202 | 6 | 3 | 通过（`tools/i19-deepagents-tools-seed-202-001`） |
| tools | 303 | 6 | 3 | 通过（`tools/i19-deepagents-tools-seed-303-001`） |
| browser | 101 | 18 | 1 | 通过（`browser/i19-deepagents-browser-seed-101-001`） |
| browser | 202 | 20 | 1 | 通过（`browser/i19-deepagents-browser-seed-202-001`） |
| browser | 303 | 17 | 1 | 通过（`browser/i19-deepagents-browser-seed-303-001`） |
| sensitive | 101 | 2 | 0 | 通过（`sensitive/i19-deepagents-sensitive-seed-101-001`） |
| sensitive | 202 | 2 | 0 | 通过（`sensitive/i19-deepagents-sensitive-seed-202-001`） |
| sensitive | 303 | 2 | 0 | 通过（`sensitive/i19-deepagents-sensitive-seed-303-001`） |
| extension | 101 | 6 | 3 | 通过（`extension/i19-deepagents-extension-seed-101-001`） |
| extension | 202 | 7 | 3 | 未通过：all_pages_read（`extension/i19-deepagents-extension-seed-202-001`） |
| extension | 303 | 8 | 3 | 未通过：report_matches_seed, business_writes_exact, all_pages_read, business_state_matches_actual_receipts, new_query_tool_used_for_all_operations（`extension/i19-deepagents-extension-seed-303-001`） |
| extension-denied | 101 | 2 | 0 | 通过（`extension-denied/i19-deepagents-extension-denied-seed-101-001`） |
| extension-denied | 202 | 2 | 0 | 通过（`extension-denied/i19-deepagents-extension-denied-seed-202-001`） |
| extension-denied | 303 | 2 | 0 | 通过（`extension-denied/i19-deepagents-extension-denied-seed-303-001`） |

普通工具2/3，101曾超过每页5条限制，报告与写入正确仍整体失败；浏览器3/3，模型18/20/17次，202用满预算；敏感拒绝3/3，数量4执行前拒绝3/3，均无业务写入。扩展1/3的失败归因见上节，实际查询工具和状态持久化未失效。冷读002为22/22；取消只证明superstep边界协作drain，未做立即抢占。I19完成的是调研步骤，运行中暂停/新容器续跑、故障窗口及多用户隔离成本仍未验证。

正式105条代理记录均为forwarded/上游200。资源采样258条、4次采样错误；包括配套服务，不作为独占资源或空闲用户成本结论。

## 最终补充：404/505

final-da-batch-001（`runtime/final-da-batch-001`）已结束，SHA-256为`eff609880e9435bbf31b43d4584d9700662e1805899d11d6976b35405c50cb0e`。五类各两组，**9/10完整通过，70次真实远程请求，14笔实际业务写入**；失败轮的5次请求和3笔写入全部保留。

| case | seed | 模型请求 | 实际写入 | 既有独立判定 |
| --- | --- | --- | --- | --- |
| tools | 404 | 7 | 3 | 16/16通过（`tools/final-da-tools-seed-404-001`） |
| tools | 505 | 5 | 3 | 16/16通过（`tools/final-da-tools-seed-505-001`） |
| browser | 404 | 18 | 1 | 13/13通过（`browser/final-da-browser-seed-404-001`） |
| browser | 505 | 20 | 1 | 13/13通过（`browser/final-da-browser-seed-505-001`）；用满20次预算 |
| sensitive | 404 | 2 | 0 | 11/11通过（`sensitive/final-da-sensitive-seed-404-001`）；执行层拒绝 |
| sensitive | 505 | 2 | 0 | 11/11通过（`sensitive/final-da-sensitive-seed-505-001`）；执行层拒绝 |
| extension | 404 | 5 | 3 | 18/19，all_pages_read失败（`extension/final-da-extension-seed-404-001`） |
| extension | 505 | 7 | 3 | 19/19通过（`extension/final-da-extension-seed-505-001`） |
| extension-denied | 404 | 2 | 0 | 11/11通过（`extension-denied/final-da-extension-denied-seed-404-001`）；数量4执行前拒绝 |
| extension-denied | 505 | 2 | 0 | 11/11通过（`extension-denied/final-da-extension-denied-seed-505-001`）；数量4执行前拒绝 |

extension404只调用一次`list_items(page=1,page_size=100)`，服务返回25件商品，违反每页最多5件的约定。报告、三笔正确写入、逐笔新增查询和原生业务态均通过，其余18项通过，不能把它写成业务态丢失或完整成功。新增浏览器两轮均完成真实三页访问、提交和CSV验收；505再次没有模型调用余量。

10份verdict的run集合、passed及metrics与batch逐一一致。10个不同Worker均经独立进程验收：同候选完整镜像ID、2 GiB/1 CPU、退出0、无OOM。70条代理记录全部为`forwarded`/上游200；金额仍unknown。资源采样173条、6次采样错误，含配套服务，不能据此计算独占成本。

### 与原101/202/303如何合看

三份batch中的候选镜像均为`sha256:626b547f46061a5ed28bfda72436cf5d5a4d8e6efef70c44bcc89a5ba35ef5e5`；完整`materials`对象相等，包括9份任务hash、adapter manifest、export `9a3eb6a5c33cd2aebb701e14c3823e1fda571504d4855653aa50ea814545684d`及调度配置。服务镜像仍为`sha256:b464a184673f27c97aa5ae0b6b1b43085663f876f80b7b0d49f6f9b002117276`，浏览器仍为`sha256:25170960c9830c43399cfe25e581f673a46afea90bddd7818407871ee65c9694`。25轮均为同一`create-deep-agent`入口和`deepseek-v4-flash`，20次/300秒预算。

验收镜像确有不同：原三组为`sha256:67f4ce1e9a1afbc790f63e2104aed014bcf1670d921aff8f3ea10f934e27acbc`，新增两组为`sha256:53fad51f6cd3572a64193bbfc6c00fed7c92749262c4f532d196b98685a97ff5`。逐一核对25份`bench-environment.json`，详细判定`runner/detailed.py`的hash均为`304dbf222c0e83490c0f297dd5768719f144c64c71f7f30098caf3d2e25c9af2`，公共判定`runner/grading.py`均为`3562066d54e4315e23a13b7661f7636f447283ba01e5a52d258d0a4cbe7f730e`，进程验收源码也相同；每类既有verdict的检查名称和说明完全一致。bench包装内有其他专项新增文件及工具/stage文件变动，不据此声称两个完整镜像相同；实际运行工具服务镜像没有更换。

据此可按同候选、同任务和已核对的判定口径列出五种子描述汇总，同时保留两套bench分层：tools4/5（29次，失败轮5次）、browser5/5（93次）、sensitive5/5（10次）、extension2/5（33次，失败轮20次）、extension-denied5/5（10次）。合计21/25、175次请求，其中失败轮25次；35笔真实写入含旧extension303的错误选择，拒绝测试也不能充当成功业务任务分母。这不是一个在同一完整镜像组合中执行的25轮批次，也不表示I23–I25已通过。

判定来源样本：原101环境清单（`extension/i19-deepagents-extension-seed-101-001`），SHA-256 `07e50ebc7c67cd35ee7a141034ec027907435468703e7087c47314744f36cb5b`；新增404环境清单（`extension/final-da-extension-seed-404-001`），SHA-256 `4a6e51ec1a4ec20d09015f2d3eeb45db0bde4dc08edd16eccb5fc8a6d6216401`。本次仅核对静态材料，没有重新评分或调用模型。

## I23 普通基线202/303补充结果

2026-10-21。i23-da-removal-baseline-rest-001（`runtime/i23-da-removal-baseline-rest-001`）已`completed`，四条无故障任务完整通过2/4：幂等服务/202、非幂等服务/303通过；幂等/303和非幂等/202仅业务`all_pages_read`未通过，报告、实际写入、scope、审计等其余业务项均正确。四格外层检查全部通过、`cleanup.remaining=[]`。索引SHA-256为`e736012dddfb51b63cfae5c2901f93abcffaad20829416552c2976817aadb90c`。

本批与此前101两模式基线使用相同`fault-send`候选镜像`sha256:9e06db842d8124548b4ca0792b47e1376b0f83bae584ba4ffbdd32052cc2cb6a`和services，但bench从`8bd7f754…`变为删除互斥修订后的`4dd8a37e…`。此前101完整0/2保留；可按同候选材料描述六条普通基线完整2/6，同时保留两套bench分层，不当成同一完整镜像组合批次或并入故障矩阵成功率。

无故障时也有分页失败，故障任务的同类失败不能全部归因于恢复机制。故障矩阵中的原生恢复、写入一致性、完整业务及实验完整性仍单独评价，详见[故障执行记录](fault-recovery-execution.md)。两路线补基线及最终关键种子均已结束，I23约定调研完成；本补基线已纳入010安全分组，21条远程200，金额未知。本次只核对已结束的既有结果与材料SHA，没有重新评分或调用模型。

## I23 最终关键故障404/505结果

i23-da-removal-final-001（`runtime/i23-da-removal-final-001`）已`completed`，8/8格有效结束，完整通过4/8。写前强杀四格完整1/4，仅非幂等服务/404通过；写后丢回执四格完整3/4，仅非幂等服务/404失败。四个失败均只有业务`all_pages_read`未通过，报告、实际写入、scope和审计等其他业务检查正确。索引SHA-256为`92d2a6b70b7341fd4780f4e8879c699af56f0ae33895043f5c045280bac88306`。

逐原件核对，四条强杀链的`original_native_crash_recovery`均通过；八格外层检查全部有效、已提交操作重发均0、`cleanup.remaining=[]`。因此保留“原生恢复/写入一致性有效、部分完整业务失败”两个结论，不用正确写入覆盖分页约束，也不把普通基线已有的同类问题全部算作恢复失败。候选`9e06db84…`、bench `4dd8a37e…`及services `1814a728…`与DA补基线和首次正式矩阵一致，分组仍分别留存。

010独立断网Docker SQL/token已通过并清理，安全分组确认本最终批50条远程200，不在宿主合计或重查原库。DSH最终8格也已结束，I23约定调研完成并保留业务失败；各阶段分别留存，不合并成功率。009历史原件保留；完整计量、金额、人工工时及升级实验仍未知，I26本轮未完成。本次仅核对既有结果与SHA，未重新评分。
