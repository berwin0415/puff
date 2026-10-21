# I25：双任务、回收、唤醒与单 Worker 故障的接线

2026-10-21。**两用户按需与常驻探索均完整执行：两者业务均3/5，漏读分页保留；回收/保留对象、原状态读回、两新任务唤醒及杀A后B原生进展均成立，整轮仍失败。旧两轮全阶段采样审计均失败，不能证明完整峰值或资源成本；已观测的两个空闲阶段内存均值按需31/48 MiB、常驻282/313 MiB。本实验按需唤醒较慢约8秒，没有成本收益比例。** 实际镜像和校准见第6节，历次基线和观察协议结果见第7节，双策略探索见第8节。本机实施状态继续由根目录 `IMPLEMENTATION_PLAN.md` 管理，本页不把I25登记为完成。

补充复核发现：**旧轮次通过的采样检查只约束空闲窗口的节奏，没有要求启动、控制、关闭等所有阶段的采样间隔均不超过5秒。** 两轮旧采样审计均失败，旧判定和第8节原始样本描述保留。后续生命周期probe001因真实exit137失败，probe002因独立评分入口导入失败；两轮原失败及002副本诊断保留。服务Init修订六文件405项校准、全新无模型probe003的完整观测门禁已通过。按需N5 `i25-da-rotation-n5-101-001`现已完整结束但成本子轮失败：7个可评分业务任务4个通过，148份采样无error且全阶段5秒检查通过，但回收后空闲阶段有1份跨入冷读启动的资源不完整样本。N10暂停，N5保留为探索结果，I25和成本节省均未通过；具体证据见本页末段。

## 1. 说人话：本轮补上什么

此前的入口只能回答“真正加载框架后，空闲 60 秒占多少资源”。新入口补上更接近使用过程的一条链：两个用户各做真实工具任务，任务结束后等 60 秒，再各发一个新的任务，最后用另外两份干净任务，只强制退出 A 的 Worker，观察 B 是否继续并完成。

两种策略使用同一个候选镜像、同一个入口、同一模型和工具服务、同一配额。常驻方式保留最近自然完成任务的真实 graph、模型客户端、工具桥和 saver；按需方式正常关闭这些对象并删除 Worker。再次执行使用新任务、新 scope、新原生状态、新工具凭证和新模型预算，不把未完成的原任务改名获得新额度。

回收后的检查读的是**刚才真实完成业务的同一 checkpoint 和消息**。常驻方式必须还是原容器、原对象；按需方式必须是新的容器，使用原 SQLite 通过公开 `aget_state` 读回相同内容。最初准备的应用哨兵只用于证明用户状态不是空目录，不能替代这次业务状态检查。这里没有恢复网页现场，也没有把状态冷读说成未完成任务续跑。

## 2. 明确留下的边界

这是受宿主可信编排的成本测量入口，**不是产品后端，也不重复证明 I24 的身份、控制和隔离能力**。成本采集器确实运行在一个 256 MiB / 0.25 CPU 容器里，占用设计中为后端预留的档位；它只采集真实原生活动，不提供用户身份服务。它的开销会计入，总配额仍不超过 6 GiB / 3 CPU。真实产品后端的开销和 resident 入口的 I24 接入仍是缺口，不能拿旧入口的 I24 结果直接代替。

故障轮只研究单 Worker 的影响面：采集器看到双方真实模型/工具活动和成功工具进展后，宿主再确认当前活动，才对固定完整 ID 的 A 发 SIGKILL。A 的退出必须保留为异常，不计入健康关闭；B 必须在这次退出之后还有真实原生进展并正常完成。退出界标采用 A 的原始 Docker `State.FinishedAt`，和 B 原生事件同属 Docker VM 时钟；后续 inspect/probe 的到达时间继续原样留存，不拿它们代替退出时间。缺少有效结束时间或 B 没有可证进展时，该项保持未验证。未命中活动窗口就保留未命中，不补触发时间。

**本入口不恢复 A。** A 原生状态、原 token、cap=20 的同一个预算库和实际写入账本继续保留；不追加 prompt、不输入 `None` 重放、不整任务重跑，也不另建预算。退出时的在途模型、工具和写入不确定性保留原件，交给 I23 独立判定。报告中的恢复时间和额外恢复调用保持未测。

这些边界决定当前输出始终是 `i25_complete=false`、`savings_percent=null`。即使源码执行后的 `cost_subset_passed=true`，它也只表示本页限定流程通过。DSH 尚未接入这条成本流程；三种子完整两策略比较、真实产品后端开销和本入口的 I24 隔离仍待补。

## 3. 文件分别负责什么

| 文件 | 作用 |
| --- | --- |
| `runner/instance_cost.py` | 原来的固定配额、用户历史材料和空闲子轮判定；继续保留 |
| `runner/instance_cost_full.py` | 在 bench 内创建真实 Fixture 任务和独立凭证；在观察容器内读取原生事件；离线核对业务结果、双活跃、预算、原生状态和完整资源清单 |
| `scripts/run_instance_cost_full.py` | 宿主只做 Docker 控制、可信 stdin 投递、采样和清理，不导入候选 SDK，不请求模型，不计算任务答案 |
| `scripts/run_instance_cost.py` | 原空闲入口；仅增加可选网络参数和长业务响应等待，旧 `run-idle` 默认仍为 network none |
| `scripts/audit_instance_cost_sampling.py` | 仅在Docker内离线审计旧轮次全部采样阶段；原trial只读，另存新审计，不改旧verdict，未知首尾和完整性不补成通过 |
| `adapters/deepagents_langgraph/resident_runtime.py` | 同用户常驻入口。保留最近自然完成任务对象；执行下一份独立任务之前正常释放上份对象；最终回收时正常关闭 saver 和 HTTP 客户端 |
| `tests/test_instance_cost_full.py`、`tests/execution/test_deepagents_resident_runtime.py` | 配额、伪并发、重复采样、预算替换、原状态替换和对象生命周期的合成边界校准草案，不能代替实测 |
| `tests/test_instance_cost_sampling_audit.py` | 离线审计反例：非业务阶段长空档、漏行、时钟倒退、序号和首尾缺口、未完成写盘、凭空补零、输出泄漏和覆盖旧原件；已纳入313/350项历史校准及当前六文件405项容器校准，均不代替真实生命周期probe |
| `tests/test_instance_cost_service_init.py` | 新轮换服务`--init`与实际Init严格核验、旧清单兼容及退出码反例；已纳入405项校准；probe001/002原失败保留，003完整观测门禁通过 |

