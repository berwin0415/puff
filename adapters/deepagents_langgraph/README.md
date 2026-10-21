# Deep Agents + LangGraph 容器入口

入口读取可信配置，把本轮工具交给官方 `create_deep_agent`，保存框架原始事件及状态。基础/敏感用例使用统一 HTTP 工具；浏览器用例通过官方 MCP Python SDK 连接统一 Playwright 服务。任务步骤由 Agent 决定，没有预写订单处理流程，也没有调用旧实验的 adapter。

## 材料与安装包

本次使用 DeepAgents 源码提交 `0089e635dce525055272883b53bc222f385beac1`，以及 LangGraph 源码提交 `98043c37abc60374030f960a03a6e0d4c0112643`。前者来自已经核验 SHA256 的官方源码压缩包，后者来自已经核验提交且干净的 Git 导出。两个来源的完整文件清单保存在构建上下文的 `SOURCE_MANIFEST.json`。

镜像会从这些导出的源码构建并安装六个 Python 包：

| 安装包 | 版本 | 对应源码目录 |
| --- | --- | --- |
| deepagents | 0.7.28 | sources/deepagents/libs/deepagents |
| langgraph | 1.2.16 | sources/langgraph/libs/langgraph |
| langgraph-checkpoint | 4.3.0 | sources/langgraph/libs/checkpoint |
| langgraph-checkpoint-sqlite | 3.1.2 | sources/langgraph/libs/checkpoint-sqlite |
| langgraph-prebuilt | 1.1.1 | sources/langgraph/libs/prebuilt |
| langgraph-sdk | 0.4.8 | sources/langgraph/libs/sdk-py |

`uv.lock` 是本实验单独生成的完整依赖锁，限定 Python 3.12、Linux arm64。I10 历史镜像使用 70 条目的锁；I19 为浏览器新增 `mcp==1.28.1` 后共 85 条目。增量解析前先放入已有锁，15 个新增包及前后 SHA256 记录在 `LOCK_I19_CHANGES.json`；原有包版本没有改变，也没有删除。新锁和新镜像必须重新验收，不能沿用 I10 的通过结论。

没有沿用旧虚拟环境，也没有沿用 DeepAgents 上游锁：那个锁中的 LangGraph 为 1.2.15，与本次固定源码 1.2.16 不一致。模型适配包固定为 `langchain-openai==1.7.2`；其完整传递依赖也由这个锁固定，包括 `openai==3.35.1`。DeepAgents 声明的 Anthropic、Google 等依赖一并保留。MCP wheel 来自 PyPI，并先核对冻结的 SHA256，再直接阅读 ZIP 内的公开 API 源码；宿主机没有导入候选 SDK。

锁中的六个本地源声明为 editable，是为了与 LangGraph 上游自己的传递路径声明保持一致，避免同一路径出现两种不同的依赖 URL。镜像安装时明确使用 `--no-editable` 构建普通 wheel；运行前还会核对六个安装包的版本、`direct_url.json` 和非 editable 标记。镜像只导入安装后的包。

构建使用固定摘要的 Python 基础镜像。`bootstrap-requirements.txt` 用哈希固定 uv 0.9.18。先按冻结锁安装外部依赖及构建工具，再用这些已经锁定的构建工具构建源码包，避免隔离构建环境另行选择依赖版本。源码文件在安装前后各核验一次；adapter 文件在安装后及每次运行前核验。镜像保存实际安装清单 `INSTALLED_DISTRIBUTIONS.txt`。

## 工具与状态边界

`http_tools.py` 从 `http://tools:8081/tools` 读取本轮统一工具的原始说明和 JSON Schema。向模型显示的名字加 `lab_`，例如 `lab_write_file`；HTTP 调用仍发送原始名字 `write_file` 和原始参数。这个前缀用来区分框架自带的同名工具，不改变工具含义。转发层不自动重试写操作，HTTP 错误会作为工具错误返回给 Agent。

