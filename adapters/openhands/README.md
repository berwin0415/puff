# OpenHands 核心 SDK 容器接入材料

对应 I15。**当前仅准备和静态核对材料；尚未构建镜像、启动候选或调用远程模型，不能据此宣布初筛通过。**

## 来源与最小产品边界

使用官方 `OpenHands/software-agent-sdk` 固定提交 `33d8aa29c3078d074799e2fbf5e847aeb2d71360`，SDK 源码版本 1.56.0，MIT。官方源码压缩包 SHA256 为 `70a8be4642612b7a6a1b43bc4dca5cc4be92fae58f98dfd10d0ff82b780b675c`，来源清单由当前 I03 导出流程生成。

本入口只安装源码里的 `openhands-sdk`，使用公开的 `LLM`、`Agent`、`Conversation` 和本地 Workspace。模型、真实 Agent 循环和工具执行都在候选容器内。该模式不需要 Agent Server、Docker-in-Docker、宿主 Docker socket、内置 terminal/browser 工具或 Agent Canvas。

I03 同时固定了 `OpenHands/OpenHands` 的 Canvas 源码；其记录保留在完整来源清单中，但本 Dockerfile 不复制、安装或构建 Canvas，来源核验明确列出 `used_source_ids: [sdk]` 和 `unused_source_ids: [canvas]`。不能把未安装的 Agent Server 或可选远程 Workspace 能力计入本入口。

如果后续需要服务化的 REST/WebSocket 控制，另建官方 Agent Server 变体，单列其依赖、端口、鉴权和常驻资源；官方 Docker/Remote Workspace 也单独评价。当前不为初筛补一套自研调度系统。

## Linux 依赖怎样固定

基础镜像复用当前实验固定摘要的 Python 3.12.13 Linux arm64 slim-bookworm。构建工具固定为 uv 0.9.18、setuptools 83.0.0、wheel 0.46.3、packaging 26.3，并带分发包 SHA256。SDK 声明的构建 backend 需要 setuptools >=83，不能照搬官方完整工作区锁中仅供其他依赖使用的 setuptools 80.9.0。

运行依赖直接来自该 SDK 源码自带的 `uv.lock`，没有自由解析一个同名 SDK registry 包：

1. `uv export --frozen --no-dev --package openhands-sdk --no-emit-workspace` 只导出核心 SDK 的依赖闭包。
2. 用 uv 0.9.18 对 CPython 3.12、Linux aarch64 做 `--only-binary :all:` 的元数据解析，不安装包、不执行候选或源码构建 backend。
3. 版本必须仍与官方锁一致，分发包哈希取解析结果和官方锁中 wheel 哈希的交集，生成 `requirements-linux-aarch64.txt`。当前 125 个 registry 包；包括 LiteLLM 1.93.0、OpenAI Python 2.33.0、httpx 0.28.1、Pydantic 2.13.5。它们是核心 SDK 的传递依赖，并不表示启动了相应服务。
4. `LOCK_PROVENANCE.json` 记录原始锁及两个 pyproject 的哈希、生成命令、目标平台、依赖清单和 requirements 哈希。构建与运行前再次核对每个版本及 artifact 哈希确属上游锁。

最终带哈希 requirements 另以 `uv pip install --dry-run --python-version 3.12 --python-platform aarch64-unknown-linux-gnu --only-binary :all: --require-hashes` 验证完整依赖关系，125 项解析通过。dry-run 未安装任何包，也未导入 SDK；它仍不能替代 Linux 镜像安装。

镜像先从固定 wheel 安装完整依赖，再 `--no-deps --no-build-isolation` 从已核验的 SDK 源码非 editable 安装。`verify_source.py` 核对安装后的版本、`direct_url.json` 指向该源码及非 editable 状态、全部 registry 依赖版本，并拒绝意外安装 `openhands-tools`、`openhands-workspace`、`openhands-agent-server`。`uv pip check` 核对依赖满足关系，冻结实际安装清单和系统包清单。

SDK 依赖使用 tiktoken 计数。Dockerfile 在构建阶段填充 cl100k_base 和 o200k_base 编码缓存；固定版本 tiktoken 自带的 SHA256 校验约束下载内容，镜像保留缓存内容哈希。运行期关闭 Hugging Face 在线下载，不使用宿主缓存。元数据解析成功不代表所有 wheel 已在 Linux 镜像安装成功，这仍须 I15 构建验收。

## 入口和工具

`entrypoint.py --request /config/request.json` 严格接受与当前 DSH、Deep Agents、Pi 相同的 11 个字段：run_id、session_id、prompt、workspace、state_dir、output_dir、tools_url、tool_token_file、max_output_tokens、timeout_seconds、max_retries。工作区、状态和证据是不同目录树；工具地址固定为 `http://tools:8081`，只读 token 来自可信输入，模型无法替换工具 scope。

