# 下一轮剩余任务与执行状态

日期：2026-10-29。状态：**M0、M1、E1 完成；E2 单用户 tools/cancel 通过、多用户未接；E3 部分；E4–E6 未开始。**

基线：[分享报告](../report/README.md)（提交 `7be4381`）。设计：[下一轮主场景实验设计](next-round-main-scenario-design.md)。冻结计划：[next-round-plan.json](next-round-plan.json)。完成记录：[M0/M1/E1](next-round-m0-m1-close.md)。

## 1. 已完成

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| M0 输入冻结 | 完成 | [冻结计划](next-round-plan.json)；token 阈值是预注册预算，非实测中位数 |
| M1 共享场景台/前置校准 | 完成 | 231 项 Docker 无模型测试；profile、worker 分派、token 代理、取消判定 |
| E1 DSH 双用户门禁 | 完成 | `i29-dsh-smoke-101-011`：scope/host/cleanup 全 true，sampling 0 |
| DSH A 取消原生终态 | 完成 | 原生 `turn/end` + idle；Proxy 吞掉 fetch 的非枚举 `stack` |

## 2. 剩余任务板

状态只用：`未开始`、`进行中`、`已完成`、`部分`、`跳过`、`受阻`、`无法本机执行`。未发生的时间/证据栏留空，不预填通过。

| ID | 任务 | 依赖 | 状态 | 开始 | 完成 | 证据 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R-E2-01 | OpenClaw 双用户主场景：A/B 主任务、跨用户拒绝、取消、live-peer/重叠、host/cleanup | M1、E1 | 部分 | 2026-10-29 |  | 单用户 tools `i29-openclaw-tools-101-001` 通过；cancel `i29-openclaw-cancel-101-001` 通过 | 多用户 gateway worker profile 仍未接；暂停/恢复未验 |
| R-E2-02 | OpenClaw 业务定制：加 1 工具 + 1 规则 + 1 状态字段；规则执行层拦截；重试/恢复后仍生效 | R-E2-01 | 未开始 |  |  |  | 定制 token/迭代单独记 |
| R-E3-01 | Deep Agents 同场景复测：主任务、取消、恢复、重叠、host/cleanup | M1、E1 | 部分 | 2026-10-29 |  | `i29-da-smoke-101-003`：A 通过、B 分页失败；A/B 取消、重叠、host、资源通过 | B 仍 `page_size=100`，保留旧失败 |
| R-E4-01 | 第二层门禁：ADK、Agents SDK；过门禁且预算允许最多 1 条全量 | M1、token 代理 | 未开始 |  |  |  | 4h/token 门禁；另一条 `NOT_TESTED` |
| R-E4-02 | 第三层门禁：OpenHands、Pi、Codex；只做门禁/跳过记录 | M1、token 代理 | 未开始 |  |  |  | 通过也不默认全量；写清 skip 五要素 |
| R-E5-01 | 过程透明：OpenClaw vs DSH，3 类注入故障，诊断时间/步骤/token/原生证据 | E2、E1 | 未开始 |  |  |  | 诊断 agent 固定模型/提示；不报统计显著 |
| R-E6-01 | 实例成本：2/5/10 用户，常驻 vs 按需，内存/CPU/唤醒/每任务成本 | E2、E3 | 未开始 |  |  |  | 装不下就 `SKIP_LOCAL` 该档，不报节省比例 |
| R-TK-01 | 接入 token 台账：每候选独立 session、ledger、尾部 `reserved`/`client_disconnected`、cap | token 代理 | 未开始 |  |  |  | 不可观测的 agent 标 `INCONCLUSIVE` |
| R-RP-01 | 更新分享报告与附录：E2–E6 结果、接入成本、跳过原因 | R-E2～R-E6 | 未开始 |  |  |  | 不把 baseline 当 E1；不跨批次加总 |
| R-CM-01 | 分阶段提交：每阶段代码/材料/状态记录 | 各阶段 | 进行中 | 2026-10-29 |  | `8c3b07f` | M0/M1/E1 已提交；后续按阶段提交 |

## 3. 执行状态记录区

每跑一次，复制一节；失败和跳过也保留。不要覆盖旧记录。

### R-E2-01 OpenClaw 主场景

