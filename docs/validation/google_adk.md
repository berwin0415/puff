# Google ADK：容器初筛

日期：2026-10-20，对应 I12。**初筛已完成：同一镜像在两个全新容器内完成真实模型和工具任务，两轮各 12 项外部检查全部通过；真实候选的重试与换容器累计限额探针 18 项通过。** 本报告只证明当前入口和基础任务可运行，不是浏览器、暂停恢复、租户隔离或选型结论。

## 来源和实际组件

官方仓库为 `https://github.com/google/adk-python`，许可证 Apache-2.0，固定提交 `365084db7b14e796f36ad2929f987e8d83874961`，版本 `google-adk==2.12.0`。官方源码压缩包 SHA256 为 `1738a04484fb362e3cc7891b6b3593eadc64a86df1b63839e34e2df1e6317571`，2990 个导出源码文件逐项核验，来源导出清单指纹为 `b02725196e3e362f0e389c68a0ff35ba83ee163932f9cd564c66e919aa5ac905`。

本次使用官方 `Agent`、`Runner.run_async`、`BaseTool` 扩展点、`LiteLlm` 模型适配器和 `SqliteSessionService`。无需托管 Vertex 运行时。安装 ADK 基础依赖及选定的 LiteLLM 依赖，没有安装包含其他框架和云服务集成的完整 extensions 集合。

独立 Linux arm64 / Python 3.12 的 `uv.lock` 冻结 76 个包条目；主要版本为 LiteLLM 1.85.7、OpenAI Python SDK 2.46.0、google-genai 2.30.0、httpx 0.28.1、flit-core 3.12.0。先冻结安装外部依赖和构建工具，再从已核验官方源码构建普通 wheel；运行前核对实际安装版本和 `direct_url.json`，不是 editable 安装。构建与运行同时核对 7 个接入文件，接入清单指纹为 `c07e9fbf6a9fb18584e44f371b19ba717878795c0dca3bb3610fafe7911058a4`。宿主只准备材料，不导入候选 SDK，不挂宿主虚拟环境。

基础镜像为固定摘要的 Python 3.12.13 Linux arm64，uv 0.9.18 也由哈希固定。正式候选镜像：

```text
sha256:bedcd97d09248265db52a57682251478c139d01508fa18e9034c69f2dc97f864
```

镜像保留安装包清单、系统包清单及来源信息。候选配额 2 GiB、1 CPU，只读根文件系统；工作区、状态、候选输出分开挂载，候选不能修改独立工具审计和模型、业务账本。

## 真实工具任务

统一目标要求 Agent 读取条件文件，查全分页，自行筛选和计算，执行三笔普通业务写入并生成 JSON 报告。步骤由官方 Agent/Runner 决定，HTTP 工具桥只透传原始名字、说明、Schema 和参数，没有预写业务流程或答案。模型使用 `openai/deepseek-v4-flash`，通过官方 LiteLLM 的 Chat Completions 路径接入统一计数代理；真实模型密钥只由代理持有。

| 运行 | 种子 | 模型请求 / 上限 | 实际写入 / 写入请求 | SDK 运行耗时 | 外部检查 |
| --- | --- | --- | --- | --- | --- |
| `i12-adk-tools-001` | 101 | 7 / 20 | 3 / 3 | 29.1168 秒 | 12/12，通过 |
| `i12-adk-tools-002` | 101 | 6 / 20 | 3 / 3 | 27.2543 秒 | 12/12，通过 |

完整候选容器 ID：

```text
i12-adk-tools-001
30162474b5394ec33daad913875c3f862970ba74122d23aac74a7b77990028a8

i12-adk-tools-002
e771709f039657f4938fc9b0b47224c4539f09740ec9688bea9f531d221950c5
```

两轮候选和验收退出码均为 0。原生均为 `status=completed`、`stop_reason=STOP`，`iterator_exhausted=true`，观察到最终完整响应且 `native_terminal.errors=[]`。没有只凭 `is_final_response()` 判完成，因为该方法也可能对错误和状态事件返回最终事件。

独立验收从种子重算结果，核对报告、精确业务写入、查全且每页不超过 5 条、作用域、工具 start/finish 配对、业务审计和模型限额。所有 13 次模型请求均在独立账本记录真实上游 HTTP 200。

第一轮容器删除后，第二轮由相同镜像创建，使用各自干净工作区、状态、预算和账本。**这验证从头执行可复现，不是继续中断任务。** 相同种子的两轮通过也不能代替后续三组或五组不同输入比较。

## 原生用量与费用

两轮 `raw-events.jsonl` 分别有 2380、2410 行流式及工具事件。仅统计 `content.role=model`、`partial!=true` 且有 `usage_metadata` 的完整响应；两轮分别得到 7、6 个唯一事件 ID，与模型请求数一致。partial 事件没有 usage，未重复累加同一响应的流式片段，也未再从 session 快照重复累加。

| 原生字段合计 | tools-001 | tools-002 |
| --- | --- | --- |
| `prompt_token_count` | 23689 | 22185 |
| `candidates_token_count` | 2887 | 2893 |
| `cached_content_token_count` | 10240 | 10240 |
| `thoughts_token_count` | 1399 | 1500 |
| `total_token_count` | 26576 | 25078 |

保留各字段的原生语义，`total_token_count` 按完整响应直接累加，缓存和推理字段不再加到总数上。虽然 LiteLLM 带本地费用目录，本次没有可靠的选定模型单价依据，独立验收的 `model_cost_usd`、`price_source` 均为 null，实际金额仍未知。SDK 耗时不包括完整镜像构建、容器启动和公共服务成本，不能单凭本次数字推算生产成本。

