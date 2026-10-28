# 暂停、释放计算资源与换容器继续

更新：2026-10-20。I22 已完成调研步骤。**六条真实暂停续跑链已结束；两路线原生暂停和恢复各3/3，DSH完整业务3/3（101为追加复核），Deep Agents完整业务1/3，另外两条分页规则失败。** 设计见 [pause-recovery-design.md](pause-recovery-design.md)，总进度以 [实施计划](../../IMPLEMENTATION_PLAN.md) 为准。

本项要回答：任务已经做了一部分，能否安全保存进度、删掉旧 Worker，等待十分钟后用新 Worker 接着做，且不重复写入、不重置模型预算。测试不要求恢复浏览器现场。

## 已完成的接线和校准

| 项目 | 本轮证据 | 可以得出的结论 |
| --- | --- | --- |
| Deep Agents 生命周期接线 | 独立上下文 `external/build-context/deepagents_langgraph-pause`；镜像 `74215097…`，接入摘要 `bfd8deda…` | SIGUSR2 请求公开 drain；保存真实 checkpoint，正常关闭后发布私有索引；B 校验原状态后以 `None` 继续，不带 checkpoint_id、不重发提示。真实业务组合待验 |
| Deep Agents 原生无模型校准 | `deepagents_langgraph/runtime/i22-native-runtime-check-001`；A/B PID 7/9，均正常退出，容器无 OOM | 固定镜像真实 LangGraph、RunControl、GraphDrained 和同步 SQLite saver 支持该条新进程续跑；已完成节点未重做。不是 create_deep_agent 业务任务通过 |
| DSH Goal 接线 | 独立上下文 `external/build-context/deepseek_harness-pause`；镜像 `d62c3c01…`，接入摘要 `a5935860…` | 单独加载官方 Goal 及 checkpoint 插件，不改上游 Agent loop；工具面需无模型校准后固定，真实组合待验 |
| DSH 适配边界校准 | `deepseek_harness/runtime/i22-goal-boundary-check-001`，42/42，无网络、无模型凭证，正常退出且无 OOM | 验证绑定、拒绝、关闭和恢复前缀检查；测试用合成原生 API，不能代替真实 DSH 暂停恢复 |
| DSH 无模型工具面 | `deepseek_harness/tools/i22-deepseek-baseline-seed-101-001/surface-calibration` | 固定镜像真实空 Agent 启动，实际11个工具；代理前后调用均0，正常退出、无OOM。工具面由独立宿主记录接受后再发业务绑定 |
| 宿主编排边界校准 | `infrastructure/runtime/i22-host-check-001`，36/36（含17项bench重叠检查）；镜像 `25838b47…` | 固定镜像、逐文件挂载、预算耗尽、失败仍清理及未知Docker错误拒绝等边界通过 |
| 公共 bench 校准 | `infrastructure/runtime/i22-bench-check-002`，720/720；镜像 `2d042168…` | 包含新增暂停判定与 DA 边界检查；仅确认判定器及公共组件，不算候选能力通过 |
| Goal 独立重放与采样增量校准 | `infrastructure/runtime/i22-grader-check-001`，327/327相关检查；镜像 `2d067a36…` | 持久 GoalSpec 与进程内 GoalView 分开核对；容器正常退出的采样竞态须按精确 ID 另行证实，缺失数据不补零 |
| 代理替换与宿主边界增量校准 | `infrastructure/runtime/i22-grader-check-004`，68/68；镜像 `fb6c800f…` | 独立检查代理实际镜像、预算挂载、额度、环境摘要、退出删除及短锁切换；未降低采样间隔要求 |
| 无模型代理替换校准 | `infrastructure/runtime/i22-proxy-restart-check-004` 及其 `independent-validation/` | 替换7.53秒、最大采样间隔2.50秒、采样错误0、模型调用0；独立固定bench无网络只读复核通过，无OOM，精确对象清理完成 |
| 宿主/容器时钟因果增量校准 | `infrastructure/runtime/i22-grader-check-005`，74/74；镜像 `a04a2e8a…` | 新链以同宿主单调时钟的完整工具、原生和控制事件前缀证明先读取、再发信号；不比较宿主与VM的UTC，不增加时间容差。旧链保持原判定 |
| 最终checkpoint与恢复会话头增量校准 | `infrastructure/runtime/i22-grader-check-008`，77/77；镜像 `53fad51f…` | 完成的LangGraph不得残留任务数组；DSH会话头只接受固定JSONL formatter补入的默认delegationDepth=0，其他字段、事件前缀及seed marker仍严格核对 |

原始证据根为 `runs/2026-10-20/`，表中路径相对此目录。完整镜像 ID、构建日志与退出状态在各记录中。

已保留两类实施失败：DSH 首次构建因复制上下文未保留符号链接而被来源校验拒绝，修正复制后重建通过；首次新 bench 检查 700 通过、20 失败，原因是旧冷读测试加载器未导入新增 helper，补全加载器后 720 通过。两者均未绕过来源或能力验收。

## 运行方式和能力边界

