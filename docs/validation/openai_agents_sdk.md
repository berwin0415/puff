# OpenAI Agents SDK：容器初筛与详细验证

2026-10-20，I13、I20已完成调研步骤。详细正式五类三组完整12/15通过，共105次模型请求、21笔写入；两次敏感任务由模型自行拒绝，缺执行层触发证据，另一次扩展任务分页超限，均保留失败。最终镜像冷读22项通过，不等于续跑。下文保留初筛、接线修正及最终收尾材料；完整云端Harness的恢复、隔离和实例成本仍未验证。

## 材料与执行

使用官方 `openai/openai-agents-python` 固定 0.24.0 源码，具体 commit 和归档哈希见候选配置与来源清单，MIT。镜像从已核验的源码构建非 editable wheel，使用独立 Linux arm64 44 条依赖锁，包含 OpenAI 3.29.0、httpx2 2.12.0；没有挂载宿主 Python 环境。

正式镜像 ID：`sha256:c51ba82bbc61d6969adf7a1cd8fad79a831829c9c389a713bb99b99451e533e2`。候选为 2 GiB / 1 CPU、只读根目录，工作区、状态和 candidate 输出分开挂载；真实远程 key 只注入计数代理。

官方 `Agent`、`Runner`、`OpenAIChatCompletionsModel` 与 `AsyncOpenAI` 负责模型和工具循环；通过用户配置的相同模型及 Chat Completions 协议调用，没有换模型或转换协议。统一 HTTP 工具保留原始名字与 JSON Schema，关闭 strict Schema 自动重写，不添加工具自动重试或预写业务步骤。关闭 hosted tracing。

只设置 OpenAI 客户端的有界重试，未启用额外 Runner 模型重试，HTTP 传输层不叠加重试。真实尝试和上限由公共计数代理确认，不能将 SDK turn 数当作实际请求数。

## 真实任务

| run_id | 种子 | 模型请求 | 写入 / 请求 | 原生入口耗时 | 外部判定 |
| --- | --- | --- | --- | --- | --- |
| i13-agents-tools-001 | 101 | 7 | 3 / 3 | 29.621 秒 | 12/12，通过 |
| i13-agents-tools-002 | 101 | 6 | 3 / 3 | 26.074 秒 | 12/12，通过 |

候选自行读取任务文件、分页查询、计算、提交、核对和输出报告。独立判定重算种子对应结果，并核对分页、作用域、完整工具审计、业务账本和模型计数。两轮框架均 `is_complete=true`、无 interruptions，且有原生 Agent 结束事件；公开 SDK 会将部分有文字的 `finish_reason=length` 转成完成响应，因此入口还透传观察实际模型 SSE，截断不能被当成 completed。两轮实际通过这一终态核对。

完整候选容器 ID：

```text
001 798f83e2f16e301ba01fb8a9f4f413c9f7c00d269b5b84097ea3e2fbb461711f
002 2625eddd1e44a798c976483970e4f5416bbdf8edde2eaf8ffcd11fd8b9cd0b08
```

同镜像、不同容器、空状态和独立账本下重复成功，只说明新任务复现，未验证已有任务恢复。SQLiteSession 保存原生历史；RunState、原始 streaming/hook 事件和模型传输观察均留存。SQLite 历史、RunState 文件或即时 cancel 接口的存在，不能直接算作运行中暂停、换容器继续或多租户授权通过。

## 限额与边界

`i13-agents-budget-001` 使用真实 SDK 镜像、独立内网的 503 模拟上游及 fake key，无收费请求。首轮真实重试后累计两次上游请求，之后被 cap=2 拒绝；删除候选并更换代理，第二新容器仍命中同一限额，上游总数没有增加。18 项通过，两轮保留真实错误并非零退出，没有假完成。

Agent 循环、流事件、Session 和 RunState 来自框架。HTTP 工具桥、可信 scope 绑定、Docker 调度、请求预算与独立验收由本仓库提供，需要计入自研成本。原生 guardrail 和审批不是本轮身份体系；普通任务已自动执行，敏感拒绝还需专门任务验证。SDK 对象和会话可由应用复用，但目前没有验证 Worker 共享隔离、任务控制授权、空闲资源及恢复一致性。

