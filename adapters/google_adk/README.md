# Google ADK 容器入口

这里负责读取本轮配置、接入统一 HTTP 工具，以及保存官方 Runner 的原始事件和 session。具体任务由官方 `Agent` 和 `Runner` 执行，没有预写业务步骤，没有使用旧实验的代码。

## 材料与依赖

源码来自 [google/adk-python](https://github.com/google/adk-python)，固定提交 `365084db7b14e796f36ad2929f987e8d83874961`，版本为 `google-adk==2.12.0`。官方源码包 SHA256 为 `1738a04484fb362e3cc7891b6b3593eadc64a86df1b63839e34e2df1e6317571`。导出的 2990 个源码文件逐个记录在构建上下文的 `SOURCE_MANIFEST.json`，导出清单 SHA256 为 `b02725196e3e362f0e389c68a0ff35ba83ee163932f9cd564c66e919aa5ac905`。

本轮通过官方 `google.adk.models.lite_llm.LiteLlm` 适配远程模型，选择安装 ADK 的全部基础运行依赖和 LiteLLM。没有安装整套 `google-adk[extensions]`，因为其中还包括本轮不用的 Kubernetes、云服务和其他 Agent 框架集成。SQLite session 服务属于 ADK 基础能力，不依赖这些扩展。

本实验独立的 `uv.lock` 限定 Python 3.12、Linux arm64，冻结 76 个包条目。主要依赖沿用固定源码的 `constraints-3.12.txt` 中版本：`litellm==1.85.7`、`openai==2.46.0`、`google-genai==2.30.0`、`httpx==0.28.1`、`flit-core==3.12.0`。上游没有提供适用于此安装组合的 `uv.lock`，所以单独解析完整传递依赖。

镜像使用固定摘要的 Python 基础镜像和哈希固定的 uv 0.9.18。先按冻结锁安装外部包和构建工具，再用已安装的 flit 从核验后的官方源码构建普通 wheel。运行前检查安装版本和 `direct_url.json`，确认 ADK 实际来自这份源码且不是 editable 安装。镜像保留实际包清单 `INSTALLED_DISTRIBUTIONS.txt` 和系统包清单。

宿主机只准备材料、解析锁及读取源码。`pyproject.toml` 的解析元数据逐项复制官方源码的基础依赖声明，使宿主机锁解析无需运行动态版本构建后端。候选 SDK 的导入、构建和执行都在 Docker 中完成。

## 任务、工具和模型

入口为 `entrypoint.py --request /config/request.json`，严格复用当前实验的 11 个可信 request 字段。只把本轮模型代理 token 交给候选，真实远程密钥由独立计数代理持有。模型路径固定为 `openai/<选定模型名>`，通过官方 LiteLLM 的 OpenAI Chat Completions 路径访问 `http://model-proxy:8082/v1`，不做私有协议转换。

`http_tools.py` 使用 ADK 官方 `BaseTool` 扩展点，把统一服务返回的名字、说明和 JSON Schema 交给模型，调用时原样转发工具参数。没有工具自动重试，也没有用户确认流程。敏感操作的拒绝由统一工具服务负责；发生写入后响应丢失时，转发层返回“副作用状态未知”，后续行为由 Agent 决定。

模型重试只设置底层 OpenAI 传输的 `max_retries`，LiteLLM 外层 `num_retries` 明确设为 0，避免两层重试相乘。实际 HTTP 请求数量由容器外的计数代理记录并实施上限，不能把 Runner 的 `max_llm_calls=20` 当成实际 HTTP 请求上限。关闭 LiteLLM 遥测，并使用随包提供的模型费用表，避免额外远程目录请求。

## 状态与结束判断

官方 `SqliteSessionService` 把 session 和事件写入 `/state/sessions.sqlite`。本轮使用固定的 screening 用户身份和可信配置中的 `session_id`；不同运行的 state 目录由编排器分别挂载。这个固定身份只是当前筛查入口，不能证明已实现多租户认证、共享进程隔离或低成本实例复用。

入口只启动全新 session。已有 session 会报错，防止把“向历史对话再发一句话”记成“恢复中断任务”。当前未实现暂停和恢复。取消使用官方 Runner 的 `abort_signal`，同时使用活动超时结束当前调用；不据此宣称已实现完整 RunControl。

ADK 的 `is_final_response()` 单独使用会把部分错误和状态事件也视为最终事件。因此只有满足以下条件才记录框架完成：官方事件流正常结束；最后一条完整模型事件确实是最终响应；原生 `finish_reason` 为 `STOP`；没有错误、中断或取消。`MAX_TOKENS`、未完成的工具调用或缺失最终模型响应记为未完成，异常保留错误内容。框架完成之后，业务结果仍由容器外的独立判分检查。

## 输出与准备

输出目录保存 `entrypoint.json`（材料与配置）、`raw-events.jsonl`（官方事件原始字段）、`state-snapshot.json`（官方 session 最后状态）和 `result.json`（结束原因及原生末次模型事件）。序列化只处理 JSON 表达和本轮 token 遮蔽，不改写事件含义。原生 SQLite 数据另留在 state 目录。采集状态或关闭资源失败会记录在结果中，不覆盖原来的模型错误。

由仓库统一来源脚本导出固定源码后运行：

```sh
python3 scripts/stage_google_adk.py
```

只有修改依赖时才重新生成锁：

```sh
python3 scripts/stage_google_adk.py --prepare-lock
uv lock --project external/build-context/google_adk/harness-lab --no-build --python 3.12
cp external/build-context/google_adk/harness-lab/uv.lock adapters/google_adk/uv.lock
python3 scripts/stage_google_adk.py
```

`--prepare-lock` 阶段的上下文不能构建镜像，直到完整 adapter 和锁再次暂存。镜像构建、模型请求预算、统一工具链及全新容器重复运行由 I12 的容器验收完成；材料检查本身不代表能力验收通过。
