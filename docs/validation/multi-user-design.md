# 两用户隔离与实例成本：本机真实实验设计

日期：2026-10-21。对应实施计划 I24、I25，当前只接 Deep Agents。**新材料下101组边界/生命周期通过但B业务失败；202组完整通过：两个原业务任务、两个取消子任务、并发/重连/跨用户拒绝及实际资源生命周期均通过。两组正向清理通过、同trial容器网络清空。303组也已完整通过。** 历史连接和删除采样失败均保留，最新修订及结果见第17节。DSH、浏览器使用期隔离及同Worker复用仍未完成；I25使用独立resident入口，其生命周期探索见[实例成本报告](instance-cost-flow.md)，不沿用本入口的隔离结论。

## 1. 先说准备做什么

先搭一个很小的模拟产品后端：它认识 A、B 两个用户，知道每个任务属于谁，再把合法请求交给真实候选。A、B 都要能查自己的状态、事件、产物，暂停、恢复和取消自己的任务。A 换成 B 的任务 ID 后，同样六种请求必须被拒绝，B 继续保持原来的归属和状态。

然后比较两种明确的部署方式。对照方式让每个用户保留一个真正加载了候选的空闲 Worker；节省方式只保留用户状态，来任务时启动 Worker，任务结束后回收。两边做相同的两个真实任务，使用相同的总配额，不能靠少跑一个任务、排队执行或留空壳容器获得低成本。

本轮优先验证工具任务。浏览器不用暂停或恢复现场；浏览器账号、cookie、上下文隔离若没有真实单独验证，结论必须写“未验证”，不能用绑定表里的账号名代替。也不建设生产身份系统、任务队列或资源池。

## 2. 当前仓库能复用什么，还需要补什么

| 落点 | 已有材料 | 下一步最小改动及归因 |
| --- | --- | --- |
| `configs/experiment.yaml` | 6 GiB / 3 CPU、最多两活跃任务、300 秒 / 20 次模型调用 / 两次重试、2 秒采样 | 继续作为唯一总预算；恢复沿用原任务累计账本，不另发预算 |
| `configs/tasks/scope.yaml`、`instance-cost.yaml` | 业务目标和成本阶段已冻结 | I24 接线前明确主任务、控制子任务和各阶段操作矩阵；当前 scope 文件仍指向旧判定器，真实实验须显式改接新判定并重新容器校准 |
| `scripts/run_detailed.py`、`runner/detailed.py` | 单任务镜像固定、准备与验收、原生结果、独立进程检查 | 作为参考复用需要的函数；不能直接并发运行两个 DetailedLab，它们会争用同一服务与固定 Alice/main 路径 |
| `execution/model_proxy.py`、工具服务、Fixture | 单任务 token/DB、调用限额、静态工具绑定、独立账本 | 每个活跃任务分别启动一套工具/代理，避免先研发多租户代理；Fixture 按完整 scope 分账 |
| `runner/scope_observation.py` | 保护侧四类观测之间的归属与因果判定 | 只是判定器，没有真实采集器；接入主任务/控制子试验矩阵，并补真实重叠与连接证据，不能把现有合成通过直接登记 I24 |
| `runner/resource_budget.py`、`process_observation.py` | 总配额和单 Worker 健康判定 | 复用判定，补多 Worker 完整清单与逐 Worker 归属，不能因两个 Worker 使用相同镜像就删掉一个观测 |
| `execution/control_backend.py`、`worker_control.py` | 首轮191项、修订轮256项Docker校准通过；首条真实单任务基线整体验收未过 | 产品后端负责认证、任务绑定、六个 API 和连接记录；每个 Worker 只持有一个固定 attempt 的真实候选子进程，控制应答不冒充原生成功 |
| `scripts/run_multi_user.py` | Docker校准通过；首次基线暴露采样及退出问题，修复待验证 | 宿主只做 Docker 编排、采样、实例更换与索引，不运行 Agent SDK 或计算任务答案 |
| `runner/multi_user.py` | Docker校准通过；首次基线业务完成，来源字段验收未过 | Docker 内准备 A/B 主任务和取消子任务；真实 HTTP 采集、原生/业务验收及资源核对；不替 Agent 选择下一步 |
| `compose/Dockerfile.multi-user-services`、`scripts/stage_deepagents_runtime.py --variant multi-user` | 专项镜像已构建并固定ID，见第11节 | 独立服务镜像包含后端和采集器；候选新上下文包含单次 Worker 控制包；宿主逐个声明隔离挂载、网络及固定配额，不复制含凭证的展开配置 |
| `adapters/<candidate>/` 的专项入口 | 当前多为一次请求后退出 | 拆出真实运行对象和薄控制入口，必要时保留空闲对象；估算并记录改动量、人工时间、第三方依赖和维护点，不能记成候选已自带服务化或休眠 |

基础配置、候选源码和新接入代码先冻结，再构建新镜像。正式执行全部在镜像内，不挂宿主源码或依赖。旧单任务镜像不能直接当多用户 Worker；新增代码、校准通过和真实实验通过分别记录。

## 3. 身份、网络和证据怎样摆放

模拟后端生成两份测试凭证，保存 credential hash → principal 的可信映射。实际认证从 HTTP Authorization 得到身份；请求 body 中即使写 `user_id=B`，也不能改变 A 的身份。任务表固定 task ID → owner、完整 scope、真实原生 ID、状态/工作区位置、当前 Worker 完整 Docker ID 和控制地址。

客户端只提交目标任务 ID 和操作。后端先按认证身份查归属，再选择控制地址；不接受客户端提供宿主路径、Worker 名称、原生 run ID 或信号目标。跨用户已存在任务返回 HTTP 403 与固定无内容响应，错误凭证和不存在任务分别保留真实 401/404，不能拿它们充当作用域拒绝。

两个活跃任务使用独立的 tools / model-proxy 服务和只读配置文件。每个 Worker 的任务网络中仍使用当前入口认识的 `tools:8081`、`model-proxy:8082` 别名，各自只解析到自己那套服务；不能把不同用户都接到同一个静态 `tool-binding.json`。Fixture 与后端使用明确的内部网络连接，代理是唯一持有远程模型密钥的组件。后端不挂 Docker socket；创建、删除 Worker 由宿主编排执行。