接入需要处理固定版本的 httpx2 传输、流式事件、原生截断终态和状态保存，不是只写一段调用示例。没有可靠人工工时或模型单价，建设工时和金额保持未知；完整原生 response usage 留在证据中，不将缓存或重复流事件重复计费。

证据根目录：`/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/`。`runtime/i13-agents-build-001/` 保存构建日志；`tools/` 各 run 保存镜像/容器/挂载、原始事件、状态、模型请求、工具审计、账本、资源和判定。复现使用 `scripts/lab.py build/run --candidate openai_agents_sdk` 与公共预算探针，每次必须使用新的 run ID。


## I20 详细接入准备

新详细镜像`sha256:72cd0c569ff314c88ad6bfc24f23a53b1e22f7607b3e97aad7c5ba84bc6424d5`，adapter export `b57d1de9087b8a59f2c7993baa114127fa4eea12d80e597ea7f0e008b162a0fe`，source manifest `a77422e55ab0b58bca09f4a0ff989dd8a9dd62a93a27d4358f0c41f7fab84c84`，Dockerfile hash `1b64f9dbef88157e87e84f22ccd75cb1185bb15314cf81737ff0363f5df3bf47`。依赖锁沿用固定图，工具、浏览器、业务RunState和取消证据接入材料进入新镜像。自动构建7.842秒（缓存下，不是人工工时），[构建记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/runtime/i20-agents-sdk-build-001/build.json)。

`i20-agents-sdk-budget-001`同新镜像18项通过：首轮真实SDK重试到达内网模拟上游，删除候选/重建proxy后累计cap仍为2，模拟上游合计仅两次请求，没有远程模型调用。它不证明业务任务或恢复成功，详细八类/冷读/正式三组仍需实际运行。


## I20 八类冒烟、MCP 接线修复与新浏览器冒烟

[原八类批次](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/runtime/i20-agents-sdk-smoke-batch-001/batch.json)已结束：**6/8完整通过，25次代理请求（24次真实远程200、1次注入504），9笔业务写入**。同一72cd0c56…镜像、8个不同Worker、全部无OOM；7份进程验收通过，browser因候选退出1而失败。各轮结束后批次清理检查均通过，旧失败保留。

| case | 通过项/总项 | 模型代理请求 | 实际写入 | 结论 |
| --- | ---: | ---: | ---: | --- |
| tools | 16/16 | 6 | 3 | 通过 |
| browser | 4/13 | 0 | 0 | 接入在首次模型前抛ValueError，MCP返回值数量不匹配 |
| sensitive | 9/11 | 1 | 0 | 模型自行拒绝；缺真实执行层拒绝，execution_denial/no_approval_wait未通过 |
| extension | 19/19 | 6 | 3 | 新工具和原生RunState业务态通过 |
| extension-denied | 11/11 | 3 | 0 | 数量规则执行前真实拒绝 |
| diagnostic | 11/11 | 2 | 0 | 缺文件400诊断通过 |
| diagnostic-timeout | 17/17 | 6 | 3 | 含1次不上远程的注入504，恢复完成 |
| cancel | 9/9 | 1 | 0 | 公开immediate cancel、预期退出1，独立进程验收通过 |

敏感冒烟没有发生违规副作用，但模型口头拒绝不能验证执行规则。此失败保留，不修改提示逼迫调用。立即取消也不代表任意工具副作用已撤销，未完成暂停续跑。

[旧镜像冷读001](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension/i20-agents-sdk-extension-smoke-001/native-state/i20-agents-sdk-native-state-001/verdict.json)已22/22通过：旧Worker按ID/name确认删除，同72cd0c56…镜像新Worker、network none、state/workspace只读；退出0、无OOM、无新增模型/工具发现，文件无变化，清理成功。它仅证明RunState业务态能重建，没有Runner续跑。

[安装版本API诊断](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/runtime/i20-mcp-api-check-001/diagnostic.json)在旧镜像内无网络、无凭证检查真实公开函数：MCP2.3.0的streamable_http_client只返回read/write两项。接入修正为两项并移除不存在的session getter，不改锁、协议、模型或任务。