- 状态：部分（单用户 tools/cancel 通过；多用户未接）
- 批次/trial：`i29-openclaw-tools-101-001`、`i29-openclaw-cancel-101-001`
- 开始/结束：2026-10-29
- 命令：`scripts/run_detailed.py --candidate openclaw --case tools|cancel --seed 101`
- 结果：tools 通过（8 次模型、3 笔写入、全部 16 项检查通过）；cancel 通过（3 次模型、原生 `assistant stopReason=aborted`、无取消后新写入）
- 证据路径：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openclaw/tools/i29-openclaw-tools-101-001/`、`.../cancel/i29-openclaw-cancel-101-001/`
- 判定：单用户接入成立；多用户隔离、重叠、暂停/恢复未验。
- 备注：E2 双用户需要新的 OpenClaw gateway worker profile；当前 `candidate_profiles.openclaw.supported=false`。

### R-E2-02 OpenClaw 业务定制

- 状态：
- 批次/trial：
- 开始/结束：
- 命令：
- 结果（规则拦截位置/重试后是否生效）：
- 证据路径：
- 判定：
- 备注：

### R-E3-01 Deep Agents 同场景

- 状态：部分
- 批次/trial：`i29-da-smoke-101-003`
- 开始/结束：2026-10-29
- 命令：`scripts/run_multi_user.py --trial-id i29-da-smoke-101-003 --candidate deepagents_langgraph`
- 结果（scope/host/cleanup）：任务级 3/4；`task-a` 通过，`task-a-cancel` 通过，`task-b-cancel` 通过，`task-b` 因 `page_size=100` 违反每页最多 5 条而失败；`product_boundary_overlap_reconnect`、`actual_container_resources_and_lifecycle` 通过；host/cleanup 通过，sampling 0。
- 证据路径：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepagents_langgraph/scope/i29-da-smoke-101-003/`
- 判定：部分通过；B 是候选/模型侧分页失败，不是 harness 失败。
- 备注：首次 `i29-da-smoke-101-002` 因 DA 控制活动窗口超时失败；003 已跑通控制链。

### R-E4-01 第二层门禁

- 状态：
- 候选：
- 门禁 token/迭代：
- 是否进入全量：
- 证据路径：
- 判定：
- 备注：

### R-E4-02 第三层门禁

- 状态：
- 候选：
- 门禁结果：
- 跳过原因（五要素）：
- 证据路径：
- 判定：
- 备注：

### R-E5-01 过程透明

- 状态：
- 批次：
- 故障类型：
- 根因定位时间/步骤/token：
- 原生证据是否足够：
- 证据路径：
- 判定：
- 备注：

### R-E6-01 实例成本

- 状态：
- 批次：
- 用户档位：
- 常驻/按需：
- 内存/CPU/唤醒：
- 采样是否完整：
- 证据路径：
- 判定：
- 备注：

### R-TK-01 接入 token

- 状态：
- 候选/阶段：
- 接入 token / 模型调用 / 迭代：
- 相对 DSH：
- 台账路径：
- 判定：
- 备注：

## 4. 完成标准

- **E2**：主任务、跨用户拒绝、取消、live-peer/重叠、host/cleanup 全过；定制和成本表有数据或明确缺口。
- **E3**：同 E2；保留上一轮失败，不把复测通过覆盖旧失败。
- **E4**：每条路线有 `PASS` / `FAIL_INTEGRATION*` / `SKIP_*` / `NOT_TESTED` 之一；跳过写五要素。
- **E5**：3 类故障 × 2 条路线；诊断时间、步骤、token、原生证据足够性可核对。
- **E6**：2/5/10 档要么有完整采样，要么 `SKIP_LOCAL` 并写配额原因；不报不完整节省比例。
- **接入 token**：每候选独立 ledger；不可观测标 `INCONCLUSIVE`，不估算过门槛。

## 5. 本机无法执行时的记录格式

```text
路线/版本：
尝试：
精确错误/证据：
为什么不是候选能力失败：
还能支撑的结论：
不能支撑的结论：
解除条件：
评审人/日期：
```

## 6. 下一步顺序

1. 先接 OpenClaw gateway worker profile（R-E2-01 的前置）。
2. 跑 E2 主场景，再补 E2 定制。
3. 跑 E3 同场景复测。
4. 并行做 E4 门禁（不占全量预算）。
5. E5/E6 依赖 E2/E3 的主场景材料。
6. 最后更新报告；不为“补齐八条”而补测。
