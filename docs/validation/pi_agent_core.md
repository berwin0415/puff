# Pi Agent Core：容器初筛

日期：2026-10-20，对应 I11。**初筛执行和证据记录已完成：第一轮工具任务 12 项通过，第二轮 11/12 项通过，分页要求未通过；预算探针 18 项全部通过。** Pi 可以接入当前模型并执行多步工具任务，但本次不能记为两轮全部通过，也不能据此得出选型结论。

## 材料与编译方式

来源为官方仓库 `https://github.com/earendil-works/pi`，提交 `73f8de80bc87021375f8b525292ac07492253002`。官方源码压缩包 SHA256 为 `ce969e2917f93ed49d8be62fc7e7a0a02caa02959f40c28c2fb87f2a62639fd8`，导出清单有 1974 个来源文件。本次使用 `@earendil-works/pi-agent-core`、`@earendil-works/pi-ai`、`@earendil-works/pi-telemetry`，版本均为 1.3.0，不将 Pi Coding Agent 或 Pi Durable 的能力计入 Core。

候选在 Linux arm64 Docker 镜像内安装和编译，未在宿主运行候选 SDK，也未挂载宿主 node_modules。基础镜像固定为 Node 24.22.0：

```text
node:24.22.0-bookworm-slim@sha256:156e9bc7a0a267cc6ca77b4283e32b123479342df684ce297455d7b95ff5a6b5
```

依赖使用上游未修改的 `package-lock.json` 和 `npm ci --ignore-scripts`，只选择 Agent、AI、Telemetry 三个 workspace 及根编译依赖。锁内 esbuild 0.28.2、OpenAI JS SDK 7.19.0、TypeBox 1.3.27 为本次实际接入材料。

源码压缩包缺少生成后的模型目录。本次已固定模型 ID，不需要模型自动发现，因此从公开 `Agent` 和官方 `api/openai-completions.streamSimple` 编译 source bundle；未修改 Agent 循环、工具执行、参数校验或模型响应处理。原始来源留在 `/opt/materials/sources/main`，安装和编译在 `/opt/pi-build` 副本完成。TypeBox、OpenAI、partial-json 从冻结依赖外置加载；bundle 放在 AI workspace 下，以解析该 workspace 的 OpenAI 7 依赖。

正式镜像及编译指纹：

| 产物 | SHA256 / 镜像 ID |
| --- | --- |
| 正式镜像，预算与两轮工具任务共用 | `sha256:d6021038530011f5d3e377923670b6a74884f5f50fb7a04f555b43ca357121e2` |
| 上游 package-lock.json | `5c7ef167a17240e02139c123442bf909391646fcf7beffd3aa12e486ef4e9647` |
| `packages/ai/pi-core.bundle.mjs` | `b93fc0cf200a10ed5abf66ca546270cf8e65c5016c2580f59b8d65dc2985b38a` |
| bundle source map | `141ca674c60e1d7d8be81b4b961a0e2729614bce6da923cb090daa83dcb54776` |
| `PI_BUILD_MANIFEST.json` | `360590b349b4bd33055417851ae04808888e01181fdc4fae9551c80f49a829d8` |

构建清单逐一核对 50 个实际源码输入，运行前再核对来源、4 个接入文件和编译输出。镜像保留 bundle metafile、实际依赖清单和系统、Node、npm 版本记录，可追到实际运行材料。

## 实际工具任务

两轮都使用已配置的远程模型 `deepseek-v4-flash`，经统一持久计数代理发送 Chat Completions 请求，真实 key 只在代理内。Pi 的 `Agent.prompt()` 自己决定读取请求文件、翻页、筛选计算、提交业务写入、生成报告的步骤。HTTP 工具桥原样传递名字、说明、Schema 和调用参数，不安排业务流程。

候选配额为 2 GiB、1 CPU，只读根目录；工作区、状态、输出分开挂载。候选只可写本轮 `candidate/` 输出，独立工具审计、模型账本和业务账本不在其挂载内。

