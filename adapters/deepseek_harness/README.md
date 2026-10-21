# DeepSeek Harness 容器接入

本接入调用固定源码的官方 `DeepSeekHarness` SDK，由官方 `dsh --profile sdk-minimal` 启动真正的 Agent 循环、工具执行和 JSONL 会话存储。只把任务、工具地址和状态目录接进去，不替 Agent 选择下一步。它是同粒度工具初筛变体，完整 `sdk` profile 后续另测。

材料依据：上游 commit `dd6322d604e00eec1ba5e0c8541159906a21094a` 的 `packages/sdk/client/src/launch.ts`、`packages/bundle/sdk-minimal/cordis.patch.yml` 和 `packages/core/tools/src/index.ts`。SDK 在缺少构建产物时自带源码启动支持，使用 `tsx/esm` 和官方源码兼容 patch；这个初筛镜像不用构建 Web 前端。依赖在 Linux 镜像内按上游 `pnpm-lock.yaml` 安装，原外部仓库不改动。

准备和构建：

```bash
.venv/bin/python scripts/prepare_candidate_source.py --config configs/candidates/deepseek_harness.yaml
.venv/bin/python scripts/stage_deepseek_harness.py
docker build --platform linux/arm64 -t harness-lab-deepseek-harness:local external/build-context/deepseek_harness
```

`stage_deepseek_harness.py` 只增加 `harness-lab/` 薄接入文件、单独的适配器哈希清单和 Dockerfile，不改已导出的 `sources/main/`。依赖安装层只复制官方源码、源码清单和独立验证器，用 `--source-only` 在冻结 lockfile 安装前后核对官方源码，不读取适配器清单；BuildKit 的 pnpm store 缓存减少重复下载。随后复制完整薄接入和适配器清单，执行完整哈希核对；每次运行入口也保持完整核对。这样仅改接入逻辑时可以复用源码与依赖层。核对失败即停止，系统依赖实际版本保存在镜像 `/opt/materials/SYSTEM_PACKAGES.txt`。

容器入口接收 `--request /config/request.json`。请求字段如下，所有字段必填；路径是容器内路径：

```json
{
  "run_id": "smoke-dsh-001",
  "session_id": "session-a",
  "prompt": "任务目标，由统一任务配置提供。",
  "workspace": "/workspace",
  "state_dir": "/state",
  "output_dir": "/evidence",
  "tools_url": "http://tools:8081",
  "tool_token_file": "/config/scope.token",
  "max_output_tokens": 4096,
  "timeout_seconds": 300,
  "max_retries": 2
}
```

候选只接收 `MODEL_API_BASE_URL=http://model-proxy:8082/v1`、选定的 `MODEL_NAME` 和 `MODEL_RUN_TOKEN_FILE=/config/model-run.token`。本地运行 token 通过只读文件挂载；入口在 `MODEL_API_KEY` 为空时读取该文件，供官方 SDK 使用。真正远程模型密钥仅由计数代理持有。`tools-plugin.mjs` 读取受信任的 scope token 文件，注册工具服务 `/tools` 给出的基础工具；原有 Bash/编辑器工具关闭，普通业务写入直接执行，模拟敏感操作由工具执行层直接拒绝。

输出 `entrypoint.json`、`raw-events.jsonl` 和 `result.json`，后者保存原生 SDK 结果及停止原因；仅有最终回答不算外部验收通过。当前入口没有实现运行中暂停，不把关闭进程当成保存后恢复，也不把 SDK 协议的限制扩大到整个框架；会话持久化、完整 SDK、取消及恢复能力仍需后续真实实验。

该镜像已完成两次全新容器的真实模型与工具任务；过程、镜像和外部验收见 [容器初筛记录](../../docs/validation/deepseek_harness.md)。真实候选重试及重启限额已补验通过，浏览器和暂停恢复等能力仍待后续任务验证。

## I17：任务作用域与浏览器接入

工具、浏览器、拒绝及业务扩展已有正式任务证据，取消有原生结束事件缺口；详见[实际运行记录](../../docs/validation/deepseek_harness.md)。下述业务状态元数据接法修正了冷读003发现的兼容问题，仍须以新镜像重新验收，不能继承旧变体的冷读结论。