业务任务目录沿用当前布局，以真实 `user-a` / `user-b`、独立 session、chain 和 trial 定位。恢复只更换 attempt / container ID，不改变原任务 scope、workspace 或预算路径。原生 ID 在完整存储和 scope 内判归属；两份独立状态库都使用 `main` 合法，不改名伪造全局唯一性。

一个 trial 的保护侧证据放在 `runs/<date>/<candidate>/scope/<trial-id>/` 或 `instance-cost/<trial-id>/`，至少包含：

- `plan.json`：启动前固定版本、镜像 ID、任务绑定、阶段矩阵、策略假设及资源清单；不保存密钥。
- `backend.jsonl`：真实身份注册、认证决定、请求、响应、连接打开/关闭；不记录凭证原文。
- `native-observations.jsonl`：保护侧读取的原生状态、公开控制派发/结果、原始事件引用；原文件/原响应另保留并记 hash。
- `files.jsonl`、`ledger.jsonl`：实际文件内容摘要与完整 Fixture 账本快照。
- `worker-lifecycle.jsonl`、`docker-observations.jsonl`、`resource-samples.jsonl`：容器/进程身份、生命周期、配额和实际采样。
- `attempts.json`、`scope-verdict.json`、`allocation-verdict.json`、`metrics.json`：来源引用、判定与汇总；失败原件不覆盖。

候选只可写自己的输出子目录，不能改上述保护侧文件。采集流名、hash、`authenticated=true`、`paused=true` 都不能单独建立信任；正常化字段必须引用实际官方接口、SQLite/checkpoint 或原始事件。保护侧使用同一时钟记录观察时间，不混比容器时钟和宿主时钟。

## 4. I24 的真实执行流程

### 4.1 主任务负责连续性，独立子任务负责取消

不能先取消业务主任务，再提交一遍任务冒充重连或恢复。每个用户至少区分两种独立 task ID 和 binding：

| 对象 | 用途 | 结束条件 |
| --- | --- | --- |
| 业务主任务 A、B | 同时执行、查状态/事件/产物、暂停/恢复、重连，以及可行的 Worker 复用/转移 | 原任务继续完成，分别通过冻结 `grade_tools`；已完成写入不因恢复重发 |
| 取消控制子任务 A-cancel、B-cancel | 在真实模型/工具活动中测试本用户取消，以及 A 请求取消 B 的拒绝 | 原生取消确实生效，允许任务未完成；按已发生副作用和取消后不再派发新写入验收，不能要求取消链同时完成全部工具任务 |

各子试验都保留独立预算、scope、checkpoint、账本和原生 ID；子任务不能借主任务的暂停或终态通过。后端凭证仍属于同一个 A/B 用户。需要模型的每条逻辑任务均沿用 300 秒 / 20 次上限，恢复只用剩余额度。

旧 `scope_observation` 的每阶段 18 项契约仍保留为历史合成材料。当前实际入口显式使用 `runner.multi_user`，分离主任务与取消子任务：主任务执行本用户读取、暂停/换容器恢复、重连及六种跨用户拒绝，取消子任务执行双方真实活动、跨用户取消拒绝及本用户原生取消。它必须重新 Docker 校准，不能借用旧判定器的通过数。

### 4.2 逐步操作

1. **准备并固定目标。** 从种子 101 准备两份真实工具任务、不同哨兵/凭证/工作区与非空产物标记。后端先注册真实存在的原生任务，验收端读取状态和存储位置；存在于后端表不等于已经在原生侧存在。
2. **同时开始两个主任务。** 两个真实 Worker 就绪后统一放行任务输入。观察真实原生活动及独立模型/工具调用，确认重叠后才进入并发阶段；没有命中重叠就保留该次未命中，不能补时间戳。
3. **本用户三种读取。** A、B 各自读取 state、events、artifacts。返回内容与保护侧原生查询、文件实际内容一致，事件/文件必须非空并含本用户哨兵。产物读取等候选实际生成内容后再做，只有任务结束才产出时就放到结束后，不用准备阶段的标记冒充 Agent 产物；阶段矩阵和判定器须允许对应的真实终态。不能只返回后端缓存的一条归属声明。
4. **A 对 B 的读取和控制拒绝。** 六类请求都替换为真实 B 的目标。pause/cancel 选 B 仍在运行时；resume 必须等 B 被自己的合法 pause 真正暂停后再测。请求前后保留 B 独立快照，确认没有错误原生派发、没有新增控制 journal 或被拒请求引起的业务副作用，且响应不含 B 数据。B 不能仅因为被查询就停止工作。
5. **本用户暂停和恢复。** A、B 分别收到自己后端身份的请求；202 只表示收到。等候选原生安全保存，再从原生 checkpoint/Goal/RunState 恢复。读取原始控制证据及稳定状态，不由新 Runner 填入“paused”答案；新 run 必须保留真实 `resumed_from` 链。
6. **真实重新连接。** 关闭 A 的产品 API/SSE 客户端，后端记录旧连接 EOF/关闭；建立新连接，重新认证，服务端生成新的 connection ID。主任务仍是原来的 task，已有 checkpoint、事件游标和产物仍可查；A 用同一条新连接访问 B 的 state 必须再收到实际 403，验收核对请求、响应及后端记录的 connection ID，不能借重连前的拒绝通过。B 不受 A 断线影响。不能只改 binding 内的字符串。
7. **可行时才做复用/转移。** 按下一节核对真实 Worker 生命周期；不支持时登记限制和成本，不伪造 reuse 阶段。主任务最终仍要分别完成原工具目标。
8. **独立取消子试验。** 启动 A-cancel、B-cancel，确认双方真实活动。先测 A 取消 B-cancel 被拒且 B 仍活动，再分别用本用户身份取消各自子任务。保留原生终态、在途调用与完整账本；已经发出的写入不能凭取消被撤回，确认之后不得新增派发写入。
9. **独立验收。** Docker bench 核对主任务结果、恢复前已写 operation 的后续 dispatch、六类本用户正例和跨 B 拒绝、重叠/重连证据，以及逐 Worker 进程状态。缺哪一项就明确哪一项未验证。

