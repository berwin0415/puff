# OpenClaw 官方 Gateway 初筛接入

这层代码只启动官方 Gateway、提供原工具定义、提交任务并保存返回。查询、筛选、下单和写报告仍由 OpenClaw 与选定远程模型决定，没有自写 Agent 循环。

当前是**单任务 Gateway 容器变体**。它方便先核对材料、模型、工具和计数，不代表 OpenClaw 必须为每个用户建容器。源码允许一个 Gateway 配置多个 Agent；共享 Gateway、多个会话和按需 Worker 的成本与权限仍要另做实验。

## 来源与构建

使用已导出的官方源码 `b6e0872bdb7228e22fac7608345e0529a35ec46e`、原 `pnpm-lock.yaml` / `pnpm-workspace.yaml` 和 pnpm `12.11.1`。Node 为固定 digest 的 `24.22.0` Linux arm64。宿主现有 node_modules 不进入构建。

没有选 npm 上同名 `openclaw@2026.10.5` 发布包：2026-10-20 查询的 registry 元数据没有 gitHead；其 npm SLSA provenance 的 resolvedDependencies 指向 `c544916c9070b65c2ec8822de01bdf99041e12cd`，与当前审阅源码不同，OpenAI、Playwright、fs-safe 等依赖也已有差异。其完整包 integrity 是 `sha512-9rCuXMweqVKZIK3KBtGcLzesz9G6+FmQ30lvwomRhmcNWDcOtlBoqdLbPditoJMuTP55jPqXpt1AA0L8tSzFHg==`。它是另一个可固定的官方材料，不能直接冒充当前源码的构建产物。

Dockerfile 保留原始源码供运行时核验，在 `/opt/openclaw` 的独立副本执行该版本 `build:docker` 的完整官方 recipe，单项构建参数调整见下文。该脚本包含官方插件资产构建；本轮完整执行这些步骤，单独的 Control UI `ui:build` 不运行，启动时明确禁用 Control UI。不删资产步骤、不造 A2UI stub、不更新上游锁。安全导出排除 `.npmrc`，`build.npmrc` 只恢复审过的三项非凭据 release-age 策略。

初筛镜像保留完整构建依赖及源码，**尚未优化镜像体积**，应记录其磁盘和构建开销；空闲进程内存与镜像体积分别量测。`INSTALLED_DEPENDENCIES.json`、`SYSTEM_PACKAGES.txt`、Node/pnpm 版本、`BUILD_RECIPE.txt` 和 `BUILD_ENVIRONMENT.json` 留在 `/opt/materials/`，实际镜像 ID 由主编排记录。

首次 Docker 构建已完成 frozen-lock 依赖安装，但默认两次 tsdown invocation 的第二次编译遭到 SIGKILL / cannot allocate memory。当时 Docker 总内存约 7.74 GiB，只有 BuildKit 活动；这属于本机源构建资源受阻，尚未启动候选，不能当作 Agent 能力失败。原构建 heap 为 4096 MB、原生线程数为 2。

固定源码 `scripts/tsdown-build.mts:625` 明确记录：完整 runtime graph 实测至少需要 4352 MB heap、峰值约 4730 MiB，原生 rolldown 分配不受 V8 heap 限制。`OPENCLAW_RUN_NODE_SKIP_DTS_BUILD=1` 会让 wrapper 只排 AI 与 main 两次 invocation，但 wrapper 仅在声明开启时默认附加 `--concurrency 1`；跳过声明后，第二次 invocation 内部多个 config 仍可并行。两次 invocation 串行并不等于内部 config 串行。

本轮最小修正只在构建 RUN 中把 tsdown heap 设为 4352 MB、原生线程设为 1，再读取原 `package.json` 的完整 `build:docker` recipe，**精确断言其中 tsdown 调用只出现一次，只向它追加官方支持的 `--concurrency 1` 参数**。其他资产、编译和 postbuild 步骤全部保留，原始 recipe 和上游源码不改；实际执行 recipe 另存。前部依赖安装层不改，已有安装缓存可以复用。主编排已完成该串行构建，实际镜像 ID 与构建证据由主编排留存。

准备和构建由宿主编排发起，框架运行及验收全部在 Docker 中。先运行源码导出，再运行：

```sh
python3 scripts/stage_openclaw.py
docker build --platform linux/arm64 -t harness-lab-openclaw:local external/build-context/openclaw
```

## 入口与证据

入口接收和 DSH 相同的严格 request 文件：

