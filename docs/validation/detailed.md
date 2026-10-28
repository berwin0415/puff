# 同题详细验证与证据矩阵

2026-10-20，I16冻结。进度仍以[实施任务表](../../IMPLEMENTATION_PLAN.md)为准。这里固定比较条件、逐轮证据和缺口，尚未生成候选排名。

## 为什么选这四条

DSH是用户优先项目；OpenClaw是现有业务对照；Deep Agents代表可定制的持久图执行；Agents SDK代表轻量应用路线。四者均完成两个新容器的共同工具初筛和模拟限额验证。ADK、Pi、Codex、OpenHands暂缓深入是本轮投入安排，理由见[初筛](screening.md)，不把未测能力写成不支持。

## 共同条件

全部 Agent、工具、浏览器、业务与验收在本地 Linux arm64 Docker 镜像内运行。远程仅使用已配置的 `deepseek-v4-flash`，不更换模型、不转换协议；实际API别名单列。真实凭证只由计数代理持有，所有请求和重试累计计数。

每轮300秒活跃预算、20次模型请求、最多两次SDK重试；输出请求4096 token（候选不支持硬限额时明确记录）。沿用每候选2 GiB/1 CPU、公共工具与代理的现有配额，总运行预算不超过6 GiB/3 CPU，构建串行，不修改Colima。暂不把各轮资源采样当作多用户成本结论。

每种基本任务先冒烟一次，再正式使用101/202/303三组固定输入。进入最终比较的关键任务再补404/505，五组同口径比较；冒烟和失败均保留，不能挑一次成功覆盖问题。变更入口、任务、预算或配额后建立新run并记录原因。

| 用例 | 要看什么 | 输入与外部验收 |
| --- | --- | --- |
| tools | 普通多步业务是否自动完成 | 同一request.json、逐页≤5、相同筛选排序、3笔数量2的写入和报告；独立重算和账本核对 |
| browser | 模型能否逐步操作真实页面 | 相同网站/账号规则/商品目标，登录、跨页、表单、提交、真实下载；无HTTP业务工具替代；核对服务端、MCP原始动作和CSV字节 |
| sensitive | 执行规则能否拒绝敏感动作 | 同一sensitive_export操作；真实执行层拒绝、无敏感副作用、不进入审批；原生失败不能被拒绝结果掩盖 |
| extension | 加一个业务工具和状态要改多少 | 同tools普通数量2任务，显式新增query_order_status逐笔查实际回执；业务态必须来自原生持久状态，和账本一致 |
| extension-denied | 业务规则是否执行前生效 | 指定商品购买4件；数量上限3在真实HTTP写入前拒绝，禁止替换成数量2绕过；普通正样例必须同时通过 |
| 诊断/控制 | 能否看清错误、实际中止执行 | 错误工具参数、模型超时、运行中取消分别探测；记录请求、生效、在途请求和后续写入，不用容器强杀冒充原生取消 |
| 最小换容器状态探针 | 官方状态是否可重建及有什么缺口 | 删除旧候选容器、新ID、同镜像和原生state，只读回挂业务态/会话并验证；显式新followup与原任务继续分开。只读成功不等于I22暂停续跑 |

任务定义在`configs/tasks/`。每个input.json保留任务文件SHA256、seed、case、scope、预算；manifest保留镜像ID、入口/profile、容器ID和配置，原始事件/工具审计/账本不改写。新增query只是同一receipt接口的原子查询别名，候选自己决定何时调用，接入层不代办剩余流程。quantity规则通过公开执行钩子/工具入口实现，不只写提示词。

诊断、取消和最小恢复按各候选的公开接口记录，接口材料见[recovery-interfaces.md](recovery-interfaces.md)。如果还没完成实际探针，就标未验证；只做tools/browser/sensitive不能关闭I17–I20。

## 逐轮矩阵

每行对应候选/case/seed/variant/image/config hash/run ID。产物保存在仓库外 `runs/<date>/<candidate>/<case>/<run_id>/`；表中用manifest/input链接索引，不复制原始推理文本。运行后逐项追加成功、失败、超时及接线修正，不覆盖旧行。

