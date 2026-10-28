# 资料来源与统计范围

本次阶段性分享的资料截至 **2026-10-21 15:29:07（北京时间）**。以下列出实验来源、统计范围和原始记录，供核对设计与结论。

三个出发点：DeepSeek Harness 客户端上云、OpenClaw 云端业务改善、更多框架的云端能力探索。**首轮只验证了官方开源框架**，自有客户端的代码盘点和迁移仍需另行开展。

## 实际受测版本与构建方式

已交叉核对候选配置、实际源码导出清单、构建文件和既有运行记录：**DeepSeek Harness 与 OpenClaw 都使用官方开源仓库的固定源码版本，由本仓库加入实验接入代码并自行构建镜像。没有使用自有客户端版本，也没有使用实际云端业务容器。**

| 候选 | 官方来源 | 源码包声明版本 | 固定源码提交 |
| --- | --- | --- | --- |
| DeepSeek Harness | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | 0.1.2-alpha.3 | `dd6322d604e00eec1ba5e0c8541159906a21094a` |
| OpenClaw | [openclaw/openclaw](https://github.com/openclaw/openclaw) | 2026.10.5 | `b6e0872bdb7228e22fac7608345e0529a35ec46e` |

导出清单记录了官方仓库、与配置一致的提交、以及导出时工作区无改动（DSH 9044 个条目，OpenClaw 57891 个）。**上游源码与实验接入文件分开登记**——本仓库增加的工具、业务规则、状态扩展和控制逻辑不算官方默认功能。

"官方版本"仅指源码来源：镜像由本仓库从固定基础环境自建，不是官方预制镜像（OpenClaw 构建还调整了并发与内存参数）。能力结论应绑定各自的实验入口、配置和接入方式。

| 核对环节 | DeepSeek Harness | OpenClaw |
| --- | --- | --- |
| 固定来源与版本配置 | [候选配置](../../configs/candidates/deepseek_harness.yaml) | [候选配置](../../configs/candidates/openclaw.yaml) |
| 实际导出清单 | [源码清单](../../external/build-context/deepseek_harness/SOURCE_MANIFEST.json) | [源码清单](../../external/build-context/openclaw/SOURCE_MANIFEST.json) |
| 镜像怎样构建 | [构建文件](../../compose/candidates/deepseek_harness/Dockerfile)、[接入说明](../../adapters/deepseek_harness/README.md) | [构建文件](../../compose/candidates/openclaw/Dockerfile)、[接入说明](../../adapters/openclaw/README.md) |
| 既有构建与运行核对样本 | [详细阶段构建记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/runtime/i17-dsh-build-004/build.json)、[实际运行来源记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepseek_harness/tools/i17-dsh-tools-smoke-001/candidate/entrypoint.json) | [详细阶段构建记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-build-002/build.json)、[正式批次](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-formal-batch-001/batch.json) |

两者的构建记录与源码清单哈希一致，抽查的运行记录保留同一提交和源码摘要，正式批次使用对应镜像。

## 背景、设计与主要结果来自哪里

| 正文内容 | 来源 |
| --- | --- |
| 业务背景与目标 | [需求说明](../../REQUIREMENTS.md)，[背景与目标](01-background-and-goals.md)中的客户端上云、云端业务改善和更多框架探索三个出发点 |
| 实验如何从问题展开、候选和比较约束 | [调研计划](../../RESEARCH_PLAN.md) |
| 最新执行状态及追加记录 | [实施任务表](../../IMPLEMENTATION_PLAN.md) |
| 八候选初筛及四路线基本任务 | [初筛记录](../validation/screening.md)、[横向比较](../validation/comparison.md) |
| 暂停、删除旧容器、等待及继续任务 | [暂停实验](../validation/pause-recovery.md) |
| 故障正式批次与失败原因 | [故障记录](../validation/fault-recovery-execution.md) |
| Deep Agents 三组双用户结果 | [多用户实验](../validation/multi-user-design.md) |
| DeepSeek Harness 单用户与双用户试跑 | [多用户接入记录](../validation/deepseek-multi-user-execution.md) |
| 两种运行方式及五用户轮换 | [资源实验](../validation/instance-cost-flow.md)、实施任务表末尾追加记录 |
| 请求和用量的统计范围 | [用量台账](../validation/costs.md)、[模型用量计量说明](../validation/token-usage-design.md) |
| 能力来源、需要自建的部分与选择条件 | [工程工作清单](../validation/engineering-inventory.md)、[阶段建议](../validation/recommendation.md) |

## 首轮收尾结果与核对入口

首轮已结束。基础与恢复研究（I00–I23）已完成，其中包含失败结果；多人边界、实例成本和完整计量（I24–I26）仍有未完成目标；条件化建议和资料复核、环境清理（I27–I28）已交付。项目容器和网络已清理，镜像、状态、预算与失败证据保留。

下列原始结果保存在仓库外，链接适用于当前机器。向其他人分享时，可先阅读正文和仓库内记录；需要复核原件时再提供可访问副本。这里的批次链接用于定位证据，不要求读者了解执行编号。

| 实验内容 | 原始结果 | 已确认的事实 |
| --- | --- | --- |
| 两路线正式故障矩阵 | [DSH 二十四格](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/runtime/i23-dsh-removal-formal-001/batch.json)、[Deep Agents 二十四格](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/runtime/i23-da-removal-formal-001/batch.json) | 完整业务分别为23/24、10/24；强制退出后恢复各6/6；业务失败均仅涉及分页要求 |
| 补普通任务对照 | [DSH 四格](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/runtime/i23-dsh-removal-baseline-rest-001/batch.json)、[Deep Agents 四格](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/runtime/i23-da-removal-baseline-rest-001/batch.json) | 分别4/4、2/4；早期首种子基线单列，不混作同一验收版本的批次 |
| 最终关键窗口补测 | [DSH 八格](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/runtime/i23-dsh-removal-final-001/batch.json)、[Deep Agents 八格](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/runtime/i23-da-removal-final-001/batch.json) | 分别8/8、4/8；强制退出后恢复各4/4。正式和最终组均未观察到重发已提交操作 |
| Deep Agents 三组双用户 | [第一组](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-101-001/scope-verdict.json)、[第二组](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-202-001/scope-verdict.json)、[第三组](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/scope/i24-da-removal-303-001/scope-verdict.json) | 三组权限、并发、重连等机制检查通过，整组2/3；第一组保留业务失败 |
| DeepSeek Harness 多人接入 | [单用户对照](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/scope-baseline/i24-dsh-baseline-101-003/baseline-verdict.json)、[双用户试跑](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/scope/i24-dsh-smoke-101-001/scope-verdict.json) | 单用户通过；双用户主任务通过，但取消因模型结果仍在途或未知而停止评分，有效并发与采样验收未过，整轮失败 |
| 两用户旧资源记录复核 | [按需运行复核](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-legacy-sampling-audits-001/proof/on-demand.json)、[持续运行复核](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-legacy-sampling-audits-001/proof/resident.json) | 两者都存在超过五秒的采样空档，旧均值只能描述已观测窗口 |
| 五用户按需轮换 | [整轮判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/flow-verdict.json)、[采样及执行状态](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/flow-host.json) | 已结束，业务4/7；新增两个用户首次任务通过；空闲阶段一份样本不完整，整阶段资源均值和峰值未知。十用户未执行 |
| 最终请求与用量核对 | [请求统计](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/research-usage-check-010/host.json)、[用量提取](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/token-usage-check-010/host.json) | 343个独立预算库、2485条记账；已知10,253,403 token。仍有17个预算库未查询，不是完整费用 |
| 资料复核与收尾 | [收尾记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/research-closeout-001/documents.json)、[实施任务表](../../IMPLEMENTATION_PLAN.md) | 已停止本轮扩跑，保留未完成目标与原始失败，完成项目资源清理 |

DSH 旧故障批次因采样问题中止的记录继续保留，包括“十八格有效、十七格完整通过”的历史批次；它们不与后来完整重跑的二十四格相加。早期专项说明和任务配置记录了当时的计划；实际完成范围以统计截止时间内的最终执行记录及原始批次为依据。

最终调用记账包括2416条远程200记录、4条远程结果未确认、1条仅有预留记录、36条内网模拟和28条本地故障注入。模型用量中已知输入为9,590,870、输出662,533；缓存读取已包含在输入内，推理计数已包含在输出内，不再重复相加。请求数、响应观察数和材料目录数是不同统计单位，没有逐条关联就不能计算覆盖率或费用排名。

完整设计和结果解释见[基本能力](experiments/01-basic-capabilities.md)、[暂停与故障](experiments/02-pause-and-failure.md)、[多人使用与成本](experiments/03-users-and-cost.md)。仓库内可读统计见[请求汇总](../validation/research-usage-summary.json)、[未登记材料](../validation/research-usage-unenrolled-formats.json)及[模型用量汇总](../validation/token-usage-summary.json)。

## 资料时间与历史记录

[来源清单](sharing-source-snapshot.json)记录60份需求、计划、验证记录和任务/候选配置的路径、大小与内容指纹，用于识别统计截止时间所对应的资料版本。清单不包含源文件全文；来源文件后续变化时，可通过内容指纹区分。

截至当日上午 11:49 的[早期实验记录](reference/2026-10-21-114945/README.md)保留了当时的结果与未完成项，用于追溯实验推进过程。当前阶段的完成范围和结论见[首轮结果汇总](03-progress.md)。

[返回分享目录](README.md)
