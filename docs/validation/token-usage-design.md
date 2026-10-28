# 从已有材料补记模型 token 的方案与结果

记录日期：2026-10-21。归属 I26；本文保留最初的只读材料审计，并记录 **enrollment-010 截点已实现、校准且在断网Docker中执行的提取结果**。本次U01–U05已完成；I26整体尚未完成，已知token部分不是研究完整总量，金额仍未知。

现有材料已补出一部分真实 token 用量。四条详细路线都保存过模型返回的输入、输出、缓存和 reasoning token 数字；同一结果会反复出现，取消或断连时也可能没有收到最终计数。当前提取已按候选去重并单列缺口：已知总量部分10,253,403 token，不能把取到的部分当成完整账单，也不能据此给候选排费用名次。

这里的 reasoning 仅指供应商返回的 token 数字。提取和报告均不输出模型正文、思考正文、工具参数、凭证或完整原生事件。

## 1. 历史材料审计与当前提取范围

最初结构审计只读取固定源码、已登记旧 run 的 JSON/JSONL 和已有 `model-calls.json`，当时未运行 Docker、SQL、模型、构建或测试，未读取 `old/`，也没有读取当时正在执行的新 DSH 正式矩阵。下表是该次历史审计，不是当前提取数量。

历史审计入口是外部enrollment-006库存（`runtime/research-usage-enrollment-006`）：238 项预算库登记，文件 SHA-256 为 `594709056edf9f12bb8160b340e4e9ff40b75f42c54b8752a86038fb682caabe`。下表只计算四条详细路线、`upstream_kind=remote`、按 `run_path` 去重后的目录；多个用户共用一个 trial 目录时仍只算一个目录。仓库中的[当前库存](research-usage-inventory.json)已更新为010，旧006、008与009原件未覆盖。

| 候选 | 已登记远程 run 目录 | 至少发现一个非空 usage 对象的目录 | 解释 |
| --- | ---: | ---: | --- |
| DeepSeek Harness | 60 | 58 | 两条取消实验没有 usage |
| OpenClaw | 27 | 27 | 其中 cancel-002 只有中止消息的默认全零，不能视为供应商真实返回零 |
| Deep Agents / LangGraph | 76 | 76 | 含按原 plan 定位到同 trial 子链的 6 个 I24 目录；普通 run 根目录本身没有这些原件 |
| OpenAI Agents SDK | 26 | 24 | 一条浏览器冒烟在模型调用前失败，另一条取消实验没有 usage |

合计 189 个 run 目录。这个表只能说明“材料中出现过 usage 对象”，不能说明每次远程请求都具备计数，更不能作为请求覆盖率。普通目录递归检查 `raw-events.jsonl` 与 `result.json`；I24 使用原 plan 中与已登记 `task_id` 对应的 `run` 字段定位原子链，精确转换可信 `/data/runs/` 挂载前缀，不放开任意跨目录扫描。已读取的这些 JSON/JSONL 没有解析失败行。

预算代理的 `calls` 表只有 `id / started_at / path / outcome / upstream_status`。代理没有保存 token、供应商响应 ID、响应正文，也没有向候选返回可用于关联的 reservation ID。因此，预算库可以证明请求记账，但不能单独补 token；目前也没有统一的请求级关联键把预算行和原生响应一一连接。

### 当前010提取结果

当前冻结库存共343项预算库登记；token提取选中四条详细路线的307项登记、285个不同run目录。去重后的原生响应观察中，2266份观察的输入、输出和总量已知，13份这三项未知；另有11个材料单元没有可用usage。后两者是不同维度，不能相加为“漏记请求数”。

| 字段 | 已知观察数 | 未知观察数 | 冲突数 | 已知部分小计 |
| --- | ---: | ---: | ---: | ---: |
| 完整输入 `prompt_tokens` | 2266 | 13 | 0 | 9,590,870 |
| 输出 `completion_tokens` | 2266 | 13 | 0 | 662,533 |
| 总量 `total_tokens` | 2266 | 13 | 0 | 10,253,403 |
| 缓存读取 `cache_read_tokens` | 2266 | 13 | 0 | 6,105,600 |
| 缓存写入 `cache_write_tokens` | 215 | 2064 | 0 | 0 |
| 推理计数 `reasoning_tokens` | 2266 | 13 | 0 | 163,614 |

