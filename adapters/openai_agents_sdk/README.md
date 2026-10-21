# OpenAI Agents SDK 容器接入

这一层把可信任务、统一工具和现有远程模型交给官方 Python `Agent` / `Runner.run_streamed`。工具怎么选、先后怎么做、是否再调用模型，都由 SDK 与选定模型决定。入口没有预写查询、下单、报告或错误恢复流程。

## 材料从哪里来

使用 I03 已固定的官方 `openai/openai-agents-python` 源码：commit `64cd5dfc5a1579870dcbed0ea42b27aa84033fee`，归档 SHA-256 `8bfb125e5d9343e2740a502aec764afaa63cf55dc17d5df068b7d3680a213321`，源码版本 `0.24.0`、MIT。只把该归档的安全导出放进镜像，不使用宿主环境或同名 registry SDK 包替代它。

已按 OpenAI Docs 技能搜索并实际读取[官方 Models and providers](https://developers.openai.com/api/docs/guides/agents/models)：第三方模型走对应语言 SDK 的 provider/adapter 接口。本文的具体调用以这次锁定的源码为准：`src/agents/models/openai_chatcompletions.py` 的公开 `OpenAIChatCompletionsModel(model, openai_client)`，以及 `src/agents/run.py` 的 `Runner.run_streamed`。

Python 为固定 digest 的 `3.12.13` Debian Bookworm Linux arm64。独立 `uv.lock` 限定该 Python 小版本和 Linux arm64，固定完整 44 项安装图，包括本虚拟项目与 SDK 源码项；`openai==3.29.0`、`httpx2==2.12.0` 与所审源码的 client 代际一致，构建后端为 `hatchling==1.28.0`。宿主仅使用 `uv lock --no-managed-python --python 3.12 --no-build` 读取依赖元数据、准备锁文件，没有安装或导入候选。上游包含多种可选云端 sandbox、语音和 tracing 集成，本入口没有启用这些 extras。

构建时先安装锁定 registry wheels 与构建后端，再对本地固定源码执行 `--no-editable --no-build-isolation` 的 wheel 安装，不额外解析隐式构建依赖。源码文件在安装前后都校验。运行时再校验 SDK 版本和 `direct_url.json`，要求其来源是镜像内 `sources/main`，且不是 editable。`INSTALLED_DISTRIBUTIONS.txt`、`SYSTEM_PACKAGES.txt`、来源和 adapter manifest 留在 `/opt/materials/`；实际镜像 ID、构建、运行与验收由主编排记录。

宿主只准备材料；框架、验证和验收都要在 Docker 内运行：

```sh
python3 scripts/stage_openai_agents_sdk.py
docker build --platform linux/arm64 -t harness-lab-openai-agents-sdk:local external/build-context/openai_agents_sdk
```

## 入口实际做什么

初筛入口继续接收原来严格的 11 键 request 文件；同题详细验证可追加一项 `candidate_options`，不改变原来字段的含义：

```text
/opt/venv/bin/python /opt/materials/harness-lab/entrypoint.py --request /config/request.json
```

追加 `--validate-only` 只核对来源、导入 SDK、读取统一工具 schema、创建模型/Agent 定义和 SQLite 会话，不调用 `Runner`，不发送模型请求。它是容器接线检查，不代表任务能力通过。

模型路径固定为 `AsyncOpenAI(base_url="http://model-proxy:8082/v1", api_key=<本地实验 token>)`，再交给公开 `OpenAIChatCompletionsModel`。exact model 只读 `MODEL_NAME`，不切模型、不切 Responses API，也不调用 OpenAI 账户。入口拒绝 `MODEL_API_KEY` / `OPENAI_API_KEY`，只接收 `MODEL_RUN_TOKEN_FILE`；远程真实 key 只留在代理容器。托管 tracing 在 SDK 导入前、全局和每次运行均关闭。

`AsyncOpenAI.max_retries` 取 request 值，最多 2 次；SDK 的 runner-managed `ModelSettings.retry` 保持未启用，HTTP transport 自带重试为 0。每次运行最多 20 个模型 turn，独立代理另外执行持久的每 run 20 次上游请求硬限额。**是否确实只重试两次、重建候选和代理后预算是否持续，仍须真实候选接 mock 上游验收**，不能从配置推断通过。

入口从统一工具服务读取原八项 schema，构造官方 `FunctionTool`，保留工具名称、粒度和参数。`strict_json_schema=False` 用于避免 SDK 严格模式把原本可省略的参数改成必填；原 schema 的参数与权限校验仍由统一服务执行。每项工具只转发一次 HTTP 请求，不自动重试。工具的端点、token 和身份来自可信启动数据，模型参数不能覆盖。403 原样作为工具拒绝返回给模型，`needs_approval=False`，不创建审批流程。工具侧的独立真实审计仍由统一服务持有。

一次入口只运行一个可信任务和一个 SQLite session；已有会话数据会拒绝启动，以免把重复提交提示冒充恢复。它是初筛变体，不意味着未来每个用户必须常驻一个进程或容器。SDK 支持同一应用管理多个 Agent/run；共享部署的租户权限、状态串扰、调度与成本须另外验证。

## 证据与终态怎么判

- `entrypoint.json`：来源、模型、工具 schema 处理、重试设置、存储与控制边界。
- `raw-events.jsonl`：SDK 原生 stream events、官方 LLM/工具/最终输出 hooks，以及逐次 provider 响应观测。Python Agent 定义仅记名称，不把工具闭包和模型 client 对象序列化；无法 JSON 序列化的额外值只标原始类型。
- `provider-response-NNN.sse` / `.body`：逐次模型 HTTP 响应原始字节。只读 observer 原样透传响应，不更改 SDK 解析、循环、重试或工具选择。
- `result.json`：原生最终输出、turn、interruptions、响应/工具 items、各次 HTTP 状态与真实 `finish_reason`。只有 stream 正常结束、原生 final output/agent end 完整、没有 interruption，而且最后 provider 明确 `stop` 和 `[DONE]` 才算运行完成。业务正确性仍以外部 ledger、文件与 grader 为准。
- `session-history.json`：从官方 SQLiteSession 重新读取的实际会话历史；数据库在 `/state/sessions.sqlite`。
- `native-state.json` 与 `/state/run-state.json`：官方 `result.to_state().to_json()` 的原生状态材料，默认不包含 tracing key。快照失败会明确记录，不用自研替代状态掩盖。

为什么额外保存 wire 终态：固定源码 `chatcmpl_stream_handler.py` 会把已经产生可见文字的 `finish_reason="length"` 包装成 `response.completed`。仅凭这个 SDK event 会把截断当作完成。observer 保留上游真实结束原因供判定，不伪造或修改 SDK event。

入口的 active timeout 或容器 SIGTERM/SIGINT 会调用官方 `result.cancel(mode="immediate")`，再有界消费结束事件并关闭 HTTP/SQLite 资源。这是取消接线；**不等同于任意运行点的暂停恢复，也不证明工具副作用已撤销**。固定源码另外有公开的 `after_turn` 协作式停止和 RunState 续跑路径，具体静态结论见下文。当前入口没有暂停控制接口，也没有执行恢复或证明跨 Worker 续跑。SQLite 历史持久化、诊断状态快照与可续跑是不同验收项。

初筛 I13 的实际镜像、两轮真实工具任务和模拟预算验收见[报告](../../docs/validation/openai_agents_sdk.md)。下面新增的详细接入只完成材料准备，没有以本次改动运行 Docker、模型或证明能力通过；是否选入 I20 由主任务安排。

## 可选的同题详细接入

基础详细 `case` 为 `tools`、`browser`、`sensitive`；业务改造另有下文的 `extension`。它们都要求只读 `/config/scope.json`，字段恰好为 `experiment_id / variant / user_id / session_id`，并与 request 的 session 一致。配置示例：

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

`tools/sensitive` 不接受 `browser` 字段，继续使用原八项 HTTP 工具。`browser` 只使用官方 `mcp.ClientSession` 和 streamable HTTP，沿用冻结依赖图中已有的 `mcp==2.3.0`，无新依赖。该版本 transport 公开返回 `(read_stream, write_stream)` 两项，没有第三项 session getter；初始化证据只保存真实公开结果，不补造 transport session ID。其它公开接口使用 `httpx2`、秒数 float、`input_schema / next_cursor / is_error`。

浏览器会读取真实 initialize 和全部 tools/list 分页，只开放 `navigate / navigate_back / snapshot / click / fill_form / type / press_key / select_option / wait_for / tabs / handle_dialog` 这 11 项原生基础操作；保留服务器的工具名称、描述和 schema。没有业务 HTTP 工具、shell、脚本或 evaluate 工具。每次调用原样传一次，SDK 和模型自行决定操作步骤；桥接不读取网页内容来代替模型做任务。

浏览器身份从单独只读 `/config/browser-credentials.json` 读取，恰好为 `url / username / password`：URL 固定 `http://site:8081`、username 匹配可信 user_id。原始任务提示保持原文，可信测试登录资料作为另一条实际模型输入提供。保存的 hooks、MCP 参数/返回值、provider 响应、状态快照和历史导出均脱敏，包括 JSON 多层转义和跨网络 chunk 的口令。脱敏只影响证据存储，传给 SDK、模型和浏览器的真实输入/响应保持原样；初筛没有凭据时仍按原字节保存 wire evidence。实际 SQLite 运行状态可能含登录历史，属于本 run 的私有状态目录，不作为脱敏证据导出；脱敏快照也不能直接声称是可恢复状态。

主编排还需把浏览器实际写入的同次运行目录以 **只读** 方式挂到候选 `/browser-artifacts`。入口只在原生成功 MCP 返回明确的 `Downloaded file ... to "PATH"` 完成事件后，复制该路径对应的真实 CSV 到 `/workspace/result.csv`。拒绝越界、符号链接、超大文件、非 CSV 和覆盖已有输出；未下载就不生成文件，也没有从 HTTP fixture 旁路读取数据。真实业务内容仍由独立 grader 校验。

新增证据：

| 文件 | 内容 |
| --- | --- |
| `browser-tool-surface.json` | 官方 MCP 版本、实际发现/允许的工具、没有旁路业务工具 |
| `mcp-events.jsonl` | initialize、list_tools、原始参数、真实成功结果或实际错误；含可信 scope/call_id/native_tool |
| `browser-actions.jsonl` | 每次调用的 finish 状态及 `source={path,line,line_sha256}`；成功索引指向实际原生成功响应 |
| `download-events.jsonl` | 原生下载事件引用、真实源文件、目标、字节数和 SHA-256；失败原因单列 |
| `http-tool-events.jsonl` | 原八项工具真实 start/finish、HTTP status/body，或真实传输错误 |
| `policy-decisions.jsonl` | 仅从真实 `sensitive_export` HTTP 403、`fixture_status=403`、明确 deny 和无需审批的响应产生，引用原证据行 |
| `evidence-index.json` | 上述 JSONL 的行数及 SHA-256 |

敏感任务没有审批等待。只有执行层真实拒绝才记录 policy decision，模型口头拒绝、超时或传输错误都不能代替它。`result.json` 保留原生 `status / stop_reason / final_response / native_terminal`；只有观察到真实拒绝且原生正常完成，附加 `experiment_policy_status=denied`。若有拒绝但运行未完整结束，明确记录 `execution_denial_observed_but_run_incomplete`，不会改写成成功。外部工具审计、账本、无副作用和任务结果仍是独立验收条件。

## 同一业务改造：工具、状态和规则

这部分对应 `configs/tasks/business-extension.yaml`，仍只准备材料，未在本次修改中运行 Docker、SDK 或模型。追加的配置严格为：

```json
{
  "case": "extension",
  "trusted_scope_file": "/config/scope.json",
  "business_extension": {"quantity_limit": 3}
}
```

`extension` 才能使用 `business_extension`，不接受 browser 配置；其它 case 和原 11 字段初筛的工具语义不变。原八项工具保留，第九项 `query_order_status(operation_id)` 使用实际读取的原 `receipt` 参数 schema，只向可信 HTTP 工具服务发一次 `receipt` 查询。返回实际 `operation_id / write_count / receipts` 所在的原始 HTTP status/body，不执行下单、补写、重试或后续流程。`http-tool-events.jsonl` 同时记 `tool=receipt` 和 `exposed_tool=query_order_status`，可与独立服务审计对应；不能以注册了工具推断模型实际使用过它。

业务字段放在交给 `Runner.run_streamed(..., context=...)` 的 **mapping context** 内，公开工具回调通过 `RunContextWrapper.context` 读取和更新：

```text
context.business_state = {
  schema_version: 1,
  operation_id: 当前操作编号或null,
  receipt_confirmed: 当前操作是否经真实回执查询确认,
  operations: {
    操作编号: {
      operation_id, receipt_confirmed, write_count, receipts,
      write_acknowledged, last_observation, source
    }
  }
}
```

`write` 开始后先标为未确认；成功写入响应只记实际已收到写入回执。只有模型明确调用原 `receipt` 或新增查询工具，实际响应的 operation、可信 scope、write_count 和 receipts 相符，才标记确认。传输错误、无效 JSON 或口头声称完成不能造出确认状态；不会为了让状态好看而自动补一次查询。多次真实写入会如实保留 write_count，是否重复由独立账本判定。

这些字段由公开 `result.to_state().to_json(strict_context=True)` 保存到原生 `/state/run-state.json`，实际 JSON 路径为 `/context/context/business_state`。`business-state.json` 只是该原生文件的只读诊断投影，保留来源 SHA-256 和 JSON pointer，**不是另造的 durable checkpoint**。入口还用公开 `RunState.from_json(agent, snapshot, strict_context=True)` 重建，再通过公开 `to_json()` 读取并比较，留 `business-state-roundtrip.json`；此处同进程 roundtrip 不能代替换容器或任务恢复。

固定 SDK 的 RunState 没有公开 `.context` getter；`business_extension.restore_business_state()` 不使用私有 `_context`，只调用上面的公开 API。后续真正续跑应把返回的 RunState 交给 Runner，并让 Runner 使用重建后的 context；不要传新的空 context 覆盖它。工具执行时重新核对不可变的可信 run/session/scope，并从当前回调 context 更新业务态，不把旧进程的闭包字典当作恢复来源。

初跑 extension 时，真实 Bearer 认证的 `GET http://tools:8081/tools` 返回 HTTP 200 后，先校验原始八项工具名称和 schema，再将实际 JSON 响应正文保存到 `/state/http-tool-definitions.json`，来源、可信 run/session/scope、HTTP 地址/状态及固定 source/adapter 来源哈希保存到 `http-tool-definitions-source.json`。两份文件的 SHA256 一并放进交给 Runner 的原生 mapping context，随原生 `run-state.json` 保存。这是重建原 FunctionTool 所需的工具定义材料，没有执行记录或业务状态，不能代替 RunState。缓存来源说明不保存 token。

为了单独验证“重连后能读取”，可以删除原候选容器，再从同一镜像启动新候选，以 **`--network none`** 运行，将原 state **只读**挂载、同一 workspace/scope/token、相同 request 身份及一个**新的可写空 evidence 目录**交给入口：

```sh
/opt/venv/bin/python /opt/materials/harness-lab/entrypoint.py \
  --request /config/request.json --restore-business-state-only
```

此模式只适用于 extension，和 `--validate-only` 互斥。它在普通工具 discovery、模型 transport 和 SQLiteSession 创建前分支：读取有大小限制的原生 `run-state.json`，核对原生 context 中的 run/session/scope、缓存绑定哈希、缓存来源与当前镜像来源，再从该缓存重建相同公开 Agent/FunctionTool；不会执行 `tools.load()`、HTTP discovery、工具回调或 Runner，也不构造/改写 SQLite 会话。模型对象仍按相同公开类型重建，但客户端 transport 拒绝所有请求并记录任何尝试；tracing 继续关闭。原工具 callable 的定义和路由保持相同，离线模式若意外执行回调会立即失败。

实际恢复仍调用公开 `RunState.from_json(agent, snapshot, strict_context=True)`，再公开 `to_json()` 读取业务态和定义绑定并比较。输出 `native-state-reconnected.json` 是原 RunState 文件的逐字节副本；`business-state-reconnected.json` 的 source 给出其 SHA256 及 JSON pointer，另留原 state 文件路径/哈希、定义来源哈希、`runner_started=false / model_calls=0 / http_discovery_requests=0 / tool_calls=0`。最后再次读取原 RunState 和两份定义文件核对未变。缓存缺失、来源/哈希不符直接失败，不能补拉工具、重发任务提示、生成空状态或用新会话替代。旧版本没有缓存的运行需要重新初跑，不能用这次定义材料倒填旧运行并声称恢复通过。

固定源码的 `RunState.from_json` 并非对所有 Agent 都保证离线：`run_state.py:3251` 附近可能调用 `agent.get_all_tools(context)`；`agent.py:262/286` 会枚举 MCP 工具及执行动态 `is_enabled` 回调。本入口 extension 仅静态 FunctionTool、`mcp_servers=[]`、布尔 `is_enabled=True`；`mcp/util.py:266` 对空 servers 不调用 discovery，因此该路径只本地枚举，不需 HTTP。此结论来自本仓库固定 commit 的源码核对；[公开 Results 文档](https://developers.openai.com/api/docs/guides/agents/results) 不作为所有 Agent 均能离线恢复的保证。尚未以新镜像执行验证，静态分析不等于 `--network none` 实测通过。

外部验收须另核对新旧容器 ID、同镜像、旧容器确实删除、网络模式、原文件未变及读取值一致。即使这一步通过，也只证明业务态重建读取，不证明暂停时点、未完成任务续跑或副作用安全。

规则变体在 `write` 的真实 HTTP 转发之前执行，数量为整数且大于 3 时直接返回 403 `quantity_limit_exceeded`，普通 quantity=2 仍原样执行，没有审批、改量或自动重试。模型若自行改变任务要求或再做其它写入，独立账本会如实反映，桥接不会掩盖。`business-tool-events.jsonl` 保留实际入口拒绝，`business-rule-decisions.jsonl` 记录可信 scope、operation、数量/上限、`layer=candidate_execution / http_forwarded=false` 和来源行。没有 HTTP 请求就不伪造 HTTP 成功或 Fixture 403。`business_rule_denial_observed` 只作附加观测，不改原生终态。

敏感 `sensitive_export` 仍真实到达统一执行服务，只按原来的实际 403 生成 `policy-decisions.jsonl`，与数量规则事件分开。后续须由同题正例和量4负例、敏感例、真实账本、业务态重建共同验收；全部拒绝不能过普通任务。这里没有修改 SDK 内核，没有新依赖或自研调度状态机，也未实际升级 SDK，不能宣布升级或跨 Worker 续跑通过。

## RunState 与外部暂停：只读评估

问题是“收到暂停请求后能否把 Worker 停掉，以后用新容器继续原任务”，不是让模型等用户批准。固定 commit 的源码存在以下公开接口：

| 公开接口 / 固定源码位置 | 能说明什么 | 不能据此认定什么 |
| --- | --- | --- |
| `RunResultStreaming.cancel(mode="after_turn")`，`result.py:941` | 当前模型响应和已排队工具执行完、保存 session，在下一 turn 前停止；须继续消费 stream | 暂停请求立即生效、在途写入撤销、容器可立刻强杀 |
| `RunResultStreaming.to_state()`，`result.py:1272` | 构建包含原输入、上下文、turn、处理响应和工具跟踪的原生 RunState | 正在运行中随意拍快照都一致，或文件写完就等于副作用也已原子提交 |
| `RunState.to_json()` / `RunState.from_json(...)`，`run_state.py:1899 / 2423` | 由可信状态重建；`Runner.run_streamed` 接收 RunState 作为输入 | 状态所属用户自动鉴权，或 Agent/工具/模型/凭据可以任意变化 |
| `RunHooks.on_llm_start/end`、`on_tool_start/end`，`lifecycle.py` | 可观测模型与工具边界，便于定位暂停请求发生在哪一步 | 自带跨 Worker 调度、任务所有权转移或外部系统事务 |

这不是只有审批中断才存在的路线：`run_internal/run_loop.py:373` 会在 turn 事件消费完后检查 `after_turn`，`1698` 附近保存 `NextStepRunAgain` 并在下一轮前退出；官方 `immediate` 路径则会取消任务、清队列并设置 `is_complete=True`，所以仅看 `is_complete` 不能证明业务完成或安全暂停。官方 [Results](https://developers.openai.com/api/docs/guides/agents/results) 与 [observability](https://developers.openai.com/api/docs/guides/agents/integrations-observability) 文档用于核对公开概念，具体行为以固定源码为准。

若选中这条路线，最小后续实验应由外部可信控制请求设置暂停意图，在所属事件循环调用公开 `cancel(after_turn)`，消费到流结束，再把原生 RunState 与必要私有会话材料原子落盘后确认暂停。随后退出容器，用同版本源码、同 Agent 名称、同工具名称/schema/路由及模型配置重建，调用 `await RunState.from_json(...)` 后把结果交回 `Runner`，检查无需重新提交原始任务、模型预算继续累计且已完成写入不重复。这里没有实现这套控制入口或自研状态机，也没有执行实验，不能将静态可行性记为通过。

`from_json` 明确只接受可信存储的完整快照；`context_override / strict_context` 不会验证快照归属与完整性。若走 SDK 原生本地 MCP，还要求保留 pending MCP 的 recipient bindings 和服务器配置/顺序；本桥接暴露的是 FunctionTool，仍需重建原来的名称、schema 和真实 callable。诊断快照已脱敏，恢复实验必须单独处理可信凭据，不可把口令替换成脱敏占位符后宣称原样恢复。

必须保留的副作用窗口也很具体：收到暂停后 `after_turn` 仍可能执行已排队写入；并行工具可能已经在途；硬超时或 kill 可能发生在服务器提交之后、客户端收到回执或状态写盘之前。RunState 不是与业务账本共同提交的事务。后续实验要用工具的真实 operation_id、回执和 ledger 检查重做或漏做；不能用 `immediate`、审批等待或重发整个提示来冒充安全暂停恢复。