正式关键路径沿用 101/202/303，每组新状态和新 trial；先用 101 做接线冒烟。没有真实 pause/resume 的路线可以完成已覆盖的读取、拒绝和取消调查，但不得标为 I24 全部通过。

**快照比较须避开自然进展的竞争窗口。** 当前判定要求拒绝前后 B 的状态、文件和账本完全相同；B 同时工作时可能合法变化，不能把这些变化判成 A 越权，也不能倒填相同快照。读取与运行态控制拒绝可安排在保护侧可观测的只读工具等待窗口：Fixture 暂缓一条真实只读请求的响应，候选仍处于原生 running，不发送 pause，不人为改原生状态。只有实际命中窗口且快照稳定才按该口径判；未命中保留未命中，不能重写时序。恢复拒绝使用 B-owner 合法暂停后真实稳定的 checkpoint。等待时间和接入成本单列；这一窗口只为核对拒绝，不能证明双方有效并发，也不加入 I25 成本任务。真正并发及成本活跃仍须在另外的区间记录双方实际模型/工具进展重叠。

## 5. 控制请求接到哪个真实入口

这些内容应先依赖 I22 的真实最小链，通过后再接后端。后端只做授权和路由，薄 Worker 控制服务只调用指定候选 API；不能自研一套“记下一步”来宣称框架支持恢复。

| 路线 | 后端合法请求的实际去向 | 当前缺口和本轮处理 |
| --- | --- | --- |
| DSH | Goal 变体的步边界暂停 → 原生 `goals.pause`、真实 turn/end、`whenIdle`/flush；恢复 `agents.resume({setup})` 后 `goals.resume`；取消保留公开 `Agent.cancel` 与对应 aborted/idle 证据 | 当前 scoped runtime 冷读不是未完成任务恢复，取消仍有终态缺口。四项官方 Goal 插件、薄接线及其额外模型成本分别记录；I22 没通过不能跳过 |
| Deep Agents / LangGraph | 所属事件循环的 `RunControl.request_drain("experiment_pause")` → 实际 GraphDrained 和 sync checkpoint；同 thread/checkpoint、输入 `None` 继续 | 当前 SIGUSR1 的 `experiment_cancel` 是 drain 停止，不能改名成 native aborted。应用可以基于原生 drain 做“停止并禁止再调度”，须记应用取消策略；若未实现/验证真正原生取消，原生 aborted 子项保持不支持/未验证，不伪造值 |
| Agents SDK | pause 使用公开 `cancel(mode="after_turn")`，消费并收束原 stream，完整 RunState durable 保存；`RunState.from_json` 后交回 Runner；cancel 使用现有 immediate 路径及公开完成/错误观测 | 当前脱敏诊断文件不是恢复输入；真实保存/续跑和多请求生命周期需要补接。SDK 的 `is_complete` 不能当业务完成。原生 immediate 并不保证统一 `stop_reason=aborted`，候选专用验收保留实际语义，不为满足通用矩阵造字段 |
| OpenClaw | 当前单任务取消是原 HTTP 客户端断连与对应原生 run 终态；状态/事件读公开 Gateway/Session；暂停恢复仅采用已被 I22 证实适合当前 HTTP 入口的机制 | 旧 `chat.abort` 不覆盖当前 HTTP 轨道；整 Gateway drain 不是单任务暂停。HTTP 冷读也不是续跑。没有可用暂停恢复时保留对照和缺口，不能在模拟后端替它“暂停” |

现有 `scope_observation` 的统一 aborted 要求不是所有候选原生语义；I24 接线须先用候选专用判定核对原始公开调用，再把结果及来源交给通用归属检查。应用基于 drain 禁止重调度可以评价为应用能力，但必须与原生立即取消分列。任何新服务化入口都使用新变体、镜像和基线，不回填旧详细实验。

后端控制服务需传播真实 request ID、完整 task/attempt 绑定及 action。当前 SIGUSR1 只是单进程触发，不能在多任务进程中广播；新控制入口须在所属事件循环调用确切任务，未匹配、过期或已经结束的请求明确拒绝。Host 只负责创建/删除容器，不替候选做工具步骤。

## 6. 怎样证明真的并发、重连和复用

**并发。** 保留两个任务的官方活动开始、实际模型/工具派发和真实终态，加上同一观察端记录的先后关系。计算两段真实执行区间的交集；交集中双方都必须发生有效调用/进展。再用完整 Docker 清单和资源采样对应两个 Worker。两个容器同时存在、两个 Future 已创建、都在 sleep 或一个在队列里，都不能通过。采样太少不能伪报稳定均值；成本轮要求重叠区间有至少两次原始采样，否则另留新轮次实测。

**重连。** 用实际客户端关闭与服务端断开记录、新连接握手/认证、同一原任务的独立原生状态串起前后关系。产品客户端与候选上游传输分开：尤其 OpenClaw 当前上游 HTTP 断线会触发取消，不能关掉它再说任务可无损重连。若需后端保留上游连接，这属于本次产品接入成本。

**复用。** 先让 A、B 各自安全暂停，解除旧任务的控制归属；然后 B 真正在 A 原来的 Worker 上继续，A 在另一 Worker 上继续。记录完整 Docker ID、容器 StartedAt、原生进程 PID/启动时间、旧任务释放、新任务接管、配置/工具/凭证绑定及活动事件。复用后再查哨兵、文件、旧消息、checkpoint 和权限拒绝，不能只查最终消息回答。

同名容器删除重建不算同容器复用；同容器内换了候选进程要分别记“容器复用、进程重建”。当前 entrypoint 的用户绑定、闭包和挂载多为启动时固定，不能假定改一个 session ID 就可切用户。若必须把原来的单任务入口改成多任务服务、重建工具、清理全局状态/缓存，实际投入作为应用改造记录。

若安全切换需要重建容器，就记录 `reuse_supported=false`、真实原因、每次重建的时间与内存开销。I25 仍可验证“按需新建并回收”，无需为完成表格先研发同进程资源池。共享了网关、用了相同 agent 名或 Worker 字符串，都不能单独证明复用。

## 7. 本机总资源怎样安排

直接把当前单 Worker Compose 复制两份会超出 CPU 总配额：两 Worker 各 1 CPU，再加两套工具/代理、Fixture 和后端为 3.5 CPU。先为专项冻结下面这一组候选配额，在同一配额下重新通过单任务基线；它不是当前详细实验的已测性能。