上述 11 字段请求仍走 I08 的官方 `DeepSeekHarness` SDK 初筛入口。只对 DSH 增加可选第 12 字段 `candidate_options`；有此字段才分派到 `scoped-runtime-entrypoint.ts`。其他候选和原初筛请求不需要改字段。新入口直接调用同一固定源码的公开 `runProfile`、`agents.create/agents.resume({setup})`、`Agent.followup/whenIdle` 与 `sessions.flush`；官方 `sdk-minimal` 的模型、循环、工具执行、JSONL 存储继续使用，关闭 SDK stdio server 和它的 stdin 启动插件。它是明确的新变体，不能把 I08 的旧实测当成新变体验收。

这部分属于实验自研接线，没有修改上游源码。改用公开 runtime API 的原因是固定版本高层 SDK server 的 `createSession` 没有开放 `setup`，无法在首轮提示词之前异步挂好该 Agent 的 MCP 工具。不得用异步 `agent/created` 监听器假装已经在首轮前完成。

普通工具或敏感任务的可选字段为：

```json
{
  "candidate_options": {
    "case": "tools",
    "trusted_scope_file": "/config/scope.json"
  }
}
```

`case` 仅接受 `tools`、`browser`、`sensitive`、`extension`。`extension` 必须带 `business_extension: {"quantity_limit": 3}`，只在该 case 增加下述原子查询工具、业务观察状态与数量规则；不能用普通工具结果充当业务改造通过。

`/config/scope.json` 由模拟产品后端生成并只读挂载，内容严格为 `experiment_id`、`variant`、`user_id`、`session_id` 四个字段，session 必须与 11 字段中的 `session_id` 一致。它和工具 token 的配对由可信编排建立；实际工具服务另行验证 token 的后端绑定，最终按真实工具/网站账本 scope 复核。Agent 工具参数不能设置这些身份字段。

浏览器选项为：

```json
{
  "candidate_options": {
    "case": "browser",
    "trusted_scope_file": "/config/scope.json",
    "browser": {
      "url": "http://browser:8931/mcp",
      "credentials_file": "/config/browser-credentials.json",
      "artifacts_dir": "/browser-artifacts"
    }
  }
}
```

这三个 browser 值和 scope 文件路径固定，不能由模型更改。凭证文件严格为 `{"url":"http://site:8081","username":"alice","password":"本轮随机测试密码"}`，username 必须匹配可信 user。凭证只从独立文件补入模型输入；审计输入不持密码，新入口事件中遮蔽已知测试密码和本地 token。原生会话存储可能包含模型使用过的测试凭证，属于该作用域的私有持久状态；本阶段没有证明多租户凭证隔离。

浏览器任务不挂 HTTP 业务/文件工具，只经官方 `dsh-mcp-client` 的 Streamable HTTP 接入浏览器。MCP plugin 先挂在 Agent 下面的私有子 scope，其完整发现列表不会进入实际 Agent 工具表；随后只把允许的原生工具定义通过真正的 `agentCtx.tools.register` 投影到当前 Agent。工具名仍是 `mcp__browser__browser_*`；未引入统一浏览器函数或预写任务步骤。私有 scope 在正常退出、创建失败和超时后显式清理，自动重连关闭；断线需要重建本轮入口，不声称浏览器恢复。

允许集为 navigate、navigate_back、snapshot、click、fill_form、type、press_key、select_option、wait_for、tabs、handle_dialog。不暴露 evaluate、run_code、网络请求、上传、配置、测试脚本和任意 HTTP。工具执行与最终内容投影沿用官方 MCP 定义；失败保留官方异常。普通业务工具也通过 unpublished Agent 的真实 scope 挂载。`tools.guard` 仅在真实 `sensitive_export` 尝试时拒绝并写可信判定；只因 `case=sensitive` 或模型口头拒绝，不会生成执行拒绝证据。

公共编排需要提供以下运行材料（本适配器不修改主 Compose 或 Runner）：

