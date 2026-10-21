# I25 启动和关闭期间连续采样：最小修正草案

状态：新轮换变体及服务 Init 最小修订已冻结，新 bench 六文件405项容器校准及无模型probe003完整观测门禁通过；001/002原失败和002副本诊断保留。按需N5 `i25-da-rotation-n5-101-001`已完整执行但成本子轮失败：148份采样无error且全阶段5秒检查通过，业务4/7，回收后空闲阶段29份资源完整、1份跨入冷读启动而不完整。N10暂停，原件和严格评分保留，I25未完成。

## 1. 要解决什么

现在启动 Worker 时，主线程拿着采样也要用的锁，等 Agent 就绪；关闭时也拿着这把锁，等进程彻底退出。等待期间，采样线程进不去。旧按需轮已看到 18 处超过 5 秒的间隔，常驻轮 14 处，其中关闭阶段有约 13 秒的间隔，整轮最大约 24 秒。原判定和原证据保留，见[已有离线审计](instance-cost-flow.md)。

新轮换入口增加了全阶段 5 秒检查，却仍复用这些长锁。不能断言每次启动、关闭都必然超过 5 秒；但只要其中一次持续持锁超过 5 秒，且发生在连续观测范围内，就会留下采样长空档或首尾缺口。增加业务轮数不能解决这个结构问题。

也不能只把锁删掉：Worker 可能在 `docker stats` 和 `docker top` 之间退出。此时少一行数据或命令失败，必须能证明是原来那个容器正常关闭，不能当成零用量，也不能把未知退出、OOM 或一般 Docker 错误放过去。

源码依据：

| 位置 | 当前持锁内容 | 风险 |
| --- | --- | --- |
| `scripts/run_instance_cost_full.py:311` 的 `worker()` | `NativeWorker` 整个构造，含创建、启动和等待就绪 | 就绪等待最长 60 秒，采样进不来 |
| 同文件 `close_worker()`，以及 `scripts/run_instance_cost.py:276` 的 `NativeWorker.close()` | 关闭 ACK、进程退出等待、reader 收尾、inspect、rm | ACK 和退出分别可等 60 秒 |
| `service()` / `retire()` | 创建、连接网络、启动，或停止、检查、删除 | 一组 Docker 操作可能占锁较久 |
| `probe()` / `sample_flow()` | 锁内等待 observer 回包；完整采样也取同锁 | 单次 probe 超时仍为 8 秒；不能据此放宽 5 秒采样标准 |

## 2. 改动范围

仅对显式 `--rotate-inactive-users` 的新轮换变体启用这套观测路径，计划和证据声明 `sampling_variant=registered-lifecycle`。旧默认流程、旧入口判定、旧 trial、I23 冻结源码和证据不改。

`scripts/run_instance_cost_full.py` 仅在轮换变体中转发到新文件 `scripts/instance_cost_lifecycle.py`。新文件沿用真实 DA resident 入口和原 `NativeWorker` 的命令/响应接口，单独实现创建、就绪和关闭边界；旧 `scripts/run_instance_cost.py` 不修改。独立判定在 `runner/instance_cost_lifecycle.py`，继续用现有 `rotation_sampling_check()` 验收全部阶段、单次跨度、连续序号和首尾 5 秒。

不在本次工作中重做调度器、产品身份体系或完整采样平台。

## 3. 主流程

正常路径是：预留配额 → 创建并登记确切容器 → 启动 → 在锁外等就绪 → 正常使用 → 登记关闭请求 → 在锁外等 ACK 和真实退出 → 保存健康终态 → 删除 → 确认确切 ID 与名称不存在。

每把短锁只管一件事：

- 登记锁只复制、追加内存登记。持锁期间不等待 Docker、就绪、关闭或 observer 回包。
- 清单锁只保护“创建后的确切 ID 登记”“ps 到 batch inspect”“rm 到确切 absence 和登记完成”这些不能被拆开的边界。网络连接、Agent 导入、就绪、关闭 ACK、进程 wait、服务 stop 等长等待均在锁外。单条 Docker 操作如果仍拖出长空档，本轮照样失败，不增加宽限。
- 控制锁只发布不可变 observer 控制文件和切换采样阶段，不等回包。另有一把首次失败锁，仅保存固定原因和原序号，不参与 Docker 清单或 Agent 等待。