| 运行组件 | 数量 | 每个内存 / CPU | 同时运行合计 |
| --- | --- | --- | --- |
| 真实候选 Worker | 2 | 2 GiB / 0.75 CPU | 4 GiB / 1.5 CPU |
| 工具服务 | 2 | 256 MiB / 0.25 CPU | 512 MiB / 0.5 CPU |
| 模型代理 | 2 | 256 MiB / 0.25 CPU | 512 MiB / 0.5 CPU |
| Fixture | 1 | 256 MiB / 0.25 CPU | 256 MiB / 0.25 CPU |
| 模拟后端 | 1 | 256 MiB / 0.25 CPU | 256 MiB / 0.25 CPU |
| 活跃阶段总计 | 8 | — | **5.5 GiB / 3 CPU** |

两套工具/代理按活跃用户绑定，空闲阶段可一同停止；两种策略在同一阶段采用相同附带服务政策。bench 评分容器为 512 MiB / 0.5 CPU，不与两个活跃 Worker 同时启动；先保留保护侧完整观测并停执行，再启动 bench 离线验收。后端、容器启动重叠、代理和短时探针都不能漏算。

原始清单使用本 trial 的可信归属标签动态发现全部容器，并和阶段开始前确定的计划逐项对应。创建/删除/接管前后都采一次，固定每 2 秒持续采样。Docker PIDs 是线程/进程混合指标；真正进程数另从容器实际进程清单记录，不能直接改名。

短时 bench 或被正常回收的 Wrapper 可能在“读完整清单”和“读 stats/top”之间退出。当前接线先 create、核对完整 ID/镜像/配额并登记，之后才 start；采样逐 ID 保留命令结果。只有该确切 bench 的实际健康 exit 0，或已事先登记回收请求的确切 Wrapper 的实际 exit 0/143，才能另记退出过渡；删除还须有此前停止快照、删除登记及实际 ID 不存在的观察。独立验收重新核对 `container-transition-registry.json` 与原始终态。137、OOM、陌生 ID、未知 Docker 错误不能被这条规则豁免，Wrapper 的正常回收也不代表候选原生健康退出。过渡项不填假的 RSS/进程数据；原始清单仍保留其原先分配额度。锁只保护短期内存登记，Docker 调用在锁外；采样间隔和单次耗时仍不得超过 5 秒。

多 Worker 使用同一镜像是正常情况。当前单 Worker 进程判定器不能直接把全部相同镜像都当作一个对象；专项按启动前固定的确切 name/image/cap 与观测到的完整 ID 分别验收，每份投影引用完整原始 Docker 清单及 hash。全局完整清单仍用于总配额验收，不能为消除歧义删掉额外实例。OOM、强制终止、采样错误与未完成任务如实保留。

浏览器不在这份工具成本档位里，两策略都不常驻浏览器。这是实验假设，不是现网事实。要补浏览器使用期隔离时，单开包含实际浏览器、Site、用户上下文和真实身份动作的资源计划；总配额不满足就记录未测，不能只挂账号名后声称已隔离。

## 8. I25：同预算比较常驻与按需

### 8.1 先固定公平口径

只比较已经通过所需 I24 范围的路线，第一轮最多选两条。每个候选分别比较自己的两种部署策略，不拿不同框架、不同模型或不同工具粒度直接算节省比例。

- **每用户常驻对照。** 每个用户拥有真实已加载框架、原生状态/会话可查询的空闲 Worker，任务结束后不回收。SDK 路线需保留真正的 Agent/graph/Session 等对象；OpenClaw 为真实 Gateway。保留一个 shell、sleep 或通用接单进程不能算候选常驻。
- **按需策略。** 用户状态和任务归属持久化，空闲时没有候选 Worker；请求到达才创建，任务完成并正常关闭原生存储后立即回收。只有确需保留未完成任务时才调用已经验证的 I22 安全暂停，不把 kill 当休眠。暂不实现同进程复用。

常驻不回收、立即回收和上述服务配额都是实验假设，不能推定 Hogee 现网也这样做。两边使用相同的版本、服务化接入、任务、模型、配额、工具与代理策略。为了保留真实候选对象而新增的服务循环和接入代码单列；不把这部分算成框架原生托管能力。

### 8.2 每个 2 / 5 / 10 档位怎样跑

1. **准备 N 个真实用户状态。** 每个用户有后端身份、原生可读状态、独立凭证与工作区、非空哨兵/产物。优先用官方无模型创建入口；入口不能创建有效原生状态时，串行执行一次真实种子任务建立状态，准备的调用、耗时和磁盘另记。不能创建 N 个空目录就声称保留了 N 个历史用户。两策略使用内容等价且互不写入的材料。
2. **全空闲 60 秒。** 常驻策略保留 N 个真实候选 Worker；按需策略为零。记录内存/CPU、真实进程/线程、容器数、状态占用、后台模型调用。若候选无法在未激活任务时保持真实空闲，直接登记该入口缺口，不用睡眠替代。
3. **固定两个活跃用户。** N=2 用前两个；N=5/10 使用预先固定的一对，并轮换到此前未活跃的用户验证冷唤醒。创建新的独立任务/session，保留这些用户之前的原生历史；统一放行冻结 tools 输入，证明两个任务实际重叠并各自完成。其他 N−2 用户始终空闲。
4. **正常结束并观察回收。** 先关闭原生 saver/Session，保留健康退出和原生状态证据。常驻继续保留真实空闲对象；按需删除 Worker。再观察 60 秒，核对无旧候选进程、无隐藏后台模型调用，状态可冷读且归属正确。
5. **再次唤醒。** 对同一对用户再发相同口径的新业务任务，测请求接收 → 排队 → Worker 就绪 → 首次有效模型/工具动作 → 完成。用新任务预算，不能把旧未完成任务的新容器当新额度；若测试的是旧任务恢复，则必须沿用旧 chain 和剩余预算并单独分类。
6. **独立故障子轮。** 从干净数据重跑两个真实活跃任务，在双方活动时只强制退出一个已固定 ID 的 Worker。保留另一用户的原生事件、结果、账本和控制可用性，测受影响任务数量、恢复时间、额外调用与写入不确定性。强杀是已知故障，不得记为健康结束；未知写入交给 I23 判定，不擅自整任务重跑。

