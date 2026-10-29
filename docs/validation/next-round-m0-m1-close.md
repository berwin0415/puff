# M0 / M1 完成记录

日期：2026-10-29。状态：**M0、M1、E1 完成；E2/E3 未开始。**

基线：[分享报告](../report/README.md)（提交 `7be4381`）。设计：[下一轮主场景实验设计](next-round-main-scenario-design.md)。冻结计划：[next-round-plan.json](next-round-plan.json)。

## 1. M0：输入已冻结

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 基线版本 | 完成 | 报告提交 `7be4381`；报告/设计/工具 SHA-256 已核对 |
| 候选版本 | 完成 | 8 条候选、三层分组写入 plan |
| 任务集 | 完成 | `configs/experiment.yaml` + 9 份任务 YAML；种子 101/202/303 |
| token 预算 | 完成 | 全轮 500M；门禁 10M/20 迭代；MSC-min 50M、全量 100M |
| 判定标签 | 完成 | `PASS` / `FAIL_*` / `SKIP_*` / `BLOCKED` / `NOT_TESTED` / `INCONCLUSIVE` |
| 时间/台账模板 | 完成 | 设计第 12 节；接入 token 台账 |

**口径说明：** 单候选 token 上限按预算冻结，**不是实测中位数**。本轮没有完成 DSH 集成 token 的实测采样，因此 50M/100M 是预注册预算，不是“平均接入成本”。M0 完成指输入冻结，不声称阈值已被实测校准。

## 2. M1：共享场景台与前置校准

| 项 | 状态 | 证据 |
| --- | --- | --- |
| token 计量代理 | 完成 | `execution/token_proxy.py`；10 项测试；真实非流式 108 token、流式 102 token |
| 候选 profile | 完成 | `execution/candidate_profiles.py`；6 项测试；DA/DSH 常量一致，OpenClaw 标 unsupported |
| 常量收口 | 完成 | `execution/candidate_constants.py`；worker/dsh/profile 共用 |
| worker 分派 | 完成 | `worker_class_for()` + `--candidate`；3 项测试 |
| 取消预算/live-peer | 完成 | `budget_snapshot(allow_inflight)` 允许尾部 `reserved`/`client_disconnected`；cancel 组只查 live-peer；5 项测试 |
| Docker 无模型校准 | 完成 | `harness-lab-bench:next-round-007`，231 项通过；network none、read-only、1 GiB/1 CPU |
| DSH 单用户 baseline | 完成 | `i29-dsh-baseline-101-001`：passed=true，9 次模型、3 笔写入、34.29 秒 |
| DSH 双用户任务级 | 完成 | `i29-dsh-smoke-101-010`：A/B 主任务、A/B 取消、live-peer/重叠全部通过 |
| A 取消原生终态 | 完成 | Proxy 吞掉 fetch 的不可枚举 `stack`；`turn/end` reason 为 aborted/hook/experiment_cancel，idle `cancel_confirmed=true` |

**M1 完成的边界：** 共享场景台是复用 I24 `ProductBackend`/worker control 并做 candidate profile 泛化，不是另建一套独立 MSC API。231 项 Docker 测试是无模型校准；DSH baseline 和双用户 smoke 是真实模型运行。

## 3. E1：完成

`i29-dsh-smoke-101-011`：

- 任务级 checks 全绿：`task-a`、`task-a-cancel`、`task-b`、`task-b-cancel`、`product_boundary_overlap_reconnect`、`actual_container_resources_and_lifecycle`。
- `host-result.json` 为 true；`sampling_errors=0`。
- `cleanup-result.json` 为 true；最终容器/网络为空。
- A 取消有原生 `turn/end` + idle，`cancel_confirmed=true`。

宿主采样竞态的处理：`sample_once` 与 `retire` 共用 `sampling_lock`，短命 worker 的 stop/rm 不再与采样并发；surface 校准容器继续排除在持续采样之外。这样采样器不会再把 `removing` 状态的 worker 当成无登记对象。

## 4. 结论

- **M0：完成**（输入冻结；token 阈值是预算值，不是实测中位数）。
- **M1：完成**（profile/dispatch、token 计量、取消判定修复、Docker 无模型校准、DSH baseline 与双用户任务级证据）。
- **E1：完成**（任务级、host、cleanup 全部通过；sampling_errors=0）。
- 下一步：E2（OpenClaw）/ E3（Deep Agents）尚未开始。