新代码必须烘焙到新的 bench 镜像。候选 resident 文件变更必须复制到**新的** stage 上下文并重新生成完整 `LAB_ADAPTER_MANIFEST.json`、export hash，再构建新候选镜像。已有上下文、旧镜像、I22/I23/I24 原证据均不覆盖。services 镜像运行已有 Fixture、工具和代理；观察容器使用 bench 镜像中的新模块，但按观察者档位限制资源，不在活跃期间运行离线评分。

## 4. 一轮怎样执行

1. 固定候选、bench、services 的确切 image ID、模型名、种子、策略、用户数和 pair ID。5/10 用户常驻档按原 2 GiB / 0.75 CPU 每 Worker 的分配会在启动 Docker 前被拒绝，这是预算拒绝，不是实测 OOM。
2. 启动真实 Fixture，在 bench 中创建 N 份独立历史材料和所有独立任务。逐用户通过真实 graph API 准备非空历史并健康退出；准备不是 Agent 业务完成。
3. 启动真实观察器并持续采样。常驻策略启动 N 个已加载候选的 Worker；按需策略保持零 Worker。观察 60 秒。
4. 给固定两个用户分别启动一套工具和模型代理；各自在自己的网络里解析 `tools`、`model-proxy`。统一进入活跃阶段，真实执行两项工具任务。不能只用两个容器或 Future 同时存在证明并发；双方要有真实模型/工具活动交集、交集中的有效工具进展，以及执行重叠内至少两份原始资源采样。
5. 任务正常结束后，常驻保留自然完成业务的真实对象，按需正常关闭并删除 Worker；两策略都停止附带工具/代理。继续观察 60 秒，再核对原业务 checkpoint。按需冷读本身的额外新容器和耗时也留证。
6. 用另一份新的相同口径任务再次唤醒两用户。保留可信编排收到请求、Worker 就绪、首次真实原生活动的时间来源。没有产品队列，所以队列时延保持未测；不能把这个数直接当现网 API 端到端时延。
7. 关闭前一阶段的 Worker，再用独立 fault scope 启动两项新任务。只在真实活动窗口内退出 A；B 继续。A 不恢复，原件保留。
8. 所有 Worker 和工具/代理停下后，bench 读取真实 Fixture 账本与各任务原预算；停止观察者和 Fixture 后再做离线总验收。所有新建资源按确切归属清理，失败日志和原材料保留。

评分进程也必须单独验收。`grade` 可以用退出码 1 表示原始 `cost_subset_passed=false`；即使调用时允许业务失败，仍须核对确切容器 ID、名称、镜像、配额、真正退出且 PID 为 0、没有 OOM/Dead/Paused/Restarting/Error，并且 Docker exit 与 CLI exit 均和原始布尔判定对应的 0/1 一致。先写出通过文件、随后异常退出或 OOM，不能采信为通过。原始 verdict、日志、完整容器观测和独立 `grade-flow-process-integrity.json` 分别保留，不回写覆盖原 verdict。

采样包含每个完整 Docker ID、同镜像的每个 Worker、cgroup 配额、`docker stats`、实际 `docker top` 原文。观察者通过受保护的 nonce 请求回执，给每次资源采样提供同一 Docker 时钟内的前后原生活动观测，避免直接混比 macOS 和容器 UTC。两秒是目标间隔，实际空档照实保存；没有补造时间戳。

远程供应商 key 仅通过 Docker 环境传给代理，临时环境文件位于 trial 之外，清理时删除，不进入 `/trial`、候选挂载或证据包。Worker 只获得自己任务的本地代理 token。

## 5. 交给执行者的命令

先在**新固定 bench 镜像内**运行校准：

```bash
python -m pytest -q tests/test_instance_cost.py tests/test_instance_cost_full.py tests/execution/test_deepagents_resident_runtime.py
```

这是容器内命令，不能在宿主执行。本次100项校准已运行通过；另有真实候选断网历史准备、冷读和关闭探针通过，详见第6节。该探针没有执行真实业务，业务完成后的对象保留、在线工具轨道及成本流程仍须在后续基线和双策略子轮验证。

先用新入口和相同服务镜像做单任务小配额基线：

```bash
python scripts/run_instance_cost_full.py run-baseline \
  --trial-id i25-da-baseline-101 --pair-id i25-da-101 \
  --strategy on-demand --users 2 --seed 101 \
  --candidate-image sha256:REPLACE_EXACT_ID \
  --bench-image sha256:REPLACE_EXACT_ID \
  --services-image sha256:REPLACE_EXACT_ID \
  --model-name SELECTED_MODEL \
  --output-root /ABSOLUTE/DATA/runs/DATE/deepagents_langgraph/instance-cost/NEW_BASELINE
```