`http_tools.py` 从统一 `/tools` 读取原始名字、说明和 JSON Schema。使用官方 `ClientTool` 的 JSON-schema action 生成与保留机制，换成真实 HTTP executor；**不会使用默认客户端工具的空执行器，也不会把“已派发”当成业务执行结果**。一处薄覆写关闭 SDK 添加的 summary/risk 参数，且逐项断言 provider-facing schema 与共享原始 schema 相等。HTTP 参数还会在可信服务再次校验。

真实 `Agent` 自行选择工具与下一步。HTTP executor 只转发名字和参数，把 HTTP 状态及 JSON 作为原生 observation 返回；错误为 `is_error=True`，网络中断明确标为副作用未知。它没有模型循环、业务步骤或写入重试。除共同业务工具外，仅保留官方 finish 控制工具；不启用 terminal、文件编辑器、子代理或额外 skill。

`NeverConfirm` 让已授权普通操作自动执行。模拟 sensitive_export 仍由公共 Fixture 的执行规则直接拒绝，不依赖提示词、不转入人工审批；敏感任务的实验状态及可信 deny 事件还须后续公共编排接入和验收，本初筛入口不据回答文字伪造拒绝判定。

## 模型、预算与终态

只接受 `http://model-proxy:8082/v1` 和当前 `MODEL_NAME`。`LLM(model="openai/" + MODEL_NAME, api_mode="chat")` 里的 openai 前缀用于选择 LiteLLM 的 OpenAI Chat provider，发送给代理的模型仍为原始 ID。没有备用模型、Responses 模式、协议转换代理或独立远程密钥；真实 API 密钥仍只在公共 model-proxy 中。

SDK 原生输出上限和 HTTP 超时来自 request。当前 SDK 的 `num_retries` 经 tenacity `stop_after_attempt(num_retries)` 控制**总尝试数**，因此 request.max_retries + 1 映射到该值，0 次重试也会有一次请求。官方底层 LiteLLM transport 已固定 `max_retries=0`，不额外叠加 SDK 重试；官方 retry listener 原样记录尝试。总调用额度依赖独立、跨容器累计的 model-proxy，不能把 max_iteration_per_run 当成调用限额。

300 秒活跃超时覆盖工具发现、初始化和真实循环，超时取消官方 `Conversation.arun()`。官方可能把取消吞入 PAUSED 状态，入口还会检查 timeout context 是否已过期，防止误记为完成。已在途 HTTP 写入不能因取消而保证撤销，须由账本核对；不能据此声称已实现运行中外部取消接口。

只有原生状态 FINISHED、没有未结束 action、日志完整，并且最后一次真实 Chat finish_reason 是 stop，或是 tool_calls 且官方 finish 工具确已返回 observation，才记为 completed。length、content_filter、缺 finish reason、仍暂停或等待等都不能算完成。外部判分仍须独立核对实际业务结果，框架结束不等于业务成功。

产物为 `entrypoint.json`、`raw-events.jsonl`、`llm-events.jsonl`、`retry-events.jsonl`、`state-snapshot.json`、`result.json`，另有官方 conversation 持久目录。原始事件与 completion log 保留原生字段，只脱敏本轮局部 token；SDK 自带价格估计不作为已核实费用，成本以公共代理独立计量为准。

## 如何准备与后续验收

先运行既有源码准备脚本，然后运行：

```sh
python3 scripts/stage_openhands.py
python3 external/build-context/openhands/harness-lab/verify_source.py --source-only
```

stage 仅复制实验文件和 Dockerfile，不导入候选、不改源码导出、不启动容器。只有完整文件、锁和来源核验齐备才生成可构建的上下文；不使用宿主 `.venv` 或已有 node_modules。

本入口只允许全新 state_dir，发现已有状态会拒绝。本地会话持久化是官方 SDK 能力，但未验证删除容器后恢复、暂停控制、共享 Worker 或两用户隔离；session_id 只是实验标识，不当作鉴权证明。Agent/LLM 配置可复用与 Conversation 独立状态的设计依据也不能代替实例成本实测。

后续由主编排接入候选服务后，仍需完成：Linux 镜像构建与安装核验；两个新容器的真实模型/共同工具任务及外部判分；模拟失败上游的原生重试、限额及换候选/计数容器后不清零；模型代理实际路径、模型 ID、协议参数和输出上限核对；错误/超时终态及证据核对。当前没有上述运行证据，不提前登记 I15 完成。