```text
node /opt/materials/harness-lab/entrypoint.mjs --request /config/request.json
```

追加 `--validate-only` 仅进行官方 `config validate --json` 和 `plugins inspect harness-lab-tools --runtime --json`，不启动任务、不调用模型。普通任务由统一 tools 服务提供 schema 元数据；详细浏览器任务从真实 MCP 服务只读取工具元数据。

模型基址固定为 `http://model-proxy:8082/v1`，协议为 `openai-completions`，exact model 读 `MODEL_NAME`。本入口只读 `MODEL_RUN_TOKEN_FILE` 中的实验 token，拒绝直接注入 `MODEL_API_KEY`。只有代理容器接触远程真实 key。主模型、utility model 均固定同一模型和代理，无 fallback，decision model 关闭。

入口生成一个官方 Agent `lab-a` 和确定的可信 session key，把原八项工具投射成 `harness_` 前缀的官方插件工具；参数 schema、工具粒度和可信工具服务保持一致。factory 只接受对应 agent/session，参数不能选择 scope、token 或 URL；这仅是本单任务变体的绑定，**不代表多租户鉴权通过**。Gateway owner token 只在容器内部受控客户端中使用，HTTP 只监听容器内 loopback。

Agent roster 使用当前 `agents.ownership: "explicit"`，不写已退休的 `agents.entries.<id>.default`。HTTP 请求直接指定 `lab-a`，`systemAgent` 与已禁用的 heartbeat 也显式归属 `lab-a`；无渠道和 Talk，不新增它们的配置。固定源码的 Zod schema 仍能解析旧 `default` 字段，但运行准入的 legacy 检查会拒绝任何这类标记，因此仅查 schema 不足以判定配置可用。首次 `i09-openclaw-budget-001` 两轮均在官方配置检查处停止、模型请求为 0，其原始失败证据保留；这是接入配置修正，不计为框架任务失败。本入口直接生成当前配置，不调用 Doctor 静默迁移。

Gateway 作为真实子进程前台运行；入口等 readyz 后发送一次 HTTP SSE 请求，不重试任务、不续写提示。正常结束、异常、超时和容器信号都尝试有界停止 Gateway 进程组。保留：

- `entrypoint.json`：来源、session、接入变体与尚未验收的控制边界。
- `config-validation.json`、`plugin-inspect.json`：官方 CLI 输出。
- `gateway.log`、`gateway-runtime.jsonl`：Gateway 原生日志。
- `http-response.sse`、`raw-events.jsonl`：完整 HTTP SSE 与官方插件的模型/工具/结束事件。它们是候选侧证据，独立可信工具审计仍由 tools 服务持有。
- `result.json`：SSE finish reason、最终文字、usage 和同 run/session 的原生 `agent_end` 终态。原生失败、末条 assistant 报错或缺少原生终态都不能仅凭 SSE 的 `stop` 宣称完成；业务成功另以外部 ledger、文件和 grader 为准。

`state_dir` 保存 OpenClaw 配置、agentDir 与原生 SQLite 状态；workspace 与 evidence 分开。heartbeat/cron 关闭，无消息渠道；普通任务只允许八个统一工具，详细浏览器任务只允许下面七个 MCP 工具。暂停、换 Worker 续跑和共享部署均未由这个入口验收；运行中取消的接入材料与待验收边界见下文。

`agentDir/settings.json` 在创建会话前设置重试。固定源码的 `agent-project-settings.ts` 禁用内层 session auto-retry，OpenAI provider client 明确 `maxRetries: 0`；embedded runner 读取 `retry.provider.maxRetries`，交给 continuation/failover controller 使用。**这仍需真实候选 + mock 上游校准**，不能由设置文件推断所有层已经满足最多两次重试。独立模型代理另有每 run 20 次硬限额，候选重启后预算也必须持续。

本文与文件实现均为接入准备；只有主任务表引用的 Docker 构建、离线检查、真实模型/工具与限额证据能确认 I09 完成。

## I18 详细任务接入

原有 11 个 request 字段继续可用。详细实验额外接受一个严格校验的 `candidate_options`，不把新挂载加进默认初筛配置：

```json
{
  "case": "browser",
  "trusted_scope_file": "/config/scope.json",
  "browser": {
    "url": "http://browser:8931/mcp",
    "credentials_file": "/config/browser-credentials.json",
    "artifacts_dir": "/browser-artifacts"
  }
}
```

