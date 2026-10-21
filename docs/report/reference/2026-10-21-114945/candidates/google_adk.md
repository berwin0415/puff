# Google ADK 候选附录

资料截点：**2026-10-21 11:49:45，Asia/Shanghai**。本候选已完成 I12 初筛，后续详细与专项未执行。比较口径见[分组结果](../02-results.md)，运行原件见[证据索引](../evidence/index.md)。

## 测试对象、版本与入口

对象为 Apache-2.0 开源 `google/adk-python`，版本 `2.12.0`，固定提交 `365084db7b14e796f36ad2929f987e8d83874961`。使用官方 Agent、`Runner.run_async`、BaseTool、LiteLlm 与 SqliteSessionService，在本地 Docker 内执行，不要求 Vertex 托管运行时。

安装 ADK 基础依赖及本轮选定的 LiteLLM 依赖，没有安装包含 Kubernetes、其他框架与云集成的完整 extensions 集合。Python/Linux 的独立 76 条目锁与实际源码 wheel 保留来源核验；依赖条目数本身不代表内存或维护难度。[接入说明](../../../../../adapters/google_adk/README.md)记录安装与入口。

## 接入与能力来源

循环、原生事件、官方模型适配和 SQLite session 来自 ADK。工具桥保留原名称、说明、schema 和参数，可信 scope、运行编排和外部业务验收由本仓库提供。模型经官方 LiteLLM 的 Chat Completions 路径接入，未转换协议或以托管服务替代本地 SDK。

SessionService 的 app/user/session 键是状态组织方式，不是用户认证。当前 screening 身份与每轮独立目录只能支持本次任务绑定，不能证明多租户共享 Runner 隔离。

## 分组实测与失败

同一镜像、两个全新容器的 seed101 工具任务均 **12/12 完整通过**；模型请求为 7/6 次，各有 3 次写入请求和 3 笔实际写入。原生 STOP、事件流正常耗尽及独立报告/分页/账本检查均有证据。第二次是新任务复现，不是恢复已有任务。

模拟累计限额 18 项通过。候选确实发出了两次到达模拟上游的请求，但没有采到原生 retry 事件，不能把实际重试说成有 ADK 原生 retry 日志。曾因上一轮未清理而被编排启动保护拒绝，发生在模型和候选启动前；LiteLLM 缺 botocore 的提示属于未使用的 AWS 路径，也不是本轮模型接入失败。详见[初筛报告](../../../../validation/google_adk.md)。

## 维护投入、适用条件与未知项

接入需要维护 Linux 依赖组合、BaseTool 桥、可信身份、原生完成/异常判定及运行存储。真实人工工时未知；不能把构建锁齐备理解为生产运行管理已经完成。

本轮表明它可以通过官方适配器使用选定非 Google 模型完成共同工具任务，适合继续评估本地 Runner/SessionService 路线。SQLite 文件与历史不证明中断任务续跑，但官方 `ResumabilityConfig` / invocation_id 也是待验证的原生线索，不能反过来断言 ADK 只能存历史。浏览器、运行中控制、恢复、副作用一致性、后端鉴权和实例成本均未实测。暂缓深入属于投入安排，见[初筛安排](../../../../validation/screening.md)。
