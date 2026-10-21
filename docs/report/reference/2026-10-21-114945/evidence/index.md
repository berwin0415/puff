# 证据索引与结果截点

本页服务于[调研报告正文](../README.md)。资料截点为 **2026-10-21 11:49:45（Asia/Shanghai）**；截点后的运行结果不回填本版报告。结论来自当时已冻结的仓库说明、已结束批次及既有独立判定，不重新执行候选、查询模型预算数据库或重跑评分器。

[source-snapshot.json](source-snapshot.json)记录报告输入文件的 SHA-256；[results-snapshot.json](results-snapshot.json)记录本次只读抽查的35份外部 JSON 的绝对路径、SHA-256、原状态和必要聚合值。前者固定“采用哪版材料”，后者固定“哪些原件已被本次报告直接核对”。未列为直接核对的明细，沿用对应冻结报告的记录，不扩大为重新审计全部原始材料。

仓库内说明采用相对链接。下列外部原件仍在本机 `/Users/hanbowen01/harness-lab-data/`，未复制进报告；这些绝对链接只能在具有相同目录的机器打开。迁移报告时，需要一并交付相应证据并重映射链接，JSON中的哈希可用于核对。动态 `docs/validation/` 后续可能更新，本版依据以输入快照哈希为准。

## 初筛：八条路线的准入证据

入口为[初筛记录](../../../../validation/screening.md)和[来源、版本及构建记录](../../../../validation/sources.md)。八候选均有材料、官方入口及容器初筛；七者两个新容器的共同工具任务完整通过，Pi为1/2，另一轮报告/写入正确但分页违约。八者的模拟重试和跨容器累计调用限额均通过；模拟预算探针不算真实业务成功，也不证明暂停恢复。

| 路线 | 候选报告 | 必须保留的边界 |
| --- | --- | --- |
| DeepSeek Harness | [DSH](../../../../validation/deepseek_harness.md) | 初筛 `sdk-minimal`、详细运行、metadata保存和Goal专项是不同变体 |
| OpenClaw | [OpenClaw](../../../../validation/openclaw.md) | 实际Gateway入口；不同版本的HTTP取消接线分别留存 |
| Deep Agents + LangGraph | [Deep Agents](../../../../validation/deepagents_langgraph.md) | 原生图/checkpoint、业务接入和实验编排分开归属 |
| OpenAI Agents SDK | [Agents SDK](../../../../validation/openai_agents_sdk.md) | 真实Runner及工具；原MCP接线失败保留 |
| Pi Agent Core | [Pi](../../../../validation/pi_agent_core.md) | Core证据不自动覆盖coding-agent或durable变体 |
| Google ADK | [ADK](../../../../validation/google_adk.md) | 初筛通过；原生恢复接口只是后续线索 |
| Codex SDK | [Codex SDK](../../../../validation/codex_sdk.md) | 原生Responses，不换模型、不转换协议；首次审批接线失败和输出token硬限额缺口保留 |
| OpenHands | [OpenHands](../../../../validation/openhands.md) | 核心SDK证据，不包含Agent Server/Canvas或完整恢复 |

## 详细正式、metadata修订与最终种子补充

F1–F8沿用[比较材料](../../../../validation/comparison.md)的编号。本次已读取八份batch并核对其哈希与冻结说明一致，以下请求数按各批既有 `runs[].metrics.model_requests` 求和，失败轮不删除。冻结候选报告已将这些正式组的代理记录核对为远程 `forwarded/200`；本次不重复打开SQL。

