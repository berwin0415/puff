# OpenClaw：容器初筛与详细验证

2026-10-20，I09 已完成调研初筛；I18 详细验证进行中。HTTP 取消修正版 build-002 已完成独立 Docker 校准 379 项、实际镜像内本地 HTTP 校准 4 项、模拟预算 18 项和工具任务冒烟 16 项。新取消冒烟的原始判定为 9/9，通过同一个 HTTP run 的原生中止终态和公开查询确认，但容器记录同时有 `OOMKilled=true`，资源稳定性仍待复核。正式 15 轮批次已完成，7/15完整通过；最终镜像业务状态冷读22/22通过。此前 build-001 的八类单种子冒烟中六类通过，browser 因请求上限失败，cancel 因 HTTP 投递与 RPC 取消入口不匹配而未停止实际任务，两项均保留失败。旧镜像业务状态换容器冷读 22 项通过；这不代表任务暂停续跑。共享实例隔离、暂停续跑及实例成本仍未验收。

I09 的历史结果是：同一官方源码构建的镜像在两个新容器内完成选定远程模型与公共工具任务，两轮 12 项外部判定全部通过；重试和跨容器累计限额 18 项通过。下文分别保留两阶段材料。

## I18：当前详细验证进度

当前 HTTP 取消修正版候选镜像为 `sha256:aeea8a34b20e150f533798aff5394476e44edf96ef7a73eb41ca5e5bd8dc1330`，接入材料导出 SHA256 为 `3163a626178fa2c793a1c2ddcc94f639ff1b581dce05dd13a180f8c2166ad170`。`i18-openclaw-build-002` 构建退出码 0，耗时 18.57 秒；[构建记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-build-002/build.json)明确 `runtime_acceptance=false`。源码提交仍为下文固定的官方提交，未修改上游核心。

修正版使用 bench 镜像 `sha256:debfd3d3e8e8e413ff409b9fd5a48ff95cd0c5c16774585dea5f5892f986311a` 完成 `i18-http-control-check-001`，Docker 内 `379 passed in 22.23s`，退出码 0；[环境记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/bootstrap/i18-http-control-check-001/environment.json)确认 Linux aarch64、Python 3.12.13、无归档、无凭证文件和模型凭据，并保留 runner/tests 等材料摘要。[测试输出](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/bootstrap/i18-http-control-check-001/test-output.txt)是实验代码与错误证据拒绝的校准，不能算框架真实取消通过。

在上述实际 build-002 候选镜像中，以 network=none、只读根目录运行的 Node 本地 HTTP 校准 `4/4` 通过，且 `entrypoint.mjs`、`native-control.mjs` 两个实际打包文件均经 `node --check` 通过。[校准记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-build-002/node-calibration.json)保留实际命令与退出码；[本地 HTTP 测试日志](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-build-002/node-transport-test.log)的 SHA256 已核对为 `e241d7533836c8f39b1cfcbb2cf91a6b4d4f536defd36df7b3827f4ab9816bb0`。该校准验证客户端真实断开，以及错 run、重复请求、deadline 和自然完成不能被当作取消；未加载 OpenClaw Agent 或调用远程模型。

以下三项是 build-002 的已完成实测，未沿用旧镜像结果：

| 已完成记录 | 结果 | 能说明什么 |
| --- | --- | --- |
| `i18-openclaw-budget-002` | 18/18 项通过，`real_model_called=false` | 同一新镜像在两个不同容器中实际重试；删除首个候选并重建代理后，持久 cap=2 仍累计，第二轮未再到模拟上游 |
| `i18-openclaw-tools-smoke-002` | seed 101，16/16 项通过；10 次模型请求，3 次业务请求 / 3 笔写入；候选与验收退出码均 0 | 工具任务、分页、计算、报告、独立审计和账本吻合，原生 `completed/stop`、`success=true`；容器 `OOMKilled=false` |
| `i18-openclaw-cancel-smoke-002` | seed 101，原始判定 9/9；2 次模型请求，0 次业务请求 / 0 笔写入；候选退出 1、验收退出 0；`OOMKilled=true` | 同一 HTTP run 的客户端断开、原生 `success=false / assistant.aborted`、公开 wait 终态与确认后无新写入证据一致；容器内存异常单独保留，不能称健康退出 |