缓存读取已包含在完整输入内，推理计数已包含在输出内，不再相加。缓存写入的0只适用于215份明确测量的观察；其他2064份未知，不能说全部请求没有缓存写入。所有字段均为`complete_research_total=false`。

本次选定来源的解析错误和原件缺失均为0，只说明这些有限来源可读，不证明所有远程请求都有原生计数。`request_linkage=not_established`、`coverage_ratio=null`；不把原生观察数与预算计数相除称覆盖率，也不从剩余差值推算漏记或金额。

安全结果见[token-usage-summary.json](token-usage-summary.json)，SHA-256为`ee30a30d4914e74a5716372bcc7aa26adb003345bda063dd381aaf43bf981131`。它绑定的010库存hash为`3d424d716689b10179dc59f16ca95bd11c7066aa3140e2d68652d0c53f1d14c3`，复用的[SQL安全汇总](research-usage-summary.json)hash为`69d2a174beb4f73f8defc9b154ad4313968eb8a63d5a038354f732326c0c44b1`。请求分类由已验证SQL结果提供；token提取本身不运行SQL或模型。

过去008截点原件仍保留：235项四候选登记、213目录、1637份去重观察，1624份输入/输出/总量已知；已知输入6,770,088、输出446,260、总量7,216,348、缓存读取4,335,360、推理计数112,293。008的13份未知观察、11个无可用usage单元及金额/请求关联限制没有被009覆盖。009只在008的271项库存后追加已结束的DA正式24格；新业务失败同样计量，未重跑候选补齐旧用量。

过去009截点也单独保留：259项四候选登记、237目录、1785份去重观察，1772份输入/输出/总量已知；已知输入7,256,969、输出512,333、总量7,769,302、缓存读取4,591,360、推理计数147,498。缓存写入仅215份明确报告0，其余1570份未知；13份输入/输出/总量未知观察、11个无可用usage单元及请求关联限制原样保留。009 token原件（`runtime/token-usage-enrollment-009`）hash为`1e750235ffbda0ca4f0fbf161d66e401f1ec1c6a3e39484bcf466807bd5ea233`。010仅在保留009全部295项原对象字节和顺序后，追加48项已完整结束的I23固定阶段；没有覆盖009原件、重跑候选或改判失败。

本版只读取窄范围主来源：Deep Agents仅取模型updates，不恢复没有模型update的usage-only chunk；SDK仅支持可信scope内的transport wire原件，忽略合成响应ID和默认零。I24按冻结登记及retired Worker确定预期attempt文件，缺文件应报原件缺口；I25只支持已登记baseline/active/wake/fault，不支持后续inactive-wake/rotation。当前17个未登记预算库未查询，其中DSH I24七条及N5八条新格式见[未支持格式说明](research-usage-unenrolled-formats.json)，另有旧DA I24两库留在SQL快照缺口中；不扩建reader或伪装旧格式补齐本轮。旧格式、无scope、原件变化和未支持结构保持未知。新实验必须结束并登记新截点，不能用重跑填补历史缺失。

## 2. 各候选有什么原件、怎么读

| 候选 | 优先读取的原件 | 可获得的字段 | 同一结果的重复来源与取舍 |
| --- | --- | --- | --- |
| DSH | `candidate/raw-events.jsonl` 中的原生 Session 事件；暂停和故障链包含 A/B 两段 | `usage.inputTokens / outputTokens / totalTokens / cacheReadTokens / cacheWriteTokens / reasoningTokens`，后四项按实际存在情况保留 | usage chunk、最终 `assistant/message`、`result.native_result.events`、会话快照可能重复。先合并同一原生事件，再按真实调用 attempt 折叠 chunk 和 message |
| Deep Agents | `raw-events.jsonl` 中的原生 AIMessage；结果或快照用于交叉核对 | `usage_metadata.input_tokens / output_tokens / total_tokens`；`input_token_details.cache_read`；`output_token_details.reasoning`；其他细分仅在原件实际存在时记录 | messages、updates、tasks、checkpoints、结果、A/B 恢复历史会反复携带同一 message。按原状态链及原生 message ID 去重，不累计每次 checkpoint 的历史列表 |
| OpenClaw | `agent_end` hook 的 `event.messages[]` 中逐条 assistant 消息；保留原响应 ID | `usage.input / output / cacheRead / cacheWrite / totalTokens / reasoningTokens`，并检查 `usageReport` 等可用性标记 | `llm_output.usage` 和 Gateway 最终 SSE usage 是 Agent 整轮汇总；`lastAssistant` 又重复最后一条。优先逐条消息，整轮汇总只核对，不相加 |
| Agents SDK | 已保存的 `provider-response-*.sse`，或 `provider_wire_event` 中的原始供应商 usage；二者只选一个主来源 | `prompt_tokens / completion_tokens / total_tokens`；`prompt_tokens_details.cached_tokens`；`completion_tokens_details.reasoning_tokens`；供应商额外字段按白名单保留 | 同一 usage 可能同时出现在 finish chunk、尾部 usage chunk、SDK 完成事件、`llm_end`、`native_responses` 和保存状态。以 transport attempt 和供应商 SSE response ID 定位一次响应，其他来源只核对 |

