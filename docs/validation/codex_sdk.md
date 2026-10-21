# Codex SDK：容器初筛记录

2026-10-20，I14 已完成初筛调研。官方 SDK/真实 Linux arm64 CLI 在两个新容器内，经原生 Responses 和官方 MCP 配置完成统一工具任务；本轮尚未验收浏览器、任务恢复或多租户。

## 来源与部署

固定官方源码commit `83086188ae0614bd882818ff1aa23f3194962012`，tag `rust-v0.163.0`，Apache-2.0。使用对应的官方SDK/CLI/platform三个发布包，逐项核对包完整性、provenance的subject/workflow/tag/commit对应关系；未独立验Sigstore证书链。实际CLI版本为`codex-cli 0.163.0`，SDK版本0.163.0，源码与发布材料清单进入镜像。没有重新编译Rust的声明。

首次镜像完成后，模拟探针暴露两项实验接入问题：CLI只输出429状态而不保留代理错误正文，原有判定器因此少通过一项；原生home的可执行别名软链接被清单程序误当异常。入口现记录链接本身、不跟随读取；预算判定只在独立内网、cap=2且上游账本全为503时，将原生429归因于本地限额。原始失败`i14-codex-budget-001`保留，未覆盖。

修正后镜像ID：`sha256:1ad00337b58c9be03034edb3e61dc75b1b5f6a99bb888afdb41617b182d075ab`。2 GiB / 1 CPU，只读根目录，真实key只由代理持有。`i14-codex-budget-002`在两个新候选和两个代理容器中通过18项检查，上游累计仅两次；无收费模型请求。

## 协议探测，业务任务未通过

`i14-codex-protocol-001`仍使用公共工具任务输入，但入口明确为`protocol_admission`，没有连接统一MCP工具。实际10次`/responses`请求均为200，原生turn.completed，入口耗时45.278秒；SDK最终回答说明缺少文件/业务工具，未生成报告或写入。独立业务验收失败，实际写入/请求0/0。不能把原生completed当任务通过，也不能据最初的Chat协议配置假设判定Codex不兼容本轮模型。

候选容器ID：`fb66589a6f764a990719cd375ea33c292d75512da71b842e22bc5d4f0defa1c7`。固定CLI在turn开始前提示第三方模型metadata缺失，使用fallback metadata；确切警告保留且与turn失败分开。该次观察到的其他内建工具面不是本轮统一工具，不能当作隔离或业务能力结论。

原生turn usage：input 65095、cached input 49664、output 3610、reasoning output 2591；这些字段的包含关系以原生协议为准，不重复相加或推算美元费用。固定SDK/CLI公开选项未找到可生效的max_output_tokens；代理只限制请求数，输出token限额仍是明确缺口。

## 统一工具实测

使用官方 `mcp_servers.harness` 启动容器内 stdio 桥，把八个原始 HTTP 工具投影成 MCP；不改 schema、不代办任务、不自动重试写入。首轮 `i14-codex-tools-001` 用 `default_tools_approval_mode=auto`，固定 CLI 仍因工具缺少 annotations 要求审批，而 turn 禁止审批等待，业务调用全部被拒。15 次模型请求、0 写入，外部任务失败，原始记录保留。固定源码 `mcp_tool_call.rs::requires_mcp_tool_approval_for_mode` 证实 auto 会检查 annotations；改用公开 `approve` 显式放行已授权的八个工具。参见 [OpenAI Docs MCP](https://developers.openai.com/codex/mcp/)。

最终镜像：`sha256:a0d58c2ea5c64893c743afa58ffc0daed1ca5de0881b92b7f62bfb7a9456d0c8`；adapter 清单 `b4568ad2e3376c9ae67c755793de44ae1f32d52bf88392330622f8d82e0826c5`。`i14-codex-budget-004` 同镜像通过 18 项模拟检查，无收费模型请求。

| 新容器任务 | 真实模型请求 | 业务写入/请求 | 外部检查 | 原生终态 | 入口耗时 |
| --- | --- | --- | --- | --- | --- |
| i14-codex-tools-002 | 7 | 3/3 | 12/12 | turn.completed，无原生工具错误 | 33.254 秒 |
| i14-codex-tools-003 | 8 | 3/3 | 12/12 | turn.completed，无原生工具错误 | 34.860 秒 |

两个容器 ID 分别为 `b556f0696e2015b1f0e1ab51d1e410d8f35b8ce0b87dbd207a6225279dc039f2` 和 `50533865c19a377f100eb68448406dfd1a76a021c807f7371aad69770f9b40ab`。均为种子101的新任务复现，不是已有任务恢复。SDK usage input/output 分别为57601/3381和69259/3395；缓存及 reasoning 字段原样保留，不重复相加。金额未知。

内建 shell、goal、image、web search 等公开开关已关闭；CLI 仍暴露 MCP 资源元数据接口，首轮确有调用，不宣称模型可见面只有八工具。这些元数据原样留存，业务边界外的其他任务工具仍判异常。输出 token 无可生效公开上限的缺口保留。浏览器、暂停恢复、租户接入与实例成本未验收；I16 暂缓进一步投入，不等于这些能力不支持。

证据根：`/Users/hanbowen01/harness-lab-data/runs/2026-10-20/codex_sdk/`，构建日志在`runtime/`，模拟探针和协议运行在`tools/`。接入材料与复现命令见[说明](../../adapters/codex_sdk/README.md)。