宿主入口是 `scripts/run_pause_recovery.py`，只编排 Docker、发送一次暂停信号、记录容器和资源。`runner/pause_recovery.py` 在固定 bench 镜像内做一次 prepare、绑定、安全点检查、等待期监控、恢复绑定和最终业务评分。A/B 各有独立候选输出目录，共用原 state、workspace、业务账本和 calls.sqlite/run.token。

候选不能写 `trusted/`、预算库或评分文件。恢复请求没有 prompt；不允许失败后退回新任务。宿主独立记录活动中的原生事件前缀、已完成写入、旧容器删除和新 ID。等待阶段每两秒记录预算、工具审计及全部实验服务资源，至少一条正式链重建代理，预算不得归零。

DSH 只接受原会话历史加恢复构造器合法产生的一个 `session/end-seed` 标记，不删除或伪造该标记。Deep Agents 分别检查指定 checkpoint 和当前 head，真正执行时不带 checkpoint_id，避免把历史回放当成续跑。

正式实验先跑同变体 seed 101 的不暂停基线，再完成 101/202/303 的独立暂停链。每条链分别报告原生续跑、业务正确性、预算和资源，所有失败保留。故障突然退出、多用户共享 Worker 和总实例成本分别在 I23–I25 验证。

六条不暂停基线已完成，共49次模型请求、18笔实际写入，Worker均健康退出且无OOM：

| 候选 | 101 | 202 | 303 |
| --- | --- | --- | --- |
| Deep Agents | 5次请求；报告/3笔写入正确，但请求每页100条超过5条限制，完整任务失败 | 6次请求，完整通过 | 6次请求，完整通过 |
| DSH Goal 变体 | 10次请求，追加复核通过 | 9次请求，追加复核通过 | 13次请求，追加复核通过 |

DSH原评分器误把持久事件中的GoalSpec当成完整GoalView，错误要求它包含进程内activation。已按固定上游strict fold独立重放完整物理会话，再核对公开live GoalView和原生completed终态。新固定bench在无网络、原证据只读条件下，对三条原链追加 `reviews/durable-goal-001/`，均通过，原verdict及哈希保持不变；未重跑模型、未覆盖旧失败。Deep Agents 101仍保留真实分页失败，没有调整提示、标准或选成功重跑。

seed101的两次实施中止均保留：`i22-deepagents-pause-seed-101-001` 已drain但宿主不识别Docker小写的“no such object”，删A确认中止；`...-002` 已删A，但bench把实际序列化的PregelTask数组误按字典验收而拒绝。两者均未进入600秒等待，不计作恢复通过。已补精确对象/大小写与错误文本混合反例、短bench退出保留，以及固定NamedTuple七字段格式校准。

`infrastructure/runtime/i22-grader-check-002` 的40项宿主检查通过；`...-003` 在镜像 `30e628b5…` 内99项相关检查通过，并以network-none/原证据只读方式核对第二条真实保存点通过，未执行模型或改写原证据。没有放宽错误、interrupt或原checkpoint绑定检查。

`i22-deepagents-pause-seed-101-003` 已完成安全暂停、删A及600秒预算/工具静默监控，代理也实际重建；但重建期间宿主持采样锁太久，资源采样最大空档21.54秒，超过5秒标准。恢复绑定正确拒绝，B未启动，原失败保留。

修复改为先创建并检查新代理就绪，再短锁停止、删除旧代理并改名；两代理暂时共存的资源仍被计入。旧代理的137退出明确记为外部停止，不算自然健康退出。保留同一个预算库、token、额度和完整环境摘要；不输出密钥。无模型校准001仍有10.14秒空档而失败；002采样通过但原清理漏了手工容器，另存精确ID清理补记，不改原失败；003和最终004均通过。004另经固定bench独立无网络复核。

## 真实暂停链结果（逐条追加）

| 候选 / 种子 / chain尾号 | 原生暂停、删A、600秒后B继续 | 业务结果 | 预算、进程及清理 |
| --- | --- | --- | --- |
| Deep Agents / 101 / `i22-deepagents-pause-seed-101-004` | 通过；新B同镜像、原checkpoint、空输入，等待期间重建代理；已完成写入未重发 | 报告和写入正确，但每页100条违反≤5规则，整链失败；同种子基线也失败，不挑成功重跑 | 全链5次模型请求、32.77秒累计活跃；Worker健康、无OOM、清理完成 |
| Deep Agents / 202 / `i22-deepagents-pause-seed-202-001` | 17项中原生续跑相关16项通过；安全暂停、删A、600秒、重建代理、原checkpoint和空输入继续成立 | 报告与三笔写入正确，但分页审计未满足每页≤5且查全的规则，整链失败；不重跑挑成功 | 全链6次模型请求、35.73秒累计活跃；无编排错误，Worker健康、无OOM、清理完成 |
| Deep Agents / 303 / `i22-deepagents-pause-seed-303-001` | 17项全部通过；安全暂停、删A、600秒、重建代理、新B沿原checkpoint空输入继续 | 完整业务通过，报告/分页正确，三笔写入且已完成写入未重发 | 全链6次模型请求、39.96秒累计活跃；Worker健康、无OOM、预算连续、清理完成 |
| DSH / 101 / `i22-deepseek-pause-seed-101-002` | 追加离线全链复核通过；同镜像新B沿原Session/Goal继续，无新prompt/followup，原生正常完成 | 三笔写入与报告、分页均正确；已完成写入未重发 | 全链12次模型请求、41.70秒累计活跃；600秒等待中重建代理，预算不清零；Worker无OOM，清理完成 |
| DSH / 202 / `i22-deepseek-pause-seed-202-001` | 17项全部通过；安全暂停、删A、600秒、新B沿同Session/Goal继续，期间重建代理 | 完整业务通过，三笔写入与报告/分页正确；已完成写入未重发 | 全链9次模型请求、39.38秒累计活跃；预算不清零，Worker健康、无OOM，清理完成 |