以上字段不是承诺每条调用都有全部细分。当前抽样的 Deep Agents 原件没有 cache-write 字段；DSH 当前 DeepSeek provider 转换也没有生成 cache-write 字段。字段缺失保留未知，不能补零。

源码依据是当前固定材料中的 DSH `packages/llm/llm/src/types.ts`、`llm-deepseek/src/translate.ts`、`token-meter/src/usage-projection.ts`，OpenClaw `src/agents/usage.ts` 与 `src/gateway/openai-agent-run-usage.ts`，Agents SDK `src/agents/models/openai_chatcompletions.py`、`chatcmpl_stream_handler.py` 与 `usage.py`，以及本仓库四条适配入口和 SDK transport observer。Deep Agents 实际保存字段已由原件核对；本地展开的源码材料不含完整 `langchain_openai` 源码，不据此扩大字段支持结论。

## 3. 必须处理的去重问题

### DSH：相同 step 不一定只有一次请求

先按“原状态链 + Session ID + event.seq”合并相同事件，并检查相同身份的原件是否一致。暂停后 B 的历史事件与 A 重叠时只保留一份。不同 run 即使都叫同一个 Session，也不能合并。

然后按原生事件顺序识别 turn、step 与 retry 边界。同一次 attempt 的 `assistant/chunk` usage 是较早计数，`assistant/message` 是最终计数，应替换而不是相加。官方 token-meter 明确处理 `llm/retry-started`：遇到该事件才为相同 turn/step 打开新的 attempt。只按 turn/step 去重会漏掉真实重试；只统计最终 message 又会漏掉“已经收到 usage chunk、随后调用失败”的用量。

`compaction/summary` 可能携带独立压缩调用的 usage，应单独检查调用出处并计入；不能把摘要文本本身当用量依据。目前完成的结构审计未作全部事件种类的调用级覆盖核验，正式提取时必须列出实际出现的事件种类。

实际例子：`i17-dsh-tools-seed-303-001` 中有 7 条带 usage 的 `assistant/message`，另有 7 条 usage chunk；两组又进入 `result.native_result.events`。四份材料不是 28 次请求。

### Deep Agents：完整历史不是新增花费

以原状态链、线程及 AIMessage ID 标识一次原生响应，校验该 ID 的 usage 是否一致。优先完整 AIMessage；流中的 chunk 和 checkpoint 历史只作补充及交叉核对，不能逐条累加。遇到同 ID 的不同计数，保留冲突并暂停该项汇总，不能随意取最大值。

恢复、唤醒和 resident 历史可能带回之前任务的 message。必须先从本任务执行前的原生状态确定已有 message 集，再识别本任务新增响应；历史用量不能记到每次唤醒。用于去重的 message ID 证明的是原生响应身份，不自动证明每次底层 HTTP 重试都有对应响应。

实际例子：`i19-deepagents-tools-seed-303-001` 中 6 个不同 AIMessage ID 的 usage 在 raw 文件中出现 81 次；`i22-deepagents-pause-seed-303-001` 的 B 结果包含 A 已有的消息，不能把 A、B 结果列表直接相加。

### OpenClaw：整轮统计和逐响应统计只能选一层