新入口本身是宿主 Docker 编排脚本。基线实际仅执行 user-001 的一个业务任务，其余任务只是独立准备材料，不能把它们计为已完成。通过后，把 action 换为 `run`，使用新的 trial/output 路径分别执行 resident、on-demand；同一组使用相同 pair ID、种子和三个镜像 ID。`plan` 只打印配置，既不运行 Docker，也不证明任何能力。种子按 101、202、303 顺序留存所有结果，不为追求通过覆盖失败原件。

| 待执行项 | 验收 | 当前状态 |
| --- | --- | --- |
| 新材料 stage、冻结和镜像构建 | 候选自身完整清单包含新 resident 文件；bench 包含新 runner/scripts/tests；旧上下文不覆盖 | 已完成，完整ID见第6节 |
| 新 bench 合成校准 | 上述校准真实容器退出成功，保存镜像 ID 和原始日志 | 002为100项通过；001收集期语法错误保留 |
| 真实候选无模型生命周期校准 | 新镜像真实 history prepare/read/close，无模型请求、无 OOM、清理成功 | native-probe-002通过；001准备期导入失败保留 |
| 同入口单任务基线 | 自然完成、真实业务判定、cap=20、累计活跃≤300秒、同服务和0.75 CPU档位 | 001预检停止；002/003业务和观察未通过；004观察/采样等检查全过，业务分页仍失败，见第7节 |
| 双策略真实成本子轮 | 原任务完成、真实双活跃、两个60秒窗口、原业务态可读回、新任务唤醒、单Worker故障影响面及全清单 | 两用户探索及旧采样补审计均失败；无模型probe003门禁通过。N5完整执行但业务4/7、回收后空闲资源完整性失败，N10暂停，详见末段 |
| I25 完整结论 | 本入口所需I24隔离和产品后端开销、三组配对、A故障恢复/I23不确定窗口、DSH缺口均明确解决或报告限制 | 尚有明确缺口；不能宣布完成 |

## 6. 本次实际材料、校准和断网探针

[lifecycle-images-001](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/lifecycle-images-001/builds.json)已串行构建：候选`harness-lab-deepagents-langgraph:instance-runtime`完整ID为`sha256:46a03db9203b0facef1399eaa590ba31640e28a15c2a0410c8b037d3a95575ed`，bench `harness-lab-bench:lifecycle-checks`为`sha256:5ac51f8ecc0407aadb0e69cbd33bb9aa7d6e2bb20d727f398a030671a223442e`。候选来自独立`deepagents_langgraph-instance-runtime`上下文，adapter export `1b39616294eb6895726ef22330239d85a620b10ba32281d7326d1c589128f8f2`；不是I24的multi-user Worker镜像。真实业务所需services尚未在本探针中启动，不能借构建成功认定在线工具已经可用。

[i25-cost-check-001](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/i25-cost-check-001/host.json)在收集期失败：`scripts/run_instance_cost.py`容器清单投影缺右花括号，两个测试模块报语法错误、退出2。修复并换新bench后，[i25-cost-check-002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/i25-cost-check-002/host.json) **100项通过，0.52秒**；原001保留。002为512 MiB/0.5 CPU、network none、无远程key/模型调用，退出0、PID0、无OOM/error，确切容器清理通过。它校准代码和判定边界，不产出真实成本数据。

第一次[native-probe-001宿主记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/i25-native-probe-host-001/host.json)只跑到bench准备阶段：从`/calibration`执行探针时找不到`runner`包，prepare退出1，尚未启动候选原生阶段。给prepare补上镜像内`/app`的`sys.path`后，使用新目录运行[native-probe-002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/i25-native-probe-host-002/host.json)，原001未覆盖。

002先在固定bench准备两份用户材料，再只启动user-001的真实候选。候选容器内两个独立子进程（PID7、11）依次准备应用历史、关闭、从同一SQLite冷读、关闭；实际加载`ChatOpenAI`、`HttpTools`、`CompiledStateGraph`和`AsyncSqliteSaver`，读到同一checkpoint，均退出0。候选为2 GiB/0.75 CPU；bench和候选都network none、无远程key、无OOM/error，确切容器清理通过。[原生探针verdict](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/instance-cost/native-probe-002/probe/verdict.json)为passed=true，SHA-256 `7fc6028899a2ac4a1a8b7db32f7178d72d1753fa99f51fce683a97267cf3afed`。

说人话：新入口确实能装载真实框架对象，保存准备的历史，换进程读回并正常关闭。它发出的模型/业务执行命令为0，明确`business_task_completed=false`、`cost_or_capacity_measured=false`、`i24_isolation_verified=false`、`i25_complete=false`。它没有验证在线工具、真正完成业务后的保留、双用户并发、60秒资源窗口、回收/唤醒或故障影响。后续同入口0.75 CPU单任务基线已尝试两次，均未整体通过，见第7节；不能从探针通过推算节省比例。

## 7. 真实基线留下的结果

| 基线 | 实际发生了什么 | 当前判定 |
| --- | --- | --- |
| `i25-da-baseline-101-001` | 启动时缺必需模型环境配置，在预检阶段停止，未执行真实业务 | 基线未通过；[原错误记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/instance-cost/i25-da-baseline-101-001/protected/flow-host-error.json)保留 |
| `i25-da-baseline-101-002` | 只执行`baseline-user-001`，5次真实模型请求均收到200，原cap=20；原生正常完成，3次写请求、3条实际写入 | 业务仅`all_pages_read`未通过，观察器及资源采样也未通过；[原独立判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/instance-cost/i25-da-baseline-101-002/protected/baseline-verdict.json)为`cost_subset_passed=false` |
| `i25-da-baseline-101-003` | 仍未通过完整业务；新诊断保存67条控制文件读取错误，原始来源为`read_probe/read_phase`，不是原生任务事件配对失败 | [原独立判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-baseline-101-003/protected/baseline-verdict.json)中业务和`no_observer_errors`失败；其余配额、原生历史、进程、正常关闭和采样检查通过，清理无遗留；整体仍失败 |