- 只读 `/config/scope.json`、`/config/browser-credentials.json`，browser case 启动固定的 Docker browser 与测试网站。
- browser 的 `/data/browser-artifacts` 和 DSH 只读 `/browser-artifacts` 指向同一轮独立目录。
- browser MCP 使用 `--file-paths absolute`，让 native 下载完成事件指向明确的 `/data/browser-artifacts/...`；不能靠扫描目录猜哪个文件是答案。

仅在真实 MCP 输出的独立 `### Events` 小节包含 `- Downloaded file ... to "..."` 完成事件后，新入口才从该路径读取原下载字节并排他写入工作区 `result.csv`。其他 Markdown 小节和 fenced code 中同样的文字不作为完成事件，避免把页面正文或快照里的伪事件当成下载来源。记录原生路径、实际源路径、目的路径、SHA-256、字节数、call_id 和源事件行号。找不到原文件、路径越界、符号链接、已有目的文件等均记 `missing`，不重造 CSV。此复制只是跨容器产物搬运，文件业务内容仍由外部判定器核对。

证据文件：

| 文件 | 内容 |
| --- | --- |
| `entrypoint.json` | 变体、公开 API、scope、来源、实际开放动作和控制边界 |
| `raw-events.jsonl` | 原生 session.event 通知，保持原生事件结构 |
| `control-events.jsonl` | 仅收到取消信号时：可信控制请求、实际 Agent.cancel 调用、原生 turn/end 与 idle 确认，以及原始证据行来源 |
| `native-errors.jsonl` | 公开 agent/error 的实际 Error 自有属性（含非枚举 message/stack/cause）、scope/session/turn/step、当时原生状态、最近已提交会话事件来源和关联控制请求；只读观察 |
| `mcp-events.jsonl` | 每次真实官方 MCP 执行的开始、canonical `{content, structuredContent}` 成功输出或原生异常 |
| `browser-actions.jsonl` | 裸 `browser_*` 名、scope、phase、status、call_id，以及对应 mcp-events 路径/行号；不改原始证据 |
| `download-events.jsonl` | 仅从实际 MCP 下载完成事件产生的复制来源和校验值/缺失原因 |
| `policy-decisions.jsonl` | 实际工具执行 guard 的 deny、scope、reason、approval_required=false、layer=candidate_execution |
| `http-tool-events.jsonl` | 仅 extension：真实 HTTP 单次执行、canonical tool/exposed_tool、参数、状态、原返回体和原生 call_id |
| `business-tool-events.jsonl` / `business-rule-decisions.jsonl` | 仅 extension：真实 quantity 超限尝试在 scoped tools.guard 拒绝，记录 status=403、参数及 http_forwarded=false |
| `business-state.json` | 仅 extension：当前业务状态及可直接跟随 hash/line/JSON pointer 的实际原生记录来源 |
| `business-state-reconnected.json` | 仅 extension 的 resume：从原生恢复会话只读取得的同一业务状态，独立于任务 completed |
| `native-business-state-record.jsonl` | 从官方 readRaw 返回的实际 JSONL 逐字复制一条完整业务状态记录；不是重造会话日志 |
| `session-before-followup.json` | 首次输入前的原生 header、完整事件、seq、firstLiveSeq、derived messages、inbox、当前 Agent 实际工具表 |
| `session-after-idle.json` | 新回合完成或只检查恢复后，同一套公开原生状态快照 |
| `session-artifact-before-resume.json` | 仅恢复路径：加载前的原生 header、实际 JSONL 文件位置及物理/解码内容哈希 |
| `session-artifact-after-flush.json` | 确认 live Session flush 成功后的实际文件位置、字节数和哈希 |
| `result.json` | 原生结果/停止原因和单列的 experiment_policy_status；原生 completed 不被抹掉 |

`mcp-events.jsonl` 是官方客户端向上暴露的 canonical 结果，不能称作完整 JSON-RPC wire 抓包：官方客户端对服务端 `isError` 转抛，也不向上保留所有 wire 元字段。若要求原始失败响应或完整 wire，需公共透明审计层补充；这里不替换官方 MCP 客户端或全局拦截 fetch。

成功 canonical 行的字段为 `time, scope, phase=finish, status=200, call_id, native_tool, result`；失败行是 `phase=error, status=500, error`，没有伪造成功 result。browser-actions 的 `source={path:"mcp-events.jsonl",line:N}` 引用对应行。download-events 同样保留这个 `source` 事件引用，实际文件路径单列 `source_file`，避免两个来源字段互相覆盖。