逐条取 `agent_end.messages[]` 中有有效 usage 的 assistant 消息，使用原状态链、session 和供应商 `responseId` 去重，检查同 ID 的一致性。无法确认响应身份或可用性的全零 usage 单独列为未知；中止消息里 SDK 预填的零不算供应商测量。

实际例子：`i18-openclaw-tools-seed-303-001` 的 `agent_end` 保存 10 条不同 `responseId` 的 assistant usage；`llm_output` 同时保存整轮 usage 和最后一条 assistant，Gateway SSE 又给出整轮汇总。它们不能相加。

### Agents SDK：当前 Chat Completions 接法的 response_id 不能用来去重

实际例子：`i20-agents-sdk-tools-seed-303-001` 有 6 次原生模型响应，但 SDK `ModelResponse.response_id` 只有 1 个不同值，`request_id` 全为空。Chat Completions 到 Responses 事件的适配会使用占位身份，不能把这个 ID 当供应商响应 ID。

同一原件里，wire usage 出现 12 次，对应 6 组不同的“transport attempt + 供应商 SSE id”。应以这组身份识别物理响应，同一响应的重复 usage 核对一致后只取一次。SDK `Usage` 会为缺少供应商 usage 的响应生成默认 token 零；若没有原始 usage 或其他明确测量证明，这些零仍标未知。若旧 run 没有 wire 原件，只能保留按原生完成顺序得到的较弱响应观察，不能声称已经完成物理请求级去重。

## 4. 统一口径：已知部分与未知部分分别留存

统一结果保存这些字段：`prompt_tokens`（完整输入）、`completion_tokens`、`total_tokens`、`cache_read_tokens`、`cache_write_tokens`、`reasoning_tokens`。每个字段同时保存状态和原件引用，状态至少区分 `reported / derived_from_reported_totals / unknown / conflict`。数字缺失时使用 `null`，不使用 `0`。

统一数字时遵守以下规则：

1. DSH 和 OpenClaw 的 `inputTokens/input` 是未命中缓存的输入桶，完整 prompt 包含 cache-read 和 cache-write；不能拿它直接与其他候选的完整输入比较。如果缓存桶缺失，但供应商总量与输出均明确可靠，可用 `total-output` 得到完整 prompt，并标记推导来源；否则完整 prompt 未知。
2. 当前 Deep Agents `input_tokens` 和 SDK/provider `prompt_tokens` 已包含缓存输入，cache-read 是其中的细分，不能再加一次。
3. reasoning token 是输出细分，不能加到 completion 或 total。缺 reasoning 字段时保持未知，不按模型名称推定有或没有。
4. 供应商明确报告的零可以记录为零；框架默认零、空 usage、`usageReport.state=unavailable`、中止消息的全零不算明确报告。
5. 只接受有效的非负安全整数，核对 total 与输入/输出、缓存子集及 reasoning 子集关系。冲突原件保留，不能通过裁剪、取最大值或估算“修好”。

每次提取至少报告这些可核验的数量：

| 指标 | 计算口径 |
| --- | --- |
| `enrolled_budget_entries` / `unique_run_directories` | 本次固定库存中实际选中的库登记数、不同 run 目录数，二者分开 |
| `proxy_reservations_by_outcome` | 复用经验证的费用汇总口径，区分远程200、远程未确认、在途未知、模拟和本地注入；不从 token 原件反推 |
| `usage_observations_before_dedup` / `unique_usage_observations` | 被选主来源中的原生 usage 观察数、按各候选真实身份和替换规则去重后的观察数；均不是自动等同于预算行数 |
| `known_by_field` / `unknown_by_field` / `conflicts_by_field` | 按去重后的响应观察逐字段统计；部分字段已知不等于整次计数完整 |
| `known_token_subtotals` | 仅汇总有效已知字段，附适用观察数和完整性状态；有缺口时命名为“已知部分”，不能命名完整总量 |
| `run_without_usage` / `evidence_missing` / `parse_errors` | 区分零模型调用、请求已发生但缺末尾usage、没有原件、文件解析失败；原因与引用分别保存 |
| `request_linkage` / `coverage_ratio` | 没有请求级关联证明时，前者标记未建立，后者为 `null`。不把“观察数÷预算行数”直接命名请求覆盖率 |