| 运行 | 种子 | 模型请求 / 上限 | 实际写入 / 写入请求 | SDK 运行耗时 | 外部判定 |
| --- | --- | --- | --- | --- | --- |
| `i11-pi-tools-001` | 101 | 6 / 20 | 3 / 3 | 27.477 秒 | 12/12，通过 |
| `i11-pi-tools-002` | 101 | 5 / 20 | 3 / 3 | 22.794 秒 | 11/12，整体未通过 |

完整候选容器 ID：

```text
i11-pi-tools-001
c0faae7aa7171816bfa75436646809f6f6e264d5bf3fa4852139e201e6fe5714

i11-pi-tools-002
44fc273ea459be9daf51122f7927c93ad78401bd67f2744b8d8223c084945c26
```

第一轮容器删除后，第二轮从同一镜像新建，各自使用干净工作区、状态、预算和业务账本。这是从头重新执行，未验证继续已有任务。

两轮原生均收到 `agent_end`，最后 assistant 的 `stop_reason=stop`，Agent 不再 streaming，无待完成工具调用及原生错误，因此候选退出码均为 0。所有模型请求都在独立代理记录中有真实上游 HTTP 200。外部检查还独立核对报告、精确写入、分页、scope、工具 start/finish 配对、业务审计及模型限额；候选 completed 不能代替这些检查。

### 第二轮为什么没有通过

第二轮唯一失败项是 `all_pages_read`。独立 `tool-events.jsonl` 记录模型调用了：

```json
{"tool":"list_items","arguments":{"page":1,"page_size":100}}
```

任务提示要求每页最多 5 条，该次调用违反了这一过程要求。公共服务 Schema 本身允许 100，因而请求被执行；最终报告与三笔业务写入仍正确，其他 11 项检查通过。验收进程退出码为 1，整体 `passed=false`。

这证明本次 Pi 与模型组合出现了提示约束未遵守，公共服务也没有硬性限制该业务规则。不能把 HTTP 成功和最终账本正确当作全过程通过，不能将这次结果扩大为 Pi 普遍不能分页。本次保留失败，不通过重跑挑选成功结果。相同种子的两轮执行也不足以衡量多输入可靠性。

### 调用量与费用

从 `candidate/result.json` 的 `native_result.messages` 中，每条 assistant 消息的 usage 只计一次，不重复累加原始事件里同一消息的 usage：

| 原生字段合计 | tools-001 | tools-002 |
| --- | --- | --- |
| `input` | 10075 | 9120 |
| `output` | 2733 | 2326 |
| `cacheRead` | 10240 | 6144 |
| `cacheWrite` | 0 | 0 |
| `reasoning` | 1339 | 1314 |
| `totalTokens` | 23048 | 17590 |

表中保留 Pi 原生字段语义，`totalTokens` 直接按消息累加，缓存和 reasoning 不再加到 total 上。Pi 要求模型描述带成本字段，本接入用零占位，并明确标记 `native_cost_rates=zero_placeholders_not_pricing`。独立验收的 `model_cost_usd` 和 `price_source` 均为 null，实际金额未知，原生 cost=0 不表示免费。SDK 耗时也不包含完整构建、编排和公共服务开销，不能据此推算生产成本。

## 预算与重试探针

`i11-pi-budget-001` 使用上述同一真实 Pi 镜像，模型上游改为内部网络固定返回 503 的模拟服务，使用 fake key，没有真实收费请求。**18 项独立检查全部通过。**

首轮真实候选进行了 HTTP 重试：两次请求到达模拟上游并返回 503，之后请求被 cap=2 的代理以 429 拦截。删除首轮候选和代理，保留同一预算数据库，再创建新的候选和代理；上游累计仍只有两次请求。两轮候选均观察到 `call_limit_exceeded`，原生 `status=failed`、`stop_reason=error`，退出码 1，没有把失败记成完成。