通过官方公开的 `HarnessProfile.excluded_tools` 排除内置文件、执行、任务委派和待办工具，并通过 `GeneralPurposeSubagentProfile(enabled=False)` 关闭默认通用子 Agent。这样基础工具用例只能经过统一工具服务。框架的原生 Agent 循环、上下文中间件和图状态仍然保留，默认 `StateBackend` 的内部文件状态仍由框架维护。工具服务负责真实工作区和权限检查，框架内部虚拟文件系统不代表 `/workspace` 的实际文件。

显式挂载官方 `AsyncSqliteSaver`，数据库为 `/state/checkpoints.sqlite`，使用可信配置中的 `session_id` 作为 `thread_id`，`durability="sync"`。普通运行仅启动全新 session；复用已有 session 会报错，避免把追加一条用户消息当成恢复。业务扩展另有只读 checkpoint 检查模式，见下文。已接入外部 SIGUSR1 到公开 RunControl 的协作停止入口，尚待新镜像实测；没有实现任务续跑。

浏览器用例不加载业务 HTTP 工具，只提供 `browser_navigate`、`browser_navigate_back`、`browser_snapshot`、`browser_click`、`browser_fill_form`、`browser_type`、`browser_press_key`、`browser_select_option`、`browser_wait_for`、`browser_tabs`、`browser_handle_dialog` 中 MCP 实际发布的工具。导航、快照、点击和填表必须存在，缺少即失败。没有业务 API、任意 HTTP、脚本执行或 evaluate 工具，也不重命名 MCP 工具。普通页面操作由 Agent 自动执行。

连接使用固定版本公开的 `streamable_http_client`、`ClientSession.initialize/list_tools/call_tool`，保留分页发现结果。没有接入 Electron、桌面 relay 或候选私有实现。每次实际工具执行分配 `call_id`，它是 adapter 本地关联 ID，不冒充 MCP 线上 JSON-RPC request ID。

### 协作停止入口与尚未验证的恢复路径

固定 LangGraph 1.2.16 已有公开 `langgraph.runtime.RunControl`。每轮创建新的 control 并传给 `graph.astream(..., control=control)`，外部任务可调用 `control.request_drain(reason)`。运行中的节点继续执行，图在下一批节点执行前停止调度；提前退出会抛公开的 `langgraph.errors.GraphDrained`。若请求到达时图已在最后一步完成，仍可正常结束。它提供协作停止机制，不能立即冻结模型请求、同步线程或外部写操作。

本入口的 `run_control.py` 只负责信号和证据接线。外部编排必须先看到真实 `tasks` 开始事件或该活动节点的 `messages` 流，再向候选进程发送 SIGUSR1。入口仅在图仍运行、原生任务开始后尚未观察到其结束且本轮未请求 drain 时，实际调用公开 `RunControl.request_drain("experiment_cancel")`；提前、空闲或重复请求会留下拒绝记录，不调用原生 API。它不调用 Python `task.cancel()`、不发进程终止信号、不阻断工具 HTTP，也不追加用户指令。活动节点可能在请求之后完成模型或工具操作，这是原生 drain 的协作语义。

只有实际捕获公开 `GraphDrained`，读取真实 `graph.aget_state` 的 checkpoint、next/tasks，并确认其 reason 与本轮 drain 一致，才记录 `native_drained_observed`、`cancel_confirmed=true` 和 `native_status="drained"`。`state-snapshot.json` 的 SHA256/JSON pointer 绑定 checkpoint ID；原生异常和 API 返回值另保留在原始流文件中。正常结束、Python 超时、无 checkpoint 或 API 已调用都不足以确认停止。drained 不是普通 completed，也不表示立即取消或续跑成功；`task_continuation_verified=false`。确认后的新业务写入仍需外部统一 HTTP 审计核对，请求后、确认前的在途工作则如实保留。

保留同一数据库与工作区，按相同源码、图结构、工具 schema 重建 graph，用可信 `thread_id` 打开同一 `AsyncSqliteSaver`；可先 `graph.aget_state(config)` 检查 checkpoint、待执行节点、错误和 interrupts，再以 `graph.astream(None, config=..., control=新的RunControl(), durability="sync")` 继续未完成图。`None` 不追加用户消息。只有状态中实际存在 `interrupt()` 等待值时才用 `Command(resume=...)`，不能把它套到所有暂停上。`RunControl` 发出 drain 后不能复用。