材料静态检查（2026-10-20）：两个 TS 入口用 Node 内置类型擦除后仅做语法解析；两个 MJS 文件通过语法解析；21 项纯配置/下载正反检查通过，覆盖未知字段、错身份/地址、非完成下载、实际字节/哈希、旧文件、越界与符号链接拒绝。重新 stage 后核对 9044 项官方源码、7 项接入文件均通过。检查没有导入/运行候选 SDK、启动 Docker 或调用模型；因此只证明材料和这些薄边界，I17 状态仍待实际容器运行。

## 业务 extension：公开工具 guard 与原生 Session 状态

可选请求仅为：

```json
{
  "candidate_options": {
    "case": "extension",
    "trusted_scope_file": "/config/scope.json",
    "business_extension": { "quantity_limit": 3 }
  }
}
```

`business-extension.mjs` 为本实验自研领域插件，未改上游循环或工具运行时。工具目录仍从可信公共服务取得原八项工具；仅在当前 Agent 增加第九项 `query_order_status(operation_id)`，复制真实 `receipt` 参数 schema。执行只向 `/call` 发一次 `{"name":"receipt","arguments":...}`，返回实际结果，不重试、不自动查询、不补写、不选择后续步骤。原生工具日志保留新增工具名，`http-tool-events.jsonl` 同时记录 `tool=receipt`、`exposed_tool=query_order_status`、原生 call_id、真实 status 和含 result 的 HTTP payload，供独立服务审计对照。

数量规则使用公开 `agentCtx.tools.guard`，该 API 在工具执行前的单调 guard 阶段生效。只有真实 `write` 尝试且 quantity 超过 3 才记数量拒绝证据，拒绝发生在 HTTP 转发之前；quantity=4 记真实参数、status=403、reason=quantity_limit_exceeded、approval_required=false、layer=candidate_execution、http_forwarded=false，以及对应执行记录行。quantity=2 不被该 guard 拦截，继续由真实工具服务执行。原敏感操作 guard 保留，不能靠全部拒绝普通操作通过验收。

状态使用当前 `sdk-minimal` 已加载的公开 `sessionProjections` 服务，领域插件注册 host-only key `harness-lab-business`。真实 write/receipt 工具的 canonical output 附完整业务观察，公开 `output.presentationMeta` 把它投影到调度器实际写入的 `tool/result.data.meta.business_state`；给模型渲染的仍是原 HTTP result。`stateOf(session,key)` 从这些原生事件读取和冷启动重建，不自建 checkpoint 文件、数据库或任务状态机。写前 flush 已存在的真实 `tool/call`，重复写使旧回执确认失效。工具保持默认 exclusive 串行；未来开启并行需要重新设计 whole-state 更新，避免覆盖。Zod schema 使用冻结上游已有依赖。

旧接法追加自定义必需事件 `harness-lab/business-state`，创建时虽能保存，但同版本加载器静态目录不认识该事件，冷读003真实拒绝。固定源码没有外部事件注册入口，`ignorable` 只适用于可安全丢弃的信息，不能用于隐藏必需业务态。原日志不修改，旧变体的创建通过和重读失败均保留。

完整业务状态包含 `schema_version=1`、原生 session_id、已验证 scope、当前 `operation_id/receipt_confirmed` 和 operations 映射。映射逐 operation 保存实际 receipts、write_count、write_acknowledged、最近观察及真实 HTTP 证据来源。新 operation 只由与参数及全部可信 scope 字段匹配的实际回执引入；模型文本、请求 prompt 或口头宣称不会创建业务确认。

`write` 成功响应只保存实际写回执并设 `receipt_confirmed=false`；仅模型明确调用 `receipt` 或新增查询工具，实际结果 operation、write_count、receipts 数量及每张回执 scope 均匹配时，才设 true。重复 write 在实际转发前撤销该已知 operation 的旧确认，传输异常保持未确认；未知 operation 的错误请求不会造出虚假回执状态。不会为填充状态偷偷补一次查询。