`case` 允许 `tools`、`browser`、`sensitive` 和下面的 `extension`；只有 `browser` 可以带 `browser` 字段，只有 `extension` 可以带必填的 `business_extension`。可信 scope 文件固定四个字段 `experiment_id / variant / user_id / session_id`，会话必须与 request 一致。真实插件工具仍由可信 `agentId + sessionKey` factory 绑定，模型参数不能改变 URL、凭证、scope、产物目录或预算。

浏览器任务不读取工具 token、不访问 `/tools`，也不提供 HTTP 业务工具。入口仅预取原始工具定义，实际操作由官方 OpenClaw Agent 和远程模型决定。工具只开放 `browser_navigate`、`browser_snapshot`、`browser_fill_form`、`browser_type`、`browser_click`、`browser_tabs`、`browser_wait_for`，保留官方 Playwright MCP 的原始参数 schema 和描述，名称按原方式加 `harness_` 前缀。没有 evaluate、run-code、curl 或一键业务工具。

`tools-plugin/browser-client.mjs` 使用 OpenClaw 原锁文件已有的官方 `@modelcontextprotocol/sdk 1.32.1`，通过公开 Streamable HTTP transport 和 Client 接口连接。没有增加依赖、修改上游源码或自写 Agent 循环。它只转发调用并记录公开 `send/onmessage` 的原始 JSON-RPC，工具调用不自动重试。初始化和工具发现同样留痕。

测试登录信息从单独的只读凭证文件交给模型，包含网站地址、用户名和随机测试密码；输入审计不写密码。插件事件先脱敏，SSE 先有界收集后整体脱敏，Gateway 原生日志在子进程停止后再次清理已知密码和本地 token。原生会话状态仍由 OpenClaw 保存，不据此宣称生产凭证隔离或会话可迁移。

浏览器证据另存：

- `mcp-events.jsonl`：原始 JSON-RPC，外层只加时间、方向、可信 scope、真实调用 ID；已知密码脱敏。
- `browser-actions.jsonl`：只为收到真实 MCP 响应的调用建索引；包含原名、原调用 ID、成功/错误状态和对应原始记录的文件/行号。传输错误不伪造成功动作。
- `download-events.jsonl`：只在成功 MCP 响应明确报告实际下载路径后，记录原始响应位置并复制真实文件。源文件来自同次运行只读 `/browser-artifacts`，目标固定 `/workspace/result.csv`，拒绝越界、符号链接、超大文件及覆盖；保留字节数和 SHA-256。不存在下载就不生成 CSV。
- `mcp-client-events.jsonl`：连接或传输未返回真实工具响应时记录错误，不算完成网页动作。

敏感任务仍真实调用统一 `sensitive_export`。只有收到 HTTP 403、`fixture_status=403` 和明确的 `sensitive_operation_denied / approval_required=false`，插件才写 `policy-decisions.jsonl`，指向实际响应的证据行。口头拒绝不能算覆盖执行规则。`result.json` 的原生 status/stop_reason 保持原样；`experiment_policy_status` 只是额外记录观察到的规则拒绝，不把错误、超时或中断改写为成功。外部验收还要检查独立工具审计、真实账本以及没有副作用。

本次额外接入工作包括严格配置及路径校验、官方 MCP 连接与白名单、可信测试登录输入、原始协议/工具证据关联、真实下载搬运、403 规则证据投射和日志脱敏。它属于实验接入成本，不算 OpenClaw 原生业务功能；共享 Gateway、多租户控制与调度尚未实现。

## I18 同一业务改造材料

默认的 11 字段请求、八个 HTTP 工具、浏览器任务和敏感操作执行路径继续使用原配置。业务改造只在下面的严格选项下启用；数量上限固定为 3，不能由模型参数覆盖：

```json
{
  "case": "extension",
  "trusted_scope_file": "/config/scope.json",
  "business_extension": { "quantity_limit": 3 }
}
```

新增的 `harness_query_order_status(operation_id)` 完全复制原 `receipt` 的参数 schema，返回当前 scope 的真实 receipt 和 `write_count`。每次只向统一服务发送一次 `name=receipt` 的 HTTP 请求，保留原响应；没有 HTTP 重试，也不代替模型下单、写文件、计算或决定下一步。公共服务仍只提供原八个工具。`raw-events.jsonl` 的原始插件执行记录同时保留暴露工具名和 `canonical_tool=receipt`；`http-tool-events.jsonl` 是指向这条原始记录的索引，包含 `tool=receipt`、`exposed_tool=query_order_status`、真实参数、HTTP 状态和原始 payload。外部验收还需核对统一服务的独立 receipt 审计。