002的报告内容、准确写入、scope、审计和敏感操作限制均通过；原业务checkpoint可读回，消息非空，没有待执行任务。它不是无业务的空壳探针，但分页失败必须保留，不能凭写入正确就算完整任务成功。本轮没有注入故障、没有恢复A，也没有执行两策略比较。

实验观测同样没有过关：原判定的`no_observer_errors=false`、`no_resource_sampling_errors=false`。只有启动阶段留下5份资源样本，实际业务及关闭阶段均无有效资源样本；**不能用启动期样本推算任务成本、峰值或资源节省。** 当前应先修正观察器/采样，再在新材料、新目录下校准并重跑基线。

清理和评分进程本身有有效证据：[清理记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/instance-cost/i25-da-baseline-101-002/protected/flow-cleanup.json)为`unconfirmed_resources=[]`；原判定确认已测配额、独立用户历史、确切Worker/服务非OOM、无遗留容器及正常关闭检查通过。[独立评分进程完整性](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/instance-cost/i25-da-baseline-101-002/protected/grade-flow-process-integrity.json)为`passed=true`，包含确切容器/镜像/配额、实际退出、非OOM及CLI/Docker退出与原失败判定一致。说人话：评分进程可靠地给出了失败结果，不是业务或成本流程通过。

因此002仍保持`i25_complete=false`、`i24_isolation_verified_by_this_entry=false`、`product_backend_measured=false`、`savings_percent=null`。001、002及断网探针均保留原状；当前没有可报告的成本收益比例，双策略、三组配对和第2节所列缺口继续待验证。


### 追加：观察器诊断校准

新观察器把phase读取也纳入异常记录，并给出固定stage/reason及适用的源文件/行号，不输出事件正文。完整非法JSON、任务标识或scope不符仍拒绝；半行只忽略未完成尾部。宿主入口明确核对`HARNESS_LAB_DATA_ROOT`，不再让缺失变量只表现为展开Compose失败。任意观察器或采样错误仍使整轮失败，8秒probe及资源判定不放宽。

这些改动连同正反回归在`native-diagnostics-check-001`容器内通过（相关总计523项，9按候选路线跳过），bench为`sha256:8a58c351654effb338e00d332b9bc1f71c49a0c25bc0c24b7fc28bb3c0bb53b5`。原002的ValueError具体根因仍未证实，必须据新实测诊断；校准不替代真实基线或成本子轮。

### 追加：实际目录挂载与控制文件协议

003把问题定位到宿主传递phase/probe的文件检查。随后改为同一descriptor读取、完整reply发布，新bench `observer-snapshot`为`sha256:86af6bf0e8b77ad43f4ae9ae91e748b4e718d77dca4bd8837b6e90dcc51de5cb`，125项容器校准通过。但真实宿主目录挂载测试001、002仍失败，不能把这项修复写成已解决。

[mount-check-002](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/observer-control-mount-check-002/host.json)记录旧、新reader分别3804、3805次错误，成功36、35次。错误瞬间路径stat为普通文件、links=1、size=45，而紧接的open为`FileNotFoundError`。它证明此挂载下反复同名原子替换存在可见性问题；没有证据证明此前基线的错误瞬间也是相同metadata，不倒推link count为0。

改用每次新的不可变文件名后，[mount-check-003](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/observer-control-mount-check-003/host.json)在真实挂载中300/300条完整读取、0错误，断网、无模型、健康退出并清理。这仅验证了传递方式，完整phase/nonce绑定、观察器和真实基线还要用新镜像验证。任何错误仍使本轮失败，8秒probe和资源采样要求不变。

### 追加：新控制协议和第四条真实基线

[control-protocol-images-001](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/control-protocol-images-001/builds.json)固定新bench `sha256:910594f3c628614a18866fa22b14297dada17b03dbbb8b370f21fd4b5b0682fe`。每条控制以独立12位连续序号文件一次发布，内容同时绑定阶段、任务类型和可选nonce；观察器按顺序消费，宿主严格核对四项reply绑定。不可变文件仍要求nlink=1、普通文件、NOFOLLOW和4096字节上限；8秒probe不变。

348项容器校准通过；[实际挂载完整协议校准](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/observer-protocol-mount-check-001/host.json)的63条控制、60探针、三阶段往返全部绑定正确，零观察错误，断网无模型，正常退出并清理。

[baseline004原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-baseline-101-004/protected/baseline-verdict.json)中，除`all_actual_business_tasks_passed`外其余七项均通过：观察/资源采样无错误，配额/真实历史/非OOM进程/正常关闭/无遗留有效。单任务原生自然完成、5次远程模型请求、3笔写入，但`all_pages_read`仍失败；不能把观察器修复当作业务通过。清理`unconfirmed_resources=[]`。

当前完整业务基线未通过。后续只允许进入明确标记的生命周期/资源探索子轮，规则如下；I25保持进行中，`i25_complete=false`、`product_backend_measured=false`、`savings_percent=null`；后续不得仅为挑成功重复同材料/种子。

### 业务失败时仍可验证的独立部分