资源测量不再拿着清单锁等完整生命周期结束。测量前后读取登记快照及实际 Docker 状态；stats/top 与退出发生交错时，按下一节的证据核验。创建登记完成后才能启动，配额预留覆盖尚未就绪的 Worker，不能因它还没 ready 就少算一个实例。

observer 的控制文件仍用不可变序号、nonce 和原 phase/kind 绑定。发布时短锁分配序号，等待回包移到锁外。一次采样的前后 probe 使用同一份采样 phase/kind，不能因主线程切阶段而改绑定。8 秒超时不变；某份完整采样超过 5 秒仍失败。

## 4. 状态和证据怎么记

登记始终保留同一个确切 ID 的全生命周期，不在发出 stop 或 close 时就删掉它。预期状态与实际 Docker 状态分别记录，“正在启动”“已请求关闭”只表示控制请求，不表示进程已经就绪或退出。

| 实际状态/边界 | 必须保存什么 | 采样如何处理 |
| --- | --- | --- |
| `created` | 创建返回的完整 ID；实际 inspect；名称、image、trial、role、配额、挂载；启动前登记 | 容器存在且已登记；尚未运行不等于缺测 |
| `running` | 原 ID 的实际 running inspect；启动请求及就绪回包来源分别保存 | 对实际运行的容器采 stats/top；就绪 ACK 不能替代 Docker 身份 |
| `exited` | 关闭请求在先；实际 exited inspect；原 ID/名称/image/归属不变；PID=0；实际退出码和 OOM 等标记 | 正常 Worker 关闭必须 exit0 且非 OOM/Dead/Paused/Restarting、无 runtime error；ACK 单独不够 |
| `removing` | 删除前已独立观察到的允许终态；删除请求；实际 removing 快照；随后同 ID absence | 它是过渡，不是健康终态；不能只见 removing 就算正常删除 |
| 已不存在 | 原注册、原终态、精确 rm 返回、同 ID 和名称的严格 absence | 从本次清单中消失可以解释；登记历史仍保留 |

stats/top 出错只检查有限的已知过渡：原 ID 已登记，关闭/删除请求有来源，实际终态符合该角色的既有规则，命令 rc/stdout/stderr 与该确切 ID 的“已停止/不存在”等已知结果一致。发生 EOF 时，也必须有同 ID 的 top 结果和实际终态，不能接受任意 EOF。没有足够证据就记录样本错误并让本轮失败，不等待多次观察来制造成功。

普通正常关闭不接受 exit137 或 OOM。已有故障阶段若主动杀 A，137 只能对应原来那份确切 A 的注入记录、杀前 live 状态和杀后非 OOM 的 exited137 状态；单独记作故障注入过渡，不能称为健康退出，也不能授权其他 Worker 使用 137。服务的退出码沿用其冻结角色策略，不能把 I24 的 `0/143` 直接套给必须正常 exit0 的 Agent Worker。

本次最小实现不新增通用的 `removing` 解析器。清单锁把正常 rm、同 ID absence、同名称 absence 放在一个边界内，采样只能看见删除前终态或删除后缺席。若实际仍观察到 `removing`、Dead 或未解释状态，当前草稿拒绝该样本。

## 5. 复用 I24 的哪些部分

I24 已有 `registrations`、`retirements`、`bench_completions`、`inventory_absences`，并独立核验正常退出、stats/top 退出交错、removing 和确切删除。优先借用这些已形成的字段和判断逻辑，避免再设计一套“忽略退出错误”的规则。