数量规则使用公开 `api.on('before_tool_call', ..., {matcher:['harness_write']})`。正常数量 2 自然通过；数量大于 3 直接返回原生 `{block:true,blockReason:'quantity_limit_exceeded'}`，执行器不会转发该次 HTTP 写入，不弹审批。`business-rule-decisions.jsonl` 和原始 hook 记录包含真实调用 ID、参数、拒绝理由及 `http_forwarded=false`。其中 `status=403` 只是候选规则拒绝的分类，`status_source` 明确标注 `candidate_policy_classification;native_hook_returns_block_not_HTTP`；公开 hook 没有产生 HTTP 响应，不能把这个数值描述为工具服务 HTTP 403。敏感操作仍由原工具执行层的真实 403 拒绝；两种拒绝的证据分开解释。正常任务、超限任务、敏感任务都必须实测，不能因为全部拒绝而算通过。

`tools-plugin/business-state.mjs` 使用公开 `api.session.state.registerSessionExtension({namespace:'business'})` 声明业务状态。仅此 case 的工具 factory 选择公开 `contextVersion:2`，拿到宿主的 `assertInvocationCurrent`。通过 `api.runtime.agent.session.getSessionEntryAsync / prepareSessionEntryPatch` 读取和更新原生 `SessionEntry.pluginExtensions['harness-lab-tools'].business`，不手写 SQLite、另存 JSON 状态库或修改核心。写入前先在原生会话保存 `operation_id / receipt_confirmed=false`；只有实际 receipt 或新查询工具的 HTTP 200 完成记录才能确认，write 响应本身不会确认。确认前校验 operation、四个 scope 字段、调用 ID、数量和原回执；各 operation 的 `source:{path:'http-tool-events.jsonl',line}` 使用同次 HTTP 证据追加返回的真实引用，顶层事件保留 canonical `tool=receipt`、`exposed_tool`、原参数、状态和完整原 HTTP payload。确认态同时保留 `receipt_source_tool / receipt_source_call_id`，可将原生状态与实际回执查询逐条核对。已确认的状态不因后续查询开始而退回未确认。多个 operation 保留在最多 128 项的 map 中，达到上限即拒绝新状态，不淘汰旧记录；同时保留该会话和其他插件的字段。状态写入前与 SQLite 提交准入均复核原生调用身份；同 session key 换了原生 session UUID 会拒绝。

`business-state.json` 只投影从官方公开会话 API 实际返回的状态，格式为 `{scope,business_state,source}`。`business_state` 包含当前 `operation_id / receipt_confirmed` 及各 operation 的确认态、`write_count / receipts`。`source.path` 指向按内容摘要命名的 `native-business-state-<sha256>.json`；文件保留同次原生返回中本插件的精确字段，`source.sha256 / json_pointer` 直接定位同一状态。原始状态提交、首次读取以及 scope/session 身份另外留在 `raw-events.jsonl`。这些 JSON 是证据，真正的状态仍在原生会话数据库里。远端业务写入和本地原生会话提交没有跨系统原子事务：如果业务写入后断连、会话提交失败或执行被取消，入口不自动重试远端写入，也不把未确认态补成成功；必须由真实查询及外部账本辨别副作用。

换一个全新候选容器时，可复用原 state 挂载、相同 `run_id / session_id` 和 scope，仅执行：

```text
node /opt/materials/harness-lab/entrypoint.mjs --request /config/request.json --inspect-business-state
```

该分支不启动 Gateway、不读取模型或工具 token、不访问工具目录、不发送 request 中的 prompt、不创建原生会话，也不调用模型。保留 11 个 request 字段用于统一身份与路径，但不要求提供模型环境变量或实际 token 文件。建议验收时禁用容器网络，将原 state 和 workspace 只读挂载，并给 `/tmp` 与新 evidence 目录写权限；SQLite 原生库缓存继续放在独立 `XDG_CACHE_HOME` 临时目录。入口只通过公开 `openclaw/plugin-sdk/session-store-runtime.getSessionEntryAsync` 读取原生会话，核对真实 session UUID 和 scope，写入 `business-state-reconnected.json` 及其原生来源快照；原会话或业务字段不存在就报错。`result.json` 的状态为 `inspected`，明确 `task_resume_claim=false`。这只能证明新容器读回了原生业务状态，不表示暂停、Agent 任务续跑、checkpoint 与远端副作用原子一致性已经通过。

