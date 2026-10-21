# 候选材料和构建来源

核对日期：2026-10-20。这里只说明材料是什么、从哪里来、怎样固定，**不表示候选已在本机 Docker 内跑通**。初筛结论见 [screening.md](screening.md)，机器与步骤见 [实施计划](../../IMPLEMENTATION_PLAN.md)。

## 已准备的八个候选

配置以 `configs/candidates/*.yaml` 为准。Git checkout 只读检查，未切换分支、重置或安装依赖；新增来源从官方 GitHub 获取固定 commit 的源码归档。模型配置未读取值、未调用收费 API。

| 候选 | 实际官方材料 | 本次固定版本 | 开源范围 |
| --- | --- | --- | --- |
| DeepSeek Harness | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)；本机 clean checkout | `dd6322d604e00eec1ba5e0c8541159906a21094a`；包声明 `0.1.2-alpha.3` | MIT；vendor 和第三方依赖仍保留各自许可及 notices |
| OpenClaw | [openclaw/openclaw](https://github.com/openclaw/openclaw)；本机 clean checkout | `b6e0872bdb7228e22fac7608345e0529a35ec46e`；包声明 `2026.10.5` | MIT；不将现网部署和托管服务算入此源码 |
| Deep Agents + LangGraph | [Deep Agents](https://github.com/langchain-ai/deepagents)、[LangGraph](https://github.com/langchain-ai/langgraph) | `0089e635dce525055272883b53bc222f385beac1` / `98043c37abc60374030f960a03a6e0d4c0112643`；源码包分别声明 `0.7.28` / `1.2.16` | 两者 MIT；本地 SDK/checkpointer，不以托管 LangSmith 服务替代 |
| Pi Agent Core | [earendil-works/pi](https://github.com/earendil-works/pi)，原 `badlogic/pi-mono` 已重定向 | `73f8de80bc87021375f8b525292ac07492253002`；`@earendil-works/pi-agent-core` 声明 `1.3.0` | MIT；Agent Core 与 coding-agent、durable 包分开记能力和投入 |
| Google ADK | [google/adk-python](https://github.com/google/adk-python) | `365084db7b14e796f36ad2929f987e8d83874961`；源码声明 `2.12.0` | Apache-2.0；本地 Python SDK，不要求 Vertex 托管运行时 |
| OpenAI Agents SDK | [openai/openai-agents-python](https://github.com/openai/openai-agents-python) | `64cd5dfc5a1579870dcbed0ea42b27aa84033fee`；源码声明 `0.24.0` | MIT；应用内 SDK，部署、工具和存储由应用负责；托管 Agents API 是另一产品 |
| OpenAI Codex SDK | [openai/codex](https://github.com/openai/codex) 的 TypeScript SDK 和本地 CLI | `rust-v0.163.0` 解引用为 `83086188ae0614bd882818ff1aa23f3194962012`；计划锁 SDK/CLI 均为 `0.163.0` | Apache-2.0；本地 SDK/CLI，不包含托管 Codex 或模型服务 |
| OpenHands | [software-agent-sdk](https://github.com/OpenHands/software-agent-sdk) 为后台，[OpenHands/OpenHands](https://github.com/OpenHands/OpenHands) 现为 Agent Canvas | 后台 `33d8aa29c3078d074799e2fbf5e847aeb2d71360`（SDK/Agent Server `1.56.0`）；Canvas `8d39305538c1d17a30cbe477e57a0e18fcee7d95`（`1.28.1`） | 两份本次源码均 MIT；Cloud/Enterprise 托管运维不在范围 |

许可证判断来自上述固定源码的根 `LICENSE` 和对应包元数据，不将项目开源解释为其所有依赖、模型或配套云服务都开源。候选镜像实际安装依赖后再补完整分发范围和 notices。八候选均保留，没有因 SDK 需要自研运行管理就将其移除。

## 本机材料位置及归档校验

DSH 和 OpenClaw 使用 `/Users/hanbowen01/repo/deepseek-harness`、`/Users/hanbowen01/repo/openclaw`；LangGraph 使用 `/Users/hanbowen01/harness-lab-sources/langgraph`。三者导出前校验完整 HEAD、origin、无工作区改动、无子模块。发现 dirty 会停止导出，不把本地修改冒充官方版本。

其余来源直接从 `/Users/hanbowen01/harness-lab-sources/` 下固定归档导出，不依赖旁边已解压目录；无 `.git` 的 Deep Agents 工作树也不直接作为构建依据。GitHub 源码包不自动提供子模块内容，当前构建所需内容须在各候选步骤再次核对。

| 来源归档 | SHA-256 |
| --- | --- |
| `deepagents-0089e635dce525055272883b53bc222f385beac1.tar.gz` | `2b0cb8cd05f7faeb8e8745845958056a9e974658fc85f4682643874c99d8a548` |
| `pi-mono-73f8de80bc87021375f8b525292ac07492253002.tar.gz` | `ce969e2917f93ed49d8be62fc7e7a0a02caa02959f40c28c2fb87f2a62639fd8` |
| `adk-python-365084db7b14e796f36ad2929f987e8d83874961.tar.gz` | `1738a04484fb362e3cc7891b6b3593eadc64a86df1b63839e34e2df1e6317571` |
| `openai-agents-python-64cd5dfc5a1579870dcbed0ea42b27aa84033fee.tar.gz` | `8bfb125e5d9343e2740a502aec764afaa63cf55dc17d5df068b7d3680a213321` |
| `codex-83086188ae0614bd882818ff1aa23f3194962012.tar.gz` | `e57f1d4f914c71c3532a12d5c14f740909fca4e7e041fa1b3db71618c88e5567` |
| `openhands-sdk-33d8aa29c3078d074799e2fbf5e847aeb2d71360.tar.gz` | `70a8be4642612b7a6a1b43bc4dca5cc4be92fae58f98dfd10d0ff82b780b675c` |
| `openhands-8d39305538c1d17a30cbe477e57a0e18fcee7d95.tar.gz` | `3296ef29e8b4690765d678917d748e54d3027660577100a570946fa9e8978028` |

下载归档和已解压源码保留在材料目录，不提交第三方源码到本仓库。Codex main 的初始调查包也保留，但当前配置已改用上述 release tag 对应源码，构建不会选用 main 调查包。

## 怎样准备构建上下文

在仓库根运行（宿主仅准备材料，不运行 Agent）：

```sh
python3 scripts/prepare_candidate_source.py --all
# 或只处理一个候选
python3 scripts/prepare_candidate_source.py --config configs/candidates/deepseek_harness.yaml
```

输出结构：

```text
external/build-context/<candidate>/
  SOURCE_MANIFEST.json
  sources/main/                 # 通常是单一官方仓库
  sources/deepagents/           # Deep Agents 组合的独立来源
  sources/langgraph/
  sources/sdk/                  # OpenHands 实际后台
  sources/canvas/               # 可选界面和编排参考
```

导出仅复制 Git 跟踪文件或经哈希核对的归档内容。排除 `.git`、宿主 `node_modules`、`.venv`、缓存、`old`、根运行状态/工作区及常见凭证位置；不把根 `.env` 搬进上下文。`.npmrc` 默认排除，候选确需包管理选项时在 Docker 构建中显式写入无凭证配置。符号链接必须留在本来源内，禁止归档路径穿越和非普通文件。源码目录中必要的锁文件、许可及 notices 保留。

清单记录固定来源、完整 commit/归档哈希、dirty 状态及其含义、排除条目数、每个导出文件的哈希和整体哈希。归档导出的 dirty 为 `null`，表示“直接从固定归档导出，没有使用工作树”，不能写成“本地无改动”。脚本只替换带自身清单的可重建目录；无清单目录拒绝覆盖。失败时不留下半份候选上下文。

这是防止已知宿主依赖和凭证路径混入的导出检查，不是任意源码内容的秘密扫描器。候选本身未随源码提供的运行材料仍须在 I08–I15 的镜像安装中补齐并锁版本。正式镜像需要复制清单和实际源码，留实际 image ID，并在容器内核对清单；导出成功不能代替镜像验收。

## OpenAI 官方文档核对

按 OpenAI Docs 流程检索并实际打开了 [Codex SDK 文档](https://learn.chatgpt.com/docs/codex-sdk)、[Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) 和 [Models and providers](https://developers.openai.com/api/docs/guides/agents/models)。这些文档用于确认产品和 SDK 边界，具体 API 和限制以固定源码交叉核对。

Codex TypeScript SDK 包装本地 CLI、支持继续 thread；源码里 `baseUrl` 是配置覆盖，不能据此认定任意 OpenAI 兼容协议可用。Agents SDK 的非 OpenAI provider 配置属于语言 SDK 接口，不能把托管 Agents API 的能力挪给 SDK。详见初筛表。