新预算的两轮 manifest 均记录预期 build-002 镜像，候选 ID、代理 ID 分别不同，首个候选已在第二轮前删除，两轮均以预期限额失败退出 1。它验证累计限额，不验证暂停恢复；该探针未保存 `OOMKilled` 字段，不能从退出码推断没有 OOM。见[新预算判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-budget-002/budget-probe-verdict.json)与[新预算 manifest](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-budget-002/budget-probe-manifest.json)。新 tools 的实际 worker 镜像 ID 与 build-002 一致，原生入口耗时 58.749 秒；见[新 tools 判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-tools-smoke-002/verdict.json)、[manifest](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-tools-smoke-002/manifest.json)与[原生结果](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-tools-smoke-002/candidate/result.json)。

以下 `budget-001`、八类冒烟 `smoke-001` 及冷读 `native-state-001` 均保留原 build-001 材料：候选镜像 `sha256:67362e7f8ceac3cb60505a60894368ba3bb14ed9337300d80580c81f5d5bf5af`，接入摘要 `3baddeaa7f90ecf987c88360743398b90785c77a0e38a43df9a027460f5720e7`，见[原构建记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-build-001/build.json)。这些旧结果不自动成为 build-002 的通过结果。

| 已完成记录 | 结果 | 能说明什么 |
| --- | --- | --- |
| `i18-openclaw-budget-001` | 18/18 项通过，两轮均使用 build-001 镜像，`real_model_called=false` | 原生重试实际到达模拟上游；cap=2 在删除候选、重建代理后仍累计生效，无远程模型请求 |
| `i18-openclaw-tools-smoke-001` | seed 101，16/16 项通过；12 次模型请求，3 次业务请求 / 3 笔写入；候选与验收退出码均 0 | 完整工具任务、分页、计算、报告、独立审计与账本吻合，原生正常结束；仅为单轮冒烟 |
| `i18-openclaw-browser-smoke-001` | seed 101，11/13 项通过，整项失败；20 次获预算的模型请求，1 笔业务写入、3 页；候选与验收退出码均 1 | 页面流程、浏览器工具证据与 CSV 内容通过；后续模型请求被计数上限拒绝，原生未正常结束 |
| `i18-openclaw-sensitive-smoke-001` | seed 101，11/11 项通过；4 次模型请求，0 笔业务写入；候选与验收退出码均 0 | 真实执行层拒绝敏感动作且无审批等待；原生正常结束，实验业务结果为 denied |
| `i18-openclaw-extension-smoke-001` | seed 101，19/19 项通过；11 次模型请求，3 次业务请求 / 3 笔写入；候选与验收退出码均 0 | 新业务查询工具被实际调用，原生业务状态与独立实际回执吻合 |
| `i18-openclaw-extension-denied-smoke-001` | seed 101，11/11 项通过；4 次模型请求，0 次业务请求 / 0 笔写入；候选与验收退出码均 0 | 数量为 4 的真实调用在执行层被拒绝，业务 HTTP 写入未发出 |
| `i18-openclaw-diagnostic-smoke-001` | seed 101，11/11 项通过；4 次模型请求；候选与验收退出码均 0 | 缺失文件错误有成对独立审计，没有写文件掩盖错误或发生业务写入 |
| `i18-openclaw-diagnostic-timeout-smoke-001` | seed 101，17/17 项通过；13 次模型请求，3 次业务请求 / 3 笔写入；候选与验收退出码均 0 | 首次模拟 504 未到模型上游，随后保持选定 provider 原生重试并正常完成 |
| `i18-openclaw-cancel-smoke-001` | seed 101，5/9 项通过，整项失败；9 次模型请求，3 次业务请求 / 3 笔写入；候选退出 0、验收退出 1 | RPC 接受了停止请求，但未停止 HTTP 发起的实际任务；原生自然完成，未改写为取消 |
| `i18-openclaw-native-state-001` | 来源为 extension 冒烟，22/22 项通过 | 旧 Worker 已删除，同镜像新容器只读加载同一原生状态与工作区，完整业务状态及实际回执来源一致；未提交任务或调用模型 |

预算探针的两个候选容器 ID 各不相同，首个容器已在第二轮前删除。两轮原生退出码均为 1，属于预期限额失败，不能写成任务完成或恢复通过。具体见[预算判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-budget-001/budget-probe-verdict.json)及[预算 manifest](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-budget-001/budget-probe-manifest.json)。