004已证明同入口模型、工具、原生状态、0.75 CPU配额及完整观察链可运行，唯一失败是模型未按分页要求完成业务。因此可以继续按冻结输入执行双策略的探索子轮，留存真实空闲、任务活动、回收、冷读、唤醒及单Worker故障的证据。所有业务失败继续计入原判定，`formal_i25_eligible=false`和`i25_complete=false`不变；完整业务门槛、同入口I24及产品后端缺口未解决前，不计算成功任务成本或节省百分比。

这是对原“基线通过后”的分拆：基础设施完整性失败仍阻止后续；单纯业务失败不阻止无关的原生生命周期/资源观察。不得改变提示词、扩大预算或重复同材料挑成功，不能把探索数据包装为完整成本比较。

## 8. 两用户按需探索的实际结果

[i25-da-od-101-001原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-od-101-001/protected/flow-verdict.json)已生成，宿主退出1，独立评分进程验收通过，清理`unconfirmed_resources=[]`。固定候选为`46a03db9…`、bench为`910594f3…`、services为`1814a728…`，均用完整image ID启动。

- 两个用户确有真实模型/工具活动交集及交集内原始采样；活跃和唤醒阶段均为8个容器。
- 初始与回收后的两个60秒空闲窗口均无Worker，只保留Fixture和观察器。真实业务checkpoint在新容器从原SQLite读回，2/2核对通过。
- 两个新唤醒任务均完整通过。固定可信编排请求至首次原生活动分别21.08、21.06秒，包含本实验的服务/容器准备；没有产品队列，不能当作产品端到端延迟。
- 故障窗口内只杀固定ID的A，真实退出137且非OOM；B在A实际退出后仍有原生进展并正常完成，但漏读分页。因此`unaffected_B_native_progress_after_actual_kill=true`，要求B业务也通过的`only_one_exact_worker_fault_and_B_progress=false`。A未恢复，原状态和累计预算保留。
- 五个可评分结果3/5通过：active-A与wake-A/B通过；active-B、fault-B仅`all_pages_read`失败。A的故障任务无结果，不计作成功；六任务累计33条模型请求计数，失败和被杀任务均保留。

| 阶段 | 有效样本 | 容器数 | 整套实测内存均值 / 峰值 |
| --- | ---: | ---: | ---: |
| 初始空闲 | 29 | 2 | 31.41 / 31.48 MiB |
| 两任务活跃 | 13 | 8 | 390.36 / 401.75 MiB |
| 回收后空闲 | 30 | 2 | 47.85 / 48.13 MiB |
| 再次唤醒 | 14 | 8 | 397.32 / 410.76 MiB |

这些数来自原判定的Docker样本汇总，包括Fixture和观察器，后者不是产品后端；未包含宿主/VM、真实产品后端或生产存储费用。观察、资源采样、配额、正常关闭和无遗留检查通过。`cost_subset_passed=false`、`i25_complete=false`、`savings_percent=null`保持原值。

### 同材料常驻配对与固定配额边界

[i25-da-resident-101-001原判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-resident-101-001/protected/flow-verdict.json)与按需轮使用同一组完整image ID、种子101和pair `i25-da-101`。宿主退出1，独立评分完整性通过，清理无遗留。

同样是五个可评分结果3/5通过：active-B、wake-A/B通过；active-A与fault-B漏读分页。六任务累计34条模型请求计数，A被杀任务原件保留、不恢复。原业务对象在回收后的常驻容器内保持并读回；观察、采样、配额、正常关闭、实际并发、两项唤醒及杀A后B进展检查通过。完整业务和要求B业务成功的故障复合检查失败，与按需轮分开留存，不修改原判定。

| 阶段 | 按需：整套内存均值 / 容器数 | 常驻：整套内存均值 / 容器数 |
| --- | ---: | ---: |
| 初始空闲60秒 | 31.41 MiB / 2 | 282.10 MiB / 4 |
| 两任务活跃 | 390.36 MiB / 8 | 390.69 MiB / 8 |
| 回收后空闲60秒 | 47.85 MiB / 2 | 312.63 MiB / 4 |
| 再次唤醒 | 397.32 MiB / 8 | 404.04 MiB / 8 |
| 两用户请求至首次原生活动 | 21.08 / 21.06秒 | 13.26 / 13.26秒 |

常驻四阶段有效样本分别30、12、29、16，零采样/观察错误。这个单种子探索说明：保留两个已加载Agent对象会增加空闲常驻内存，按需则要支付启动等待；活跃阶段的整套内存接近。它不证明十用户容量、成功任务经济性、生产API延迟或现网收益，`i25_complete=false`、`savings_percent=null`保持不变。

常驻[N=5拒绝记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-resident-5-refusal-101-001/protected/refusal.json)和[N=10拒绝记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-resident-10-refusal-101-001/protected/refusal.json)已分别生成，CLI均退出2：按冻结的每Worker配额与总预算，启动前拒绝，`Docker_started=false`、`observed_RSS=null`。这不是实测OOM或框架最大用户数。按需5/10、其他种子、未活跃用户轮换、DSH及第2节缺口仍待验证。

## 9. 待验证的新变体：让原来没执行任务的用户真正醒一次

说人话：原来的流程一直在唤醒001、002，只能说明“刚用过的两个用户可以再接新任务”。用户池有5人或10人时，还要检查原先只存了历史、一直没运行任务的003、004。否则不能说明按需方式能服务整个用户池。

新入口必须显式传 `--rotate-inactive-users`，仅接受N=5或N=10。原两用户入口默认行为不变，旧轮次也不会被重新解释成轮换实验。流程先执行001、002的原有任务、回收和再次唤醒，等其Worker及附属服务退出，再为003、004各启动新Worker、工具服务和模型代理；同时最多两个业务任务。005及以后继续保留独立历史，不假称所有用户都已醒过。