下表列出接入依据。SIGUSR1/drain 已实现但尚未执行本轮镜像验证；恢复路径仍只做了固定源码及上游测试阅读，没有在本入口实现或验证：

| 位置（相对构建上下文的 `sources/langgraph/`） | 核验点 |
| --- | --- |
| `libs/langgraph/langgraph/runtime.py:79` | RunControl 的公开定义及线程可调用的 drain 信号 |
| `libs/langgraph/langgraph/pregel/main.py:3154`、`:3582` | 异步流接受 control，drain 后抛 GraphDrained |
| `libs/langgraph/langgraph/pregel/_loop.py:723` | 下一批节点执行前检查 drain |
| `libs/langgraph/langgraph/pregel/main.py:1489` | aget_state 从 checkpointer 读取实际状态 |
| `libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py:138`、`:367` | 持久数据库重开及按 thread/checkpoint 读取 |
| `libs/langgraph/tests/test_runtime.py:173`、`:722` | drain 后从 checkpoint 续跑、运行中由外部任务发 drain 的上游用例 |
| `libs/langgraph/tests/test_pregel_async.py:2938`、`:6161` | 静态断点用 None 续跑；实际动态 interrupt 用 Command(resume) |

云端还需自行实现产品控制 API、单一运行者、停止超时、进程退出与恢复编排，并重新挂载可信身份、工具凭证和业务工作区。这里的 SIGUSR1 接线不代替产品 API。数据库不保存活动 HTTP/MCP 连接、页面进程或外部事务；重建浏览器可重新登录，本轮不要求保留页面状态。强制取消/进程中断时，外部写入可能已成功但结果尚未进入 checkpoint，恢复前必须用业务账本和 operation_id 核对，不能凭 checkpoint 宣称不会重复写。当前入口对已有状态仍明确拒绝运行。

## 输入与证据

入口命令为 `entrypoint.py --request /config/request.json`。严格保留基础 11 字段：`run_id`、`session_id`、`prompt`、`workspace`、`state_dir`、`output_dir`、`tools_url`、`tool_token_file`、`max_output_tokens`、`timeout_seconds`、`max_retries`；仅允许一个可选的第 12 字段 `candidate_options`。没有这个字段时仍执行基础筛选。候选只拿到模型代理地址、模型名和本轮代理 token 文件。真实远程模型密钥由独立计数代理持有。

浏览器详情配置为：

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

`case` 也可为 `tools`、`sensitive`、`extension`；只有 browser 可含 browser 字段。可信 scope 文件必须恰好包含 `experiment_id`、`variant`、`user_id`、`session_id`，且 session 与公共请求一致。浏览器凭证文件必须恰好包含 `url`、`username`、`password`，站点固定为 `http://site:8081`，用户名与可信 user_id 一致。测试凭证由后端单独注入模型输入，持久 JSON 证据遮蔽已知密码和本轮 token。工具事件使用可信 scope，不能从模型参数推断身份。

统一浏览器服务须使用 `--file-paths absolute`，输出目录为 `/data/browser-artifacts`，同一卷只读挂载到候选的 `/browser-artifacts`。只有真实成功的 MCP 响应在 `### Events` 中报告 `Downloaded file ... to "..."`，且对应下载确实存在时，才将其原始字节复制为工作区 `result.csv`，记录来源、字节数及 SHA256。拒绝逃逸/软链/非普通文件、超过 2 MiB 的文件和已有目标。不会由页面文本、文件名猜测、全目录扫描或重建 CSV 代替下载。

`extension` 必须另含 `business_extension: {"quantity_limit": 3}`。它增加精确名 `query_order_status`，输入 schema 直接复用统一服务的 `receipt`，每次只转发一条真实 `receipt` 请求，没有自动查询、补写或代办剩余任务。原来的 `lab_receipt` 仍保留。`lab_write` 在实际 HTTP 转发前检查原始 `quantity`，大于 3 时直接拒绝；数量 2 继续按原工具执行。敏感导出仍由统一执行服务真实返回 403，不把数量规则或口头拒绝当成敏感权限证据。