资源不足在启动前就应停止加实例。按上述固定 2 GiB / 0.75 CPU 每用户 Worker，对照常驻 N=5/10 会先超过总分配上限；它们可记录为“该固定配额下无法进入运行阶段”，不是已实测 RSS OOM，也不是框架无法承载五个用户的普遍结论。按需路线仍可测试 N 份状态和两个 Worker，但不能给缺失的同档常驻结果编节省百分比。

不临时压低某一方配额、不扩大 Colima、不关闭无关业务来凑档位。如需研究更小 Worker 配额，另冻结一个双方一致的变体、重跑单任务基线与两个活跃任务，再比较；低配启动但任务失败不能算成功省资源。

### 8.3 最后怎样评价

先看相同业务结果和隔离是否成立，再报总内存均值/峰值、CPU、真实容器/进程数、状态及镜像磁盘、冷/热唤醒和排队时间、额外模型请求/token、回收后遗留进程与故障影响人数。共享后端/Fixture 的固定开销与每空闲用户增量分列，价格未知时金额为 unknown。

用量统计区分准备用户状态、正常任务、控制、再次唤醒和故障重试；每项保留原始计数。按需冷启动更慢、当前接口不能恢复、同 Worker 复用需重构，都作为取舍写进报告。只报告本机固定版本与档位，不外推生产吞吐或现网收益。

## 9. 后续执行任务与验收

| 顺序 | 任务 | 验收标准 | 当前状态 |
| --- | --- | --- | --- |
| M01 | I21 确定专项路线；核对 I22 真实控制和单任务小配额基线 | 每种控制写明公开 API、已验证语义、缺口、应用补齐及镜像；无原生证据的动作不进入能力通过项 | 同一I24入口首次0.75 CPU基线整体验收失败；修订镜像256项校准通过，新基线待跑，不借旧1 CPU或I25 resident结果通过 |
| M02 | 修改 scope 任务/判定契约，区分主任务和取消子试验 | 各 phase、task/control_subtrial、操作矩阵可逐项核验；不要求取消后复活；旧校准不冒充新契约通过 | 独立接线及修订轮256项Docker校准通过；真实双用户流程待跑 |
| M03 | 两用户准备、最小后端与受保护采集器 | 实际凭证认证和任务归属；用户不能替换路由；真实原生/文件/账本来源；凭证不入证据；所有 API 在 Docker 内执行 | 合成校准通过；真实候选的双用户认证/读取/控制尚未实测 |
| M04 | 新专项镜像及八容器配额计划 | 两个真实 Worker、隔离网络与两套绑定；全清单 5.5 GiB / 3 CPU；删除重建状态和预算归属正确；不加载 `old/` | Worker、services、bench已构建并写入pins；首次单任务清单有采样/退出验收缺口，双用户全清单仍待冒烟验收 |
| M05 | 一组真实 I24 冒烟 | 两主任务实际重叠；本用户读取/暂停恢复可验；六类跨 B 请求对有效目标被拒；真实客户端重连；取消子任务原生证据完整 | 未开始 |
| M06 | 按实际能力验证复用或登记不支持 | 有完整容器/进程释放与接管证据；无法复用时写明原因、替代部署和开发成本，不能凭 Worker 字符串通过 | 未开始 |
| M07 | 三组正式 I24 与独立结果验收 | 101/202/303 分别保留全部证据、主任务结果、调用与副作用；未测浏览器或控制缺口明确列出 | 未开始 |
| M08 | 2 / 5 / 10 两策略 I25 | 固定两个真实活跃任务；空闲/执行/回收/唤醒/故障阶段完整采样；超预算档位诚实停下，双方同口径才给节省比例 | 未开始 |
| M09 | 更新报告与实施任务表 | 能力、控制语义、实例关系、资源数字、未完成项和研发成本都有来源；候选原生/官方配套/应用接入/实验编排分别注明 | 未开始 |

本文的任务表是 I24/I25 开工设计清单，不新增一套全仓库进度入口；实际执行状态仍回写根目录实施计划及对应实验报告。

## 10. I24 首次执行入口

以下是复现模板；构建和校准记录在第11节，首次真实基线失败记录在第12节。修复须先用新材料校准，再用新trial执行基线；不可复用下面示例中已使用的`i24-da-baseline-101-001`。先结束并清理当前所属实验，确认没有其他 `harness-lab` 容器，再冻结代码及镜像。宿主只导出材料、构建镜像及编排；校准和业务验证都在 Docker 内进行。

若尚无包含 `execution/worker_control.py`、`runner/shared_backend.py`、`runner/shared_supervisor.py` 的 multi-user 候选上下文，使用新的目录导出并 stage，不能覆盖已有实验上下文：

```sh
uv run --offline python scripts/prepare_candidate_source.py --config configs/candidates/deepagents_langgraph.yaml --destination-root external/build-context/i24-multi-user-001
uv run --offline python scripts/stage_deepagents_runtime.py --context external/build-context/i24-multi-user-001/deepagents_langgraph --variant multi-user
docker build --platform linux/arm64 -t harness-lab-deepagents-multi-user:local external/build-context/i24-multi-user-001/deepagents_langgraph
docker build --platform linux/arm64 -f compose/Dockerfile.multi-user-services -t harness-lab-multi-user-services:local .
docker build --platform linux/arm64 -f compose/Dockerfile.bench -t harness-lab-bench-i24:local .
```

已有未改动且材料 hash 一致的 multi-user 候选上下文可复用。本次“重连后跨用户拒绝”和“短容器采样过渡”修订只改 I24 host/runner/tests；不改候选 overlay。修订后需要重建 bench 与 multi-user-services。以后若修改 Worker 或其共享模块，必须另建候选上下文、重新 stage/build、固定新的 adapter export 和镜像 ID。

先以新 bench 的完整镜像 ID 替换下面的 `<bench-image-id>`，执行无模型校准。命令不注入 `.env`、模型凭证、宿主源码或实验数据，`--network none` 仍允许容器内的 loopback HTTP 测试：

