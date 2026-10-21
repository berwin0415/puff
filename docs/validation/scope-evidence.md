# I24 作用域验收证据契约

本模块先准备验收规则，尚未运行真实候选，也不代表 I24 已完成。入口是 `runner.scope_observation.grade_scope_observations(expectation=..., sources=..., attempts=...)`，它只读传入的 JSON 值，不访问网络、文件、候选 SDK，不修改原始结果。现有 `runner.grading.grade_scope_attempts` 保持原状；后续真实实验应显式调用这个新入口，不能把旧校准的通过当成本模块或候选的通过。

要验证的是：A、B 都能读取和控制自己的真实任务；A 换成 B 的任务标识后，三种读取和暂停、恢复、取消都会被拒绝，而且 B 未受影响。`202` 只是收到请求。控制操作还须有对应原生调用、原生完成结果和独立查询到的状态。`404` 可能是任务不存在，`401` 可能是凭证无效，运行中的“不能恢复”可能只是状态不对，均不能代替权限拒绝。

## 可信输入和来源

`expectation` 由模拟产品后端/测试编排固定，不能从候选的回答或可替换请求用户字段复制。`sources` 分为四个完整采集流，必须由验收端读取，保存在候选无写权限的位置。传入一个名为 `native` 的数组，或给候选内容算 SHA，均不能建立这个信任边界。采集时间 `time` 是同一可信观察端的带时区时间；原生时钟另行保留，不能直接与宿主时钟混比。

| 流 | 来源与必须保留的内容 |
| --- | --- |
| `backend` | 实际注册的身份与任务归属，认证后的 principal/credential ID，收到的目标与操作，授权决定、实际 HTTP 状态和完整响应 JSON；不保存凭证原文 |
| `native` | 独立查询的真实任务存在性、原生状态、完整 session/run/thread 等 ID、实际状态路径、完整控制请求 journal；公开控制调用与原生结果、原始事件读取结果。仅将官方接口中可证明的结果归一化，不用本地 `paused=true` 标记或 HTTP 接受回执充当原生状态 |
| `files` | 从绑定工作区实际读取的文件清单与内容 SHA、所属完整 scope；实际产物查询结果。不能只检查文件名 |
| `ledger` | 独立 Fixture 账本 ID、全部 requests/writes/faults 与计数；空账本须有完整结构与持续一致的 ID，`{}` 不算空账本 |

原生控制的候选专用验收仍须核对原始公开接口/事件，例如取消确有 `aborted` 终态，暂停确有持久 checkpoint，恢复确实使用该 checkpoint。本模块核对这些可信采集结果之间的身份、因果和归属，不能让接入层根据期望值生成一个成功结果。

## 固定哪些内容

`expectation` 的顶层是 `schema_version: 1`、`phases`、`reuse`、`bindings`、`probes`。默认阶段必须包含 `concurrent` 和 `reconnect`。每阶段固定 18 个探针：A 的六个本用户正例、B 的六个本用户正例、A 请求 B 的六个拒绝。不能用 A 的成功代表 B 成功，也不能全部拒绝。

每个 binding 精确包含：`binding_id`、`phase`、`principal_id`、`credential_id`、四字段 `scope`（experiment_id/variant/user_id/session_id）、`task_id`、`native_ids`、`state_dir`、`workspace`、`worker_id`、`connection_id`、`credential_fingerprint`、`browser_account`、`sentinel`、非空 `artifacts`。产物项为相对 `path` 和内容 `sha256`；凭证指纹只用于合成测试材料。`native_ids` 必须列全该入口实际提供的原生标识，不能为方便省去 run ID 或 session/thread ID。

A、B 的凭证、哨兵、浏览器账号、状态和工作区分别绑定。`state_dir` 和 `workspace` 是可信观察端确认的宿主真实持久存储定位，例如不同 Docker volume 对应的实际宿主路径，不能直接填所有容器都相同的 `/state`、`/workspace`。本契约要求两人的持久目录互不包含、互不重叠。原生 ID 是这个完整 binding 内的局部标识：独立状态库都使用 `session_id=main` 或同一个 agent 配置名合法，不要求这些字符串全球唯一；每份原生、文件、账本证据仍须携带并匹配完整 binding，不能只凭 `main` 判归属。相同 Worker、存储和原生 ID 且没有独立 scope 的同一个目标，不能当作两个已隔离任务。

重连保持原任务及持久目录，但换真实后端连接。只有本用户的恢复允许更换 incarnation：恢复产生新 run ID 时，预先由可信观察端注册新 binding；probe 的 `target_binding` 指向原 incarnation，`result_binding` 指向新 incarnation。任务、用户、scope、凭证和持久目录仍相同，不从模型返回值决定归属。暂停、取消、读取和跨用户拒绝必须保持同一个完整 binding，不能拿同一业务任务下另一个 run 的取消成功冒充原 run 已取消。