`business_extension.py` 通过公开 `create_deep_agent(state_schema=...)` 扩展 `DeepAgentState`。工具通过公开 `ToolRuntime` 读取可信原生态，返回公开 `Command(update={"business_state": ..., "messages": [ToolMessage(...)]})`，匹配真实 native tool_call_id；不直接改 `runtime.state` 或使用外部 JSON 代替 checkpoint。模型看到的原始 JSON schema 不包含 runtime。状态保存当前 operation_id、逐操作 receipt_confirmed、实际 write_count/receipts 和来源；单独的写入成功响应只表示 write_acknowledged，必须真正执行 receipt 查询且返回值匹配可信 scope 后才确认。原生 reducer 按完成观测合并，保留并行工具的独立观测，不把后一个工具的状态整体覆盖前一个。它记录实际重复写入次数，是否符合任务要求仍由外部判分。

换容器读取命令为：

```sh
entrypoint.py --request /config/request.json --restore-business-state-only
```

此模式要求同一 session_id、相同可信 scope、原 `/state/checkpoints.sqlite` 和全新空证据目录。它在构造模型和 HTTP/MCP 客户端前分支，通过公开 `aiosqlite` 只读 URI 重开数据库，再用公开 `AsyncSqliteSaver.aget_tuple` 读取同 thread 最新 checkpoint 的 `channel_values.business_state`，校验业务投影与原生观测一致。输出原始 checkpoint、业务态、checkpoint_id、SQLite 前后哈希和 `task_continuation_verified=false`。不执行 graph、模型、工具或原 prompt。该证据仅证明原生 checkpoint 反序列化及换容器业务态读取；实际跨容器必须由外部编排记录不同容器 ID，仍不能据此声称暂停续跑、故障恢复、框架升级或后续任务已通过。后续新指令是新任务，不能重发原 prompt 冒充恢复。

冷读仅允许宿主已确认旧 Worker 删除、没有其他写者且持续只读挂载的数据库。干净关闭的数据库可能仍有 WAL 模式文件头，但没有 `-wal/-shm`；普通 `mode=ro` 读取可能要求创建 shm 而失败。bench 独立读取与候选公开 saver 均先用 `lstat` 检查主库及 `-wal/-shm/-journal`，拒绝符号链接、异常文件类型及非空 WAL/rollback journal，再采用公开 `mode=ro&immutable=1` URI，并核对读取前后主库/伴随文件哈希、权限和目录条目。不绕过 saver 的正常 `setup`。`immutable` 是调用方对无写者、不变文件的承诺，不会自己冻结文件；非空 WAL 即使可能已合并也保守拒验，不能删除、忽略或凭主库快照代替它。带未收束 WAL 的状态须另行取得一致快照，本轮不支持。

输出目录保存：