```sh
docker run --rm --name harness-lab-i24-calibration --platform linux/arm64 --label com.docker.compose.project=harness-lab --network none --read-only --memory 512m --cpus 0.5 --pids-limit 128 --security-opt no-new-privileges --tmpfs /tmp:size=128m --entrypoint python <bench-image-id> -m pytest -q -p no:cacheprovider tests/test_multi_user.py tests/test_multi_user_host.py tests/test_worker_control.py tests/test_container_control.py tests/test_shared_supervisor.py
```

校准必须核对旧连接上的拒绝不能代替新连接、串行成功不能冒充并发、原预算/写入不重发，以及精确登记容器正常退出与错误身份、OOM、137、未知 Docker 错误等反例；还要核对 baseline 只能启动 A、不能走 B/取消/暂停恢复，也不能作为 I24 完整验收。合成校准通过后，仍需真实候选单任务小配额基线及一组 I24 冒烟；不能直接登记能力通过。

`scripts/run_multi_user.py` 必须接收启动前冻结的 pins JSON，字段来源如下；不使用旧单任务 adapter export 替代新 overlay export，不把服务 tag 当固定镜像身份：

| pins 字段 | 实际来源 |
| --- | --- |
| `worker_image_id` | 上述 multi-user Worker build 后，Docker image inspect 返回的完整 `sha256:…` ID |
| `bench_image_id` | 无模型校准所用新 bench 的完整镜像 ID |
| `services_image_id` | 新 `Dockerfile.multi-user-services` 镜像的完整 ID；不能使用只包含 fixtures/execution 的普通 services 镜像 |
| `source_export_sha256` | 该候选上下文 `SOURCE_MANIFEST.json` 的 `export_sha256` |
| `adapter_export_sha256` | 同一上下文 stage 后 `LAB_ADAPTER_MANIFEST.json` 的 `export_sha256`，其中包含 multi-user overlay |
| `adapter_helper_sha256` | 同一冻结版本 `adapters/deepagents_langgraph/pause_recovery.py` 实际字节的 SHA-256；bench/services 内也须包含同一份 helper |
| `model_name` | Compose 解析后的模型名称；由宿主捕获读取，不输出含密钥的完整 Compose config |

pins 文件不含任何 token 或密钥。先用同一 multi-user Worker 镜像和配置执行单任务小配额基线：

```sh
uv run --offline python scripts/run_multi_user.py --baseline --trial-id i24-da-baseline-101-001 --date 2026-10-20 --seed 101 --pins-file /absolute/path/to/i24-material-pins.json
```

这一步只发 A 的主任务，使用原 `worker_control`、后端、工具和模型代理。Worker 配额仍是 2 GiB / 0.75 CPU；加上单套工具/代理、Fixture 和后端，总计 5 容器、3 GiB / 1.75 CPU，bench 不与 Worker 并行。任务位于独立 `multi-user-baseline` scope，使用新状态和预算，证据位于 `runs/<date>/deepagents_langgraph/scope-baseline/<trial-id>/`。为了复用当前固定双用户后端，prepare 仍生成 B 和取消子任务的无模型输入材料、空状态目录与测试凭证；它们不启动服务或 Worker，也不发 Agent/模型/工具请求，验收核对其预算 DB、候选事件和 Fixture 账本均无活动。基线只允许后端读取 A 的状态、事件及实际产物，不触发取消、暂停或恢复。

容器内独立验收核对 A 的完整业务、原生正常终态、实际 SQLite checkpoint、可信来源、实际 HTTP 读取、原预算、确切子进程及容器配额/回收。输出 `baseline-verdict.json`，固定 `baseline_only=true`、`i24_capability_verified=false`；不生成 `scope-verdict.json`，不代表并发、隔离或控制能力已通过。失败结果保留，按同一材料/配额另起新 trial，不能拿旧 1 CPU 详细实验或另一 resident 入口补位。

基线通过后再使用正式双用户入口，每组新 trial ID；先完成 seed 101 冒烟及验收，再决定是否进入 101/202/303 正式组，失败证据不覆盖：

```sh
uv run --offline python scripts/run_multi_user.py --trial-id i24-da-smoke-101-001 --date 2026-10-20 --seed 101 --pins-file /absolute/path/to/i24-material-pins.json
```

一次 trial 先执行两个主任务的真实重叠、保护侧读取/拒绝、原生暂停、删除旧容器、同 checkpoint 空输入恢复及业务验收，再执行两个独立取消子任务。需要模型的四条逻辑任务各自最多 20 次 / 300 活跃秒；同时最多两个，主任务恢复延用各自剩余预算。验收看 `scope-verdict.json`、`host-result.json`、真实 HTTP/原生/工具证据及完整资源记录；命令非零、缺证据或清理失败都不能记成功。

## 11. 本次已完成的构建和无模型校准

lifecycle-images-001构建记录（`runtime/lifecycle-images-001`）串行构建成功，实际固定材料如下：

| 角色 | 实际标签 | 完整镜像ID |
| --- | --- | --- |
| I24 Worker | `harness-lab-deepagents-langgraph:multi-user` | `sha256:3e4e2a2ecac1d4b3d02e22135b54d7f5006fcbfaf819bc5bfca62e359fa379f7` |
| I24 services | `harness-lab-services:multi-user` | `sha256:6396aee4242a1e644687e632fcc46d984769344c52b3dc9e4cdaecbac7aeccf1` |
| 校准bench | `harness-lab-bench:lifecycle-checks` | `sha256:5ac51f8ecc0407aadb0e69cbd33bb9aa7d6e2bb20d727f398a030671a223442e` |

首轮入口的pins文件（`runtime/lifecycle-images-001`）SHA-256为`4f299c3f6c0a4ced15babeda25e09e36187533bfaf57b9717810a7c392074a17`。它固定上述三镜像、模型`deepseek-v4-flash`、上游export `dbd26083214e82cfd957ec6a8eaa3077e73d28cfa4f62d8e3185a6f58f81b17c`、multi-user adapter export `2e7bb73c447242150185c509706ecd0f0b6aac298d927e08f43e53a1c5985f57`和helper hash，不含凭证。这是第12节失败基线使用的历史材料；修订轮`--pins-file`改用第13节新文件，不修改旧pins。

