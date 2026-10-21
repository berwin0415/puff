# Codex SDK 容器统一工具接入

本入口使用固定的官方 TypeScript SDK 和真实 Codex CLI，通过本轮选定的 `deepseek-v4-flash` 和模型代理运行共同工具任务。这个版本的 Codex 只支持 Responses API。入口保持它的原生协议，不把 Chat Completions 包装成 Responses，也不切换模型。

先前只有协议接入的镜像已经证明本轮端点可使用原生 Responses；它没有连接统一工具，真实任务未通过，不应把它记为协议受阻或共同任务成功。当前新增的 MCP 材料仍需重新构建，并完成模拟预算和两个新容器的真实工具验收；本次改动本身不是验收证据。隔离、暂停恢复和实例成本仍待专项实测。

## 固定材料

官方源码为 [openai/codex](https://github.com/openai/codex) 的提交 `83086188ae0614bd882818ff1aa23f3194962012`，对应发布 tag `rust-v0.163.0`。源码包 SHA256 为 `e57f1d4f914c71c3532a12d5c14f740909fca4e7e041fa1b3db71618c88e5567`，源码清单摘要为 `c3c767a93ef93cdc22666039a48be5a2292b0929c48bd933d39500e097fcd156`。

这次执行的是官方发布包，与已经核验的源码版本关联；没有声称在镜像内重新编译 Rust CLI。Linux arm64 的完整运行依赖只有三个包：

| 官方发布包 | 固定版本 | 用途 |
| --- | --- | --- |
| `@openai/codex-sdk` | `0.163.0` | 官方 TypeScript SDK |
| `@openai/codex` | `0.163.0` | CLI 包及平台依赖声明 |
| `@openai/codex`，以 `@openai/codex-linux-arm64` 别名解包 | `0.163.0-linux-arm64` | 官方 Linux arm64 实际二进制 |

其他五种操作系统/架构的可选包不适用于本机容器。没有额外运行依赖，也没有假造 npm lock。准备脚本从 npm 官方 registry 重新获取三个包的 metadata、完整 attestations 和 tgz；逐个校验预先固定的 SHA512、registry SHA1、attestation subject 的 SHA512、官方 release workflow/tag 和相同源码 commit，再安全解包并生成独立文件清单。这里检查了来源材料的对应关系，没有独立验证 Sigstore 证书链和签名，不把它描述为完整签名验真。

`REGISTRY_MANIFEST.json` 记录上述下载 URL、版本、完整性值、关联 commit 以及所有包文件的 SHA256。镜像不运行 npm 安装或生命周期脚本，直接复制完整的三个已校验包。构建时实际执行 CLI `--version`，保存到 `CLI_VERSION.txt`；入口要求实际值为 `codex-cli 0.163.0`。固定 Node 24.22.0 基础镜像也使用内容摘要。

## 配置和证据边界

源码中的 `CodexOptions` 接受 `codexPathOverride`、`env`、`config`、`baseUrl` 和 `apiKey`。入口通过这些公开字段启动官方 CLI，使用官方 `startThread().runStreamed()` 和 `TurnOptions.signal`。`CODEX_HOME` 为可信 state 目录内的 `codex-home/`，工作目录为 `/workspace`，证据为 `/evidence`。当前只接受全新 home，不把追加历史对话当成恢复任务。

候选仅拿到本轮代理 token，真实远程密钥仍由代理独立持有。自定义 provider 的 `base_url` 指向 `http://model-proxy:8082/v1`，`env_key` 读取本轮 token，`wire_api` 固定 `responses`。provider 的 HTTP `request_max_retries` 使用本轮值，`stream_max_retries=0`，避免增加流重连层；活动超时和取消通过官方 AbortSignal 传递。具体请求数和原生重试表现仍由容器验收确认。

固定源码的 SDK options 与 CLI 配置 schema 没有公开的模型 `max_output_tokens` 设置。入口仍严格接收统一 request 的 11 个字段，但在 `entrypoint.json` 和 `result.json` 明确记录这个输出预算缺口，不传一个实际不会生效的配置项。模型代理本轮只限制实际请求次数，不能据此宣称限制了远端输出 token。

输出包括官方 `raw-events.jsonl`、入口材料和配置、最后结果，以及原生 home 文件清单。完整线程/session 文件保留在 state 挂载中。原始异常、native `error` 和 `turn.failed` 的内容会保留并遮蔽本轮 token，包括代理限流错误的 marker。只有官方事件流正常耗尽、发生 `turn.started`、最后原生终态为 `turn.completed`、没有原生运行错误、未完成 item 或取消，才记录本轮框架完成。没有终态即为未完成。独立 MCP 工具失败保留为 `native_tool_errors`，不改写成框架失败，也不能据框架完成就认定工具任务成功；真实账本、文件和审计由外部判定。SDK 的 turn 事件不提供模型 finish reason，不能补造一个 `STOP`；输出预算缺口继续单独记录。

## 统一工具桥和残留内建能力

正常模型任务使用官方 `mcp_servers.harness` stdio 配置，由 CLI 在同一个候选容器内启动 `http-mcp-bridge.mjs`。入口先向统一服务的 `/tools` 读取并核对八个工具，保留原名字、说明和 JSON Schema，写入可信 binding；MCP 只把 `input_schema` 投影为标准的 `inputSchema`。服务要求 live connection、`required=true`，白名单固定为这八个工具；普通工具显式设置 `default_tools_approval_mode=approve`，整个 turn 的 `approvalPolicy=never` 不等待审批。首次使用 `auto` 时，固定版本因 MCP 工具没有 destructive/open-world annotations 仍要求审批，真实工具任务未执行；该失败保留，不能把 auto 当作始终放行。

桥是自研的传输适配，不是 Codex 原生业务工具或自研 Agent 循环。它仅实现 tools-only MCP 的初始化、原始工具枚举、单次工具调用、ping 和请求取消；协议版本协商为 `2025-03-26`，没有声明 sampling、elicitation、resources、prompts 或可持久任务能力。它使用 Node 标准库，以换行 JSON-RPC 与官方 CLI 通信，向固定的 `http://tools:8081/call` 转发原工具名和参数，保留真实 HTTP 状态、JSON 和 MCP `isError`；不自动重试、不筛选商品、不计算金额、不生成报告、不预写流程。工具 token 只由可信文件读取，模型参数无法改地址或凭证；实际 scope 仍由统一服务绑定的凭证约束。本桥新增了需要维护的传输代码，应计入接入和后续自研成本。

HTTP 错误返回真实 MCP 错误结果；断连、超时或无效 JSON 明确记为副作用未知，不把写入重试当作安全默认。原始 MCP 请求/响应及 HTTP 结果保留在 `mcp-bridge-events.jsonl`，它们是桥的执行审计，不冒充官方 Agent 事件。官方工具 item 仍在 `raw-events.jsonl` 和 `native_mcp_calls` 中，公共业务工具服务另留独立 start/finish 审计和账本。

固定源码 `codex-rs/features/src/lib.rs` 的 `shell_tool`、`view_image`、`goals`、`sleep_tool` 均支持显式 false；入口已关闭这些及原有 Code Mode、JS REPL、多 Agent、浏览器和 web search 开关。这里使用 `features.view_image`，因为当前固定版本的 `ToolsToml` 不接受现代文档中的 `tools.view_image`。`tool_search` 在该版本已是不可用旧开关关闭的内建行为；连接 MCP 时还可能提供资源枚举工具，因此不宣称模型工具面严格只有八个。桥不提供任何资源能力；其余内建元数据工具与实际暴露面仍须从真实请求和原生事件核对。若原生事件显示 shell、文件补丁或 web search 被使用，入口会记录边界错误并使本轮失败。

预算探针的可信模型名恰为 `probe-model` 时，入口保留独立 `mock_model_budget_probe` 模式，不启动 MCP，不冒充共同工具任务；仍用相同官方 SDK、CLI、Responses provider 及重试设置，让既有模拟 503 和跨容器累计限额探针专门观察模型预算。正式模型不会因任务 prompt 内容进入该模式。

公开配置依据：[OpenAI Docs 的 MCP 文档](https://developers.openai.com/codex/mcp/)和[配置参考](https://developers.openai.com/codex/config-reference/)；实现时以固定提交 `codex-rs/core/config.schema.json` 的 `RawMcpServerConfig`、`AppToolApproval`、`McpStartupReadiness` 及 feature 实现为准。

固定CLI在turn开始前用`item.error`报告缺少第三方模型metadata，此条确切警告单列保留，实际turn错误仍判失败。CLI的home包含官方可执行文件别名软链接；状态清单记录链接本身及目标，不沿链接读取。模拟限额中CLI仅留下原生429状态而丢弃错误正文，因此不能声称看到了代理marker；只有结合内网模拟上游始终503、cap=2和两次上游账本，才将该429归因于本地限额。

## 准备与执行

首次准备官方发布材料：

```sh
python3 scripts/stage_codex_sdk.py --fetch-packages
```

只修改入口或文档时，复用已固定并核验的材料重新暂存：

```sh
python3 scripts/stage_codex_sdk.py
```

宿主机仅下载、核验和暂存材料，不导入 SDK、不运行实际 CLI。Docker 构建和模型接入验收由仓库统一编排执行。官方文档参考：[TypeScript SDK](https://developers.openai.com/codex/sdk/)、[配置参考](https://developers.openai.com/codex/config-reference/)；具体字段以本次固定源码中的 SDK types 和 `codex-rs/core/config.schema.json` 为准。