003、004使用新的 `inactive-wake` 业务任务，scope、会话、状态目录、工作区、工具凭证、代理凭证和cap=20预算均独立准备。启动前检查新任务的执行目录为空、预算中只有新token。分别在业务前后，通过真实公开状态接口读取原来准备的非空历史，核对同一个原始checkpoint和内容；业务结束、Worker回收后，再换Worker冷读刚才真实业务的状态。原始历史检查和新业务检查各有各的证据。业务失败仍保留失败，历史核验照常进行，不重跑任务、不换额度，也不把新建的哨兵当成原历史。

N=5/N=10常驻策略仍按N个Worker计算完整固定分配，并在超过6 GiB / 3 CPU预算时启动前拒绝；不能为了跑通把常驻改成仅两个Worker。按需轮换通过也不代表最大用户容量、产品调度器或真实后端已验证，仍保持 `i25_complete=false`、`savings_percent=null`。

### 全阶段采样补审计的口径

新轮换变体要求**全部采样阶段**符合5秒门槛，包括准备服务、启动Worker、控制/冷读和关闭阶段。每个样本的前观察→资源测量→后观察必须在同一宿主monotonic时钟上有序，每份完整采样耗时及相邻采样开始间隔均不超过5秒，采样启动至首样本和末样本至采样结束也不超过5秒。连续样本序号、落盘行数、采样线程完成标志及独立保存的起止记录均要一致。缺样、异常、时钟倒退、长空档或线程提前结束一律不能通过；原probe超时仍为8秒，不因这条要求放宽。

旧按需 `i25-da-od-101-001` 和常驻 `i25-da-resident-101-001` 另用离线审计器检查，**不覆盖原verdict**。它逐行处理完整JSONL，核对所有阶段的时钟和间隔，以及前后observer与不可变control文件的序号/nonce绑定。控制序号允许因phase切换或其他probe而跳号，不能拿它冒充连续资源样本序号。输出只有固定原因码、允许的阶段名、计数、时间差和来源SHA；不输出sample正文、模型/tool内容、环境值或异常文本。

旧证据缺少 `sample_sequence` 和采样线程起止/完成/落盘总行数记录。即使已看到的相邻样本全部不超过5秒，也只能说“已观测区间符合”，**不能证明首尾无缺口或日志尾部没有丢失**。未知字段保持null/unverified，不能补成0秒或自动通过。空阶段只报告零样本，不捏造阶段持续时间。若已观测区间有长空档则明确失败，与这些未知项同时保留。原始样本和旧verdict的SHA进入新审计文件，便于复核来源。

### 交给后续串行执行的任务

DSH的I23正式批 `i23-dsh-timing-formal-001` 已因采样完整性停止并清理，DA首次24格正式矩阵已结束，专项结果见[故障报告](fault-recovery-execution.md)；两路线I23仍未全部完成。此前在DSH停止批清理后，串行构建了独立bench `harness-lab-bench:token-rotation`（完整ID `sha256:bbd604f4fb4177dec7b77b6f2cfc050a3d859961505bdbe869869fc4c7d8c186`），断网、无模型凭证完成token及实例成本四文件校准，共313项通过，健康退出并确切清理。见[校准记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/token-rotation-check-001/host.json)。该313项和后续350项只证明各自版本代码校准通过。probe001真实执行失败后，服务Init最小修订已通过新bench六文件405项校准；probe002因独立grader导入失败、无verdict而未通过，其独立副本23项离线诊断通过但不改原失败。全新003完整观测门禁已通过；按需N5 `i25-da-rotation-n5-101-001`已完整结束、成本子轮未通过，N10暂停。

| 步骤 | 内容 | 验收标准 | 状态 |
| --- | --- | --- | --- |
| 1 | 静态复核轮换代码，增加独立离线审计器和反例草案 | 默认流程不变；N5/N10只唤醒003/004；不改旧verdict和原预算；敏感正文不输出 | 生命周期与后续服务Init修订均已冻结、独立review；当前新bench六文件405项容器校准通过 |
| 2 | 串行窗口内构建全新bench镜像，冻结完整ID | 新镜像包含轮换、审计器和对应测试；旧镜像与原证据保留 | 当前Init修订已构建独立bench `863b70d9…`，完整ID见下；历史镜像/冻结保留 |
| 3 | 新bench内断网运行校准 | 真实容器健康退出；完整记录准确ID/配额/退出状态；反例均拒绝 | 历史313/350项及当前六文件405项通过；断网无key、exit0且清理通过，不代替真实probe |
| 4 | 原trial只读挂载，分别审计旧按需/常驻轮 | 新文件保存已观测长空档、未知首尾/序号/完整性；来源SHA和原verdict未变；不宣称完整通过 | 已审计；两份均failed，详情见下；原verdict保持不变 |
| 5 | 先验证真实启动/关闭期间的连续采样 | 不调用模型；生产观测路径覆盖真实容器启动、关闭和删除；保持5秒及8秒probe标准；长等待不得锁住采样；原件和确切清理完整 | 已通过：全新probe003的35份样本、23项检查、独立评分进程及确切清理均通过；001/002原失败及副本诊断保留；不作为业务或节省通过 |
| 6 | 按需N5再N10，各用新trial执行显式轮换 | 最多双活跃；003/004首次业务、原历史前后不变、业务状态回收后可读、全部采样≤5秒、原失败保留且清理无遗留 | N5完整执行、独立评分和清理完成，成本子轮失败；003/004业务及历史/状态检查通过，但业务总计4/7且回收后空闲资源完整性失败。N10暂停 |