| 角色 | 第一轮完整容器 ID | 第二轮完整容器 ID |
| --- | --- | --- |
| Pi 候选 | `c3b3d4cc335e754eeeacf31737ba9a6fea7f4ec520878212208b0b888f8a0ba2` | `02667926ce61c3db35d3695b3f39f466f8bb1bc48cc685676da542100311864d` |
| 计数代理 | `cea9c8eebc9d09c1535f19550b43321191abc1de3fd7b44ea277213da65a79ec` | `81f5315ae74fe97f2b50f2d1ca7621b561b9fff3cb30b5617e0078b3a61aae53` |

Pi 没有记录可供该探针读取的原生 retry 事件，快照中 `candidate_native_retry_recorded=false`、`candidate_native_retry_count=0`。实际多次尝试由官方 fetch 扩展点记录的 `transport-events.jsonl` 与独立上游账本确认；两轮 `provider_http_attempts` 均为 3。不能把 HTTP 尝试记录描述成 Pi 发出了原生 retry 事件。

本探针验证框架实际重试能被持久累计限额挡住。计数和预算数据库由仓库公共代理提供，不是 Pi 内置能力，也不是任务暂停恢复实验。

## 接入失败与建设投入

本次保留三轮构建记录，均未调用模型：

- `i11-build-001`：上游 tsconfig 的路径映射使 TypeBox 依赖被打进源码 bundle，输入来源检查报 `Unexpected bundled dependency input: node_modules/typebox/build/system/memory/metrics.mjs`，阻止了构建。接入显式外置依赖后修正。
- `i11-build-002`：接入误假设 OpenAI 安装在根 node_modules，读取 `/opt/pi-build/node_modules/openai/package.json` 报 ENOENT。改为按 AI workspace 解析依赖，并将 bundle 放到该 workspace。
- `i11-build-003`：构建成功，产生本报告固定镜像，随后用于真实实验。

前两次是本仓库编译接入错误，不属于 Agent 业务能力失败。接入需要处理源码编译、依赖解析、原始事件和严格终态判断，建设成本不是零。日志中的自动安装耗时或缓存重建耗时不能替代人工研发工时，本次不虚构精确人时。

Agent 循环、原生工具执行和模型调用来自 Pi。统一 HTTP 工具桥、可信身份绑定、Docker 编排、模型预算代理、业务 Fixture 和独立验收来自本仓库，需要计入方案自研投入。普通工具自动执行、敏感工具拒绝由公共执行层提供；本任务无敏感副作用通过，不代表专门的敏感操作对抗测试通过。

## 证据、复现与后续边界

证据根目录为 `runs/2026-10-20/pi_agent_core/`：

- `runtime/i11-build-001/` 至 `i11-build-003/`：构建日志和镜像来源记录。
- `tools/i11-pi-tools-001/`、`i11-pi-tools-002/`：完整 manifest、独立判定、模型调用、工具审计、业务账本、资源采样；`candidate/` 下保留原始事件、transport 尝试、原生结果和状态观察。
- `tools/i11-pi-budget-001/`：两轮候选与代理身份、独立上游账本、快照、原始事件和 18 项预算判定。

复现入口如下，每次使用新的 run ID，保留失败记录：

```bash
uv run --frozen python scripts/lab.py build --candidate pi_agent_core
uv run --frozen python scripts/lab.py run --candidate pi_agent_core --case tools --seed 101 --run-id UNIQUE_ID
uv run --frozen python scripts/probe_candidate_budget.py --candidate pi_agent_core --run-id UNIQUE_BUDGET_PROBE_ID
```

本次保存的 `/state/observed-state.json`、`state-snapshot.json` 是应用观察的进程内 Agent 状态，不是官方 durable checkpoint。入口只接受新会话，发现已有观察状态就拒绝，不能将这些文件算作恢复能力。

浏览器、任务运行中暂停和取消、删除容器后继续已有任务、故障一致性、租户隔离、共享 worker 和空闲成本均未验证。Pi 的进程内 Agent 对象可作为后续共享实例方案材料，但本轮一个候选对象一个容器的部署既不证明共享隔离，也不说明 Pi 必须给每个用户常驻一个容器。是否能改善现有 OpenClaw 产品的实例成本仍需后续实验。
