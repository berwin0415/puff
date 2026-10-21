# DSH I24 零模型工具面预算读取修订

日期：2026-10-21。主任务报告真实同入口基线 `i24-dsh-baseline-101-001` 在首个 `prepare-surface` 阶段、远程模型调用前失败，清理通过。失败原件保留。本修订只编写源码与回归用例，**未执行测试、编译、Docker、SDK或模型**；不能据此宣称新基线通过。

`runner.pause_recovery.budget_snapshot` 默认 `allow_empty=False`，因此有效新库即使token/cap正确且0次调用，也会被 `model_call_reservation_history_invalid` 拒绝。I22原任务启动前使用 `budget_snapshot(..., allow_empty=True)`；原I22工具面校准另行读取实际调用计数并要求0。DSH新工具面准备/验收遗漏了这个空历史参数。

最小生产修改仅有两处：`runner/multi_user_dsh.py` 的 `prepare_surface` 和 `accept_surface` 显式传 `allow_empty=True`。外层仍严格要求count为0，共享函数仍实际读取SQLite并校验预算行 `(1, 当前run token摘要, 20)`；普通活跃任务的非空历史要求、调用次序、成功状态及身份检查均不改。

`tests/test_dsh_multi_user.py` 的工具面fixture改用真实 `CallBudget` 初始化同schema SQLite及run token，删除原 `budget_snapshot` stub。待容器执行的回归包括：

- 准备阶段接受真实空库，前后token及完整快照相同；同一个库通过普通快照默认入口仍被非空历史门禁拒绝。
- 验收阶段使用真实空库完成原始工具面字节与pins校验，并核对预算未变。
- 准备及验收分别拒绝真实库的token不匹配、预算cap不匹配和已有一次成功调用。
- 原有挂载、配额和零模型反例也读取真实库；“已有模型调用”反例通过实际reserve/finish写入，不模拟计数。

离线用例不会访问远程模型或启动候选进程；合成工具面仍不是原生能力证据。主任务需在后续独占窗口重建bench后校准，并用全新trial ID重试基线。没有修改I23/I25、共享budget函数、候选核心、原初交付freeze及旧失败结果。

修改文件与本说明的摘要另存 [本轮SHA清单](deepseek-multi-user-surface-budget-fix.sha256)。此前 [初交付清单](deepseek-multi-user-source-freeze.sha256)、[首次评审修订清单](deepseek-multi-user-review-fixes.sha256) 作为历史快照保留。