首轮i24-control-check-001（`runtime/i24-control-check-001`）失败：`scripts/run_multi_user.py`的容器清单投影少一个右花括号，pytest收集即语法错误、退出2，尚未进行真实候选任务。修复并构建新bench后，i24-control-check-002（`runtime/i24-control-check-002`） **191项通过，3.02秒**；原001日志和失败状态保留。

002使用上述bench、512 MiB/0.5 CPU、`network none`，未调用模型、未用远程key；宿主证据确认退出0、PID0、无OOM/error，确切校准容器清理成功。它验证的是身份路由、控制及判定的正反例，包括新连接拒绝、短容器退出过渡和单任务基线限制；**还没有让真实A/B两用户按完整流程跑一遍**。后续首次真实基线未通过，见第12节；不能用191项校准关闭I24。

## 12. 首次真实单任务基线：业务完成，整体验收未过

`i24-da-baseline-101-001`已在同一I24入口、2 GiB / 0.75 CPU Worker档位执行完毕，证据保存在原trial目录（`deepagents_langgraph/scope-baseline`）。本次只执行`task-a`，业务结果完成；B、取消子任务和替换Worker未执行，不能评价双用户隔离、重连、控制或复用。

独立基线判定（`scope-baseline/i24-da-baseline-101-001`）实际为`passed=false`、`baseline_only=true`、`i24_capability_verified=false`：

| 检查 | 实际结果 | 如何理解 |
| --- | --- | --- |
| 原业务、原生来源、预算及HTTP联合核对 | 未通过，`baseline_native_scope_materials_or_active_budget_changed` | 业务完成不能替代原生字段、scope和材料绑定验收；按来源接线问题修正，旧结果不改 |
| B、取消及替换未执行 | 通过 | 只证明基线没有偷跑其他试验，不是隔离能力通过 |
| 同镜像/0.75 CPU及宿主执行采样 | 未通过，`host_execution_or_sampling_failed` | 缺完整采样和退出健康证据，不能给整体验收通过 |

宿主记录（`scope-baseline/i24-da-baseline-101-001`）虽有`passed=true`、`errors=[]`、`resource_overages=[]`，仍记录5次采样`RuntimeError`：准备阶段1次、业务阶段3次、评分阶段1次。这个宿主汇总布尔值不能覆盖独立基线失败。实际还暴露Init接线及服务退出137问题；服务被强制结束不等于健康关闭。

当前窄修针对原生来源字段核对、Init/服务关闭和采样处理，修订材料已完成第13节的新镜像Docker校准，**全新trial的真实基线仍待验证**。第11节旧pins、镜像、原日志与失败verdict全部保留；不把校准写成真实基线已经通过。基线整体通过后才进入两用户101冒烟，I24保持进行中。

## 13. 修订镜像与第三次无模型校准

recovery-lifecycle-images-001构建记录（`runtime/recovery-lifecycle-images-001`）已成功构建新bench及multi-user services：

| 角色 | 新标签 | 完整镜像ID |
| --- | --- | --- |
| bench | `harness-lab-bench:recovery-lifecycle` | `sha256:aa0fb8cce18aa18b9876fe6cdabaecd6e20af0519f7894cc6f6869b35463c771` |
| I24 services | `harness-lab-services:multi-user-init` | `sha256:7efae0475998cfa0d71065b7ddffdb581b1d44d552935f2dc6b0127cfa4e8c52` |

Worker仍使用第11节固定镜像。新pins文件（`runtime/recovery-lifecycle-images-001`）SHA-256为`03b01d4cfe8f80ee5b354b21b128db0f01a84aeb3c77df0215ae1d1952de9eaa`；后续执行必须绑定这轮完整材料，不能改写第11节旧pins。

i24-control-check-003（`runtime/i24-control-check-003`）在新bench中 **256项通过，4.21秒**，断网、无模型/远程key，健康退出及确切容器清理通过。这是接线与判定边界校准；实际服务是否健康关闭、资源采样是否完整，仍须由新真实基线自身证明。两用户101冒烟及三种子正式实验未因此通过或开始。


### 追加：第二条真实基线与退出采样

`i24-da-baseline-101-002`已经结束。业务、真实entrypoint来源绑定、0.75 CPU单任务预算及HTTP检查通过；`--init`生效，Worker、后端、工具、代理都以143正常退出，原137问题未再发生。整体仍失败：三次采样与已登记的正常退出重叠，`stats`返回空stdout/exit1/EOF，`top`同时精确确认同一ID已停止或不存在。原错误和失败保留。

新增的处理只接受这一完整证据链：相同预登记ID、实际正常终态、原始退出/删除记录，以及`top`对同一ID的停止/不存在确认；137、OOM、无记录、别的ID、连接错误或单独EOF继续拒绝。过渡记录保留真实命令输出，不补造CPU、内存或进程数据，5秒采样限制不变。host和容器验收各自核对；正反回归已计入`native-diagnostics-check-001`的523项通过。新bench与services分别为`8a58c351…`、`907fe9c8…`，完整ID见`infrastructure/runtime/native-diagnostics-images-001/builds.json`，新pins为同目录`i24-material-pins.json`。下一步仍需新ID单任务基线，再执行双用户冒烟；本项尚未通过真实并发验收。

`i24-da-baseline-101-003`已完整通过，三项外部检查全通过，host sampling_errors/resource_overages/errors均为空；整体退出0。相同pins下真实双用户冒烟`i24-da-smoke-101-001`已结束，结果见下节；正式三种子尚未运行。

## 14. 首条双用户冒烟与恢复后的连接问题

i24-da-smoke-101-001（`scope/i24-da-smoke-101-001`）为`passed=false`。它已执行主任务并发、真实暂停、旧Worker退休、新Worker绑定；`main-resume`采集器轮流等待两用户时，第二条HTTP连接超过后端15秒空闲时限，收到`RemoteDisconnected`。这是本轮采集接线问题，不能评价为框架无法恢复，也不能凭前半段完成宣称完整隔离通过。中止清理还留下两条未事先登记过渡的采样错误，原错误保留，不豁免。

