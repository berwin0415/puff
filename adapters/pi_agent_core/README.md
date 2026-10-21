# Pi Agent Core 容器初筛入口

本轮只验证 Pi Agent Core 自己能否运行模型与工具任务，不把 Pi Coding Agent 或 Pi Durable 的额外能力计入 Core。

## 用的是什么材料

固定官方仓库 `https://github.com/earendil-works/pi` 的提交 `73f8de80bc87021375f8b525292ac07492253002`，来自已校验 SHA256 的官方源码压缩包。压缩包 SHA256 为 `ce969e2917f93ed49d8be62fc7e7a0a02caa02959f40c28c2fb87f2a62639fd8`；来源清单逐一记录了 1974 个导出文件。

这个版本已经使用 `@earendil-works` 包名，不能按旧 `@mariozechner` 名字安装。源码中的 `pi-agent-core`、`pi-ai`、`pi-telemetry` 都是 1.3.0。镜像从固定源码编译这些模块，依赖安装使用源码自带的完整 `package-lock.json` 和 `npm ci`；没有另装一个名字相近的 registry Agent 包，也没有使用宿主 node_modules。

基础镜像沿用本实验已经固定摘要的 Node 24 Linux arm64 镜像。编译器 esbuild 0.28.2、OpenAI JS SDK 7.19.0、TypeBox 1.3.27 均来自官方锁。安装时只选择 Agent、AI、Telemetry 三个 workspace 和根目录编译依赖，关闭生命周期脚本。实际安装清单保存在镜像的 `INSTALLED_DEPENDENCIES.json`。

## 为什么单独编译入口

官方源码压缩包没有生成后的模型目录 JSON。完整 AI 产品构建会要求先下载并生成模型目录，而本实验已经明确指定模型 ID 和模型代理，不需要自动发现模型。

`build-bundle.mjs` 使用上游锁里的 esbuild，将公开 `Agent` 与官方 `api/openai-completions.streamSimple` 编译成一个 source bundle。workspace 包名映射到原始源码，外部依赖仍从冻结安装的 node_modules 加载。TypeBox、OpenAI 和 partial-json 显式列为外部依赖，防止上游 tsconfig 的 TypeBox 路径映射将 registry 文件混入源码 bundle。没有改 Agent 循环、参数校验、工具执行或模型响应处理逻辑。也没有生成假的模型目录。

编译时逐一比较实际输入和来源清单的哈希，保存 `PI_BUILD_MANIFEST.json` 及 `PI_BUNDLE_METAFILE.json`。前者记录编译输入、编译器、依赖锁和输出 bundle 的哈希；运行前再次核验原始来源、adapter 和编译输出。bundle 放在 AI workspace 下，保留 npm 对其独立 OpenAI 7 依赖的解析路径，不误用其他 workspace 的 OpenAI 6。`sources/main` 保持不可变，安装与编译在镜像里的独立目录完成。

## 怎么运行任务

统一入口为 `entrypoint.mjs --request /config/request.json`，严格复用当前 DSH/OpenClaw request 的 11 个字段。入口读取本轮局部 token，并且只允许模型调用走 `http://model-proxy:8082/v1/chat/completions`。真实远程密钥仍只由模型代理持有。

入口从统一 HTTP 工具服务读取原始工具名字、说明和 JSON Schema，原样交给 `Agent`。调用工具时转发原始名字和参数，返回结构化结果；HTTP 错误明确标记为 `isError`。转发层不自动重试写入，也不安排业务步骤。`Agent.prompt()` 决定模型与工具的完整循环。

模型使用 OpenAI chat 协议，明确关闭 store、developer role 和 reasoning_effort 等扩展，输出上限和超时来自统一 request。Pi 官方重试次数显式设为 request 中的 `max_retries`。没有在框架外添加模型重试。

模型描述里的 65536 上下文长度只是本地初筛上限，不代表已核实的远程模型能力。Pi 要求提供成本字段，因此原生模型结构里用零作占位；入口和结果均标明这不是真实价格，实际成本以独立代理记录为准。

## 怎么判断运行完成

`prompt()` 返回本身不足以判定成功。必须同时满足：收到原生 `agent_end`、Agent 已空闲、没有未结束工具调用、没有原生错误，而且最后一条 assistant 的 `stopReason` 必须是 `stop`，才记为 completed。

`length`、`toolUse` 和 `deferred` 记为 incomplete；`error`、`aborted` 记为 failed；任务超时单独记录。原生最终错误文字会保存在证据中，包括代理拒绝调用时的 429 原因。本轮框架完成仍由外部判分进一步确认业务产物正确。

输出包括：

- `entrypoint.json`：来源、编译、工具和状态边界。
- `raw-events.jsonl`：`subscribe()` 收到的原始 Agent 事件，不替换原生终态。
- `transport-events.jsonl`：通过官方 fetch 扩展点只读记录的 HTTP 尝试序号、时间、方法和路径，不记录请求体、header 或 token；用来辅助核对原生重试。
- `state-snapshot.json` 和 `/state/observed-state.json`：应用写出的 Agent 内存状态观察结果。
- `result.json`：明确状态、最后 assistant stopReason、原生 agent_end 结果及 HTTP 尝试数。

这些文件遮蔽本轮局部 token。源码和运行时依赖路径位于只读根目录；工作区、状态和证据目录由统一编排分别挂载，资源限制为 2 GiB、1 CPU。

## 当前能力边界

Agent Core 里的 Agent 是进程内对象。多个对象可以在同一进程里使用，但会话、工具凭证、工作区绑定和并发隔离仍要由产品实现，是否能减少常驻实例需要另做实测。

这里保存的 JSON 是应用观察结果，不是官方 durable checkpoint。本入口仅允许新会话，发现已有观察状态会报错，不把重放消息算作暂停恢复。当前未实现外部暂停或恢复；取消使用官方 `Agent.abort()`，资源释放仍需容器实测。Pi Durable 和 Coding Agent 的持久化能力仅列为后续可选补齐路线。

## 准备流程

官方来源导出完成后，运行 `python3 scripts/stage_pi_agent_core.py`。随后由仓库统一 Docker 编排构建并执行；宿主机不运行候选 SDK。

源码核验、静态解析和材料 staging 只表示输入材料已准备。镜像构建、真实模型与工具任务、预算探针和新容器重复实验必须另有容器证据，才能把 I11 标为通过。