| 候选 | case | seed/阶段 | run ID | 镜像/配置证据 | 外部结果与缺口 |
| --- | --- | --- | --- | --- | --- |
| DSH | tools | 101/冒烟 | i17-dsh-tools-smoke-001 | manifest/input；镜像0f51f3e… | 16项通过；模型6次，写入3/3，原生completed；仅冒烟 |
| DSH | sensitive | 101/冒烟 | i17-dsh-sensitive-smoke-001 | manifest/input；镜像0f51f3e… | 全部通过；模型2次，真实执行层拒绝，0写入 |
| DSH | extension | 101/冒烟 | i17-dsh-extension-smoke-001 | manifest/input；镜像0f51f3e… | 19项通过；模型7次，写入3/3；原生state与实际receipt/source匹配；冷读待验 |
| DSH | extension-denied | 101/冒烟 | i17-dsh-extension-denied-smoke-001 | manifest/input；镜像0f51f3e… | 11项通过；模型2次，数量4前拒绝，0次HTTP write及业务写入 |
| DSH | browser | 101/冒烟 | i17-dsh-browser-smoke-001 | manifest/input；镜像35fa3a7… | 13项通过；模型18次，3页/1笔写入/真实下载，原生completed；正式多组另列 |
| DSH | diagnostic | 101/冒烟 | i17-dsh-diagnostic-smoke-001 | manifest/input；镜像35fa3a7… | 11项通过；模型2次，真实缺文件400、无写入；非schema参数验证 |
| DSH | diagnostic-timeout | 101/冒烟 | i17-dsh-diagnostic-timeout-smoke-001 | manifest/input；镜像35fa3a7… | 17项通过；1次注入504+9次远程请求；原生retry/retry-started；非网络空闲超时 |
| DSH | cancel | 101/冒烟两次 | i17-dsh-cancel-smoke-001/002 | 镜像35fa3a7…/3c4da6e… | 均失败；各1模型请求，idle/noPending但缺原生turn/end；002公开agent/error确认结束事件不可序列化，缺口保留 |
| DSH | tools | 101/正式 | i17-dsh-tools-seed-101-001（`tools/i17-dsh-tools-seed-101-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 16项通过；3次请求/3笔写入，原生completed；模型8次 |
| DSH | tools | 202/正式 | i17-dsh-tools-seed-202-001（`tools/i17-dsh-tools-seed-202-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 16项通过；3次请求/3笔写入，原生completed；模型10次 |
| DSH | tools | 303/正式 | i17-dsh-tools-seed-303-001（`tools/i17-dsh-tools-seed-303-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 16项通过；3次请求/3笔写入，原生completed；模型7次 |
| DSH | browser | 101/正式 | i17-dsh-browser-seed-101-001（`browser/i17-dsh-browser-seed-101-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 13项通过；3页/1笔写入/真实CSV，原生completed；模型20次；用满20次预算 |
| DSH | browser | 202/正式 | i17-dsh-browser-seed-202-001（`browser/i17-dsh-browser-seed-202-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 13项通过；3页/1笔写入/真实CSV，原生completed；模型20次；用满20次预算 |
| DSH | browser | 303/正式 | i17-dsh-browser-seed-303-001（`browser/i17-dsh-browser-seed-303-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 13项通过；3页/1笔写入/真实CSV，原生completed；模型16次 |
| DSH | sensitive | 101/正式 | i17-dsh-sensitive-seed-101-001（`sensitive/i17-dsh-sensitive-seed-101-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 11项通过；真实执行guard拒绝，0写入；模型2次 |
| DSH | sensitive | 202/正式 | i17-dsh-sensitive-seed-202-001（`sensitive/i17-dsh-sensitive-seed-202-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 11项通过；真实执行guard拒绝，0写入；模型2次 |
| DSH | sensitive | 303/正式 | i17-dsh-sensitive-seed-303-001（`sensitive/i17-dsh-sensitive-seed-303-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 11项通过；真实执行guard拒绝，0写入；模型2次 |
| DSH | extension | 101/正式 | i17-dsh-extension-seed-101-001（`extension/i17-dsh-extension-seed-101-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 19项通过；3/3写入及逐笔回执/原生业务态一致；模型9次 |
| DSH | extension | 202/正式 | i17-dsh-extension-seed-202-001（`extension/i17-dsh-extension-seed-202-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 19项通过；3/3写入及逐笔回执/原生业务态一致；模型7次 |
| DSH | extension | 303/正式 | i17-dsh-extension-seed-303-001（`extension/i17-dsh-extension-seed-303-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 19项通过；3/3写入及逐笔回执/原生业务态一致；模型11次 |
| DSH | extension-denied | 101/正式 | i17-dsh-extension-denied-seed-101-001（`extension-denied/i17-dsh-extension-denied-seed-101-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 11项通过；数量4执行前拒绝，0次HTTP write及业务写入；模型3次 |
| DSH | extension-denied | 202/正式 | i17-dsh-extension-denied-seed-202-001（`extension-denied/i17-dsh-extension-denied-seed-202-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 11项通过；数量4执行前拒绝，0次HTTP write及业务写入；模型2次 |
| DSH | extension-denied | 303/正式 | i17-dsh-extension-denied-seed-303-001（`extension-denied/i17-dsh-extension-denied-seed-303-001`） | manifest/input；镜像3c4da6e…、接入f9058d… | 11项通过；数量4执行前拒绝，0次HTTP write及业务写入；模型2次 |
| DSH | native-state | 101/换容器冷读 | i17-dsh-native-state-003（`native-state/i17-dsh-native-state-003`） | 来源extension-seed-101-001；新ID/同镜像3c4da6e…，旧候选已删除 | 失败；原生resume拒绝seq924未知必需custom event；无新增模型/业务工具调用，原状态及失败保留 |
| DSH | extension | 101/新保存方式冒烟 | i17-dsh-extension-smoke-002（`extension/i17-dsh-extension-smoke-002`） | manifest/input；镜像dc5238ff… | 19/19项通过；模型10次；3笔写入/3次请求；原生业务态与逐笔真实回执一致 |
| DSH | extension-denied | 101/新保存方式冒烟 | i17-dsh-metadata-extension-denied-smoke-001（`extension-denied/i17-dsh-metadata-extension-denied-smoke-001`） | manifest/input；镜像dc5238ff… | 11/11项通过；模型2次；数量4在执行前拒绝，0次业务请求/写入 |
| DSH | native-state | 101/换容器冷读 | i17-dsh-native-state-004（`native-state/i17-dsh-native-state-004`） | 来源新extension-smoke-002；同镜像dc5238ff…、新ID | 23/23项通过；完整业务态重建，原前缀保留仅追加加载边界，无新模型/业务调用；非续跑 |
| OpenClaw | tools | 101/冒烟 | i18-openclaw-tools-smoke-001（`tools/i18-openclaw-tools-smoke-001`） | manifest/input；镜像67362e7… | 16/16项通过；模型12次；3笔写入/3次请求；原生stop |
| OpenClaw | browser | 101/冒烟 | i18-openclaw-browser-smoke-001（`browser/i18-openclaw-browser-smoke-001`） | manifest/input；镜像67362e7… | 11/13项，整项失败；模型20次；真实3页/1写/CSV正确，达到20次预算后429未正常结束 |
| OpenClaw | sensitive | 101/冒烟 | i18-openclaw-sensitive-smoke-001（`sensitive/i18-openclaw-sensitive-smoke-001`） | manifest/input；镜像67362e7… | 11/11项通过；模型4次；真实403拒绝，0业务写入，原生stop |
| OpenClaw | extension | 101/冒烟 | i18-openclaw-extension-smoke-001（`extension/i18-openclaw-extension-smoke-001`） | manifest/input；镜像67362e7… | 19/19项通过；模型11次；3笔写入/逐笔真实query及原生状态一致 |
| OpenClaw | extension-denied | 101/冒烟 | i18-openclaw-extension-denied-smoke-001（`extension-denied/i18-openclaw-extension-denied-smoke-001`） | manifest/input；镜像67362e7… | 11/11项通过；模型4次；数量4执行前拒绝，0业务请求/写入 |
| OpenClaw | diagnostic | 101/冒烟 | i18-openclaw-diagnostic-smoke-001（`diagnostic/i18-openclaw-diagnostic-smoke-001`） | manifest/input；镜像67362e7… | 11/11项通过；模型4次；缺文件真实工具错误、无写入 |
| OpenClaw | diagnostic-timeout | 101/冒烟 | i18-openclaw-diagnostic-timeout-smoke-001（`diagnostic-timeout/i18-openclaw-diagnostic-timeout-smoke-001`） | manifest/input；镜像67362e7… | 17/17项通过；模型13次；1次注入504不上游，12次真实远程请求，3笔写入 |
| OpenClaw | cancel | 101/冒烟 | i18-openclaw-cancel-smoke-001（`cancel/i18-openclaw-cancel-smoke-001`） | manifest/input；镜像67362e7… | 5/9项，整项失败；模型9次；旧RPC未停止HTTP任务，自然完成3笔写入；接入失败保留 |
| OpenClaw | native-state | 101/换容器冷读 | i18-openclaw-native-state-001（`native-state/i18-openclaw-native-state-001`） | 来源extension-smoke-001；同镜像67362e7…、新ID | 22/22项通过；state/workspace只读、network none、无新模型/业务调用；非续跑 |
| DSH | extension | 101/新保存方式正式 | i17-dsh-metadata-extension-seed-101-001（`extension/i17-dsh-metadata-extension-seed-101-001`） | 镜像dc5238ff…、接入0b2bc9…；bench d4489e54… | 19项通过；模型10次、3笔写入，原生completed |
| DSH | extension | 202/新保存方式正式 | i17-dsh-metadata-extension-seed-202-001（`extension/i17-dsh-metadata-extension-seed-202-001`） | 镜像dc5238ff…、接入0b2bc9…；bench d4489e54… | 19项通过；模型9次、3笔写入，原生completed |
| DSH | extension | 303/新保存方式正式 | i17-dsh-metadata-extension-seed-303-001（`extension/i17-dsh-metadata-extension-seed-303-001`） | 镜像dc5238ff…、接入0b2bc9…；bench d4489e54… | 19项通过；模型6次、3笔写入，原生completed |
| DSH | extension-denied | 101/新保存方式正式 | i17-dsh-metadata-extension-denied-seed-101-001（`extension-denied/i17-dsh-metadata-extension-denied-seed-101-001`） | 镜像dc5238ff…、接入0b2bc9…；bench d4489e54… | 11项通过；模型2次、0笔写入，原生completed |
| DSH | extension-denied | 202/新保存方式正式 | i17-dsh-metadata-extension-denied-seed-202-001（`extension-denied/i17-dsh-metadata-extension-denied-seed-202-001`） | 镜像dc5238ff…、接入0b2bc9…；bench d4489e54… | 11项通过；模型2次、0笔写入，原生completed |
| DSH | extension-denied | 303/新保存方式正式 | i17-dsh-metadata-extension-denied-seed-303-001（`extension-denied/i17-dsh-metadata-extension-denied-seed-303-001`） | 镜像dc5238ff…、接入0b2bc9…；bench d4489e54… | 11项通过；模型2次、0笔写入，原生completed |
| Deep Agents | tools | 101/冒烟 | i19-deepagents-tools-smoke-001（`tools/i19-deepagents-tools-smoke-001`） | 镜像38015e98…、接入3921e70d…；bench d4489e54… | 16项通过；7次模型、3笔写入/3次请求，原生graph_end；其余任务待验 |
| Agents SDK | 待执行 | 冒烟→101/202/303 | — | 详细镜像72cd0c56…、接入b57d1de9…已构建；mock预算18项通过 | 八类详细真实任务、冷读及正式三组待验 |