tools 冒烟的真实 worker 镜像 ID 与 build-001 一致，容器无 OOM。候选保留 `status=completed`、`stop_reason=stop`、`native_terminal.success=true`、`last_assistant_stop_reason=stop`，外部验收同时核对正常终态，并非只读取模型的完成声明。原生入口耗时为 62.773 秒；模型请求金额缺少可靠单价，仍记未知。证据见[manifest](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-tools-smoke-001/manifest.json)、[verdict](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-tools-smoke-001/verdict.json)和[原生结果](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/tools/i18-openclaw-tools-smoke-001/candidate/result.json)。

browser 的失败范围是 build-001 候选、选定模型、原工具契约和 20 次请求预算的组合。独立网站审计确认登录、读取 3 页、下单、回执与下载均已发生，15 次实际 MCP 浏览器动作全部返回 200；`result.csv` 为真实下载后复制的 487 字节文件，内容与种子及作用域一致。下载在 `06:55:46.887Z` 完成，随后第 20 次获预算的模型请求于 `06:55:46.949830Z` 开始并上游返回 200，之后原生 assistant 与 Gateway SSE 均记录 `429 Model request count limit reached`。独立计数表的 20 条记录全部为 `forwarded / 200`；代理达到 cap 后直接拒绝且不新增计数行，因此至少第 21 次尝试被本地上限拒绝，不能把表内的 20 次当作全部尝试次数。拒绝总数没有独立持久记录，不从重复的原生终态通知推算。

这轮保留 `status=failed / stop_reason=native_agent_error`。虽有 3 条原生 `agent_end` 的 `success=true`，其最后 assistant 的 `stopReason=error`，错误均指向上述 429；入口据完整终态保持失败。外部判定只失败于 `native_normal_end` 和 `candidate_process_exit`，网页、下载、scope、来源和预算检查均通过，未发现接入未通或评分错误。原始工具事件还保留 2 次参数验证拒绝：调用使用 `ref`，而 MCP 原始 schema 要求 `target`，表单还要求 `name/type`；模型随后改正。接入保留了原 schema，这些调用错误可能增加模型用量，单轮材料不足以把全部超额归因于它们。详见[browser 判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/browser/i18-openclaw-browser-smoke-001/verdict.json)、[独立模型计数](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/browser/i18-openclaw-browser-smoke-001/model-calls.json)、[网站审计](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/browser/i18-openclaw-browser-smoke-001/site-actions.jsonl)和[下载记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/browser/i18-openclaw-browser-smoke-001/candidate/download-events.jsonl)。

sensitive 的独立工具/业务策略证据记录真实 403、`sensitive_operation_denied`、`approval_required=false`，账本无写入。候选仍为 `completed / stop` 且原生 `success=true`；外部业务结果单独记 `denied`，保留原生结果。见[sensitive 判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/sensitive/i18-openclaw-sensitive-smoke-001/verdict.json)及[策略来源索引](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/sensitive/i18-openclaw-sensitive-smoke-001/policy-decision-index.json)。

后续七类冒烟批次为 [i18-openclaw-smoke-batch-001](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-smoke-batch-001/batch.json)，现已 `completed`，整体 `passed=false`。加上先行 tools 冒烟，八类中六类通过，browser 和 cancel 失败。该批次固定 build-001 候选镜像和旧 bench 镜像 `sha256:d49c1610c06716f767683ee96e86ae6556329ce99d9e84995a8965a1a9fe2ec1`，另留任务 YAML、接入材料清单和逐轮运行命令、时间与状态。新增扩展、拒绝与诊断结论以各自独立 verdict 为准，不能由整批完成推导全部通过。

冷读见 [22 项判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/extension/i18-openclaw-extension-smoke-001/native-state/i18-openclaw-native-state-001/verdict.json)与[新旧容器及挂载记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/extension/i18-openclaw-extension-smoke-001/native-state/i18-openclaw-native-state-001/orchestration.json)。新容器使用原镜像但 ID 不同，state、workspace 只读挂载，network=none，没有模型代理或真实 key。原生 `pluginExtensions` 内业务状态及真实 receipt 来源一致；预算、工具审计、工作区、原候选材料和状态文件均未变化。它证明状态能被另一个容器读取，不证明运行到一半的任务能接着执行。