| 编号 | 原件 | 范围 | 完整通过/尝试 | 全部模型请求；失败轮请求 |
| --- | --- | --- | ---: | ---: |
| F1 | [DSH原正式](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/runtime/i17-dsh-formal-batch-001/batch.json) | 五类×101/202/303，原自定义事件保存 | 15/15 | 121；0 |
| F2 | [DSH metadata正式](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/runtime/i17-dsh-metadata-formal-batch-001/batch.json) | extension及extension-denied×三组，新保存接法 | 6/6 | 31；0 |
| F3 | [OpenClaw正式](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-formal-batch-001/batch.json) | 五类×101/202/303 | 7/15 | 142；92 |
| F4 | [Deep Agents扩展正式](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/runtime/i19-deepagents-extension-formal-batch-001/batch.json) | extension×101/202/303 | 1/3 | 21；15 |
| F5 | [Deep Agents其余正式](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/runtime/i19-deepagents-core-formal-batch-001/batch.json) | 其余四类×101/202/303 | 11/12 | 84；5 |
| F6 | [Agents SDK正式](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/runtime/i20-agents-sdk-formal-batch-001/batch.json) | 五类×101/202/303 | 12/15 | 105；8 |
| F7 | [Deep Agents最终补充](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/runtime/final-da-batch-001/batch.json) | 五类×404/505 | 9/10 | 70；5 |
| F8 | [DSH最终补充](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/runtime/final-dsh-batch-001/batch.json) | 五类×404/505，新保存接法 | 10/10 | 78；0 |

F4+F5是互不重叠的15轮；F1和F2有重复任务且候选接法不同，不能累计成新方案的21次独立同口径测试。F1较早的batch没有顶层 `status/passed` 字段，15/15取自逐轮记录，不能假填一个原件不存在的顶层状态。

F7与F4/F5的候选镜像和材料相同，bench镜像变化；冻结报告逐轮核对详细判定、业务评分和进程验收源码及check口径相同，因此允许五种子**描述汇总**：tools4/5、browser5/5、sensitive5/5、extension2/5、extension-denied5/5，共21/25、175次请求。其中含拒绝测试，21不能用作“成功业务任务”成本分母，也不是生产成功率。

F8与F2候选和任务材料相同，但bench及进程判定升级；F1又属于另一候选镜像和保存方式。三组分别呈现，不生成从未在同一新镜像/判定下执行的“DSH25轮”。F7的extension404分页违规及5次请求、原三组的分页/错误商品失败、DSH旧冷读与取消失败都保留。详细分组见[DSH报告](../../../../validation/deepseek_harness.md)、[Deep Agents报告](../../../../validation/deepagents_langgraph.md)。

## 冷读：已完成状态在新容器内可重建

| 编号 | 已核对原判定 | 结果与边界 |
| --- | --- | --- |
| Cold-DSH | [metadata冷读004](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/extension/i17-dsh-extension-smoke-002/native-state/i17-dsh-native-state-004/verdict.json) | 23/23；state允许原生加载边界追加，属于受校验rw挂载，不能写成全程只读 |
| Cold-OC | [正式镜像冷读002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/extension/i18-openclaw-extension-seed-202-001/native-state/i18-openclaw-native-state-002/verdict.json) | 22/22 |
| Cold-DA | [正式镜像冷读002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/extension/i19-deepagents-extension-seed-101-001/native-state/i19-deepagents-native-state-002/verdict.json) | 22/22 |
| Cold-SDK | [正式镜像冷读002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension/i20-agents-sdk-extension-seed-101-001/native-state/i20-agents-sdk-native-state-002/verdict.json) | 22/22 |

它们验证删除旧Worker、同镜像新ID、原生业务态一致、无新增模型或业务调用；没有继续未完成任务。DSH冷读001/002接线失败、003必需自定义事件加载失败另见[原失败与重设计](../../../../validation/deepseek_harness.md)，新metadata结果不覆盖旧失败。

## I22暂停：六条已结束完整链与追加复核

[暂停报告](../../../../validation/pause-recovery.md)记录安全停点、删除A、600秒等待、替换代理和同镜像B沿原进度继续；[资源汇总](../../../../validation/pause-resource-summary.json)保留分阶段测量。六条链原生暂停/恢复均成立；完整业务DSH3/3、Deep Agents1/3。