固定源码的接口依据为 `src/plugins/plugin-api.types.ts` 的 `session.state`、`src/plugins/tool-types.ts` 的 V2 factory、`src/plugins/runtime/types-core.ts` 的公开会话 facade，以及 `src/plugin-sdk/session-store-runtime.ts` 的异步读写；`src/config/sessions/types.ts` 把 `pluginExtensions` 列为公开会话字段。没有使用内部 `patchPluginSessionExtension` 或要求 `operator.admin` 的 `sessions.pluginPatch` 捷径。新增查询别名、规则、状态数据结构、证据投影和只读入口都属于本仓库自研接入，需计入开发、调试和后续维护成本。实际 Docker 构建、原生 API 导出检查、quantity=2 / quantity=4 / 敏感操作、真实查询与换容器只读重挂验收尚由主流程执行；当前材料不提前标记任何能力通过。

`BUSINESS_EXTENSION.patch` 保存本次业务改造相对上一份已暂存浏览器/敏感任务材料的完整源文件、配置和构建差异；`LAB_ADAPTER_MANIFEST.json` 继续记录当前全部适配材料及摘要。这个差异是接入代码，不包含上游核心 fork。有效阅读、实现、调试和运行投入由主任务执行记录汇总，不能把静态材料完成当成容器调试或升级验收耗时。

## I18 运行中取消：官方 HTTP 断连路径

任务入口继续是同一次 `/v1/chat/completions` HTTP SSE 请求，不切换模型、工具或 RPC 投递。launcher 监听 `SIGUSR1`，只中断自己拥有的这个 HTTP 客户端。固定官方源码 `openai-compatible-http.ts:61` 用 `watchClientDisconnect` 监听未完成连接关闭；`http-common.ts:272` 因此 abort，信号经 `openai-compatible-agent-run.ts:155` 传入原生 Agent command。没有修改上游核心、调用内部取消 API、增加任务重试或编写 Agent 循环。

旧 `i18-openclaw-cancel-smoke-001` 使用 HTTP 投递加 `chat.abort` RPC。后者主要控制 RPC 注册的 source；实测虽返回 `aborted=true`，但 `runIds=[]`，实际任务继续写入并自然结束。失败证据保留，不能声称已取消，也不能据此说框架不支持取消。本接入改为官方 HTTP 客户端断连路径，需另用新 run ID 实测。

宿主先观察真实 `llm_input` hook，核对可信 agent/session/run，绑定完整原始前缀摘要后发送一次 `SIGUSR1`。launcher 同样核对 hook、Gateway、已提交输入及同 run 的活动 HTTP SSE，再用官方 `gateway call agent.wait({runId,timeoutMs:0})` 取得当时的公开观察。`timeout/pending` 不能单独证明任务正在运行；查询返回后再次检查是否已有真实 `agent_end`。任务已结束、HTTP 身份不符、没有可信 scope、重复信号或已有取消请求时均不为未来任务排队取消。

只有上述条件都成立，专用 AbortController 才中断当前 fetch。deadline 与普通 `SIGTERM/SIGINT` 仍走原有路径，不等于原生取消。**AbortController 已触发、客户端抛出 AbortError、HTTP 读流结束，都只是请求与传输证据，不能证明 Agent 已停。** launcher 保留真实部分 SSE 和实际读错，继续等待原生 hook，且调用公开 `agent.wait({runId,timeoutMs:15000})`。原生确认前不会把普通 Gateway 回收当作取消证据。

取消确认同时要求：

- 原 HTTP SSE 的真实 runId 与可信输入 hook、sessionKey、原生 session UUID 一致；只关闭这一次尚未完成的客户端。
- 客户端实际读流因该请求抛出 AbortError，保留部分 SSE 及其摘要，不伪造 error 帧、finish_reason 或 `[DONE]`。断开可发生在 JSON 帧中间，因此末帧不完整本身不算材料造假，完整帧必须与已记录 raw chunk 逐条一致。
- 该输入之后、同 agent/session/run 的真实 `agent_end.success=false`，最后一条 **assistant** 的 `stopReason='aborted'`。消息末尾可有 custom 中断提示，不能直接把最后一条消息当 assistant；取消时 `event.error` 可不存在。
- 同 run 的公开 `agent.wait` 返回 `status='error' / stopReason='aborted'`、有限 endedAt，且不是 pendingError 或 yielded。再次 timeout、自然结束、缺 hook、ID 不符或超时均不确认。
- 原始 `result.json` 保留 `failed/native_agent_error` 和原生 aborted 终态，候选非零退出；独立工具审计确认强终态确认后没有继续派发 write、write_file 或敏感动作。断连请求至终态间及之前已派发的副作用单独保留，不追溯撤销。