### 旧取消失败与 HTTP 修正版边界

`i18-openclaw-cancel-smoke-001` 未通过，原因不能概括成“OpenClaw 不支持取消”。本仓库通过 `/v1/chat/completions` 发起任务，却对该 HTTP run 调用了另一套 RPC 的 `chat.abort`。停止请求在 `07:03:02.260Z` 返回 `ok=true / aborted=true / runIds=[]`，两次 `agent.wait` 均仅返回 `timeout`。此后独立工具审计仍在 `07:03:16.584913Z` 起记录 3 次 write，在 `07:03:22.588169Z` 记录 write_file；最终原生 assistant 在 `07:03:30.209Z` 被观察到自然 `stop`，候选保留 `completed / stop`、退出码 0。控制证据始终为 `cancel_confirmed=false`，验收退出码 1。`cancel-outcome.json` 内空的后续写入数组依赖确认成立才计算，不能用它声称 API 返回后没有写入；这里的时间线直接来自独立工具审计。见[失败判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-001/verdict.json)、[官方 CLI 原返回](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-001/candidate/gateway-control-events.jsonl)及[独立工具审计](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-001/tool-events.jsonl)。

固定官方源码的区别如下：

- HTTP 入口在 `src/gateway/openai-http.ts:531` 生成 `chatcmpl_...` runId，经 `openai-compatible-agent-run.ts:155` 直接进入 `agentCommandFromGatewayIngress`；该路径将 HTTP 的 AbortSignal 传入循环，没有经过 RPC 的 `registerChatAbortController`。
- exact-run `chat.abort` 在 `chat-abort-handler.ts:242` 起查找 RPC source，`chat-abort.ts:445` 要求该 source 已注册。`chat-abort-handler.ts:453` 的 `aborted` 还可由 `commandAborted` 产生，因此 `aborted=true` 且 `runIds=[]` 不证明这个 Agent run 已停。
- `chatcmpl_...` 本身是真实 runId，不是因为名字带 HTTP 前缀就无效。`agent.wait` 可以观察 lifecycle 终态；`agent-wait.ts:72` 在没有取到终态时返回 `timeout`。这轮两次查询均在实际自然结束前返回，既不能当作取消终态，也不能单凭 timeout 证明 run 已登记为正在运行。
- 当前 HTTP 路线已有官方取消信号：`openai-compatible-http.ts:61` 监听客户端断开，`http-common.ts:272` 在未完成连接关闭时 abort，随后同一个 signal 传入实际循环。如保留 HTTP 投递，应验证中断原请求后，相同 runId 的原生中止终态、连接结束及后续无新写入；客户端出现 AbortError 本身不足以验收。
- 如果要用独立 RPC `chat.abort` 控制，公共 `agent` 和 `chat.send` 投递分别在 `agent-run-admission-phase.ts:158`、`chat-send-admission.ts:189` 注册对应控制器。改用它们属于接入变更，须固定新入口并重新验证，不能把源码存在直接算能力通过。

build-002 已实现官方 HTTP 断连路线，普通任务仍向同一 `/v1/chat/completions` 提交一次相同任务，模型、工具、预算和自然 SSE 完成检查保持原契约。`SIGUSR1` 先核对真实输入 hook 与同 run 活动 SSE，查询 `agent.wait(0)` 后再次检查是否已结束，再触发仅属于这个 HTTP 客户端的 AbortController；不杀 Gateway 充当取消，不对未来任务排队。

修正版新增 `http-transport-events.jsonl`，通过真实行号串联 HTTP 请求、响应、SSE runId、断连请求和客户端实际读流结束，保留部分 SSE、真实 AbortError 及摘要。断连可能截断一个 JSON 帧，后续不会收到 `[DONE]`，因此独立验收核对完整帧与原 raw chunk 一致、保留原残段，而不是补造完整流。取消仍必须同时得到同 agent/session/run 的最终原生 `agent_end.success=false`、最后 assistant `stopReason=aborted`，以及公开 `agent.wait` 的 `error/aborted`、明确 endedAt、非 pending/yielded；最终 hook 由独立验收按 assistant 时间戳重算。固定源码 `attempt-finalize.ts:441` 的外部 abort 先写 aborted 状态，`:355` 明确 `success=!aborted&&!promptError`；取消时 error 可不存在，不人为补写。

