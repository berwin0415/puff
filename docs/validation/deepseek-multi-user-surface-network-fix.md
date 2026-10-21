# DSH I24 工具面校准网络身份修订

日期：2026-10-21。本轮仅编写源码和离线测试，**未执行测试、编译、Docker、SDK或模型**。原失败基线001/002及此前冻结清单不修改；后续必须使用新材料与新trial。

基线002的 [原始surface记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepseek_harness/scope-baseline/i24-dsh-baseline-101-002-main-a/trusted/surface-container.json) 显示同一个网络名 `harness-lab-i24-dsh-baseline-101-002-a`、别名 `surface-a`：创建态 `network_id` 为空，退出态补全为 `393a44e9de23f735cc50dd5079d5c9f3e6154c6222e8175052b998487f8af902`。校准进程退出0且确切容器删除已记录。原验收将两个网络字典整份比较，错误拒绝了该创建态到退出态的正常补全；此记录不等于基线或Agent任务通过。

最小修订位于DSH工具面路径：

- `scripts/run_multi_user.py:calibrate_surface` 从 `self.network_registrations` 选取此前真实 `docker network create` 返回的唯一同名登记，核对key/id/本trial一致；不满足时在启动校准前拒绝。以独立 `network_binding` 保存确切name/id/trial及预定alias，不根据退出态随意选一个非空ID。
- `runner/multi_user_dsh.py:accept_surface` 核对绑定来自当前task的trial/字母，前后观察均只含同一个预期网络名及精确alias。创建态ID仅允许空串或该固定ID；退出态必须等于该固定ID。不同非空ID、退出态空ID、多个网络及别名变化继续拒绝。
- `tests/test_dsh_multi_user.py` 的surface材料按实际“创建态空ID→退出态固定ID”构造，仍使用真实SQLite及完整验收器。新增空/已固定创建态正例、错误前后ID/多网络/改alias/改名/缺登记反例；宿主测试执行真实calibrate方法及JSON写入，仅Docker I/O被假实现替换，并检查缺登记、错误key、同名多登记、异trial都在启动前拒绝。

没有修改公共资源采样、网络清理、候选核心、I23/I25冻结代码或其他实际试验的验收口径。新测试是边界校准，不证明真实网络或候选能力。主任务需在后续独占窗口构建新bench，再以新trial验证。

本次修改文件摘要另存 [网络修订SHA清单](deepseek-multi-user-surface-network-fix.sha256)。历史 [初交付](deepseek-multi-user-source-freeze.sha256)、[首次评审修订](deepseek-multi-user-review-fixes.sha256)、[空预算修订](deepseek-multi-user-surface-budget-fix.sha256) 均保留。