| 编号 | 原件 | 完整业务；全链请求 | 复核边界 |
| --- | --- | --- | --- |
| P-DSH-101 | [原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/tools/i22-deepseek-pause-seed-101-002/verdict.json)及[追加复核](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/tools/i22-deepseek-pause-seed-101-002/reviews/restored-header-002/verdict.json) | 原16/17；复核17/17；12次 | 官方会话头默认值物化修订后离线只读复核，无新模型；原失败和hash保留 |
| P-DSH-202 | [原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/tools/i22-deepseek-pause-seed-202-001/verdict.json) | 17/17；9次 | 原判定直接通过 |
| P-DSH-303 | [原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/tools/i22-deepseek-pause-seed-303-001/verdict.json) | 17/17；14次 | 原判定直接通过 |
| P-DA-101 | [原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/tools/i22-deepagents-pause-seed-101-004/verdict.json) | 16/17；5次 | 业务分页失败，原生续跑成立 |
| P-DA-202 | [原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/tools/i22-deepagents-pause-seed-202-001/verdict.json) | 16/17；6次 | 业务分页失败，原生续跑成立 |
| P-DA-303 | [原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/tools/i22-deepagents-pause-seed-303-001/verdict.json) | 17/17；6次 | 原判定直接通过 |

六完整链52次请求、18笔写入；六条不暂停基线49次及四条实施中止15次另计，显式I22范围合计116次。DSH基线另有 `reviews/durable-goal-001/` 追加复核，不能将其计为新任务或重复模型用量。删除A确认、checkpoint序列化、采样空档、跨时钟比较等旧中止全部在暂停报告保留。等待时Worker为0，但共享服务仍占资源，不等于总成本归零。

## I23故障：冒烟已结束，正式矩阵未完成

原设计、故障窗口和各代材料见[故障设计](../../../../validation/fault-recovery-design.md)、[执行记录](../../../../validation/fault-recovery-execution.md)。当前材料分别为DSH `fault-round-order`、Deep Agents `fault-send`，不得与旧诊断、普通详细或暂停镜像混合。

| 编号 | 已核对原件 | 截点结果 |
| --- | --- | --- |
| Fault-DSH-base | [两模式正常基线](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/runtime/i23-dsh-round-baseline101-001/batch.json) | 2/2；20次模型请求 |
| Fault-DSH-smoke | [非幂等四故障冒烟](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/runtime/i23-dsh-round-smoke-001/batch.json) | 4/4；51条reservation，含1次本地超时注入 |
| Fault-DA-base | [两模式正常基线](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/runtime/i23-da-send-baseline101-001/batch.json) | 0/2；10次模型请求，均分页失败 |
| Fault-DA-smoke | [非幂等四故障冒烟](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/runtime/i23-da-send-smoke-001/batch.json) | 2/4；27条reservation，含1次本地注入；drop/kill仅完整业务分页失败，原生恢复等外层检查成立 |
| Fault-DSH-formal | [24格批次](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/runtime/i23-dsh-round-formal-001/batch.json)及[第二格原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/fault-recovery/i23-dsh-round-001-formal-model_timeout-id-202/verdict.json) | 11:44:02停止；有效完成1/24。第一格model_timeout/idempotent/101通过、12条计数；第二格202有13条计数，业务结果通过；9项外层检查中8项通过，仅`resource_limits`因`fault_resource_sampling_gap`失败，按完整性错误停止 |

第二格不计候选能力通过或失败，不以“1/2成功率”描述；其原失败和消耗保留。其余正式格、补充基线和最终故障种子尚未完成。源比较页将DSH基线日期链接写为10月21日；本次按实际存在的同run原件链接到10月20日目录，未修改源说明或原件。

## I24多用户：边界通过与业务失败分开报告

设计与范围见[多用户设计](../../../../validation/multi-user-design.md)、[作用域证据](../../../../validation/scope-evidence.md)。只覆盖本仓库独立模拟后端和固定入口，不代表接入了用户已有产品租户系统。

| 编号 | 原件 | 截点事实 |
| --- | --- | --- |
| S-fresh-original | [旧fresh原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-fresh-smoke-101-001/scope-verdict.json) | 整体false；两主任务失败，边界/重叠/重连及取消通过；资源生命周期因旧终态观察问题失败。费用闭合不等于该trial全清理 |
| S-removal-101 | [新101原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-101-001/scope-verdict.json)及[正向清理](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-101-001/cleanup-result.json) | 6项中仅task-b失败；A、两取消、后端边界/重连及资源生命周期均通过，整体false。清理passed=true，同trial容器/网络最终均为空 |
| S-removal-202 | 截点状态来自冻结[比较材料](../../../../validation/comparison.md) | 11:49:45仍运行中；本版不采用随后产生的判定 |

