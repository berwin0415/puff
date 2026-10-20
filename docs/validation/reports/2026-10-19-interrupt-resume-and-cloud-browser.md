# 中断恢复、OpenClaw 适配器与云浏览器验证

日期：2026-10-19

本报告记录三项新增验证：

1. Deep Agents/LangGraph 的非审批暂停与跨进程恢复（主证据），以及可选的工具审批中断；
2. OpenClaw 统一适配器、真实 API 和 T1 工具任务；
3. 无 Electron 的 Chromium + `@playwright/mcp` 云浏览器轨道。

## 1. Deep Agents 非审批暂停与跨进程恢复

云端 Harness 的暂停恢复不需要等待人工批准。本轮因此把**模型节点安全点暂停**
作为主证据，审批中断只保留为可选能力。

实现位置：

- `adapters/deepagents_langgraph/worker.py`
  - `pause_after_model=True` 时用 `interrupt_after=["model"]` 重新编译图；
  - `mode=run` 输出 `pause_requested` / `graph_paused`；
  - `mode=resume` 使用 `Command(resume=True)` 继续同一 `thread_id`；
  - `interrupt_on` 仍可用于可选的工具审批；
  - `mode=inspect` 只读检查 SQLite checkpoint。
- `adapters/deepagents_langgraph/adapter.py`
  - `run_paused()`、`resume_paused()`、`inspect_checkpoint()`、`snapshot()`；
  - `run_interruptible()`、`resume_run()` 保留为审批场景。
- `scripts/run_deepagents_pause.py` 生成非审批暂停证据。

真实模型运行（run_id `deepagents-pause-d25e50a2-caec-4826-9ef3-6a566e6c93cb`）：

```text
first process:
  process_started → graph_started → graph_update ×2
  → pause_requested → graph_paused → process_exited
  pause_kind: after_model_safe_point
  approval_required: false
  checkpoint_id: 1f1cbc6a-a42a-6844-8002-4dbae599dbf5

resume process (new adapter/process, same SQLite db + thread_id):
  process_started → graph_resumed → graph_update ×3
  → graph_completed → process_exited
  answer: fixture-alpha
```

边界：暂停发生在模型节点结束后的安全点，不是模型调用中途的即时抢占；
`cancel`、实时 `send_input` 和容器级恢复仍未验证。审批中断的 run_id
`deepagents-interrupt-8103c501-e33e-44f2-b5b1-e324f1f26628` 仍保留，但它不是
云端 Harness 的必要条件。

证据目录：
`~/harness-lab-data/runs/2026-10-19/deepagents_harness/pause-resume/deepagents-pause-d25e50a2-caec-4826-9ef3-6a566e6c93cb/`

## 2. OpenClaw 统一适配器

新增 `adapters/openclaw/`：

- `preflight()` 校验 pinned commit、`openclaw.mjs`、`package.json` 和 LICENSE；
- `build_config()` 为 OpenAI-compatible provider 生成 pinned JSON5 配置，
  API key 只通过 `${MODEL_API_KEY}` 引用；
- `run_once()` 调用 `openclaw agent exec --message-file - --json --config ...`；
- 将 JSON envelope 映射为 `process_started`、`assistant_output`、`candidate_error`、
  `process_exited`，并写入 `manifest.json`、`events.jsonl`、`envelope.json`；
- pause/resume/cancel/approval 明确 `NotImplementedError`，因为 `agent exec` 是一次性入口。

真实运行：

| 任务 | run_id | 结果 |
| --- | --- | --- |
| API 冒烟 | `98ccbdf5-1ea4-42fc-acc3-82cf32384ea5` | `OPENCLAW_API_OK`，exit 0 |
| T1 工具 | `3727278b-24f0-43e9-bcf6-0bf9f0f20731` | 通过 shell/curl 读取 Fixture，返回 `60` |

证据目录：
`~/harness-lab-data/runs/2026-10-19/openclaw/agent-smoke/<run_id>/`

### T2 浏览器：修正此前的 relay 判断

此前把 T2 阻断归因于“缺少浏览器扩展 relay”，这个判断不准确。OpenClaw 内置了
托管 `openclaw` headless profile，extension relay 只用于接管已登录的用户 Chrome。
适配器已能生成托管浏览器配置；agent 调用已到达导航策略层。

但 agent 导航到 loopback fixture 时仍返回
`browser navigation blocked by policy`，即使设置
`dangerouslyAllowPrivateNetwork: true` 和 `allowedHostnames: ["127.0.0.1"]`。
模型最终回退到 curl。因此 T2 仍未通过，阻断点是浏览器导航/SSRF 策略，
**不是 relay**。要补齐需要继续定位该策略的实际生效层，或改用非 loopback 的测试站点。

## 3. 云 Chromium + Playwright MCP

实现 `execution/browser/mcp_cdp_smoke.py`：

- 启动独立 headless Chromium，暴露 CDP endpoint；
- 启动 `node @playwright/mcp/cli.js --cdp-endpoint ... --headless --output-dir ...`；
- 使用 MCP JSON-RPC 调用 `browser_navigate`、`browser_evaluate`、`browser_type`、
  `browser_click`、`browser_wait_for`、`browser_snapshot`、`browser_tabs`、
  `browser_take_screenshot`；
- 关闭并重启 Chromium，验证 localStorage 恢复。

验证结果：

```text
transport: playwright-mcp-cdp
electron: false
mcp tools: 25
tabs: 3 (new tab + window.open)
snapshot contains accepted:harness-lab: true
screenshot: mcp-browser.png
persisted_after_restart: true
profile_flush_seconds: 6
```

说明：MCP 客户端关闭不保证 Chromium 立即提交 localStorage，因此脚本等待
`profile_flush_seconds` 后优雅关闭浏览器；该等待可通过
`HARNESS_LAB_PROFILE_FLUSH_SECONDS` 调整。

证据目录：
`~/harness-lab-data/runs/2026-10-19/browser-mcp/chromium-cdp/`

### 未覆盖：下载

`@playwright/mcp 0.0.82` 没有暴露 download 工具或稳定的下载产物契约，本轮未把
下载列为通过项。导航、表单、多 tab、`window.open`、截图和重启恢复均已覆盖。

## 4. 测试与剩余缺口

- 默认测试：`44 passed, 6 skipped`（实时模型测试默认跳过）。
- 设置 `RUN_LIVE_MODEL_TESTS=1`：`50 passed`。
- 仍未验证：任意点 pause/resume、cancel、实时输入、OpenClaw Gateway 会话恢复、
  OpenClaw T2 浏览器、DeepSeek 云端浏览器全链路、T6 实际改动量。