原生 flush 后，入口从官方 `readRaw` 找到最后一条带业务元数据的真实 `tool/result`，核对实际事件、metadata和公开 projection 的值，再把该原始 JSONL 行逐字复制到 `native-business-state-record.jsonl`。`business-state.json` 的 `source={path,sha256,line:1,json_pointer:"/data/meta/business_state"}` 可直接读取相同业务态；另存原生日志位置、原始行号、整文件物理/解码哈希和 event_seq。验收绑定真实 query_order_status 的 callId、sourceEventSeqs、operation、原生结果内容及独立 HTTP 回执；不能只验证形似状态的 JSON。若原记录含秘密而不能逐字导出则明确失败。没有真实业务观察时 `source=null/status=no_business_observation_persisted`。

extension 同样可用 `session_action=resume`，复用同一可信 setup、规则和 projection。在发送任何新输入前就读取恢复状态；不带 followup 只检查恢复，输出新的 `business-state-reconnected.json`，不会调用模型或重发原任务。业务字段恢复和单独 grade 不意味着任务已自动继续，也不是运行中暂停通过。原生事件与外部业务写入不在同一个事务，丢回执/落盘失败仍存在不确定窗口；实际确认须依据真实 receipt/ledger。

公开依据为固定源码的 [tools output.presentationMeta 与 guard](../../external/build-context/deepseek_harness/sources/main/packages/core/tools/src/index.ts)、[真实 tool/result 提交](../../external/build-context/deepseek_harness/sources/main/packages/core/agent-loop/src/tool-calls.ts)、[session projection 契约](../../external/build-context/deepseek_harness/sources/main/packages/session/session-projection/README.zh.md) 和[事件兼容目录](../../external/build-context/deepseek_harness/sources/main/packages/core/session/src/known-event-types.ts)。新保存方式的普通完成、数量拒绝、真实查询和换容器读取仍须用新镜像独立验收。

extension 增量静态核对（2026-10-20）：2 个 TS 入口仅类型擦除/语法解析、3 个 MJS 语法解析、36 项纯业务观察/数量边界/恢复不重放检查通过；覆盖写回执不冒充查询确认、错 scope/operation/count 拒绝确认、未知请求失败不造状态和重复写撤销旧确认。stage 后核对 9044 项官方源码与 8 项接入材料。未导入候选 runtime、未启动 Docker、未调用模型；不是原生持久化或模型完成的验收证据。

## 最小会话冷启动分支：材料已实现，未容器验收

仅 detailed runtime 的 `candidate_options` 接受 `session_action`，省略或 `create` 保持原来的新建会话逻辑。`resume` 明确调用公开 `agents.resume({resumeSessionId, setup})`，不会回退到 create。同一个 setup 在恢复后的 Agent scope 发布前重挂工具、敏感 guard 和需要时的 MCP；不沿用旧进程连接或闭包。

容器 A 执行普通工具回合后，入口等待 idle、要求 inbox 为空、确认 `sessions.flush(session)` 返回 true，再记录真实原生 Session 与实际 JSONL 文件。A 退出并删除后，新容器 B 挂同一轮 `/state`、`/workspace`，用原 session_id 和新的独立 `/evidence`，其可选字段为：

```json
{
  "candidate_options": {
    "case": "tools",
    "trusted_scope_file": "/config/scope.json",
    "session_action": "resume"
  }
}
```

这时只加载和检查会话，不发送模型请求。共同 11 字段中的 `prompt` 仍必填，但恢复路径忽略它，不能默默重放原始任务。成功结果严格为 `status=session_restored`、`native_status=idle`、`stop_reason=resume_inspection_only`、`followup_submitted=false`；这表示加载和检查通过，不表示任务 completed 或旧任务已续跑。

如需验证新回合能使用旧上下文，由可信请求明确增加 `"followup":"一个新的只读问题"`；该字段只允许与 `session_action=resume` 一起使用，必须为非空、至多 65536 字符的文本。仅这条新 followup 会提交，随后仍根据本进程新产生的原生 `turn/end` 判 completed/incomplete，不拿历史成功回合替新回合报成功。浏览器凭证仅在确实提交输入时补入，不会因只检查恢复而提交。