六条链只证明这些固定样本。I22调研步骤已完成，不能写成所有业务都通过；故障突然退出和多用户能力仍另验。

DSH `i22-deepseek-pause-seed-101-001` 已实际暂停、正常关闭并删A，但旧判定比较了不同来源的UTC：工具容器finish为10:36:28.605770，宿主sent_at为10:36:28.603829，相差约1.94毫秒，因此在等待前中止。不能靠增加时间容差或事后补造观察记录使原链通过。新宿主记录完整前缀及单调时间，独立bench校验写入/活动已被读取、信号前尚无pause request、原生handler request随后出现；新74项校准通过。旧DSH保存点另在network-none/原材料只读条件下单独核对原生Goal暂停、blocked turn/end和关闭通过（`i22-dsh-pause-audit-001`），仅作原生暂停诊断，不是完整trigger或续跑通过。

新链101002完成后，原评分仅在会话头完全相等一项失败，其他16项通过。固定上游 `packages/session/session-persistence-jsonl/src/format.ts` 的 `toHeaderLine/fromHeaderLine` 会把A内存中省略的delegationDepth写成0并在B读回；原事件1481条与B前缀完全相同，仅多一个合法seed marker。已修正为只接受这一个官方默认值物化，并校准身份、cwd、父会话及非整数/错误深度的反例。`reviews/restored-header-002/` 用新固定bench、network-none、原材料只读，完整重验17项通过；原verdict及其SHA保留，未重跑候选。复核001错误挂入含old的数据根，被环境检查拒绝；未加载归档，改为五个当前目录逐项只读挂载后002通过。构建006/007是增量准备，未作为校准通过证据；正式增量校准为008。

最后一条DSH303 `i22-deepseek-pause-seed-303-001` 已完整17项通过：14次模型请求、48.365396秒累计活跃、3次写入请求/3次实际写入；无编排错误、无OOM、清理完成。两路线六条完整链共52次模型请求、18笔写入；六基线49次及实施中止15次另计。合计I22这些显式链为116次模型请求，离线复核未增加调用。

## 分阶段资源观察

[可复现汇总](pause-resource-summary.json)由 `scripts/summarize_pause_resources.py` 只读取六条已结束链的原始采样、编排和清理元数据生成，没有重新评分或运行候选。所有阶段采样错误均为0。下表是每条链相应阶段的样本均值范围，不是时间加权平均、CPU秒或生产费用。

| 阶段 | Deep Agents | DSH | 解释 |
| --- | --- | --- | --- |
| A活跃的Worker内存 | 93.13–96.96 MiB | 121.88–154.84 MiB | 各样本仅Worker，附带服务另计 |
| B活跃的Worker内存 | 81.76–106.28 MiB | 141.20–151.39 MiB | 恢复后阶段，样本少，不能当生产容量 |
| 暂停等待的Worker数量和内存 | 0个、0 MiB | 0个、0 MiB | 真实删除Worker，未以冻结进程代替释放 |
| 暂停等待的共享服务内存 | 58.63–59.04 MiB | 58.12–58.59 MiB | Fixture、工具和计数代理仍在运行 |
| 暂停阶段总内存（含监测/短时bench） | 78.82–79.86 MiB | 78.23–78.67 MiB | 不能把实验监测器开销算给候选本身 |

这证明可释放单任务Worker的计算资源，但不意味着暂停后总成本为零。等待期间仍保留原生状态、工作区、预算和实验共享服务；存储费用及生产共享服务摊销未测。阶段的原始样本时间范围约601–603秒，完整600秒等待的能力判定仍引用原独立verdict，汇总不替代它。

同一宿主时钟记录的“发出暂停信号 → 观察到A退出”分别为Deep Agents 2.38–2.52秒、DSH 2.56–2.86秒；“B启动 → 观察到退出”为12.87–21.68秒、19.02–25.14秒。两者包含宿主轮询和Docker检查，前者是收束退出的观察耗时，不能当成精确原生安全点时延；后者不是产品API唤醒到首字时间。原始起止和逐链数值见同一汇总。统一正式token、金额与精确首个原生活动时延尚无汇总，保持未测。