新轮修订不回填旧fresh的健康/清理结论。三种子、DSH对照、浏览器隔离和同Worker复用未完成；“边界验收通过”不能写成全部业务完成或已完成多租户产品验证。

## I25实例成本：同材料两用户探索

完整过程见[实例成本报告](../../../../validation/instance-cost-flow.md)。[baseline004原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-baseline-101-004/protected/baseline-verdict.json)为8项中7项通过，完整业务因分页失败；其后两策略均明确属于探索，`formal_i25_eligible=false`。

| 编号 | 原件 | 可评分业务；预算计数 | 初始/回收后空闲总内存样本均值 |
| --- | --- | --- | --- |
| C-demand | [按需探索原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-od-101-001/protected/flow-verdict.json) | 3/5；33条 | 31.41 / 47.85 MiB，2个容器 |
| C-resident | [常驻探索原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-resident-101-001/protected/flow-verdict.json) | 3/5；34条 | 282.10 / 312.63 MiB，4个容器 |
| C-refusal-5/10 | [N=5拒绝](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-resident-5-refusal-101-001/protected/refusal.json)、[N=10拒绝](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-resident-10-refusal-101-001/protected/refusal.json) | 固定配额启动前拒绝 | `Docker_started=false`、`observed_RSS=null`；没有RSS实测或OOM结论 |

两策略均有原业务态保留/冷读、两新任务唤醒、真实双任务活动和杀A后B的原生进展，但B完整业务仍分页失败，A没有恢复。五个可评分业务不包含被杀后无最终任务结果的A；该任务2条预算计数仍保留。两策略均 `cost_subset_passed=false`、`i25_complete=false`、`savings_percent=null`。表中内存是单轮阶段样本描述，含服务，不据此计算节省比例、生产容量或成功任务成本。按需5/10、其他种子、未活跃用户轮换、故障A恢复和该入口I24证据仍缺。

## 全研究用量：固定006闭合范围，不是总账或账单

取自[研究用量台账](../../../../validation/costs.md)、[冻结清单](../../../../validation/research-usage-inventory.json)、[只读统计快照](../../../../validation/research-usage-summary.json)。本次核对了原[enrollment-006清单](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/research-usage-enrollment-006/inventory.json)哈希和[check-006执行记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/research-usage-check-006/host.json)，没有再次打开预算SQLite。

| 006既有统计 | 数量 | 含义 |
| --- | ---: | --- |
| 已登记并闭合的独立预算库 | 238 | 按预算库去重，A/B和追加复核不重复计数 |
| reservation计数行 | 1528 | 以下各类之和；不是收费请求数 |
| 远程forwarded/200 | 1479 | 代理记录收到上游成功响应；不是可核验账单金额 |
| 内网mock forwarded | 36 | 不当成远程请求 |
| 本地注入504 | 8 | 未到远程上游 |
| 远程结果未确认 | 4 | client_disconnected，结果与计费未知 |
| 仅reserved，记账未完成 | 1 | 是否发出、完成或计费未知；不意味着当前仍有运行容器 |

该快照包含F7/F8的148次远程请求、I22失败中止、已结束I23基线/冒烟、旧I24 fresh和I25两策略等，**不含新I24 removal101/202与本次DSH fault formal**。不将这些较新轮次的消耗手动加进006；需另一次有来源和闭合证据的正式登记。

`unresolved=[]`，去重别名0；另有2个旧I24未登记库仅列路径、未查询。发现范围不是所有日期所有run目录的穷尽扫描；删除的临时库、未留库的直接API探针、其他日期/归档和未登记链仍有缺口。reservation之前的401/429等拒绝不进calls表，所以表内拒绝记录0不等于真实拒绝次数0。统一token、实际计费、人工工时、完整基础设施费用均未知。

费用闭合只证明登记的付费writer及必要路径已停止；不改写原业务失败、进程健康或整个trial清理结论。失败和中止消耗照计，拒绝/诊断/取消通过不能充当成功业务任务成本分母。完整成本方法见[比较材料](../../../../validation/comparison.md)和[成本台账](../../../../validation/costs.md)。