可参考 `scripts/run_multi_user.py` 的 `explained_sampling_transition()`、`recorded_removal_matches()` 和 `runner/multi_user.py` 的对应独立核验。I25 只做薄的身份字段与角色策略适配：instance-cost trial、native Worker 的 exit0、原故障注入终态都要保留自己的规则。复用证据结构不等于借用 I24 通过结果；不要为本次工作大范围改写 I24。

每个新样本保存实际 Docker 前后投影、完整 stats/top 命令参数、目标 ID、开始/结束时刻、rc/stdout/stderr，以及原登记/请求的 SHA 引用。最终登记文件包含创建、启动请求、关闭/故障请求、实际终态和精确删除五张表，只有整轮退出、服务收尾后才冻结。

独立评分按“登记时刻 ≤ 清单时刻 < 同名称 absence 完成时刻”重建每份前后清单必须包含的完整 ID 集；即使把清单、stats、top 和过渡记录一并删空，也不能通过。关闭请求必须早于实际负面命令结束；`No such container` 还必须有在该命令结束前发出的同 ID rm 请求。之后才发出的 close/rm 不能解释之前的 Docker 异常。

缺失资源读数不补 0。过渡样本声明 `resource_measurement_complete=false`；某个阶段只要有一份资源不完整，就保留原始已测行，但不输出整阶段资源或实际进程数的均值/峰值。idle 阶段必须没有过渡且实际资源身份全量，否则失败。

首次采样错误或原 5 秒时序违规会锁存固定原因和原序号，保留原失败样本。新业务 wave 和每次 `run_task` 提交前检查该状态；已失败就走既有异常清理，不提交后续付费任务，不重放在途任务。仍在等待的一份样本若已超过 5 秒，也不能继续放行新任务。最终独立 grader 继续重核所有原样本，门禁记录不替代评分。

## 6. 先跑什么无模型验证

用当前机器已有精确 image ID，新建独立 probe 目录。材料准备在 bench 镜像内完成，宿主只控制 Docker。Agent Worker 使用真实 DA resident 入口、真实绑定和公开状态接口；网络为 none、不注入模型密钥、不发业务任务。fixture 与 observer 使用真实实现，验证生产 `service()`、`probe()`、`sample_flow()` 和关闭路径；不据此声称未启动的模型代理或其他服务已经验收。

流程先收集稳定样本，再依次做三组真实启动/关闭：001/002、原来未活跃的 003/004、再启动 001/002。每组最多两个 Worker，执行历史准备/读取或状态读取后正常 close，全程持续采样。结束后保留真实末样本、采样线程状态、每个容器的退出和删除证据。该探针不运行模型，也不作为轮换业务通过结果。

并行上限按实际启动清单核对，不给每个 Worker 单独一份总预算。使用现有配额时，双 Worker 4 GiB/1.5 CPU，加 fixture 与 observer 后为 4.5 GiB/2 CPU；探针在连续窗口内另启停一个使用独立数据库的真实 fixture，观察实际 4.75 GiB/2.25 CPU 组合。bench 准备与 Worker 阶段分开。配额独立评分还按全部登记到真实终态的区间折叠，覆盖落在两次采样之间的短命容器。若额外加入工具或代理，仍须在整组 6 GiB/3 CPU 内逐个预留。

探针源码在 `/tmp/harness_lab_instance_lifecycle_probe.py`，由主线程在冻结后串行执行。旧 bench `sha256:bbd604f4fb4177dec7b77b6f2cfc050a3d859961505bdbe869869fc4c7d8c186` 不包含本次新模块，不能直接据此验收；探针在准备历史或启动 Worker 前先核验 bench 内三个独立评分模块的源码 SHA 与冻结宿主一致。`compose/Dockerfile.bench` 的 `COPY . /app/` 已覆盖新增文件。服务 Init 修订使用的新 bench 已固定为 `sha256:863b70d9c145cd69c760e57418afbc9189054401ead63c29c073ea716e944b3d`，六文件 405 项校准只证明该版本代码校准通过。

