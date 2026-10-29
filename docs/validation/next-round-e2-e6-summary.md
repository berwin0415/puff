# E2–E6 执行汇总与缺口

日期：2026-10-29。范围：下一轮主场景实验的可执行部分。结论：**实验已执行并留存证据，但 E2/E5/E6 是部分通过，不是候选全通过，也没有跨场景赢家。**

基线：[分享报告](../report/README.md)。设计与计划：[设计](next-round-main-scenario-design.md)、[冻结计划](next-round-plan.json)。任务板：[剩余任务与执行状态](next-round-remaining-tasks.md)。

## E2 OpenClaw

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 单用户 tools | 通过：8 次模型、3 笔写入、16 项检查 | `i29-openclaw-tools-101-001` |
| 单用户 cancel | 通过：3 次模型、原生 aborted、取消后无新写入 | `i29-openclaw-cancel-101-001` |
| 双容器并发 tools（B） | 通过：11 次模型、3 笔写入 | `i29-openclaw-e2h-tools-101` |
| 双容器并发 cancel（A） | 通过：原生 aborted，取消检查全过 | `i29-openclaw-e2g-cancel-101` |
| 2 GiB 并发 | A 被 `OOMKilled` | `i29-openclaw-e2e-cancel-101` |
| 2.5 GiB 并发 | A/B 同时通过、无 OOM | 上述 e2g/e2h |

**未执行/缺口：**

- 没有共享产品后端，因此不是同一产品下的 A/B 用户。
- 跨用户 403 未测。
- live-peer / 有效重叠未测。
- 暂停/恢复未测；OpenClaw 当前 one-shot 入口没有 pause/resume 控制面。
- 不能把“两个独立 compose project 并发”写成完整 E2 门禁。

## E3 Deep Agents

`i29-da-smoke-101-003`：

- A 主任务通过；A/B 取消通过。
- B 主任务失败：`page_size=100`，违反每页最多 5 条。
- live-peer/重叠、host、资源生命周期、cleanup 通过；sampling 0。

判定：**部分通过**。B 是候选/模型侧分页失败，不是 harness 失败；保留上一轮同类失败。

## E4 候选门禁

单用户 tools 能力门禁全部通过：

| 候选 | trial | 模型 | 写入 |
| --- | --- | ---: | ---: |
| Google ADK | `i29-adk-tools-101-001` | 7 | 3 |
| OpenAI Agents SDK | `i29-agents-tools-101-001` | 5 | 3 |
| OpenHands | `i29-openhands-tools-101-001` | 8 | 3 |
| Pi Agent Core | `i29-pi-tools-101-001` | 5 | 3 |
| Codex SDK | `i29-codex-tools-101-001` | 7 | 3 |

**边界：** 这只是单用户 tools 能力门禁；设计里的 4 小时多用户服务门禁未做。不能据此让任何候选进入全量，也不能据此排名。

## E5 过程透明

单用户故障诊断切片：

| 故障 | OpenClaw | DSH |
| --- | --- | --- |
| 缺文件 | 通过，5 次/68.0s | 通过，2 次/7.3s |
| 模型 504 timeout | 失败，7 次/90.3s，`all_pages_read` 失败 | 通过，9 次/29.2s |
| 运行中取消 | 通过，3 次模型 | 通过，7 次模型 |

两者 timeout 都保留了 504 注入和原生重试证据。

**缺口：** 没有多用户过程透明、独立诊断 agent、人工定位时间；不能报“谁更好”的显著性。

## E6 实例成本

2 用户按需并发（OpenClaw，2.5 GiB/用户）：

| 用户 | 内存峰值 | CPU 峰值 | 模型调用 |
| --- | ---: | ---: | ---: |
| A cancel | 2.28 GiB | 200.94% | 2 |
| B tools | 2.19 GiB | 205.19% | 11 |

- 2 用户按需：可报。
- 5/10 用户：按 2.5 GiB/用户 + services 超过本机 8.3 GiB / 4 CPU，`SKIP_LOCAL`。
- 常驻模式、唤醒成本：未测，不报。
- 采样含 services，不是纯候选内存；峰值不能直接相加当常驻成本。

## 总判定

| 阶段 | 判定 |
| --- | --- |
| E2 | 双容器并发切片通过；完整双用户门禁未通过（403/live-peer/暂停/共享后端缺口） |
| E3 | 部分通过；B 分页失败 |
| E4 | 单用户能力门禁通过；多用户服务门禁未做 |
| E5 | 单用户诊断切片完成；多用户/独立诊断 agent 未做 |
| E6 | 2 用户按需成本已测；5/10 `SKIP_LOCAL`；常驻未测 |

**没有跨场景赢家，也没有“接入成本低”的结论。** 单候选 token 上限仍是预注册预算，不是实测中位数。

## 证据根目录

- OpenClaw：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openclaw/`
- Deep Agents：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepagents_langgraph/scope/i29-da-smoke-101-003/`
- DSH：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepseek_harness/`
- 其他候选：`/Users/hanbowen01/harness-lab-data/runs/2026-10-29/{google_adk,openai_agents_sdk,openhands,pi_agent_core,codex_sdk}/tools/`