修订为：等待每个用户恢复结果前，用相同身份创建新连接查询；已经接受的resume请求不重发。新材料由recovery-path-images-001/pins（`runtime/recovery-path-images-001`）固定：Worker `3e4e2a2e…`、bench `8bd7f754…`、services `ad8daf31…`；相关`recovery-path-check-001`共330项通过、9项按路线跳过，未调用模型。新真实基线`i24-da-reconnect-baseline-101-001`开始于2026-10-21，完成后再决定是否进入新双用户冒烟；校准不替代这两项真实验收。

## 15. 新材料基线的业务结果与后续边界

i24-da-mount-baseline-101-001（`scope-baseline/i24-da-mount-baseline-101-001`）仍因原业务任务失败而整体未通过；其余两项均通过，host错误/采样错误/资源超额全部为空。Mounts顺序误判未再出现，原候选同镜像、同0.75 CPU配额的模型/工具/状态/退出轨道可用。此前baseline003完整成功保留，不用新业务失败推翻已验证的运行轨道。

执行口径补充：若基线只剩业务结果失败、模型/工具接线与来源/预算/资源/退出检查均通过，可以继续独立隔离控制的探索轮，验证HTTP归属、跨用户拒绝、实际暂停和换Worker；各主任务业务继续原标准评分，整轮失败仍失败。这是为了区分任务完成质量和隔离控制，不能据探索轮宣布完整I24通过，不为获得成功重复基线。

## 16. 新双用户探索轮已经完整执行

i24-da-fresh-smoke-101-001原判定（`scope/i24-da-fresh-smoke-101-001`）整体`passed=false`。新bench `5c6c0ea9…`、services `ea24a2ec…`的256项容器校准通过后，本轮完整执行主任务和独立取消子任务。

| 验证项 | 本轮实际结果 |
| --- | --- |
| 产品边界、真实重叠、重连及跨用户拒绝 | `product_boundary_overlap_reconnect`通过 |
| A/B自己的取消 | 两条均通过，分别3/4次模型请求，按原生证据验收 |
| A/B主任务 | 两条业务未通过，保留原失败 |
| 资源与生命周期 | 未通过，main-resume采样出现`transition_terminal_not_healthy`，继续核原始终态，不能豁免 |
| 同Worker复用、浏览器使用期隔离 | 均未测试；不能从工具任务推断 |

先前main-resume的闲置连接断开未再中止流程。本轮的通过项只代表已测边界，不把整体失败改为成功；新发现的退出采样问题与主任务业务分别保留。正式三种子仍未运行。

## 17. 删除期间的原始状态与新验证

第16节唯一采样错误的原件已定位：`main-tools-b`完整ID `0c46df8e59fcd90452cbbf6368eff103085e5058991cde706c70261f273ef0f7`在采样前仍运行，后一次inspect看到`Status=removing`、`Dead=true`、Exit143、非OOM。同ID原登记的`after_stop`为健康exited，除了Status/Dead外完整投影一致；采样窗口覆盖已登记的删除请求。这是删除期间的中间状态，**不能直接把removing/Dead认作健康终态**。旧失败不重新评分。

新接线保留中间原件，再独立inspect同一个完整ID，只有精确的不存在响应才形成absence证明；核对原停止记录、删除请求、完整字段和时序。确认后短锁重新读取登记，避免错用inspect前的旧快照；已存在字段若变化仍拒绝。正常终态来源始终是原始`after_stop`，不放宽137/OOM或未知Docker错误，不补造CPU/内存数据，5秒采样要求不变。清理新增`cleanup-result.json`，记录创建返回的确切ID、停止/删除/缺失证明、网络ID及最终同trial库存；查询失败或所有权不明仍失败，不按发现列表扩大删除范围。

scope-removal构建材料（`runtime/scope-removal-images-001`）固定bench `sha256:96fe603525839efe1c30a67246a0090c7bdc0c4c96674136ada24a7da6762397`、services `sha256:a68a4e1815cc529ff0ffa3b6c40ffba2e9b3a8dc1eec236326deb2f2567fe06d`，Worker仍为`3e4e2a2e…`。351项Docker校准（`runtime/scope-removal-check-001`）通过，覆盖删除交错、精确ID、异常终态、拒删foreign、删除未生效及费用闭合反例；断网、无模型、健康退出并清理。

i24-da-removal-101-001原判定（`scope/i24-da-removal-101-001`）已结束，CLI退出1：

| 验证项 | 新101轮实际结果 |
| --- | --- |
| 产品边界、真实重叠、重连及跨用户读取/控制拒绝 | 通过 |
| A主任务 | 完整通过，7次模型调用 |
| B主任务 | `original_business_task_failed`，保留失败；全量请求由费用台账另计 |
| A/B自己的取消 | 均通过，分别3/4次模型调用 |
| 实际资源、生命周期、采样 | 通过，host的errors/采样错误/超额全部为空 |
| 清理 | 正向清理原件（`scope/i24-da-removal-101-001`）为passed=true、failures=[]；本trial容器及网络最终库存均为空 |

本轮消除了已定位的实验台误判，同时仍给出业务失败，不能据此宣布整个I24通过。正式其余种子、DSH、浏览器使用期数据和同Worker复用继续是缺口；本轮每用户独立Worker不证明共享Worker的安全边界。

### 同材料第二、三组：202及303完整通过

i24-da-removal-202-001（`scope/i24-da-removal-202-001`）使用第17节相同pins、全新状态和seed202。`scope-verdict.json`与`host-result.json`均为passed=true；两个原业务任务均通过，各6次模型，两个取消子任务各4次，共20次。产品边界/并发/重连与实际资源生命周期检查均通过，host errors、sampling_errors、resource_overages均为空。

cleanup-result.json（`scope/i24-da-removal-202-001`）为passed=true、failures为空，最终同trial容器及网络库存均为空。取消语义仍是原生superstep边界协作drain，`immediate_cancel=false`；未验证浏览器隔离，不支持同Worker换用户复用。这些边界没有因业务通过而改变。

相同pins的第三组i24-da-removal-303-001（`scope/i24-da-removal-303-001`）已完整通过，两个主任务各6次模型、两个取消各4次，共20次；host无错误/采样错误/超额，正向cleanup通过、同trial容器网络最终为空。三组中两组整体通过、101组B业务失败；三组边界/并发/重连、资源生命周期与清理均通过。第一组101及所有历史完整性失败继续保留，不以新成功替换。