成功的 Docker inspect/image inspect 日志只保存白名单投影和原输出 SHA/字节数，不先写出原 `Config.Env` 值；严格 absence 的 `[]` 和错误原文保留。Worker 断网且无模型密钥，不启动代理/工具、不提交业务任务，因此探针不能证明这些未运行服务、模型成本、故障恢复或业务质量已经通过。

真实 probe001 的额外 Fixture 在 `docker stop` 约 10.177 秒后实际退出137、非 OOM，普通服务只允许0/143，故严格拒绝并保留原失败。随后仅给新轮换服务增加 `--init`，创建登记和独立评分均核对真实 `HostConfig.Init is True`；Worker、旧全局投影与退出码策略不改，见[修订说明](instance-cost-service-init-revision.md)。405 项校准不能代替该修订的真实探针验收。

[probe002 原结果](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-002/protected/probe-result.json)为 `probe_passed=false`、`grade_passed=false`、`cleanup_confirmed=true`；[采样线程原记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-002/protected/rotation-sampler-status.json)保存35份样本和 `completed=true`，原JSONL没有采样错误行。但[命令710原stderr](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-002/protected/commands/command-000710.stderr)显示绝对脚本 `/probe.py` 导入 `runner.instance_cost` 时失败，grader退出1，未写 `protected/probe-verdict.json`；宿主随后读该文件才记录 `FileNotFoundError`。该次原执行没有完成独立评分，清理成功不等于整轮通过。原判定、冻结材料和001/002原件保持不变。

独立材料副本随后追加离线诊断。[i25-probe002-independent-audit-check-001宿主记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-probe002-independent-audit-check-001/host.json)确认断网、无模型调用、评分容器exit0、`passed=true`、`cleanup=true`；[副本verdict](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-probe002-independent-audit-001/input/protected/probe-verdict.json)23项检查全过，35份样本、最多2个Worker，完整登记区间的配额峰值5100273664字节/2.25 CPU。原002的2748个文件SHA全未变。该诊断定位评分入口导入接线，不为原002补判通过，也不代替新轮探针门禁。

新helper SHA256为`a2a615652404cbe7879cc4031beb6d63ca51145bb990c8f7701f282758366d52`，仅给最终独立grader补`PYTHONPATH=/app`，先保存原执行证据、缺verdict即失败；见[评分入口修订说明](instance-cost-probe-import-revision.md)及[修订冻结清单](instance-cost-probe-import-source-freeze.sha256)。

全新 `i25-native-lifecycle-sampling-probe-003`已实际结束、exit0。[原结果](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-result.json)为`probe_passed=true`、`grade_passed=true`、`cleanup_confirmed=true`、`host_error_type=null`；[原verdict](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-verdict.json)的35份样本、23项检查全过，最多2个Worker、登记配额峰值5100273664字节/2.25 CPU。[独立评分进程完整性](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-grade-process-integrity.json)为`passed=true`、CLI及Docker exit0；[确切清理](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-cleanup.json)为`unconfirmed_resources=[]`、无遗留容器。003完整观测门禁通过，001/002原失败不改。003为0业务/0模型，不证明I25业务轮换、成本节省或故障恢复通过。

## 7. 源码文件职责

| 文件 | 作用 |
| --- | --- |
| `scripts/instance_cost_lifecycle.py` | 显式轮换专用的短锁边界、真实 Worker 启停、observer probe、前后清单和命令采样、首次失败门禁 |
| `runner/instance_cost_lifecycle.py` | 无 SDK/Docker 调用的独立身份、时序、健康终态、精确删除、全生命周期配额和资源完整性核验 |
| `scripts/run_instance_cost_full.py` | 仅轮换时接入上述路径；样本原件落盘后锁存错误；新付费提交前检查；正常收尾冻结登记 |
| `runner/instance_cost_full.py` | 仅轮换增加登记原件评分和资源完整性；旧默认汇总保持原算法；全阶段 5 秒规则不放宽 |
| `tests/test_instance_cost_lifecycle.py` | 合成身份/时序/异常反例、同步漏掉全部行、未来请求、缺测当 0、observer 等待释放锁；不是实际框架通过证据 |
| `tests/test_instance_cost_lifecycle_host.py` | Events 固定 native ACK 和真实 wait 边界，检查采样锁可用；首次失败锁存与阻止新提交的合成反例 |
| `/tmp/harness_lab_instance_lifecycle_probe.py` | 真实 Docker 无模型观测探针，独立冻结 bench 评分和精确清理证据；001/002原失败保留，003完整观测门禁通过 |

