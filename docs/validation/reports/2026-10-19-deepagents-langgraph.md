# Deep Agents + LangGraph 本地验证报告

## 运行范围

- 候选：Deep Agents + LangGraph
- 运行日期：2026-10-19
- Deep Agents 来源：官方 archive ref `0089e635dce525055272883b53bc222f385beac1`
- Deep Agents archive SHA-256：`2b0cb8cd05f7faeb8e8745845958056a9e974658fc85f4682643874c99d8a548`
- LangGraph 来源：官方 Git checkout `98043c37abc60374030f960a03a6e0d4c0112643`
- 许可证：两个项目仓库均为 MIT
- 外部源码根目录：`~/harness-lab-sources/`

## 来源与依赖验证

Deep Agents 的历史 pinned ref 在本机通过官方 GitHub archive 下载并解包，
源码目录包含 `libs/deepagents/pyproject.toml`（版本 `0.7.28`）和 MIT
LICENSE。LangGraph checkout 工作区干净，`libs/langgraph/pyproject.toml`
版本为 `1.2.16`，HEAD 与 pinned commit 一致。

候选 Python 3.12 环境使用 Deep Agents 自带 `uv.lock` 安装基础依赖，再以
LangGraph pinned checkout 的 `langgraph`、`langgraph-prebuilt`、
`langgraph-checkpoint` 和 `langgraph-sdk` 可编辑包覆盖运行时版本。最终导入
验证通过：`deepagents 0.7.28`、`langgraph 1.2.16`、`langchain 1.4.6`、
`langchain-openai 1.7.2`、`langgraph-checkpoint-sqlite 3.1.2`。

## 最小运行

适配器 `adapters/deepagents_langgraph/` 通过外部 venv 的 Python 启动
`worker.py`，使用 `create_deep_agent`、SQLite checkpointer 和自定义
`fixture_lookup` 工具。远程模型配置来自仓库外 `.env`，密钥不进入事件或文档。

本次真实 API 运行成功：

```text
candidate: deepagents_langgraph
model: deepseek-v4-flash
base_url: http://api.dbh.baidu-int.com/v1
prompt: Call fixture_lookup with key alpha, then reply with only the returned value.
answer: fixture-alpha
returncode: 0
normalized_event_count: 9
run_id: 52921584-baec-47cf-a4eb-acd9cb041781
```

脱敏运行清单保存在外部 artifact 根目录：
`~/harness-lab-data/runs/2026-10-19/deepagents_langgraph/52921584-baec-47cf-a4eb-acd9cb041781/manifest.json`。

事件顺序为 `process_started`、`graph_started`、五个 `graph_update`、
`graph_completed`、`process_exited`。其中 `graph_update` 能看到模型节点、
工具节点和工具结果，说明候选 loop 与工具路由已接入统一事件摘要。

## 非审批暂停与跨进程恢复

云端 Harness 的暂停不需要人工审批。主机制是模型节点安全点：

- `pause_after_model=True` 时，worker 用 `interrupt_after=["model"]` 重新编译图；
- 第一次进程在模型节点结束后输出 `pause_requested` / `graph_paused`，
  checkpoint 写入 SQLite；
- 新进程使用同一 `thread_id`，以 `Command(resume=True)` 继续；
- 恢复进程输出 `graph_resumed`、`graph_update`、`graph_completed`，最终答案为
  `fixture-alpha`。

真实运行 run_id：
`deepagents-pause-d25e50a2-caec-4826-9ef3-6a566e6c93cb`。复现脚本：
`scripts/run_deepagents_pause.py`；实时测试需设置 `RUN_LIVE_MODEL_TESTS=1`。

边界：暂停发生在模型节点结束后的安全点，不是模型调用中途的即时抢占；
cancel、实时输入和容器级恢复尚未验证。审批中断（`interrupt_on`）仍作为可选
能力保留，但不是云端 Harness 的必要条件。

## 当前结论

已验证：

1. 官方来源和 MIT 许可证可复核；
2. Deep Agents 与 LangGraph 在 Python 3.12 外部环境可导入；
3. OpenAI-compatible 远程模型 API 可被 `ChatOpenAI` 调用；
4. Deep Agents 可调用确定性工具并返回结构化事件；
5. LangGraph SQLite checkpoint 后端可初始化并产生候选状态文件；adapter 可通过 `worker.py --mode inspect` 离线读取 checkpoint，无需模型调用或 API key。
6. 非审批模型安全点暂停与跨进程 resume 已通过真实模型验证；审批中断为可选能力。

尚未验证：统一 Playwright 浏览器轨道、真实暂停/恢复、取消、人工审批和
Runner 重启后的候选状态恢复。离线 checkpoint 可读不等于任务可 resume；
当前 Worker 仍是一次性进程。当前 worker 为一次性进程，生命周期能力需在
后续专项中增加长连接或可恢复 worker；不能把 SQLite checkpoint 的存在直接
等同于 pause/resume 已通过。
