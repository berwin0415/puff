# Harness Lab

调研开源 Agent 框架实现云端 Harness 的能力、缺口及成本。当前计划是本仓库唯一实验；候选、工具、浏览器和验收均在本机 Docker 容器内执行，模型调用远程 API。

- [需求](REQUIREMENTS.md)：已确认的目标和约束。
- [调研计划](RESEARCH_PLAN.md)：验证什么、为什么验证、怎样评价。
- [实施计划与任务表](IMPLEMENTATION_PLAN.md)：本机方案、目录职责、步骤和验收状态。
- [本轮结论和条件化建议](docs/validation/recommendation.md)：各路线适用条件、能力缺口和后续投入方向。
- [智能助手上云实测报告](docs/report/README.md)：**分享文档**。八条开源路线的九个实验维度、284 次运行、2,453 次模型请求，给出结论与各路线下一步建议；数据截至 2026-10-28。详细设计与逐组结果见[附录](docs/report/appendix/README.md)。

云端任务默认尽量自动放行，过于敏感的操作任务直接拒绝，本轮不建设审批流。业务现状以用户明确说明为准，Hogee 源码仅作技术参考；任务作用域与鉴权接入单独验收。

本轮已收尾：I00–I23 已完成调研步骤，I24–I26本轮未完成，I27条件化建议已交付，I28复核与资源清理已完成；“完成调研”包含已经查明并留存的失败，不代表所有能力通过。实验项目容器和网络已清理，镜像、状态、预算及证据保留，收尾记录见[任务表I28](IMPLEMENTATION_PLAN.md)。

| 已有证据 | 目前能说明什么 |
| --- | --- |
| 八候选初筛 | 七者两个新容器工具任务通过；Pi为1/2。八者模拟重试和跨容器累计限额通过，见[初筛记录](docs/validation/screening.md) |
| 四路线详细验证 | DSH、OpenClaw、Deep Agents、Agents SDK完成同题详细实验；分页、浏览器预算、拒绝触发及取消问题逐项保留，见[比较结果](docs/validation/comparison.md) |
| 暂停换容器 | DSH与Deep Agents各三种子原生续跑3/3；完整业务分别3/3、1/3。等待期Worker为零，共享服务仍有开销，见[暂停记录](docs/validation/pause-recovery.md) |
| 故障验证 | 最新DSH四项冒烟完整通过；Deep Agents两项通过，另两项业务分页失败，但强杀换容器恢复独立检查通过。删除边界修订后DSH完整正式24格已结束、23/24完整通过，唯一失败为分页验收；DA完整10/24、14格仅分页失败。两路线六条kill各原生恢复6/6、各格已提交操作重发0。补普通基线DSH4/4、DA2/4；最终404/505各8格为DSH8/8、DA4/8。业务失败均仅分页验收，旧DSH停止批独立保留，见[故障记录](docs/validation/fault-recovery-execution.md) |
| 两用户及实例成本 | Deep Agents三组用户边界及资源生命周期检查均通过，完整业务为101失败、202/303通过。旧两策略成本探索各业务3/5，均有超过5秒的采样空档。DSH单用户基线003通过，双用户smoke两主任务通过，但取消因模型结果在途/未知未评分，有效工具并发窗口和删除采样未通过，整组失败、清理通过。生命周期无模型probe003通过；按需N5业务4/7，三项因每页请求100条违反最多5条要求失败，故障时B实际仍有进展。回收后空闲窗口29份完整、1份跨阶段缺测，资源汇总和收益未知，N10先停，见[DSH记录](docs/validation/deepseek-multi-user-execution.md)、[用户边界](docs/validation/multi-user-design.md)和[实例成本](docs/validation/instance-cost-flow.md) |
| 模型用量 | 010截点核343库/2485条reservation，2416条远程200；四路线取得10,253,403已知token。17个未登记库未查询（DSH I24七条、N5八条及旧DA两条），另有缺失usage和未知请求，无完整账单、请求级关联或金额结论，见[用量台账](docs/validation/costs.md) |

当前专项选择DSH与Deep Agents，依据见[专项选择](docs/validation/selection.md)。I24/I25的DSH接入、浏览器使用期隔离、同Worker复用和真实产品后端成本仍未完成；本轮建议是优先试接DSH、保留Deep Agents自建和继续改OpenClaw的选项；具体业务确定前，不据本地样本决定生产迁移或承诺节省比例。每步任务和最新原件索引以[唯一任务表](IMPLEMENTATION_PLAN.md)为准。

仓库内的历史实验归档 `old/` 已于2026-10-21按用户要求清理，包含旧代码、配置、测试、报告和虚拟环境。清理前已确认当前实验没有运行、构建或挂载依赖；当前代码、报告、`.env`及仓库外证据保留，详见[清理记录](IMPLEMENTATION_PLAN.md#执行记录)。凭证不提交 Git。

`.dockerignore`、`.rgignore` 和 `pytest.ini` 已排除归档的默认构建、代码搜索及测试发现。

当前唯一入口约定为 `configs/experiment.yaml`、`compose/docker-compose.yml`、`scripts/lab.py` 和 `docs/validation/`，按任务逐步落实。原始数据统一放仓库外的 `/Users/hanbowen01/harness-lab-data/`，其中 `old/` 仅保存历史材料。
