# 下一轮剩余任务与执行状态

日期：2026-10-29。状态：**M0、M1、E1 完成；E2 双容器并发切片通过（2.5 GiB）；E6 已测 2 用户按需成本、5/10 用户本机跳过；E3/E4 部分；E5 未执行。**

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
| R-E2-01 | OpenClaw 双用户主场景：A/B 主任务、跨用户拒绝、取消、live-peer/重叠、host/cleanup | M1、E1 | 部分 | 2026-10-29 |  | 双容器并发 2.5 GiB：A cancel `i29-openclaw-e2g-cancel-101` + B tools `i29-openclaw-e2h-tools-101` 同时通过 | 403/共享后端/live-peer/暂停未接；不是同一产品后端 |
| R-E2-02 | OpenClaw 业务定制：加 1 工具 + 1 规则 + 1 状态字段；规则执行层拦截；重试/恢复后仍生效 | R-E2-01 | 未开始 |  |  |  | 定制 token/迭代单独记 |
| R-E3-01 | Deep Agents 同场景复测：主任务、取消、恢复、重叠、host/cleanup | M1、E1 | 部分 | 2026-10-29 |  | `i29-da-smoke-101-003`：A 通过、B 分页失败；A/B 取消、重叠、host、资源通过 | B 仍 `page_size=100`，保留旧失败 |
| R-E4-01 | 第二层门禁：ADK、Agents SDK；过门禁且预算允许最多 1 条全量 | M1、token 代理 | 部分 | 2026-10-29 |  | ADK tools `i29-adk-tools-101-001`、Agents SDK tools `i29-agents-tools-101-001` 均通过 | 单用户能力门禁通过；多用户服务门禁未做 |
| R-E4-02 | 第三层门禁：OpenHands、Pi、Codex；只做门禁/跳过记录 | M1、token 代理 | 部分 | 2026-10-29 |  | OpenHands/Pi/Codex tools 均通过 | 单用户能力门禁通过；多用户/服务门禁未做 |
| R-E5-01 | 过程透明：OpenClaw vs DSH，3 类注入故障，诊断时间/步骤/token/原生证据 | E2、E1 | 受阻 |  |  |  | 依赖 E2 多用户；当前只有 OpenClaw 单用户材料 |
| R-E6-01 | 实例成本：2/5/10 用户，常驻 vs 按需，内存/CPU/唤醒/每任务成本 | E2、E3 | 部分 | 2026-10-29 |  | 2 用户按需：A/B 峰值 2.28/2.19 GiB；5/10 用户 `SKIP_LOCAL`（8.3 GiB/4 CPU 装不下） | 常驻模式未测；不能报节省比例 |
| R-TK-01 | 接入 token 台账：每候选独立 session、ledger、尾部 `reserved`/`client_disconnected`、cap | token 代理 | 未开始 |  |  |  | 不可观测的 agent 标 `INCONCLUSIVE` |
| R-RP-01 | 更新分享报告与附录：E2–E6 结果、接入成本、跳过原因 | R-E2～R-E6 | 未开始 |  |  |  | 不把 baseline 当 E1；不跨批次加总 |
| R-CM-01 | 分阶段提交：每阶段代码/材料/状态记录 | 各阶段 | 进行中 | 2026-10-29 |  | `8c3b07f` | M0/M1/E1 已提交；后续按阶段提交 |

## 2.1 E2 最小切片与明确缺口

已执行的最小切片（单用户 + 双容器并发）：

| 项 | 状态 | 证据 |
| --- | --- | --- |
| OpenClaw tools 单用户 | 通过 | `i29-openclaw-tools-101-001`：8 次模型、3 笔写入、16 项检查全过 |
| OpenClaw cancel 单用户 | 通过 | `i29-openclaw-cancel-101-001`：3 次模型、原生 aborted 终态、取消后无新写入 |
| 双容器并发 B tools | 通过 | `i29-openclaw-e2b-tools-101`：11 次模型、3 笔写入、16 项检查全过 |
| 双容器并发 A cancel | **通过** | `i29-openclaw-e2g-cancel-101`：2.5 GiB，原生 aborted，全部取消检查通过 |
| 双容器并发 B tools | **通过** | `i29-openclaw-e2h-tools-101`：2.5 GiB，11 次模型、3 笔写入 |

未执行、列为明确缺口：

| 缺口 | 状态 | 原因 |
| --- | --- | --- |
| OpenClaw A/B 双用户并发 | 未执行 | 缺 `worker_control_openclaw`；one-shot 入口没有常驻控制面 |
| 跨用户 403 | 未执行 | 缺 OpenClaw 任务归属/产品后端接入 |
| live-peer / 有效重叠 | 未执行 | 依赖双用户 worker |
| 暂停/恢复 | 未执行 | 当前 OpenClaw 入口无 pause/resume 控制面；不能拿 SIGSTOP 冒充 |
| 双用户 host/cleanup | 未执行 | 依赖双用户 worker |

判定：**E2 部分，不能记通过。** 单用户 tools/cancel 只能证明 Gateway 入口和断连取消可用，不能证明多用户隔离、403 或暂停恢复。

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

- 状态：部分（单用户能力门禁通过；多用户服务门禁未做）
- 候选：Google ADK、OpenAI Agents SDK
- 门禁 token/迭代：ADK 7 次模型、Agents SDK 5 次模型；各 3 笔写入
- 是否进入全量：否；未做多用户服务门禁
- 证据路径：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/google_adk/tools/i29-adk-tools-101-001/`、`.../openai_agents_sdk/tools/i29-agents-tools-101-001/`
- 判定：单用户工具任务成立；不能据此说多用户隔离/恢复/成本通过。
- 备注：多用户 worker profile 尚未接。

### R-E4-02 第三层门禁

- 状态：部分（单用户能力门禁通过；多用户/服务门禁未做）
- 候选：OpenHands、Pi Agent Core、Codex SDK
- 门禁结果：tools 均 passed；OpenHands 8 次模型、Pi 5 次、Codex 7 次；各 3 笔写入
- 跳过原因（五要素）：未跳过；多用户服务门禁未做，不能据此进入全量
- 证据路径：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openhands/tools/i29-openhands-tools-101-001/`、`.../pi_agent_core/tools/i29-pi-tools-101-001/`、`.../codex_sdk/tools/i29-codex-tools-101-001/`
- 判定：单用户工具任务成立；不证明多用户隔离/恢复/成本。
- 备注：Tier3 仍不默认全量；只有门禁显示明显更优且预算允许才加做。

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

- 状态：部分
- 批次：`i29-openclaw-e2g-cancel-101` + `i29-openclaw-e2h-tools-101`
- 用户档位：2 用户并发
- 常驻/按需：按需；常驻未测
- 内存/CPU/唤醒：A 峰值 2.28 GiB / CPU 200.94%；B 峰值 2.19 GiB / CPU 205.19%；两栈并发无 OOM；5/10 用户按 2.5 GiB/用户 + services 超过本机 8.3 GiB，`SKIP_LOCAL`
- 采样是否完整：A 31/31，B 47 样本 1 次 grading 采样错误；candidate_active 0 错误
- 证据路径：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openclaw/`
- 判定：2 用户按需成本可报；5/10 用户不报，常驻/唤醒不报
- 备注：采样含 services，不是纯候选内存；不能把峰值相加当常驻成本

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