## 8. 执行任务表

| 步骤 | 内容 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| L1 | 确认本草案的最小范围 | 新轮换变体明确；旧流程、旧证据、5 秒/8 秒标准保持原意；不先跑付费 N5 | 已经主线程确认范围 |
| L2 | 拆出就绪和关闭等待，补确切登记与删除边界 | 长等待不持采样锁；created 到删除都能按原 ID 核对；配额在启动前预留；真实 stats/top 退出交错有严格来源；缺测不补 0；失败不提交后续付费任务 | 实现和独立定点静态复核完成；无模型probe003的完整观测、评分和清理已通过，业务轮换另验 |
| L3 | 补必要反例与断网容器校准 | 锁外长等待仍可采样；错误/缺失 ID、未知 Docker 错误、未登记消失、未知137/OOM、ACK无终态、未来请求、缺测当 0、门禁失效均拒绝；默认旧入口行为保持 | 生命周期修订历史350项、服务Init修订六文件405项断网容器校准通过；各自原记录保留，不代替真实probe |
| L4 | 固定新 bench/宿主源码，运行上述无模型真实探针 | 全部阶段、单份采样跨度、相邻起点及首尾均≤5秒；无样本错误/未知过渡；Worker健康exit0；每条命令有状态；精确ID清理无遗留 | 已通过：全新003的35份样本、23项检查、独立评分进程及确切清理均通过；001/002原失败和副本诊断保留；不作为业务通过 |
| L5 | 根据探针决定付费轮换 | 探针完整通过后再按需N5；N5结束并独立评分/清理后再决定N10；任一完整性错误先停，不豁免、不覆盖旧结果 | N5完整执行、独立评分和清理完成，成本子轮失败：业务4/7、回收后空闲资源完整性失败；003/004首次业务及历史/状态读回通过。N10暂停 |

独立副本离线诊断和全新probe003均已完成，003完整观测门禁通过。N5的[原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/flow-verdict.json)为`cost_subset_passed=false`，评分进程完整性通过、清理无遗留。7个可评分任务4个通过；`active-user-001`、`wake-user-001`、`fault-user-002`各只在`all_pages_read`失败，实际使用`page_size=100`违反每次最多5条约束。故障复合项还要求B业务通过，因该分页约束失败而false；B在A实际退出后仍有原生进展，单独检查为true。

N5的148份采样无sample error、全阶段5秒和精确登记检查通过，但[第82份原样本](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/flow-resource-samples.jsonl:82)使回收后空闲资源完整性失败：样本开始时标记`recycled-idle`，前清单只有Fixture和observer；空闲结束后立即启动业务状态冷读，后清单新增user001冷读Worker，缺少它的本份stats/top。因此前后集合不同，`resource_complete=false`成立；该阶段29份完整、1份不完整，整体均值/峰值不输出。既有服务命令均正常，缺口发生在阶段交接，不是采样节奏超限。

**N10暂停，N5保留为探索失败。** 空闲退出没有等待旧阶段在途样本落盘，来源见[原执行顺序](/Users/hanbowen01/repo/puff/scripts/run_instance_cost_full.py:626)、[阶段快照与清单边界](/Users/hanbowen01/repo/puff/scripts/instance_cost_lifecycle.py:306)及[资源完整性规则](/Users/hanbowen01/repo/puff/runner/instance_cost_lifecycle.py:359)。本次只记录既有证据，不改标签、删样本、补零、放宽规则或重跑；后续是否修订既有阶段交接另行决定。完整结果见[流程文档](instance-cost-flow.md)，`i25_complete=false`、`savings_percent=null`保持不变。