两条路径都在任何新输入前保存 `session-before-followup.json`。它直接读取公开 Session/Agent API，保存 header/cwd、全部事件、next_seq、last_seq、first_live_seq、原生 deriveMessages、inbox 和真实 scope 工具名。事件 SHA-256 对遮蔽前的原生 JSON 数据计算；证据内容仍经已知凭证/token 遮蔽，不能直接用遮蔽后的文件重算该原生哈希。构造器加载的历史不会重新发布到 `session/event`，因此 `raw-events.jsonl` 只保存本进程 live 通知，不能用它单独证明历史加载。恢复产生的合法 `session/end-seed` 边界保留。

文件证据通过公开 `sessionPersistence.readRaw/locate` 取得 header 和原生位置，实际文件必须位于可信 `/state` 下、非符号链接且为有界普通文件。分别记录真实文件字节 SHA-256 与官方 readRaw 返回的解码文本 SHA-256，未重新拼装 JSONL。恢复前先核对原生 session_id/cwd，恢复后的 setup 再核对。原生 header 不提供租户授权；状态挂载和 user/session 绑定仍由可信后端编排保证，不能从历史内容猜身份。

该探针明确排除持久 pending inbox。若恢复后 inbox 非空，在 unpublished setup 阶段保存 `session-refused-pending-inbox.json` 并拒绝发布，避免普通 handle.dispose 清空队列后还报保留成功。成功路径也要求退出前无 pending 输入。编排必须保证单写者、A 退出后才能启动 B，并核对退出后原生日志和工作区；不能两个 Worker 同时写同一日志。

这不是运行中暂停协议，没有启用 Goal/round driver，也没有重建旧 turn 的执行位置。固定版本 JSONL 对崩溃尾部的 interrupted 修复不证明业务写入 exactly-once；敏感 guard/token 仍重挂，写后无回执须核对真实 receipt。跨容器真实模型探针、运行中暂停、10 分钟资源释放和升级均未在此材料阶段验收；具体范围和后续原生 Goal 路线见 [恢复接口分析](../../docs/validation/recovery-interfaces.md)。

本次增量静态核对（2026-10-20）：两个 TS 入口仅类型擦除及语法解析、两个 MJS 语法解析通过；25 项纯配置/原始提示不重放检查通过，覆盖显式 create/resume、只检查恢复、单独新 followup、未知/空动作、无效/过长 followup 和越权字段。检查未导入候选运行时、未启动 Docker、未调用模型；原生 API 的实际加载/落盘行为仍以新镜像 A/B 探针为准。

## 运行中取消：公开 Agent.cancel 接线

仅带 `candidate_options` 的 detailed runtime 接收容器 `SIGUSR1`，由入口实际调用当前公开 `handle.agent.cancel({kind:'hook',reason:'experiment_cancel'})`。可信编排先观察本进程真实 `raw-events.jsonl` 中的 `turn/start`，在 Agent 仍有运行中工作时发送：

```bash
docker kill --signal=SIGUSR1 <candidate-container>
```

接线要求当前 handle 存在、实际 input 已提交、已观察到尚未结束的原生 turn、Agent.status 为 running；不满足时只写 `not_cancelled` 和实际原因，不调用 API，也不把这次信号排到以后生效。`SIGKILL`、删除容器或只收到信号均不算原生取消。

`control-events.jsonl` 保留同一 request_id、实际请求时间、可信 scope、turn、输入已提交标记、调用前后原生状态及 api_executed。阶段依次为 `requested`、`native_api_returned`、`native_turn_end_observed`、`native_idle_observed`。调用返回本身不确认取消；只有实际调用成功后，同一 turn 原生结束原因为 `{kind:'aborted',reason:{kind:'hook',reason:'experiment_cancel'}}`，并且 Agent 实际到达 idle，最后一行才记 cancel_confirmed=true。`request_source`、`api_source` 指向前面的控制行，`source` 指向真正的原始 turn/start 或 turn/end 通知；竞态下自然完成、其他 abort 原因或缺少原生结束通知均不冒充确认。