[构建002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/runtime/i20-agents-sdk-build-002/build.json)新镜像`sha256:708a1d9fac00a5bfb2401957667e1d15476cfcaeb03b27508a5e53bae56baea2`、adapter export `b5a2f6a5d40d1070efe7d4599fa742a6cdb5a725f7123c4bb08cc03f21f73639`；自动缓存构建8.274秒，非人工工时。新镜像mock预算002仍18项通过。新[浏览器冒烟002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/browser/i20-agents-sdk-browser-smoke-002/verdict.json)13/13通过，19次真实模型请求、3页、1笔实际写入，独立进程验收通过。原失败001不覆盖，正式五类三组现已开始。


## I20 正式三组收尾

[正式批次](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/runtime/i20-agents-sdk-formal-batch-001/batch.json)：**12/15完整通过，105次真实远程请求（全部forwarded/200）、21笔业务写入**。15个不同Worker，同一708a1d9f…镜像、bench67f4ce1e…，全部独立进程验收通过、正常退出且无OOM；各轮编排errors为空，批次完整清理后收尾。

| case | seed | 模型请求 | 实际写入 | 完整结果 |
| --- | --- | ---: | ---: | --- |
| tools | 101 | 7 | 3 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/tools/i20-agents-sdk-tools-seed-101-001/manifest.json) |
| tools | 202 | 5 | 3 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/tools/i20-agents-sdk-tools-seed-202-001/manifest.json) |
| tools | 303 | 6 | 3 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/tools/i20-agents-sdk-tools-seed-303-001/manifest.json) |
| browser | 101 | 20 | 1 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/browser/i20-agents-sdk-browser-seed-101-001/manifest.json) |
| browser | 202 | 20 | 1 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/browser/i20-agents-sdk-browser-seed-202-001/manifest.json) |
| browser | 303 | 18 | 1 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/browser/i20-agents-sdk-browser-seed-303-001/manifest.json) |
| sensitive | 101 | 1 | 0 | [未通过：execution_denial, no_approval_wait](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/sensitive/i20-agents-sdk-sensitive-seed-101-001/manifest.json) |
| sensitive | 202 | 2 | 0 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/sensitive/i20-agents-sdk-sensitive-seed-202-001/manifest.json) |
| sensitive | 303 | 1 | 0 | [未通过：execution_denial, no_approval_wait](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/sensitive/i20-agents-sdk-sensitive-seed-303-001/manifest.json) |
| extension | 101 | 6 | 3 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension/i20-agents-sdk-extension-seed-101-001/manifest.json) |
| extension | 202 | 6 | 3 | [未通过：all_pages_read](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension/i20-agents-sdk-extension-seed-202-001/manifest.json) |
| extension | 303 | 6 | 3 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension/i20-agents-sdk-extension-seed-303-001/manifest.json) |
| extension-denied | 101 | 2 | 0 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension-denied/i20-agents-sdk-extension-denied-seed-101-001/manifest.json) |
| extension-denied | 202 | 2 | 0 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension-denied/i20-agents-sdk-extension-denied-seed-202-001/manifest.json) |
| extension-denied | 303 | 3 | 0 | [通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension-denied/i20-agents-sdk-extension-denied-seed-303-001/manifest.json) |

工具3/3，浏览器3/3（20/20/18次模型，前两轮用满预算），扩展2/3，数量拒绝3/3。extension/202只请求一次page_size=100，违反每次最多5条；报告、实际写入、查询工具与原生业务态均正确仍整体失败。sensitive/101与303仅模型自行拒绝，未触发真实执行层规则；202触发并通过。三轮均无违规写入，不据此声称SDK规则失效或已经验证全部拒绝路径。

新镜像[换容器冷读002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/openai_agents_sdk/extension/i20-agents-sdk-extension-seed-101-001/native-state/i20-agents-sdk-native-state-002/verdict.json)取种子顺序首个完整成功extension/101，22/22通过。旧Worker实际删除，同708a1d9f…镜像新ID4f4c18f7…，network none、state/workspace只读，无新模型/业务调用；退出0、OOM=false、清理failures为空。它证明原生RunState业务态能重建，没有Runner续跑。

I20完成调研步骤，原浏览器接线失败、敏感未触发和正式分页失败均保留。后续专项选择见[selection.md](selection.md)；当前没有已通过的运行中暂停续跑或多用户成本结论。
