# OpenHands SDK：容器初筛

2026-10-20，I15调研步骤已完成。固定镜像、两个全新容器和空状态，公共工具任务均通过12项外部判定；模拟重试与跨容器累计限额18项通过。尚未验证浏览器、运行中暂停恢复或共享Worker隔离。

## 材料及实际入口

官方`OpenHands/software-agent-sdk` 1.56.0，commit `33d8aa29c3078d074799e2fbf5e847aeb2d71360`，核心MIT。使用该源码构建非editable SDK；125条Linux arm64依赖来自官方uv.lock的闭包，require-hashes、binary-only安装并核对。Agent Canvas来源独立记录，未安装或执行，不把托管/企业功能纳入本次证据。

正式镜像：`sha256:b296d40f8dccfab4594938eab71d5483a97ba976796df886a7ee6b683c5fdfa3`。2 GiB / 1 CPU、只读根目录；官方`LLM → Agent → Conversation.arun()`运行于候选容器，local workspace及独立持久目录，无内置terminal或Agent Server。选定模型通过原生Chat Completions调用；真实key仅模型代理持有。

八个HTTP工具用官方ClientTool及真实ToolExecutor接入，参数schema保留原定义，不代写业务步骤，不重试工具HTTP。NeverConfirm使普通授权业务自动执行；敏感拒绝仍由公共执行服务提供，不能称作OpenHands原生规则验收。仅额外保留官方FinishTool作为原生完成信号。

## 真实工具任务

| run_id | 种子 | 模型请求 | 写入 / 请求 | 候选入口耗时 | 外部结果 |
| --- | --- | --- | --- | --- | --- |
| i15-openhands-tools-001 | 101 | 6 | 3 / 3 | 23.303 秒 | 12/12，通过 |
| i15-openhands-tools-002 | 101 | 7 | 3 / 3 | 26.402 秒 | 12/12，通过 |

两轮均为原生FINISHED，无pending action。最后模型响应为tool_calls，随后原生finish工具及成功observation明确完成，因此没有伪造finish_reason=stop。length、paused、error、缺少完成证据和活动超时不作为完成。独立验收重新计算种子目标、分页上限、报告和三笔真实写入，未凭最终文本判通过。

候选容器ID：

```text
001 0b1a2f4f1ba9f79dc03b8b57e8f9a195512769fdab7094ace3aa804d39590f78
002 b3e3d4c4f4b83d7f115cccb1280da316bfb4acfd4d26fbcc823deca82075159b
```

仅说明新任务可复现，不能把两个新容器等同同一未完成任务恢复。原生conversation持久状态、动作/observation、模型完成与重试记录均留存。

## 接入问题、限额及边界

源码复核发现官方Agent即使NeverConfirm也会请求risk schema；薄接入改为保留原始业务schema，不因该提示抛错。首次`budget-001`又在模型调用前发现SDK基类自动把派生HttpTool命名为http，导致八个工具重名；0模型请求，失败保留。明确恢复实例name属性，同时检查实际导出name和schema后重建第三版镜像。

正式镜像的`budget-002`使用内网503模拟上游和fake key。SDK num_retries实际是总尝试数，配置为统一max_retries+1；底层LiteLLM传输不叠加重试。两个候选及两个代理容器共仅两次上游请求，随后拒绝且计数未清零，18项检查通过，无收费请求。

部分可恢复AgentErrorEvent可能使当前入口的pending观察比原生状态更保守；参数错误后的纠正路径仍须诊断专项核对，不能据初筛宣称已覆盖。源码中有公开pause和持久Conversation路线，尚未在本实验验收，不能写成不支持。当前入口拒绝已有state，恢复需单独接入和验证。

本方案不必部署整套Canvas/Agent Server，不能仅因项目定位代码Agent或125条依赖就断言不适合通用任务、内存更高。相反，本次只覆盖同一基础工具任务；完整云端运行管理和暂停资源收益仍未知。模型价格表没有当前模型，SDK也提示无法估价；金额保持未知，保存原生usage供后续去重汇总。

HTTP桥、身份绑定、Docker编排、模型预算和外部判定由本仓库提供，应计入自研投入。没有可靠人工工时，不将自动运行秒数倒算开发工时。

证据根：`runs/2026-10-20/openhands/`。`runtime/`保存三个构建版本，`tools/`保存失败探针、正式预算和两轮真实任务。复现入口为`scripts/lab.py build/run --candidate openhands`及公共预算探针；每次使用新run ID。材料说明见[adapter README](../../adapters/openhands/README.md)。