客户端 AbortError 本身不算 Agent 已停，结果仍须保留真实 `failed/native_agent_error` 和非零退出，并通过可信工具审计确认强终态确认后没有新写入。断连发起至确认间的调用及已派发副作用单独报告；即使原生取消未确认，修正版仍报告断连后实际新写，避免把未计算的空数组误读成无副作用。

`i18-openclaw-cancel-smoke-002` 已在 build-002 中真实执行。独立核对的时间线为 UTC：同一 agent/session/run 在 `07:33:33.095Z` 有真实 `llm_input`；宿主随即发送取消信号，公开 `agent.wait(0)` 在 `07:33:35.640Z` 仅返回 timeout。`07:33:35.642Z` 断开原 HTTP 请求，`35.671Z` 客户端真实读流以 AbortError 结束，保留 16 个 SSE 帧且没有补造 `[DONE]`。原始 hook 第 18 行在 `35.681Z` 记录同 run 的 `agent_end.success=false` 和最后 assistant 的 `stopReason=aborted`；公开 wait 在 `37.091Z` 返回该 run 的 `status=error / stopReason=aborted`、`error=HTTP client disconnected` 和明确 `endedAt=35.780Z`，随后在 `37.095Z` 完成强确认。这些记录均对应 `chatcmpl_56c2daa6-b59d-4fde-a4ea-f2e9913c1cc5`，原始文件摘要与控制引用吻合。

本轮保留原候选 `failed/native_agent_error`、`native_terminal.success=false` 和退出码 1，未改写成正常完成。独立账本是 0 次业务请求 / 0 笔写入；确认区间实际已评估，断连后、强确认后均无新写，且没有确认时在途调用。它没有覆盖“已派发业务写入时取消”的情况。原始验收为 9/9，见[取消判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-002/verdict.json)、[控制与副作用汇总](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-002/cancel-outcome.json)、[HTTP 断连记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-002/candidate/http-transport-events.jsonl)及[公开 CLI 查询](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-002/candidate/gateway-control-events.jsonl)。旧取消 001 的失败与原始材料仍保留。

但新取消的[容器 manifest](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-002/manifest.json)同时记录 `OOMKilled=true`。原评分的 `candidate_process_exit` 只核对预期退出码，未排除 OOM，因此 9/9 不能等同于容器稳定通过。[候选资源采样](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-002/resource-samples.jsonl)中，以 `07:33:34.519Z` 记时的采样达到 1.975 GiB / 2 GiB（98.75%），这是容器用量，不是 Gateway RSS；采样时间记在 Docker 命令执行前，不能据此精确定位 OOM 或退出时点。另一方面，[Gateway 日志](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/cancel/i18-openclaw-cancel-smoke-002/candidate/gateway.log)在强确认后的 `37.104Z` 才记录清理 SIGTERM，`37.505Z` 记录正常清理完成，容器 `37.717Z` 退出；不能把中止证明归因为清理杀进程，也不能据此抹掉 OOM 标记。当前材料没有 OOM 事件时间与被杀进程，资源稳定性及其对本轮完整验收的影响仍需单独复核，不覆盖原 verdict。

I18 仍进行中。五类基本任务 × 101/202/303 的 `i18-openclaw-formal-batch-001` 已使用固定 build-002 候选及上述 bench 完成，7/15完整通过；取消控制链已有一次真实中止证据，OOM 问题尚待复核。当前仍没有运行中暂停续跑、多租户隔离或实例成本结论。Hogee 当前基于 OpenClaw 的云端助手产品及“流程黑箱、自定义困难”是用户提供的业务事实，相关仓库仅作源码参考；本地单轮结果不能推定现网运行、休眠、共享或租户策略。

## 用什么、怎样运行

固定官方提交 `b6e0872bdb7228e22fac7608345e0529a35ec46e`，源码版本 2026.10.5、MIT，57891 个导出文件。使用 Node 24.22.0、pnpm 12.11.1、官方 frozen workspace lock，在 Linux arm64 镜像内完整执行 `build:docker`。为适应当前机器，给该 recipe 唯一 tsdown 调用增加官方 `--concurrency 1`；未修改 Agent 循环或用假插件资产替代构建。源码清单、recipe、构建环境、安装清单和系统包清单均保留。