结果仍按原生 turn/end 保存，取消正常产生 `status=native_status=incomplete`、`stop_reason=aborted` 和非零退出码；外部验收可以把“正确取消”单独判通过，入口不会把它改成 completed。默认原生 cancel 同时清空 pending inbox，因此这不是暂停或可自动继续的协议。已发出的模型 HTTP、工具调用或业务写入可能仍完成、计费或提交；取消不能撤回这些副作用，必须继续核对实际工具审计、receipt 和业务账本。当前接线仍需新镜像的运行中信号探针验收。

## 超时与工具错误的证据定位

本轮只核对固定源码的现有错误与重试语义，未修改重试策略。详细入口的整体 active deadline 来自可信 request.timeout_seconds；其模型连接另有 `streamIdleTimeoutMs=60000` 的流空闲超时。旧 SDK 初筛入口的 requestTimeoutMs 属于另一条路径，不能混为当前直接 runtime 的模型超时。

| 现象 | 原生表现及核对材料 |
| --- | --- |
| 模型流长时间没有数据 | 官方 DeepSeek adapter 的空闲 watchdog 映射为 LlmError.code=TIMEOUT；显式调用者 abort 映射为 ABORTED，传输失败可为 TRANSPORT |
| 计数代理或上游返回 HTTP 504/其他 5xx | 官方 adapter 映射为 SERVER，并保留 HTTP status；不会仅因 504 的文字把它当作 TIMEOUT |
| 框架自行重试 | raw-events 的原生 llm/retry 保存 failure、provider、retry/maxRetries、delayMs、turn/step、retryId/policyKey；真正开始下一次尝试才追加 llm/retry-started，最终 completed 也不会抹掉先前失败 |
| 模型给出错误工具参数或读取不存在的文件 | 原生 tool/call 保存原参数 JSON 与 callId，tool/result 保存返回的错误内容、isError 和可用的错误 name/code；sourceEventSeqs 关联原调用。HTTP 工具的参数校验由实际服务处理，不保证都得到 INVALID_ARGS |
| 任务整体 deadline 到达 | result.json 保存 timed_out/active_timeout；该文件在 finally 清理前写入，之后追加的 raw-events 可能更完整，须一并检查 |

可信侧 `model-calls.json` 和计数代理 calls.sqlite 可对照 injected_timeout、upstream_timeout、upstream_error、client_disconnected、forwarded 及 upstream_status；`candidate.log` 和 `model-proxy.log` 由编排保留。代理未保存完整原始模型 HTTP 返回体，不能声称完整 wire 抓包。工具侧应对照真实 `tool-events.jsonl` 的 start/finish/rejected、HTTP status 和错误；仅 extension 另有候选 `http-tool-events.jsonl` 的实际状态与返回体。浏览器错误沿用上述 `mcp-events.jsonl` canonical 异常证据，同样不等同完整 JSON-RPC wire。

取消/下载增量静态核对（2026-10-20）：2 个 TS 入口仅类型擦除及语法解析、3 个 MJS 语法解析、stage 脚本 AST 与 README 本地链接/空白检查通过。22 项纯下载小节检查覆盖独立 Events、页面正文、各级标题切换、反引号/波浪号 fenced code、缩进/长 fence、CRLF、重复和非完成事件。stage 核对 9044 项官方源码与 8 项接入文件通过，adapter export 为 `4840c21833e069b85b3e1de18d009e591c41314bd1c79ce64302bf97e2ed7315`；未运行候选 runtime、Docker 或模型，不能替代真实原生取消验收。

取消诊断补充：固定源码的 turn() 在 aborted catch 后的 finally 无条件 append turn/end；whenIdle 等待驱动结束，但驱动边界会包含异常后回到 idle。因此 idle 本身不能证明 turn/end 已成功记录。入口另订阅公开 agent/error，把实际错误投影到 native-errors.jsonl，保留不可枚举的错误字段并遮蔽已知秘密；不补造 Session 事件，也不更改既有 status/stop_reason。公开 Agent.cancel 的 cause 签名确实为 `{kind:'hook',reason:string}`。Node 24.22 的无网络 stdlib fetch/abort 探针确认 fetch 可在同一个 plain cause 上增加不可枚举 stack，而固定上游的 lossless JSON 校验拒绝这类 own property；是否正是实际取消失败原因，由新镜像的公开错误观察进一步确认。
