# 下一轮实验运行记录 001

日期：2026-10-28。状态：**M0、M1、E1 完成；E2 双容器并发切片通过；E5 单用户故障诊断切片完成；E6 2 用户成本已测；E3/E4 部分。**

基线：[分享报告](../report/README.md)（提交 `7be4381`）。设计：[下一轮主场景实验设计](next-round-main-scenario-design.md)。

## 1. 这一轮做了什么

| 项 | 结果 | 证据 |
| --- | --- | --- |
| M0 基线冻结 | 固定基线、候选版本、任务集、种子、总 token 上限、阶段与标签；单候选 token 阈值待 DSH 校准 | [冻结计划](next-round-plan.json) |
| token 计数代理 | 新增 `execution/token_proxy.py`：转发 OpenAI/Anthropic 兼容请求，解析 usage，写 JSONL 台账，执行 token cap | 代码、离线测试 |
| 代理离线测试 | 10 项通过（含 CLI 进程）：JSON/SSE、Anthropic 合并、Responses、未知 usage、冲突、cap、401、health | `tests/execution/test_token_proxy.py` |
| 候选 profile 抽象 | 新增 `execution/candidate_profiles.py`：DA/DSH 常量与现有 worker 一致，OpenClaw 显式标 unsupported；6 项测试通过 | `tests/execution/test_candidate_profiles.py` |
| 候选常量收口 | 新增 `execution/candidate_constants.py`；`worker_control` / `dsh_materials` / profile 共用同一组 DA/DSH 启动常量，值未变 | `execution/candidate_constants.py`、worker/dsh 回归 |
| worker 入口按 candidate 分派 | `worker_class_for()` + `--candidate`；DA/DSH 选择正确，未知候选拒绝；3 项测试通过 | `tests/execution/test_worker_factory.py` |
| 已有代理/计量回归 | 66 项通过（model proxy + token usage）；新增 token 10 项 + profile 6 项，合计 82 项通过 | `tests/execution/test_model_proxy.py`、`tests/test_token_usage.py` |
| worker/dsh 常量回归 | 189 项通过（worker_control、DSH 多用户、model/token proxy、token usage） | `tests/test_worker_control.py`、`tests/test_dsh_multi_user.py` 等 |
| Docker 无模型校准 | bench `sha256:e6bbd866…`；186 项通过；network none、read-only、1 GiB/1 CPU；容器清理为空 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-28/next-round-cal-001/host.json` |
| Docker 无模型校准（含 worker 分派） | bench `sha256:1450a290…`；189 项通过；network none、read-only、1 GiB/1 CPU；容器清理为空 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-28/next-round-cal-002/host.json` |
| Docker 无模型校准（含取消预算修复） | bench `sha256:76aa6efc…`；228 项通过；network none、read-only、1 GiB/1 CPU；容器清理为空 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/next-round-cal-003/host.json` |
| DSH 单用户 baseline | trial `i29-dsh-baseline-101-001`：`passed=true`，9 次模型、3 笔写入、34.29 秒；`all_pages_read` 通过；cleanup 最终容器/网络为空 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-28/deepseek_harness/scope-baseline/i29-dsh-baseline-101-001/{baseline-verdict.json,host-result.json,cleanup-result.json}` |
| 取消预算评分修复 | `budget_snapshot(..., allow_inflight=True)`：取消子试验只允许**最后一行**是 `client_disconnected` 或 `reserved`，前面的调用仍须 2xx；新增 3 项反例测试 | `runner/pause_recovery.py`、`tests/test_pause_recovery.py` |
| DSH 双用户 smoke 001 | A/B 主任务通过；两取消因 `client_disconnected` 被旧预算校验判失败；host/cleanup 通过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepseek_harness/scope/i29-dsh-smoke-101-001/` |
| DSH 双用户 smoke 002 | A/B 主任务通过；B 取消通过；A 取消缺原生 `turn/end`+idle 终态；有效工具重叠未命中；host `passed=true`、cleanup 空 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepseek_harness/scope/i29-dsh-smoke-101-002/` |
| 取消组方案 A | 有效重叠只查 `main`；`cancel` 改为 live-peer（双方原生前缀增长），允许最后一行 `reserved`/`client_disconnected`；新增 live-peer 2 项测试 | `runner/multi_user.py`、`tests/test_cancel_live_peer.py` |
| DSH 双用户 smoke 005 | live-peer/重叠通过；host/cleanup 通过；A 取消预算尾部 `reserved` 被旧口径拦住；B 取消缺原生终态 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepseek_harness/scope/i29-dsh-smoke-101-005/` |
| DSH 双用户 smoke 006 | live-peer/重叠通过；B 取消通过；A 取消缺原生 `turn/end`+idle；host 采样 flake、cleanup 空 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepseek_harness/scope/i29-dsh-smoke-101-006/` |
| DSH 取消根因修复 | `fetch` 在 abort cause 上 `defineProperty` 不可枚举 `stack`，DSH lossless JSON 拒绝 `turn/end`；用 Proxy 吞掉 `stack`，保留 `kind/reason`；新 worker `harness-lab-deepseek-harness:cancel-terminal-002`（`sha256:256f…`） | `adapters/deepseek_harness/scoped-runtime-entrypoint.ts`、`external/build-context/deepseek_harness-multi-user-002` |
| DSH 双用户 smoke 008 | A/B 主任务、A/B 取消、live-peer/重叠全部通过；A 取消有原生 `turn/end`+idle；host 采样 flake、cleanup 空 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepseek_harness/scope/i29-dsh-smoke-101-008/` |
| 采样/生命周期串行化 | `sample_once` 与 `retire` 共用 `sampling_lock`；surface 校准容器排除持续采样；消除 logout/removing 竞态 | `scripts/run_multi_user.py` |
| DSH 双用户 smoke 011 | `scope=true`、`host=true`、`cleanup=true`；A/B 主任务、A/B 取消、live-peer/重叠、资源生命周期全部通过；sampling 0、容器 0 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepseek_harness/scope/i29-dsh-smoke-101-011/` |
| DA smoke 001 | bench helper 与 DA worker staged helper 不一致；`image_bundled_helper_hash_mismatch`；未进入任务 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepagents_langgraph/scope/i29-da-smoke-101-001/` |
| DA smoke 003 | 任务级 3/4：A 通过、A/B 取消通过、B 因 `page_size=100` 失败；重叠、host、资源、cleanup 通过；sampling 0 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/deepagents_langgraph/scope/i29-da-smoke-101-003/` |
| OpenClaw tools 单用户 | `i29-openclaw-tools-101-001`：passed，8 次模型、3 笔写入、16 项检查全过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openclaw/tools/i29-openclaw-tools-101-001/` |
| OpenClaw cancel 单用户 | `i29-openclaw-cancel-101-001`：passed，3 次模型，原生 aborted 终态，取消后无新写入 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openclaw/cancel/i29-openclaw-cancel-101-001/` |
| OpenClaw 双容器并发（2.5 GiB） | `i29-openclaw-e2g-cancel-101` 通过（A cancel，原生 aborted）；`i29-openclaw-e2h-tools-101` 通过（B tools，11 次模型、3 笔写入）；同一秒启动、无 OOM | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openclaw/` |
| E6 2 用户按需成本 | A 峰值 2.28 GiB/200.94% CPU；B 峰值 2.19 GiB/205.19%；5/10 用户超过 8.3 GiB/4 CPU，`SKIP_LOCAL`；常驻未测 | 同上 `resource-metrics.json` |
| E5 单用户诊断切片 | 缺文件：OpenClaw 5 次/68.0s 通过，DSH 2 次/7.3s 通过；timeout：OpenClaw 7 次/90.3s `all_pages_read` 失败，DSH 9 次/29.2s 通过；取消：OpenClaw 3 次/DSH 7 次通过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openclaw/`、`.../deepseek_harness/` |
| ADK tools 单用户门禁 | `i29-adk-tools-101-001`：passed，7 次模型、3 笔写入、16 项检查全过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/google_adk/tools/i29-adk-tools-101-001/` |
| Agents SDK tools 单用户门禁 | `i29-agents-tools-101-001`：passed，5 次模型、3 笔写入、16 项检查全过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openai_agents_sdk/tools/i29-agents-tools-101-001/` |
| OpenHands tools 单用户门禁 | `i29-openhands-tools-101-001`：passed，8 次模型、3 笔写入、16 项检查全过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/openhands/tools/i29-openhands-tools-101-001/` |
| Pi tools 单用户门禁 | `i29-pi-tools-101-001`：passed，5 次模型、3 笔写入、16 项检查全过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/pi_agent_core/tools/i29-pi-tools-101-001/` |
| Codex tools 单用户门禁 | `i29-codex-tools-101-001`：passed，7 次模型、3 笔写入、16 项检查全过 | `/Users/hanbowen01/harness-lab-data/runs/2026-10-29/codex_sdk/tools/i29-codex-tools-101-001/` |
| 真实非流式 smoke | 200；input 88 / output 20 / total 108；reasoning 18；ledger `reported` | `/Users/hanbowen01/harness-lab-data/runs/2026-10-28/token-proxy-smoke-001/{smoke.json,ledger.jsonl}` |
| 真实流式 smoke | 200；input 88 / output 14 / total 102；reasoning 12；ledger `reported` | `/Users/hanbowen01/harness-lab-data/runs/2026-10-28/token-proxy-smoke-002/{smoke.json,ledger.jsonl}` |