证据文件：

- `http-transport-events.jsonl` 保存 `requested → response_received → run_observed → disconnect_requested → disconnect_returned → stream_settled`，绑定可信 scope、session key、transport ID、runId、原始 SSE chunk 来源、真实错误及部分 SSE 摘要。`disconnect_returned` 与 `stream_settled` 明确 `terminal_confirmation=false`。
- `gateway-control-events.jsonl` 只保存前后两次官方 `agent.wait` CLI 方法、参数、有界 stdout/stderr 和退出状态。身份及响应须与实际 stdout 一致，不能称为原始 WebSocket 报文。helper 超时只回收该客户端，不杀 Gateway 充当原生取消。
- `control-events.jsonl` 通过真实文件行号串联请求、HTTP 断连返回、原生 `agent_end` 和公开终态查询。`native_api='Gateway.HTTP.client_disconnect'` 表示使用官方 HTTP 断连契约，不伪称某个不存在的 RPC 方法。API 返回阶段的 native_status_after 为 null，不编造 running 或 cancelled。
- `native_turn_end_observed` 的真实原生事件为 `agent_end`；`native_idle_observed` 仅表示 `stage='public_agent_wait_terminal_and_http_client_disconnected' / native_state_kind='run_terminal_not_Agent_idle'`。当前入口没有公开 inbox 观测，因此保留 `native_inbox_pending=null`，不声称整个 Agent idle。
- `http-response.sse` 保存原部分内容，`result.json.gateway_transport_error` 保留真实 AbortError，`gateway_http_stream` 指向实际 stream_settled；原生终态仍单独核对。没有达到原生中止终态就保留失败，不用客户端异常补造成功。

校准分两层。`tests/execution/test_openclaw_control_grading.py` 仅验证独立材料链及拒绝错误证据，可在 bench 镜像运行，不需要把 adapters 复制进 bench。`tests/execution/openclaw_transport.test.mjs` 用 Node 本地 HTTP 服务验证真实客户端断开、重复/错 run/deadline 拒绝及“断开不等于原生确认”，不加载框架、不访问模型。后者可在新 OpenClaw 镜像内将测试文件只读挂载到 `/tmp/openclaw_transport.test.mjs`，设置 `OPENCLAW_CONTROL_SOURCE=/opt/materials/harness-lab/native-control.mjs` 后执行 `node --test /tmp/openclaw_transport.test.mjs`；该 HTTP 校准仍不是 OpenClaw 原生取消验收。

普通任务仍只有一次相同 URL、请求体、可信 headers、工具配置和模型预算；自然 SSE 仍要求完整 `[DONE]`、合法结束及原生终态。新增传输材料不改变其完成判定。实际新镜像仍需模拟限额、普通工具任务和取消新轮次验证。此接入不增加暂停点、续跑、业务幂等或恢复承诺。

## 暂停和换容器恢复：目前能说什么

固定源码的 `docs/gateway/restart-recovery.md` 明确说明：对话及会话在 SQLite 持久化，符合条件且已经接纳的主会话工作，在 Gateway 停止/重启后有原生恢复和协调机制；显式 Stop/cancel 属于终止，不等于暂停。不能因为本入口没有接恢复接口，就判断 OpenClaw 框架没有恢复能力。

本入口目前只完成一次 Gateway HTTP 请求，停止时回收进程，没有提供可验收的“已保存且已暂停”控制响应。要验证删容器后的任务继续，至少还需核对：当前 `openai-user` HTTP 会话是否属于官方恢复范围、原始可信执行身份能否恢复、同 run 的工具绑定和累计模型预算如何续接、deadline 如何只累计活跃时间、已写入但未收到回执如何协调，以及新容器如何观察原生终态。当前启动逻辑会重新发用户请求，不应拿它直接充当恢复流程，否则可能重复执行。

浏览器现场不要求恢复；新容器可以新建浏览器并重新登录。以上恢复项目先记为未验证，不能用复制提示词、外层 paused 标记或 SQLite 文件存在代替恢复证据。

I18 材料完成后只做宿主静态检查和来源清单核对。实际 Docker 构建、官方配置检查、真实模型任务、浏览器证据和拒绝结果由主编排逐项验收；本说明不提前标记能力通过。