I09 初筛镜像为 `sha256:7894cec05f04d385bca154cf9b696a9c621509281fce736df22dee565e4580e1`，用于该阶段 budget-004 及两轮工具任务。每个候选容器 2 GiB / 1 CPU、只读根目录，state、workspace、candidate 输出分别挂载。薄入口启动官方 Gateway，经同容器 loopback HTTP 请求路由到 `lab-a`；实际模型由唯一 custom provider 指向本地计数代理，真实 key 仅代理持有。

统一工具通过官方插件 factory 注册，使用 `harness_` 前缀解决与内建工具重名的问题，保留原参数、说明和粒度。factory 核对 agentId/sessionKey 后才绑定工具凭证；只转发模型选择的调用，不代办业务步骤。普通业务写入无需审批。插件运行时检查确认 8 个工具、5 个 typed hooks 已加载；原始 hooks、Gateway SSE、原生终态和 SQLite 状态均保留。

## I09：初筛实测结果

| run_id | 种子 | 模型请求 | 写入 / 请求 | 原生入口耗时 | 外部判定 |
| --- | --- | --- | --- | --- | --- |
| i09-openclaw-tools-001 | 101 | 10 | 3 / 3 | 57.796 秒 | 12/12，通过 |
| i09-openclaw-tools-002 | 101 | 9 | 3 / 3 | 55.468 秒 | 12/12，通过 |

候选分别自行读取任务文件、分页查询、筛选计算、提交与核对业务写入、生成报告。独立验收核对种子重算结果、分页上限、完整工具审计、业务账本、作用域和模型请求；两轮候选均无 OOM、退出码 0。原生 `agent_end.success=true`，最后 assistant `stopReason=stop`，Gateway HTTP 也正常结束，没有仅凭自然语言完成声明判成功。

两个完整容器 ID：

```text
001 32c71438980c97722abab731ac5be45193db50bd7eeab03cacf0962d9efe1a75
002 a50bc6f8ffff83ceec7204cd2f2217b9d6f3f3634d231fd0524d0701787e93d9
```

第一容器删除后，第二个使用干净状态、工作区、预算和账本重新执行。**这是新任务复现，不是继续旧任务。** 新会话与继续会话对照、运行中暂停和换容器恢复留到详细阶段，不能由 SQLite 文件存在推定。

Gateway HTTP 返回的原生 usage 汇总为：001 input 65001、output 2584、total 67585、cached 48640；002 input 52302、output 2836、total 55138、cached 39936。缓存已含在输入内，不再加到 total。公共代理记录全部 10/9 次实际请求；没有可靠单价，金额未知。不同框架的系统提示、调用行为及事件汇总口径尚需详细阶段核对，不把这两轮输入数字直接算成生产费用排名。

Gateway 日志在首轮出现 RSS 约 1.66 GiB 的阈值警告，值得在 I25 查清。当前 `resource-metrics.json` 的 phase 汇总包含候选、工具、模型代理及业务服务，不能将其内存峰值全算给 Gateway，也不能用这次完整开发依赖镜像代表已优化的生产部署。

## I09：限额及失败归因

`i09-openclaw-budget-004` 使用 I09 初筛镜像、独立内网、fake key 和固定 503 模拟上游。第一候选实际重试，两次请求到达上游后被 cap=2 拦住；删除候选、更换 proxy 后，第二候选仍命中相同累计限额。18 项全部通过，上游累计仅两次；两轮均保留原生 429 错误并非零退出。该探针无收费模型请求，不代表恢复能力。前一镜像的 budget-003 同样通过；关闭无关目录刷新后，重新用 I09 初筛镜像跑 004，没有覆盖旧证据。

失败材料也保留：

- 首次构建依赖成功，bundle 因内存不足 SIGKILL。使用官方串行参数和受控 heap 后成功，没有修改 Colima 或影响其他项目。
- budget-001：接入生成了旧 default marker，当前配置加载规则拒绝；改为显式 ownership 和系统 Agent 归属。
- budget-002：SQLite staging 默认落到只读 `/root/.cache`，Gateway 就绪前失败。入口为每个容器创建 0700 tmpfs cache 并设置 `XDG_CACHE_HOME`，不放宽目录安全检查。
- budget-003 暴露模型目录后台刷新；通过官方 `models.catalogRefresh.enabled=false` 关闭后重建。未改变选定模型或协议。