### 旧两轮的全阶段采样审计结果

按需[新审计](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-legacy-sampling-audits-001/proof/on-demand.json)检查136个样本，发现18处相邻采样起点间隔超过5秒，最大23.670秒；常驻[新审计](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-legacy-sampling-audits-001/proof/resident.json)检查140个样本，发现14处，最大23.598秒。长空档包含启动、关闭、唤醒和故障等阶段。因此前文原评分中的“采样通过”只按旧判定范围理解，不能扩为全流程满足5秒或未漏资源峰值。

两轮的单样本跨度、已记录时钟、控制绑定、完整JSON行及无样本错误检查通过；样本连续序号、全流程首尾、采样线程完成和实际落盘行数缺原件，仍为`null`，不补造。两份审计自身均退出1、`passed=false`；其宿主记录确认这个预期退出、非OOM及确切清理，不把“审计程序按预期结束”写成“采样通过”。旧业务判定和观察到的内存数字保留，但不足以计算完整峰值、成功任务经济性或收益比例。

第3步在固定bench容器内执行。以下为初版校准命令示例；350项历史校准及当前六文件405项实际结果见下文独立host记录，不能用初版命令代替新校准：

```bash
python -m pytest -q -p no:cacheprovider \
  tests/test_instance_cost.py tests/test_instance_cost_full.py \
  tests/test_instance_cost_sampling_audit.py
```

第4步分别对两份旧trial执行以下**容器内命令**。原trial挂到 `/trial:ro`，新审计目录单独挂到 `/audit`；容器network none、无模型配置，设置 `HARNESS_LAB_ENVIRONMENT=container`。输入、输出必须为规范绝对路径，含 `old`（不区分大小写）、`..` 或其他非规范拼写会在读取前拒绝；不能用路径归一化绕过归档不读写规则。输出文件必须新建且位于原trial之外：

```bash
python scripts/audit_instance_cost_sampling.py \
  --trial-root /trial --output /audit/NEW_TRIAL_SAMPLING_AUDIT.json
```

退出0仅表示完整审计通过；退出1表示明确失败或完整性未验证；退出2表示运行环境、输入或输出错误。必须另外检查准确容器ID、镜像、配额、退出和非OOM状态。一次审计的失败或未知不能通过覆盖旧文件或补边界数据来消除。第6步沿用第5节宿主编排入口，增加 `--rotate-inactive-users --users 5` 或 `--users 10`，使用新的trial/pair/output和新bench完整ID；不是把上述容器内审计搬到宿主计算。


### 当前生命周期修订：probe003门禁通过，N5探索失败，N10暂停

独立[构建记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/lifecycle-dsh-review-images-001/build.json)固定新bench为`sha256:fe7a98caf582514178bc0492dba35a4df30b0cda15513f79b0508f0500909c68`。生命周期采样修订源码已冻结并完成独立静态复核，当前五测试文件在该镜像内[350项通过](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/instance-lifecycle-check-001/host.json)；断网、无模型key、exit0且清理通过。初版313项和两份旧采样失败审计继续保留，不使用新校准改写旧verdict。

350项只证明当时的合成边界和代码校准通过。真实无模型[probe001结果](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-001/protected/probe-result.json)为失败：11个Worker正常关闭，共留下42份样本，第42份在额外Fixture停止阶段记录ValueError，未完成独立grade；清理`cleanup_confirmed=true`。原[stop命令677](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-001/protected/commands/command-000677.json)持续约10.177秒；其后[同ID inspect728](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-001/protected/commands/command-000728.stdout)显示实际exit137、PID0、非OOM。普通服务只接受0/143，故终态登记和采样严格拒绝；stats的0B读数或top已停止错误不能把137解释为健康。原generic错误码不补写，原失败和冻结材料保留。

最小修订只给新轮换的服务容器加`--init`，让初始化进程转发停止信号，并在初创建inspect中记录真实`HostConfig.Init`，登记前及独立评分均要求布尔true；Worker创建入口、旧全局清单投影与退出策略不改，未知137仍失败。见[修订说明](instance-cost-service-init-revision.md)、[新修订SHA清单](instance-cost-service-init-source-freeze.sha256)，[原冻结清单](instance-cost-lifecycle-source-freeze.sha256)不覆盖。

新bench完整ID为`sha256:863b70d9c145cd69c760e57418afbc9189054401ead63c29c073ea716e944b3d`，源码SHA与修订冻结一致，独立review已通过。[六文件405项校准](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/instance-service-init-check-001/host.json)在断网、无模型key条件下完成，测试容器exit0、非OOM且清理确认；[原测试输出](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/instance-service-init-check-001/test-output.txt)保留。它证明合成边界通过，**不证明真实5秒采样、服务停止、业务或成本已经通过**。

使用新bench的真实无模型[probe002结果](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-002/protected/probe-result.json)已收尾，`probe_passed=false`、`grade_passed=false`、`cleanup_confirmed=true`。它使用真实DA resident、Fixture和observer，Worker断网、不注入模型密钥、不发业务任务；[采样线程记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-002/protected/rotation-sampler-status.json)为35份样本、`completed=true`，原JSONL没有采样错误行。

失败发生在独立评分入口：[命令710原stderr](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-002/protected/commands/command-000710.stderr)显示`/probe.py`导入`runner.instance_cost`时抛出`ModuleNotFoundError: No module named 'runner'`，grader退出1，未生成`protected/probe-verdict.json`。宿主随后读取该缺失文件，才记录`FileNotFoundError`。该次原执行没有完成独立评分，35份零错误样本和清理成功均不改变本轮失败。原件和原结果保留。