- `entrypoint.json`：材料版本、哈希、公开 profile 排除项和工具名字映射。
- `raw-events.jsonl`：官方流中的 updates、messages、tasks、checkpoints 数据；保留原生字段，仅将非 JSON 对象序列化，并遮蔽本轮 token。
- `model-terminals.jsonl`：原生模型 finish metadata 及对应流证据行。
- `control-events.jsonl`：实际 SIGUSR1 请求、活动任务证据、原生 API 调用和真实 GraphDrained/checkpoint 确认。request/source 指回真实证据行；drain 的在途工作和立即取消限制明确保留。此文件不作为独立业务审计的替代。
- `state-snapshot.json`：官方 `aget_state` 的最后状态。
- `result.json`：原生终态、最终响应、checkpoint ID 和原生结果。只有图无待执行节点/interrupt/error、最后为真实 AIMessage、无有效或无效的待处理工具调用且原生 `finish_reason="stop"` 才记 native completed。`length`、缺失 finish、异常或未闭合状态都不能算正常完成。业务结果另由外部判分检查。
- 浏览器：`browser-tool-surface.json`、`mcp-events.jsonl`、`browser-actions.jsonl`、`download-events.jsonl`。MCP 证据保留公开 SDK 的完整 typed result 字段（包含 isError、structuredContent）或原生协议错误数据；这是 SDK 解码后的规范证据，不是完整网络报文捕获。
- HTTP 详情：`http-events.jsonl`；实际敏感拒绝另写 `policy-decisions.jsonl`。仅当实际 `sensitive_export` 执行返回 fixture 403、`sensitive_operation_denied` 和 `approval_required=false` 时认定执行层拒绝。case=sensitive、提示词、Agent 的口头拒绝都不足以证明拦截；普通工具行为不变。
- 业务扩展：真实 HTTP 记录改用与其他候选一致的 `http-tool-events.jsonl`，保留 canonical/exposed 工具名。执行前数量拒绝写 `business-tool-events.jsonl` 和 `business-rule-decisions.jsonl`，source 指向实际本地拒绝记录并明确 `http_forwarded=false`。`business-state.json` 是 `state-snapshot.json` 的只读投影，source 含原始文件 SHA256 及 `/values/business_state` JSON pointer。换容器只读模式保存 `native-checkpoint-reconnected.json` 和 `business-state-reconnected.json`，后者 source 指向前者的 `/checkpoint/channel_values/business_state`；这些投影不是另一套持久机制。
- `evidence-index.json`：各事件文件最终 SHA256 与行数。browser-actions/policy 的 source 记录原始证据文件、行号和该行 SHA256，可与可信 scope、call_id、native_tool 交叉核验。失败浏览器调用也保留，但 status 500 不进入成功动作集合。

实际敏感拒绝与框架终态分开记录。只有原生 completed 且观察到真实执行拒绝才把本轮实验 status 标为 denied；同时 `native_status` 仍是 completed，`experiment_policy_status` 标明 execution_denied。不能将截断响应或运行异常改写成成功拒绝。工具加载与图运行共用剩余活动时间；MCP 会话和 HTTP 资源最终关闭。

## 准备与构建

先由仓库的来源准备脚本生成 `external/build-context/deepagents_langgraph/`，再运行：

```sh
python3 scripts/stage_deepagents_langgraph.py
```

日常构建和执行通过仓库统一 Docker 编排入口完成，不在宿主机直接运行候选 SDK。

只有修改固定依赖时才重新生成锁，宿主机这一步仅解析材料，不执行候选代码：

```sh
python3 scripts/stage_deepagents_langgraph.py --prepare-lock
# 增量改锁时先放入已保存的旧锁，避免无关已有依赖版本被重新选择。
cp adapters/deepagents_langgraph/uv.lock external/build-context/deepagents_langgraph/harness-lab/uv.lock
uv lock --project external/build-context/deepagents_langgraph/harness-lab --no-build --python 3.12
cp external/build-context/deepagents_langgraph/harness-lab/uv.lock adapters/deepagents_langgraph/uv.lock
python3 scripts/stage_deepagents_langgraph.py
```

`--prepare-lock` 会使上下文暂时不可构建，直到完整锁和 adapter 再次完成 staging。SDK 动态版本的解析元数据逐项抄自固定源码的 `pyproject.toml`，以便宿主机无需执行构建后端；镜像会真实构建并核验安装版本。

I19 浏览器材料此前已完成配置/原生终态/真实拒绝/下载/证据静态检查。业务扩展使用同一 85 条目锁，没有新增依赖；`langchain-core==1.6.11` 的固定 wheel 先核对锁中 SHA256，再静态阅读其 `StructuredTool` 原始 dict schema、参数透传和公开 ToolRuntime 注入路径，未在宿主机导入。正常 staging 会验证完整源码和 adapter 文件清单。源码、语法及锁解析不能替代新镜像构建、真实浏览器/模型执行和独立重复运行；新镜像和业务扩展必须重新验收，I10 历史镜像证据只对应原 70 条目锁。