每个 probe 为 `probe_id`、`phase`、`actor_binding`、`target_binding`、`result_binding`、`operation`。`reuse` 显式记录 `supported`。不支持时给出实际限制 `reason`；声明支持时还必须包含 `reuse_b`、`transfer_a` 两阶段，核对输入 binding 声明 B 使用 A 的 Worker、A 换另一 Worker 后的同一完整矩阵。这只验证作用域证据与声明的 Worker 分配关系一致。本模块没有 Docker 生命周期采集，不能凭字符串相同断言“确实释放并复用了同一个容器/进程”。返回指标明确为 `worker_reuse_declared`，`worker_lifecycle_verified` 恒为 false；实际复用验收还须独立的容器/进程身份、释放和重新接管时间证据。模块不实现资源池，也不把不支持复用记为复用通过。

## 一次探针留什么

`attempts` 只提供 `probe_id` 与来源引用。引用形状为 `{"stream": "native", "line": 1, "sha256": "…"}`，行号从 1 开始；SHA 使用模块的 `evidence_sha256(record)` 对保留 JSON 值计算。每个来源行都有 `kind`、`time`，请求相关行都有同一 `request_id == probe_id`。

所有探针都引用 `request`、`authorization`、`response`（backend 流），以及 `before/after`（native）、`files_before/files_after`（files）、`ledger_before/ledger_after`（ledger）。每个 binding 还须在 backend 流有且仅有一条真实 `registration`。三个独立快照使用 `kind: snapshot` 和完整 `binding`，避免只比较两个空字典。

原生快照包含 `exists: true`、`status`、累计 `control_request_ids`、`event_cursor` 和明确的 `event_cursor_domain`，以及 `stable`：哨兵、checkpoint 所属 scope、真实 checkpoint 的 ID/SHA、凭证指纹和浏览器账号。文件快照 `items` 为带完整 scope 的真实文件清单。账本快照有 `ledger_id` 和完整 `ledger`。

游标必须说明计数范围。任务级累计游标的 domain 为 `{"kind":"task","scope":完整scope,"task_id":业务任务ID}`，同一任务恢复后仍不能倒退；单个原生 run 的游标为 `{"kind":"native_run","scope":完整scope,"task_id":业务任务ID,"native_ids":该run完整原生标识}`。同一个 domain 内始终单调，不能更改 domain 名称掩盖游标倒退。只有本用户恢复确实生成新原生 ID、且原生来源链验证通过时，允许 `native_run` domain 跟着换代并从零计数；不要求不同 run 的局部计数连续。两种计数范围不能在一次探针中互换。

| 探针 | 额外证据和判定 |
| --- | --- |
| 本用户 state/events/artifacts | `query_result` 引用独立原生或文件读取的原始结果；HTTP 200 的 body 必须逐值相符。events 和 artifacts 必须非空、有本用户哨兵/内容与完整归属 |
| 本用户 pause/resume/cancel | `native_dispatch`、`native_terminal` 引用实际原生调用与完成结果，同请求 ID、正确任务与 incarnation。202 只作接受回执；200 同理；204 必须保留真实空 body。暂停最终 paused 且 checkpoint 相符；恢复最终 running 且使用此前 checkpoint；取消最终 cancelled 且原生 stop_reason 为 aborted，不能自然完成替代。恢复换原生 ID 时，terminal 的实际 result 还须保留 `resumed_from: {native_ids: 旧run完整原生标识, checkpoint: 实际使用的旧checkpoint}`，并与独立 before 快照相符 |
| A 请求真实 B | 授权结果须为 `deny/scope_mismatch`，实际 HTTP 403 且 body 恰为 `{"error":{"code":"scope_denied"}}`。不接受 `foreign_content:false` 自述。没有对应原生控制派发，控制 journal 未变，B 的稳定状态、文件和业务账本未变 |

`resume` 另有跨探针约束：目标必须先由自己的合法 pause 探针实际进入 paused；checkpoint、原生 ID、控制 journal 与时间须能连起来。因此 A 恢复 B 的拒绝不能靠“不存在的任务”或“B 其实没暂停”通过。

`resumed_from` 来自实际公开恢复接口、实际加载的原生状态或候选专用验收已核对的原始调用与事件，不能从 `target_binding` 或期望 checkpoint 直接复制生成。只注册一个新 binding、看到新 run 正在运行，尚不能证明它继续了原任务。

活动期允许正常原始日志增加，不要求整份事件日志 SHA 在请求前后不变。拒绝探针窗口应选择业务状态/写入稳定的测试屏障，B 可以仍处于 running；对比稳定的 checkpoint/业务哨兵、凭证归属、文件与独立账本，并核对被拒请求没有到达原生控制。若窗口内 B 正常业务推进改变了这些值，应重新选择可观察窗口或补充独立因果证据，不能把普通日志增长直接当串任务，也不能删除变化后的证据。

完整合成样例及语义破坏样例在 `tests/execution/test_scope_observation.py`。合成校准只证明判定器能识别这些证据，真实能力仍须在 Docker 内由真实候选产生上述来源，并另跑冻结工具结果验收。