真实 smoke 使用 `.env` 中的固定模型 `deepseek-v4-flash`，每次只发一条很小的非敏感请求；台账不包含提示、响应正文、密钥或请求头。两次 smoke 都通过本地 token 代理转发，证明“代理 → 供应商 → usage → ledger”这条链在本机可用。

## 2. 能证明什么，不能证明什么

**能证明：**

- OpenAI Chat Completions 的非流式和流式响应都能取到供应商 usage，并写入逐请求台账。
- 流式请求会被补 `stream_options.include_usage=true`，供应商确实返回最终 usage。
- 台账字段与 cap 逻辑在离线夹具下成立；未知 usage 不会被补成 0。

**还不能证明：**

- Anthropic Messages / OpenAI Responses 只用合成夹具验证过，没有真实供应商 smoke。
- token cap 没有在真实长会话里触发过；只验证了离线夹具。
- 工具调用、shell/构建/测试次数没有并入 token；它们按设计单列。
- 还没有开始候选接入、共享场景台校准或 DSH/OpenClaw/Deep Agents 的主场景实验。

## 3. 下一步

1. **M0 收尾**：DSH 基线后校准单候选绝对 token 上限；已冻结的基线、候选版本、任务集、种子、总 cap 和阶段不再改。
2. **M1 共享场景台**：复用 I24/I25 的身份、路由、采样、清理，抽候选 profile；先做无模型校准。
3. **E1 DSH 门禁**：用 token 代理接一个最小适配器，先做提交/状态/取消三接口；记录 token/迭代，验证门禁 10M/20 次是否合理。
4. **校准 Anthropic/Responses**：如果接入 agent 用 Claude Code 或 Responses 协议，先用合成上游 + 一次真实小请求补验，再进入候选。

## 4. 复现命令

```sh
PYTHONPATH=. .venv/bin/python scripts/smoke_token_proxy.py \
  --output /Users/hanbowen01/harness-lab-data/runs/2026-10-28/token-proxy-smoke-001

PYTHONPATH=. .venv/bin/python scripts/smoke_token_proxy.py --stream \
  --output /Users/hanbowen01/harness-lab-data/runs/2026-10-28/token-proxy-smoke-002
```

离线测试：

```sh
.venv/bin/python -m pytest -q tests/execution/test_token_proxy.py
```