随后在独立材料副本上追加离线诊断。[i25-probe002-independent-audit-check-001宿主记录](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-probe002-independent-audit-check-001/host.json)确认断网、无模型调用，评分容器exit0、`passed=true`、`cleanup=true`；[副本verdict](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-probe002-independent-audit-001/input/protected/probe-verdict.json)的23项检查全过，35份样本、最多2个Worker，完整登记区间的配额峰值为5100273664字节/2.25 CPU。原002的2748个文件SHA全未变。该诊断定位到评分入口导入接线，**不能给原002补判通过，也不能代替新探针的实际执行、评分和清理验收**。

修订helper SHA256为`a2a615652404cbe7879cc4031beb6d63ca51145bb990c8f7701f282758366d52`，仅为最终独立grader补`PYTHONPATH=/app`、保存原执行证据并明确缺verdict即失败。见[评分入口修订说明](instance-cost-probe-import-revision.md)及[修订冻结清单](instance-cost-probe-import-source-freeze.sha256)。

全新trial `i25-native-lifecycle-sampling-probe-003`已实际结束、exit0。[原结果](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-result.json)为`probe_passed=true`、`grade_passed=true`、`cleanup_confirmed=true`、`host_error_type=null`；[原verdict](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-verdict.json)保存35份样本、23项检查全过，最多2个Worker、登记配额峰值5100273664字节/2.25 CPU。[独立评分进程完整性](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-grade-process-integrity.json)为`passed=true`、CLI及Docker exit0；[确切清理](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-003/protected/probe-cleanup.json)的`unconfirmed_resources=[]`、无遗留容器。至此003完整观测门禁通过，001/002原失败不改。

003提交0个业务任务、没有模型调用，只证明该轮生命周期观测、评分和清理可靠；真实业务与完整资源计量仍由后续轮次分别验收。

### N5本轮探索结果与N10门禁

按需N5 `i25-da-rotation-n5-101-001`已完整执行，CLI返回1。[原独立判定](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/flow-verdict.json)为`cost_subset_passed=false`；[评分进程完整性](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/grade-flow-process-integrity.json)通过，CLI与实际Docker退出码均为预期的1；[清理](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/flow-cleanup.json)为`unconfirmed_resources=[]`。148份采样无sample error、无observer error，全阶段5秒、精确生命周期登记、真实双活跃、原历史和业务状态回收后读回等检查通过。

| 未通过项 | 原件说明了什么 |
| --- | --- |
| `all_actual_business_tasks_passed` | 7个可评分任务4个通过。`active-user-001`、`wake-user-001`、`fault-user-002`各只有`all_pages_read`失败；三者实际用`page_size=100`查询19条商品，违反任务明确要求的每次最多5条。报告和精确写入等其余业务检查、正常原生完成、预算绑定与转发均通过。003/004首次唤醒任务均按每页5条读完并通过 |
| `only_one_exact_worker_fault_and_B_progress` | 这是复合条件，最后还要求B的`fault-user-002`业务任务通过；该任务因上述大页查询失败。A完整ID绑定、实际137且非OOM、只有一个Worker故障均有原件；独立的`unaffected_B_native_progress_after_actual_kill=true`，因此不能把复合false解释成B停止或误杀 |
| `recycled-idle_rotation_resources_complete` | 回收后空闲阶段29份资源完整、1份不完整；阶段资源均值、峰值和进程总量保持不可用。采样无error、节奏通过，不代表资源总量完整 |

业务判定来源为[分页规则](/Users/hanbowen01/repo/puff/runner/grading.py:123)和[故障复合条件](/Users/hanbowen01/repo/puff/runner/instance_cost_full.py:811)。大页查询的可信工具原件分别见[active001](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/users/user-001/trusted/active/tool-events.jsonl:1)、[wake001](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/users/user-001/trusted/wake/tool-events.jsonl:1)、[fault002](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/users/user-002/trusted/fault/tool-events.jsonl:1)。本轮是大页越过分页约束，不能沿用旧轮“漏读分页”的原因。

资源缺口在[第82份原样本](/Users/hanbowen01/harness-lab-data/runs/2026-10-21/deepagents_langgraph/instance-cost/i25-da-rotation-n5-101-001/protected/flow-resource-samples.jsonl:82)：样本开始时仍标记`recycled-idle`，前清单只有Fixture和observer，两者stats/top均正常；采样过程中空闲60秒结束，主线程进入业务状态冷读，后清单多出新的user001冷读Worker `9343bf9cd4621f5372b09ec348cfce068f642b064b9f77f0e4b93d175cac0803`。该Worker没有本份stats/top，前后对象集合不同，因此[资源完整性规则](/Users/hanbowen01/repo/puff/runner/instance_cost_lifecycle.py:359)正确标记false。第83份已属于冷读阶段并资源完整。

[执行顺序](/Users/hanbowen01/repo/puff/scripts/run_instance_cost_full.py:626)在空闲结束后立即进入冷读，没有等待旧阶段的在途采样落盘；[采样入口](/Users/hanbowen01/repo/puff/scripts/instance_cost_lifecycle.py:306)只在开始时取得阶段标签，清单前后检查之间允许真实生命周期推进。于是空闲尾样本跨入新Worker启动，原评分又按标签要求空闲阶段每份资源完整，失败成立。

**N10当前暂停。** 保留N5为本次探索结果，不删除82、不改原标签、不补零，也不以29份完整样本替代整个空闲阶段。后续是否修订既有阶段交接边界另行决定；本次不扩建实验台、不修改评分或重跑。`i25_complete=false`、`savings_percent=null`，不能报告成本节省比例。
