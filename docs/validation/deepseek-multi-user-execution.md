# DSH 两用户验证记录

日期：2026-10-21。总进度以 [实施任务表](../../IMPLEMENTATION_PLAN.md) 为准；接线依据 [设计](deepseek-multi-user-design.md)。本页记录真实执行，不覆盖原交付、冻结清单或失败结果。

先说人话：先用一个用户确认这套新入口能真正完成任务，再让两个用户同时工作，核对各自的数据和控制权限。前两次基线都在模型调用前被实验接线挡住，不能据此判断 DSH 能力。

| 运行 | 实际结果 | 如何处理 |
| --- | --- | --- |
| `i24-dsh-baseline-101-001` | 新建预算库没有请求，工具面准备误按普通非空快照规则拒绝；未调用模型，清理通过 | 只在零模型工具面前后检查允许空历史，仍要求计数为0及原token/cap绑定；原失败保留 |
| `i24-dsh-baseline-101-002` | 真实 Node 校准正常退出，发现11个工具；Docker 创建态网络ID为空、启动后补全，原字典相等检查误拒；未开始业务，清理通过 | 从可信网络创建登记固定完整ID，仅允许创建态空ID补全为该ID；网络名、别名、trial和唯一网络继续严格核对；原失败保留 |
| `i24-dsh-baseline-101-003` | 完整通过：9次模型、3笔正确写入、约34.60秒；分页/原生完成/预算/同入口0.75CPU/host采样配额及正向清理均通过 | 仅单用户门禁通过；旧失败不改 |
| `i24-dsh-smoke-101-001` | 流程结束、整体失败：两主任务独立判定通过（10/9次模型）；两取消预算结果仍有在途/未知，取消评分未完成；并发有效工具进展未命中窗口；删除中采样过渡未证明 | `scope-verdict`和host均false；正向清理通过。不能称完整隔离、并发或取消能力通过，不直接进入正式三种子 |

修订说明：[空预算](deepseek-multi-user-surface-budget-fix.md)、[网络身份](deepseek-multi-user-surface-network-fix.md)。独立只读审查未发现阻断。新bench相关六文件 **430项容器校准通过**，断网、无key、退出0、无OOM、确切容器清理通过；[原始记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/dsh-network-revision-check-001/host.json)。这些校准检查接线和拒绝边界，不代表真实基线或两用户能力通过。

新材料登记位于 [revision-materials.json](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/network-init-revisions-images-001/revision-materials.json)，对应 [构建记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/network-init-revisions-images-001/build.json) 证明构建前后源码未变：

| 材料 | 固定值 |
| --- | --- |
| Worker | `sha256:9ff8a3a596b822d080d88a6eb5f0950d6ecb10f23f77de2f29c4ecc4a3825ad6` |
| bench | `sha256:863b70d9c145cd69c760e57418afbc9189054401ead63c29c073ea716e944b3d` |
| services | `sha256:ae6f79fda70dbbbd49e90095e1d35c56607453a528cd8038eae4cfb8a17f02d7` |
| 模型 | `deepseek-v4-flash` |
| 新pins SHA-256 | `de23ee8c67a5215531760709bffb7a2a048346653020502fb399f1069572c344` |

Worker和服务入口没有变更，复用各自已验证镜像；网络登记修订在宿主编排，独立验收修订在新bench。零模型工具面会针对每个新scope重新运行，不借用旧通过文件。

同入口单用户基线与双用户101冒烟均已结束。由于冒烟的预算结果、有效并发窗口及观测完整性未闭合，本轮不扩大为正式三种子；缺口进入比较与后续投入判断。原失败保留，不通过反复重跑挑成功。

取消请求ACK不表示取消完成。缺少原生结束事件时保留取消缺口，不能用暂停或强制退出补通过。同Worker换用户不支持；当前采用独立Worker，浏览器使用期隔离尚未验证。这些边界和额外接入成本都进入后续比较。

本轮smoke的[原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/scope/i24-dsh-smoke-101-001/scope-verdict.json)分别保留六项检查。两主任务通过说明各自业务、原状态恢复及累计预算链成立，不替代整体产品边界检查。两个取消的实际错误是`inflight_or_unknown_model_result`，发生在取消原生判定前，不能将它写成再次证实缺少结束事件。取消请求量待独立SQL截点统计，不从turn数倒推。

唯一采样错误针对用户B的旧attempt包装容器：原终态exit143、非OOM；rm已请求，但样本看到`removing/Dead=true`，当时确切ID仍存在。最终删除确认比该检查晚约21毫秒，因此原规则正确拒绝，不能拿未来删除记录补判先前样本。它属于实验观测完整性问题。并发检查的`tool_progress_not_within_actual_native_overlap`另列为未命中规定有效窗口，不能只凭两个容器同时存在宣称并发验证通过。