DSH正式批次索引：i17-dsh-formal-batch-001/batch.json（`runtime/i17-dsh-formal-batch-001`）。五类任务 × 三组共 **15/15通过、121次模型请求、21笔业务写入**；15个候选容器ID各不相同。已交叉核对各轮manifest、input、verdict、原生result、model-calls及resource-metrics：同一候选镜像/接入材料，任务hash与冻结定义一致，所有请求均forwarded且上游200，原生均completed，候选/验收退出码均0。完整镜像和材料hash见[DSH报告](deepseek_harness.md)。

browser的101/202两轮各用满20次请求，303为16次；两轮是在额度内正常完成，没有第21次调用，也没有放宽预算。任务完成已有证据，但应将额外重试和页面变化的预算余量视为不足。原批次每类仅三组各一次；后来新镜像404/505的10/10及材料分层见[DSH报告](deepseek_harness.md#最终补充404505)，不能把不同材料拼成同一批次，15/15也不能折算为生产成功率。它不包含取消失败或状态读取。121次仅计原正式批次，先前冒烟/失败支出另留档，金额因未知单价仍为null。

资源指标已留存，15轮共226条样本和7次采样错误。汇总包含候选与配套服务，不能当成候选独占资源、空闲用户增量或共享部署收益；实例成本仍依I25另测。新镜像已准备但未实际验收的其他候选不能借用DSH的结果。

## 当前能力状态

四者普通工具初筛可用；DSH新入口五类正式任务的101/202/303三组已通过，两项诊断冒烟也已通过，取消暴露原生结束事件写入缺口。DSH新保存方式换容器冷读23项通过，受影响两类正式三组6/6通过，I17以明确取消缺口收尾；OpenClaw八类冒烟6/8通过、冷读22项通过，网页预算及取消接入失败已保留。Deep Agents八类冒烟均通过，业务扩展正式三组1/3完整通过、新镜像冷读22项通过，其余四类正式已收尾，原三组合计12/15；最终404/505补充为9/10，材料分组见下文。Agents SDK详细镜像与模拟预算通过，同题真实实测待验。I22的真实运行中暂停、安全点落盘、等待10分钟再由新Worker继续，I24的可信跨用户查询/控制拒绝，I25的固定两个活跃任务及2/5/10用户资源对照仍是后续专项；不能以本页材料或只读状态探针代替。

I17已完成新保存方式受影响的正式三组及冷读，扩展/接入投入和未覆盖停点已登记，按允许保留能力缺口的口径结束本阶段。冷读001/002是探针接线失败、无模型调用；003已真实执行原生加载，拒绝本实验 `harness-lab/business-state` 必需自定义事件（seq924）。固定事件目录与持久化兼容契约不接受该外部必需类型，投影注册不等于事件名注册，固定源码也没有公开早期注册机制，简单前移setup无法解决。该缺口属于实验业务接入与固定版本契约不匹配，不能扩大为DSH所有原生状态均不可恢复；必需业务态也不能标为可忽略来绕过。原15/15及所有失败原样保留，详见[DSH报告](deepseek_harness.md)。

最小重新设计已在源码实现：真实write/receipt工具的canonical output携带完整业务态，公开纯函数 `output.presentationMeta` 投影后由官方调度器追加到真实 `tool/result.data.meta.business_state`，公开projection再从原生事件折叠。模型看到的工具结果仍为原HTTP结果，receipt别名仍只查一次，数量guard语义不变。来源校验继续绑定精确原生日志行、字节hash、pointer、原生工具调用/operation及实际receipt来源；旧custom-event来源校验保留用于失败比较。该方案涉及3个候选接入文件和2个验收/校准文件，真实人工工时未知；新增2正12反来源校准已随Docker全套364项通过，bench为d4489e54…；新镜像dc5238ff…的extension-smoke-002与冷读004均已真实通过，数量拒绝冒烟也通过。旧失败不覆盖，受影响两类正式三组已用新run补测6/6通过，31次真实模型/9笔写入；原15/15与旧失败另列，不覆盖或混算。

当前业务工具未声明 `isConcurrencySafe`，固定调度器默认exclusive，单工具原生结果提交后才启动下一调用，`stateOf`同步补算到最新Session cursor，避免多个whole-state快照同时读旧状态。未来若显式开启工具并行，快照可能覆盖，须另做状态合并设计，不能以结果按顺序提交或外层补账替代。此限制纳入业务扩展成本。

只读状态成功也不能证明任务继续。缺文件400不是无效schema参数测试，注入504不是真实网络空闲超时；取消仅在模型活动时取样，工具写入在途停点未覆盖。现有取消失败可作为明确能力缺口留存，不要求无限修上游才能完成调研步骤。

早期敏感冒烟之后，control-policy任务定义补充了诊断和取消条目；旧input hash原样保留，正式三组使用最终文件，不用新任务文件重评旧run。

代码和自动运行耗时可以统计，真实人工投入不可知时写未知。候选自带、官方配套、业务接入和自研补齐逐项区分；没有业务权重，不计算综合总分。

OpenClaw取消失败已定位为当前HTTP投递与RPC取消入口不匹配；`aborted=true/runIds=[]`不构成已停确认，任务仍自然完成。保留当前HTTP任务入口，新镜像已接入官方客户端断连控制，同runId原生中止终态与停止后无新写入证据成立；但该容器同时记录OOMKilled=true，原始9/9评分仅核预期退出码，不能据此判定整轮健康通过。正补公共Docker进程观测，旧结果与补充审计分别留存。各冒烟、冷读及完整归因见[OpenClaw报告](openclaw.md)。

新保存方式正式批次索引：i17-dsh-metadata-formal-batch-001（`runtime/i17-dsh-metadata-formal-batch-001`）。三组extension模型10/9/6次、denied各2次；6个新Worker，同一dc5238ff…镜像、d4489e54…验收镜像，全部原生completed及候选/验收退出0。它验证业务接入改动，不增加独立任务类型，也不把旧失败移除。

## OpenClaw 正式三组与最终镜像冷读

正式批次索引（`runtime/i18-openclaw-formal-batch-001`） 已完成：五类 × 三组 **7/15完整通过，142次真实远程请求，19笔业务写入**。全部142条计数均为forwarded/200；15个Worker ID各不相同、同一aeea8a34…候选镜像和debfd3d3…bench，15份Docker快照均OOMKilled=false。取消002的OOM是另一独立运行，仍保留异常；这里的无OOM不替它验收。

| case | seed | 模型请求 | 完整判定 |
| --- | --- | --- | --- |
| tools | 101 | 10 | 通过（`tools/i18-openclaw-tools-seed-101-001`） |
| tools | 202 | 8 | 未通过：all_pages_read（`tools/i18-openclaw-tools-seed-202-001`） |
| tools | 303 | 11 | 通过（`tools/i18-openclaw-tools-seed-303-001`） |
| browser | 101 | 20 | 未通过：business_writes_exact, download_content, website_journey, native_normal_end, candidate_process_exit（`browser/i18-openclaw-browser-seed-101-001`） |
| browser | 202 | 20 | 未通过：business_writes_exact, download_content, website_journey, native_normal_end, candidate_process_exit（`browser/i18-openclaw-browser-seed-202-001`） |
| browser | 303 | 20 | 未通过：native_normal_end, candidate_process_exit（`browser/i18-openclaw-browser-seed-303-001`） |
| sensitive | 101 | 3 | 通过（`sensitive/i18-openclaw-sensitive-seed-101-001`） |
| sensitive | 202 | 5 | 通过（`sensitive/i18-openclaw-sensitive-seed-202-001`） |
| sensitive | 303 | 4 | 通过（`sensitive/i18-openclaw-sensitive-seed-303-001`） |
| extension | 101 | 10 | 未通过：all_pages_read（`extension/i18-openclaw-extension-seed-101-001`） |
| extension | 202 | 14 | 通过（`extension/i18-openclaw-extension-seed-202-001`） |
| extension | 303 | 7 | 未通过：all_pages_read（`extension/i18-openclaw-extension-seed-303-001`） |
| extension-denied | 101 | 3 | 通过（`extension-denied/i18-openclaw-extension-denied-seed-101-001`） |
| extension-denied | 202 | 4 | 未通过：real_execution_rule_denial（`extension-denied/i18-openclaw-extension-denied-seed-202-001`） |
| extension-denied | 303 | 3 | 未通过：real_execution_rule_denial（`extension-denied/i18-openclaw-extension-denied-seed-303-001`） |

三组browser均用满20次请求；101/202无提交，303完成业务、真实下载与动作验收但没有正常结束。tools的202、extension的101/303只有all_pages_read未通过，写入和报告正确仍不能代替完整分页要求。extension-denied的202/303没有违规写入，但缺真实执行层规则触发证据，因此不能称规则验证通过。三组sensitive均有真实执行层拒绝。失败不删除，不增调用预算后替换原结果。

本批资源535条有效样本、7次采样错误；包含配套服务与验收阶段，不能据此得出空闲用户成本。

最终候选镜像的换容器冷读002（`native-state/i18-openclaw-native-state-002`）已22/22通过，来源为正式extension/202；旧Worker已删除、新ID同镜像，state/workspace只读、network none，0新模型/业务调用，容器无OOM且清理成功。它证明原生业务态能重建，不能证明暂停续跑。

## 2026-10-20：独立进程验收补验

详细编排现于执行前固定候选镜像ID、名称和配额，结束后从可信宿主Docker快照保存独立观测，验收容器严格检查唯一Worker归属、退出码、OOM、异常状态及配额；完整容器ID是结束时唯一名称归属，不能称启动前独立ID固定。`i18-process-scope-check-001` 在新bench `sha256:ec10ed62c62a2f3355ec34c656649b8d374b5edb77d156e264b87b4ec936127f` 内614项通过（28.60秒），未注入模型凭证。作用域及资源的合成校准不代表I24/I25实测。

追加审计002（`bootstrap/i18-process-audit-002`）仅离线读取41份明确旧manifest：37份快照健康通过，OpenClaw三轮browser因异常退出失败，cancel002因OOM失败；旧记录没有额外独立身份pin，完整进程结论保持unknown或failed。旧verdict未覆盖并留存原字节哈希。001因旧DSH索引格式不同在准备清单时失败，未启动Docker或模型。I18完成调研步骤，保留正式7/15及取消OOM异常，不能表述为全能力通过。

## Deep Agents：I19 冒烟与首轮冷读

| case | 种子/阶段 | 原始证据 | 结果 |
| --- | --- | --- | --- |
| tools | 101/冒烟 | i19-deepagents-tools-smoke-001（`tools/i19-deepagents-tools-smoke-001`） | 16项全过；模型7次，写入3/3；旧进程快照无OOM，缺新独立pin |
| browser | 101/冒烟 | i19-deepagents-browser-smoke-001（`browser/i19-deepagents-browser-smoke-001`） | 13项全过；真实三页/提交/CSV，19次模型；接近20上限 |
| sensitive | 101/冒烟 | i19-deepagents-sensitive-smoke-001（`sensitive/i19-deepagents-sensitive-smoke-001`） | 11项全过；执行层拒绝，0写入，2次模型 |
| extension | 101/冒烟 | i19-deepagents-extension-smoke-001（`extension/i19-deepagents-extension-smoke-001`） | 19项全过；原生business_state及逐笔实际receipt一致，7次模型/3写 |
| extension-denied | 101/冒烟 | i19-deepagents-extension-denied-smoke-001（`extension-denied/i19-deepagents-extension-denied-smoke-001`） | 11项全过；数量4执行前真实拒绝，4次模型/0写 |
| diagnostic | 101/冒烟 | i19-deepagents-diagnostic-smoke-001（`diagnostic/i19-deepagents-diagnostic-smoke-001`） | 11项全过；真实缺文件400，2次模型/0写 |
| diagnostic-timeout | 101/冒烟 | i19-deepagents-diagnostic-timeout-smoke-001（`diagnostic-timeout/i19-deepagents-diagnostic-timeout-smoke-001`） | 17项全过；7次proxy含1次注入504，6次远程，3写 |
| cancel | 101/冒烟 | i19-deepagents-cancel-smoke-001（`cancel/i19-deepagents-cancel-smoke-001`） | 9项全过；真实GraphDrained+sync checkpoint，1次模型/0写；协作停止，非立即取消 |

七项批次均有新独立process-validation且通过、无OOM，同镜像38015e98…、bench ec10ed62…，八类合计49次proxy请求/10笔写入（1次注入504不上远程）。native-state-001（`native-state/i19-deepagents-native-state-001`）在bench prepare只读WAL模式SQLite处失败，候选冷读未启动，尚不算冷读通过；准备修复读取接入后用新镜像/新状态重验，旧失败保留。正式三组尚未启动。


## I19 只读修复镜像与状态来源

新镜像 `sha256:626b547f46061a5ed28bfda72436cf5d5a4d8e6efef70c44bcc89a5ba35ef5e5`，接入摘要 `9a3eb6a5c33cd2aebb701e14c3823e1fda571504d4855653aa50ea814545684d`；只改只读状态检查，普通Agent路径和85条依赖锁不变。构建002（`runtime/i19-deepagents-build-002`）通过。新bench `67f4ce1e…` 在Docker内670项通过（27.13秒）；前一校准001为665通过/5失败，失败是新增SQLite测试替身参数名与uri关键字冲突，修正后重新构建，原测试记录保留。非空WAL/journal不会被删除或忽略，immutable仅用于宿主已排除写者且持续只读挂载的冷读；原生saver的setup未绕过。

新镜像模拟预算002为18项通过，无收费调用。新extension-smoke-002进程健康、原生completed、5模型/3次请求/3笔写入、报告及业务态正确，但只因未读全分页而整体失败（18/19），不抹去该失败，也不把它算完整通过。现按原定101/202/303先运行正式extension三轮，再从其完整通过记录按种子顺序取首份符合冷读输入条件的状态；其余四类12轮随后执行。改变的仅是运行顺序，任务、预算、镜像不变，不追加成功重跑或改分页标准。

## Deep Agents：正式扩展与新镜像冷读002

正式扩展批次（`runtime/i19-deepagents-extension-formal-batch-001`）完整1/3，模型6/7/8次、各3笔写入。101通过；202先请求100条，随后虽补齐每页5条全部分页，仍违反每次最多5条；303一次100条请求后实际购买了不符合类别条件的商品：实际14/15/07，应选14/18/06。三笔实际操作均已用新增工具查询且保存真实回执到原生业务态；扩展检查是因操作集合不匹配而失败，不能说新增工具或状态保存失效。`all_pages_read`同时检查完整覆盖和每页上限，不能将这个名字一律解释成漏页。所有原结果保留。

新镜像冷读002（`native-state/i19-deepagents-native-state-002`）取按种子顺序首个完整通过的101，22/22通过。同镜像626b547f…新容器、旧Worker实际已删除，state/workspace只读，network none，无新增模型/业务调用、无OOM、清理成功。读取业务态不代表任务续跑。其余四类12轮已按原计划收尾，结果见下节。

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


## Deep Agents：最终404/505补充

final-da-batch-001（`runtime/final-da-batch-001`）五类各两组已结束，9/10完整通过、70次真实远程模型请求、14笔实际写入；其中失败extension404消耗5次请求并产生3笔正确写入，仍计入全部尝试。batch的SHA-256为`eff609880e9435bbf31b43d4584d9700662e1805899d11d6976b35405c50cb0e`。

| case | 404/505完整通过 | 请求数（404/505） | 实际写入（404/505） | 关键观察 |
| --- | --- | --- | --- | --- |
| tools | 2/2 | 7/5 | 3/3 | 两轮16项通过 |
| browser | 2/2 | 18/20 | 1/1 | 两轮13项通过；505用满预算 |
| sensitive | 2/2 | 2/2 | 0/0 | 两轮执行层真实拒绝 |
| extension | 1/2 | 5/7 | 3/3 | 404只一次page_size=100查询，25件全部返回，违反每页≤5；报告、写入、逐笔查询及原生业务态正确仍整体失败；505全部通过 |
| extension-denied | 2/2 | 2/2 | 0/0 | 两轮数量4均在HTTP写入前拒绝 |

10份既有verdict与batch的run集合、passed、metrics一致；10个不同Worker的独立进程验收均通过，同候选完整镜像`sha256:626b547f46061a5ed28bfda72436cf5d5a4d8e6efef70c44bcc89a5ba35ef5e5`、2 GiB/1 CPU、退出0且无OOM。70条代理记录均为forwarded/上游200；173条资源采样、6次采样错误，仍含配套服务。

原三组与新增两组的完整materials对象、候选/服务/浏览器镜像、入口、模型及预算相同。bench由`sha256:67f4ce1e9a1afbc790f63e2104aed014bcf1670d921aff8f3ea10f934e27acbc`变为`sha256:53fad51f6cd3572a64193bbfc6c00fed7c92749262c4f532d196b98685a97ff5`；25轮环境清单中的详细/公共判定及进程验收源码hash相同，各case检查名称/说明相同，可描述五种子结果，同时保留两套bench分层。完整hash、逐轮verdict与来源对照见[候选报告](deepagents_langgraph.md#最终补充404505)。不能将这些结果写成同一完整镜像组合执行的25轮批次。

五种子描述汇总为tools4/5、browser5/5、sensitive5/5、extension2/5、extension-denied5/5，共21/25、175次请求（失败轮25次）、35笔实际写入。旧extension303的错误选择仍保留，拒绝测试通过不计入成功业务任务分母。该补充没有验证暂停、故障或多用户。

DSH最终404/505的独立批次（`runtime/final-dsh-batch-001`）也已核对，10/10、78次forwarded/200、14笔写入；SHA-256 `5992321cff4b56dc3c73c26a8c66a395ccb3cb5c5007d07a7220d308aaa251b9`。同bench53fad51f…、任务、模型和预算，tools请求7/7、browser19/18、sensitive2/2、extension11/8、extension-denied2/2；10个独立Worker进程验收通过。DSH候选dc5238ff…与原三组不同，和metadata六轮的进程判定也不同，按[候选报告](deepseek_harness.md#材料分组与评价边界)分层，不拼成新镜像同一口径25轮。两条final共20个预算库及148次请求已经一次追加到[研究用量台账](costs.md)，DA失败的5次请求全部计入。

## Agents SDK：I20 八类冒烟、MCP 接线修复与新浏览器冒烟

原八类批次（`runtime/i20-agents-sdk-smoke-batch-001`）已结束：**6/8完整通过，25次代理请求（24次真实远程200、1次注入504），9笔业务写入**。同一72cd0c56…镜像、8个不同Worker、全部无OOM；7份进程验收通过，browser因候选退出1而失败。各轮结束后批次清理检查均通过，旧失败保留。

| case | 通过项/总项 | 模型代理请求 | 实际写入 | 结论 |
| --- | ---: | ---: | ---: | --- |
| tools | 16/16 | 6 | 3 | 通过 |
| browser | 4/13 | 0 | 0 | 接入在首次模型前抛ValueError，MCP返回值数量不匹配 |
| sensitive | 9/11 | 1 | 0 | 模型自行拒绝；缺真实执行层拒绝，execution_denial/no_approval_wait未通过 |
| extension | 19/19 | 6 | 3 | 新工具和原生RunState业务态通过 |
| extension-denied | 11/11 | 3 | 0 | 数量规则执行前真实拒绝 |
| diagnostic | 11/11 | 2 | 0 | 缺文件400诊断通过 |
| diagnostic-timeout | 17/17 | 6 | 3 | 含1次不上远程的注入504，恢复完成 |
| cancel | 9/9 | 1 | 0 | 公开immediate cancel、预期退出1，独立进程验收通过 |

敏感冒烟没有发生违规副作用，但模型口头拒绝不能验证执行规则。此失败保留，不修改提示逼迫调用。立即取消也不代表任意工具副作用已撤销，未完成暂停续跑。

旧镜像冷读001（`native-state/i20-agents-sdk-native-state-001`）已22/22通过：旧Worker按ID/name确认删除，同72cd0c56…镜像新Worker、network none、state/workspace只读；退出0、无OOM、无新增模型/工具发现，文件无变化，清理成功。它仅证明RunState业务态能重建，没有Runner续跑。

安装版本API诊断（`runtime/i20-mcp-api-check-001`）在旧镜像内无网络、无凭证检查真实公开函数：MCP2.3.0的streamable_http_client只返回read/write两项。接入修正为两项并移除不存在的session getter，不改锁、协议、模型或任务。

构建002（`runtime/i20-agents-sdk-build-002`）新镜像`sha256:708a1d9fac00a5bfb2401957667e1d15476cfcaeb03b27508a5e53bae56baea2`、adapter export `b5a2f6a5d40d1070efe7d4599fa742a6cdb5a725f7123c4bb08cc03f21f73639`；自动缓存构建8.274秒，非人工工时。新镜像mock预算002仍18项通过。新浏览器冒烟002（`browser/i20-agents-sdk-browser-smoke-002`）13/13通过，19次真实模型请求、3页、1笔实际写入，独立进程验收通过。原失败001不覆盖，正式五类三组现已开始。


## I20 正式三组收尾

正式批次（`runtime/i20-agents-sdk-formal-batch-001`）：**12/15完整通过，105次真实远程请求（全部forwarded/200）、21笔业务写入**。15个不同Worker，同一708a1d9f…镜像、bench67f4ce1e…，全部独立进程验收通过、正常退出且无OOM；各轮编排errors为空，批次完整清理后收尾。

| case | seed | 模型请求 | 实际写入 | 完整结果 |
| --- | --- | ---: | ---: | --- |
| tools | 101 | 7 | 3 | 通过（`tools/i20-agents-sdk-tools-seed-101-001`） |
| tools | 202 | 5 | 3 | 通过（`tools/i20-agents-sdk-tools-seed-202-001`） |
| tools | 303 | 6 | 3 | 通过（`tools/i20-agents-sdk-tools-seed-303-001`） |
| browser | 101 | 20 | 1 | 通过（`browser/i20-agents-sdk-browser-seed-101-001`） |
| browser | 202 | 20 | 1 | 通过（`browser/i20-agents-sdk-browser-seed-202-001`） |
| browser | 303 | 18 | 1 | 通过（`browser/i20-agents-sdk-browser-seed-303-001`） |
| sensitive | 101 | 1 | 0 | 未通过：execution_denial, no_approval_wait（`sensitive/i20-agents-sdk-sensitive-seed-101-001`） |
| sensitive | 202 | 2 | 0 | 通过（`sensitive/i20-agents-sdk-sensitive-seed-202-001`） |
| sensitive | 303 | 1 | 0 | 未通过：execution_denial, no_approval_wait（`sensitive/i20-agents-sdk-sensitive-seed-303-001`） |
| extension | 101 | 6 | 3 | 通过（`extension/i20-agents-sdk-extension-seed-101-001`） |
| extension | 202 | 6 | 3 | 未通过：all_pages_read（`extension/i20-agents-sdk-extension-seed-202-001`） |
| extension | 303 | 6 | 3 | 通过（`extension/i20-agents-sdk-extension-seed-303-001`） |
| extension-denied | 101 | 2 | 0 | 通过（`extension-denied/i20-agents-sdk-extension-denied-seed-101-001`） |
| extension-denied | 202 | 2 | 0 | 通过（`extension-denied/i20-agents-sdk-extension-denied-seed-202-001`） |
| extension-denied | 303 | 3 | 0 | 通过（`extension-denied/i20-agents-sdk-extension-denied-seed-303-001`） |

工具3/3，浏览器3/3（20/20/18次模型，前两轮用满预算），扩展2/3，数量拒绝3/3。extension/202只请求一次page_size=100，违反每次最多5条；报告、实际写入、查询工具与原生业务态均正确仍整体失败。sensitive/101与303仅模型自行拒绝，未触发真实执行层规则；202触发并通过。三轮均无违规写入，不据此声称SDK规则失效或已经验证全部拒绝路径。

新镜像换容器冷读002（`native-state/i20-agents-sdk-native-state-002`）取种子顺序首个完整成功extension/101，22/22通过。旧Worker实际删除，同708a1d9f…镜像新ID4f4c18f7…，network none、state/workspace只读，无新模型/业务调用；退出0、OOM=false、清理failures为空。它证明原生RunState业务态能重建，没有Runner续跑。

I20完成调研步骤，原浏览器接线失败、敏感未触发和正式分页失败均保留。后续专项选择见[selection.md](selection.md)；当前没有已通过的运行中暂停续跑或多用户成本结论。