## 预算与失败终态

`i12-adk-budget-001` 使用同一正式 ADK 镜像和真实 Agent 循环，模型上游替换为内部网络固定返回 503 的模拟服务，使用 fake key，没有真实收费请求。底层 OpenAI 传输设置最多两次重试，LiteLLM 外层 `num_retries=0`，避免两层重试相乘；实际 HTTP 请求仍由外部持久代理限额，不能把 Runner 的 `max_llm_calls=20` 当作覆盖内部重试的请求上限。

探针 cap=2。首轮重试使两次请求到达模拟上游，后续尝试被代理 429 拦截；删除首轮候选和代理后，分别创建新容器并保留同一预算数据库，累计上游请求仍只有两次。18 项独立检查全部通过。

| 角色 | 第一轮完整容器 ID | 第二轮完整容器 ID |
| --- | --- | --- |
| ADK 候选 | `85cb18e792c42cf986fdc92455de541d88dedc2a3c5d0d9ecafd1fedf240b63c` | `a659eaacba453dab3f561a1e696dd59748b623e3a4c2203aa6151ca1e4c5098e` |
| 计数代理 | `6adaad01e34e82b61a863e9f72fbfaa57af8bef4ada94485c43b9f51b8170f69` | `099fcaff23302965a78dc98c9e42bb60e517591f3aaa36f6353c6067e90f9497` |

两轮候选均以 `status=failed`、`stop_reason=runtime_error`、`RateLimitError` 和退出码 1 结束，原生错误保留“Model request count limit reached”，`iterator_exhausted=false`，没有把错误当完成。探针没有采到原生 retry 事件，快照中 `candidate_native_retry_recorded=false`、计数为 0；实际重试由候选确实发出并到达模拟上游的两次请求确认，不能描述成 ADK 发出了原生 retry 事件。

累计预算数据库和拦截能力来自本仓库模型代理，需要计入公共运行层建设成本。更换候选和代理后限额不归零，不等于任务恢复通过。

## 启动保护与旁路提示

第二轮第一次发起启动时，前一轮尚未完成清理，编排保护以 `Another experiment is still running` 拒绝。本次在创建新运行目录和启动候选之前退出，模型请求为 0。原始日志已单独保存在 `runtime/i12-start-guard-001/start-guard.log`。前轮清理后，正式 `i12-adk-tools-002` 从新状态正常运行；这不是覆盖一次候选失败。

LiteLLM 日志有缺少 `botocore` 的旁路提示，仅表示未安装 AWS Bedrock/SageMaker 事件流解码依赖。当前使用 OpenAI Chat Completions 路径，两轮真实任务正常完成，该提示不计作本次接入失败。

## 状态、能力来源和未验证项

官方 `SqliteSessionService` 把 session 和事件保存到 `/state/sessions.sqlite`，原始事件及最终 session 快照均留存。本次原生身份为 `app_name=harness_lab_google_adk`、`user_id=screening`、`session_id=main`，由可信编排为每轮提供独立 state 目录；业务工具另外绑定本轮 `experiment_id/variant/alice/main`。这些字段和目录提供本次筛查绑定，不等于真实多租户鉴权或共享 Runner 隔离。

入口明确只创建新 session，已有 session 即报错。SQLite 对话历史本身不证明能恢复中断的 Agent 循环。官方 `abort_signal` 与活动超时已经接到 Runner，但尚未实测运行中取消生效时点、在途写入、资源释放和外部控制授权，不能据此声称暂停、恢复或完整任务控制通过。

Agent 循环、原生事件、官方模型适配和 SQLite session 由 ADK 及官方配套提供。统一 HTTP 工具桥、可信作用域绑定、Docker 编排、计数代理、测试业务、独立验收和严格终态整理由本仓库补齐，需要计入方案投入。没有修改上游内核，但模型依赖选择、完整 Linux 锁、工具桥和错误判定仍是实际接入工作，本次不虚构精确研发人时。

浏览器、多输入详细任务、后续输入、运行中暂停、换容器继续、取消副作用、故障一致性、租户后端鉴权、共享 Worker 和空闲用户成本均未验证。普通任务中无敏感副作用通过，也不等于专门敏感操作拒绝任务通过。Runner 可以服务 session 是后续实例复用方案的材料，是否能降低现有每用户容器成本仍需实际实验。

## 证据与复现

证据根目录为 `/Users/hanbowen01/harness-lab-data/runs/2026-10-20/google_adk/`。`runtime/i12-adk-build-001/` 保存构建日志和镜像 ID；两个工具 run 保存完整容器/挂载 manifest、独立判定、模型请求、工具审计、业务账本、资源采样，以及 `candidate/` 下原始事件、原生结果、来源和 session 快照。预算 run 另存两个候选与代理身份、模拟上游账本和全部断言。候选原生 SQLite 位于对应独立 state 目录。

复现时使用新 run ID，保留成功、失败和保护拒绝记录：

```bash
uv run --frozen python scripts/lab.py build --candidate google_adk
uv run --frozen python scripts/lab.py run --candidate google_adk --case tools --seed 101 --run-id UNIQUE_ID
uv run --frozen python scripts/probe_candidate_budget.py --candidate google_adk --run-id UNIQUE_BUDGET_PROBE_ID
```