`unknown_by_field` 只统计已经识别出来的响应观察，不包含所有可能丢失的响应。因此还要单列 `unmeasured_remote_activity`：已知有远程请求发生，但没有足够材料确定每个请求的计数或关联。无法关联时不能简单用“预算行数减去原生消息数”生成精确漏记次数。

以下具体缺口已经确认，不需要重新调用模型来“补”历史：

| 原 run | 原记账与材料 | 结论 |
| --- | --- | --- |
| `i17-dsh-cancel-smoke-001`、`002` | 各有 1 条 `forwarded/200`，raw/result 中均无 usage | 两次请求的 token 未知；200 不表示已取得末尾用量 |
| `i20-agents-sdk-cancel-smoke-001` | 1 条 `forwarded/200`，无 usage | token 未知，SDK/会话状态不能替代供应商计数 |
| `i20-agents-sdk-browser-smoke-001` | `model-calls.json` 为 0 行，执行在模型前失败 | 没有已记录远程调用；不把“无usage”报告成漏记了一次付费调用 |
| `i18-openclaw-cancel-smoke-002` | 中止 assistant 无 `responseId`，无可用性报告，usage 为默认全零 | 不能把默认零当明确零；保留未知 |
| I25 `fault-user-001` 的中断部分 | resident 有断连未确认行，按需模式有未完成 reservation；其他完成响应可保留usage | 已知部分可以计量，末尾丢失部分不能从请求数、输出文本长度或模型平均值补齐 |

供应商价格、折扣、缓存计费规则尚未确认，token 数字补齐后也不自动得到金额。OpenClaw 等原件的 `cost` 可能来自框架默认或配置估算，不用作本轮真实账单。

## 5. 本次落地与验收

已实现独立离线提取器 [summarize_token_usage.py](../../scripts/summarize_token_usage.py) 及 [校准用例](../../tests/test_token_usage.py)，完成源码审查与容器校准。固定bench `sha256:bbd604f4fb4177dec7b77b6f2cfc050a3d859961505bdbe869869fc4c7d8c186`的313项校准（`runtime/token-rotation-check-001`）通过，覆盖token、实例计划、完整轮换与采样审计四份测试；这是工具验收，不代表真实轮换实验已运行。

随后本次实际提取（`runtime/token-usage-check-010`）在Docker中断网、无模型key，只读挂载冻结库存与已结束原件，未挂载预算库，使用独立输出目录；实际退出0、健康终态、无OOM且确切清理通过。没有恢复Agent、调用模型或重跑任务。当前活动材料必须先结束并明确登记，才进入下一截点。

提取器只输出数字白名单、原生身份的哈希、来源相对路径/行号或 JSON pointer、原文件 SHA-256、缺失及冲突原因。禁止将整个原生 message、事件或异常对象复制进结果。读取范围由库存和可信 plan 的精确绑定限定，拒绝符号链接、路径穿越、`old/` 及未登记扩张路径。

| 步骤 | 具体工作 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| U01 | 冻结输入库存、候选范围和各原件的只读清单 | 库存hash固定；普通run与I24/I25的精确绑定可追溯；新活跃run不混入 | 已完成本次010截点；307项四候选登记、285目录 |
| U02 | 实现四候选独立提取与原生去重 | 上述7/7、6/81、10条OC、6组SDK例子可复核；A/B历史不重复，retry不漏，冲突不吞 | 已完成本次支持范围；源码审查及容器校准通过，未支持主来源明确列为限制 |
| U03 | 校准缺失、默认零和字段换算 | 取消未知保留、SDK零调用不误报漏费、缓存不重复、reasoning不重复、缺细分不补零 | 已完成本次范围；反例随313项容器校准通过，未知字段保留 |
| U04 | Docker内断网只读执行冻结旧材料 | 不产生模型请求；输入hash不变；保留逐观察证据引用、分类数量和已知部分汇总；精确清理容器 | 已完成本次010截点；退出0、健康终态、清理通过，无模型/SQL/预算库挂载 |
| U05 | 对齐I26费用文档 | 请求数与token分别报告，未建立关联不报覆盖率，未知部分和金额限制保留；旧截点不覆盖 | 已完成本次010截点；费用文档和安全JSON同步，I26整体仍未完成 |

这些校准是计量工具的验收，不是重新评价候选能力。历史缺失的末尾计数即使以后重跑成功，也不会被新实验覆盖。