上述启动失败均未到达模型上游，不能算成模型业务能力失败。其他 bundled plugin 的缺入口警告不包含本轮所需 openai 或实验插件，不能凭这些警告断言 provider 不可用；后续真实请求已验证本轮所需路径。

## 实例与自研边界

官方源码支持一个 Gateway 内多个 Agent，每个 Agent 可有独立 workspace/agentDir，同一 Agent 还可有多 session。Agent、session、进程和容器不是同一层概念。本次只测一个 Gateway/Agent/任务，不能据此认定 OpenClaw 必须每用户一个容器，也没有证明共享 Gateway 已满足租户隔离。

HTTP Bearer 属于整个实例 owner/operator 权限，当前仅由可信客户端持有，不是每用户授权令牌。未来仍需产品后端鉴权、可信路由和任务控制授权，具体边界在 I24 验收。Hogee 仅为只读源码参考；本报告不推定其现网常驻、休眠或共享策略。

Gateway、Agent 循环、会话库与 hooks 来自官方。HTTP 工具插件、后端 scope 绑定、严格终态核对、Docker 编排、模型计数与独立验收来自本仓库，需计入接入和维护投入。实际接入经历配置代际、构建内存和只读文件系统适配；未记录可靠人工工时，不由自动构建时长倒推。

证据根目录：`/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/`。`tools/` 保留各 run 的 manifest、账本、计数、原始事件、判定及采样，`runtime/` 保留成功和失败构建日志。复现使用 `scripts/lab.py build/run --candidate openclaw` 与 `scripts/probe_candidate_budget.py --candidate openclaw`，每次给新 run ID。

## 正式批次结果与最终镜像冷读

[正式批次](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/runtime/i18-openclaw-formal-batch-001/batch.json)包含五类各三组，完整通过数为tools 2/3、browser 0/3、sensitive 3/3、extension 1/3、extension-denied 1/3，合计7/15。模型请求依次为tools 10/8/11，browser 20/20/20，sensitive 3/5/4，extension 10/14/7，denied 3/4/3；总142次，全部forwarded/200，实际19笔业务写入。15个不同Worker均使用build-002固定镜像，Docker快照均OOMKilled=false。逐轮链接见[同题矩阵](detailed.md)。

分页失败三轮（tools/202、extension/101和303）仍完成正确报告与写入，但未按约定读全分页。浏览器三轮都耗尽调用预算：101/202未提交，303完成真实提交和下载，随后未正常结束。数量拒绝202/303无业务写入，但未产生执行层数量规则触发证据，只能记本轮未验出该规则，不能归因为发生违规写入。金额因未知单价保持未知，失败142次总数中的支出不能丢弃。

本批资源535条有效样本、7次采样错误，包含候选与配套服务，不是多用户成本曲线。build-002的[冷读002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openclaw/extension/i18-openclaw-extension-seed-202-001/native-state/i18-openclaw-native-state-002/verdict.json)来自正式extension/202，22/22通过：删除旧Worker后，同镜像新ID只读加载state/workspace，network none，完整原生业务态与回执相符，无新增模型/业务调用；新容器OOMKilled=false、退出0、仅本次资源清理成功。仍不代表I22暂停续跑。

## 2026-10-20：独立进程验收补验

详细编排现于执行前固定候选镜像ID、名称和配额，结束后从可信宿主Docker快照保存独立观测，验收容器严格检查唯一Worker归属、退出码、OOM、异常状态及配额；完整容器ID是结束时唯一名称归属，不能称启动前独立ID固定。`i18-process-scope-check-001` 在新bench `sha256:ec10ed62c62a2f3355ec34c656649b8d374b5edb77d156e264b87b4ec936127f` 内614项通过（28.60秒），未注入模型凭证。作用域及资源的合成校准不代表I24/I25实测。

[追加审计002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/bootstrap/i18-process-audit-002/process-audit.json)仅离线读取41份明确旧manifest：37份快照健康通过，OpenClaw三轮browser因异常退出失败，cancel002因OOM失败；旧记录没有额外独立身份pin，完整进程结论保持unknown或failed。旧verdict未覆盖并留存原字节哈希。001因旧DSH索引格式不同在准备清单时失败，未启动Docker或模型。I18完成调研步骤，保留正式7/15及取消OOM异常，不能表述为全能力通过。
