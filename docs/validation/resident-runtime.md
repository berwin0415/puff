# 常驻与按需：当前最小实现及待测范围

2026-10-20。以下保留最初空闲子轮的设计，**不是I25成本结果**。专项镜像已构建，100项Docker无模型校准和真实断网生命周期探针002已通过；业务基线、60秒空闲测量及完整成本流程仍未执行。双任务、回收、唤醒及单Worker故障接线见[instance-cost-flow.md](instance-cost-flow.md)，完整设计仍以`multi-user-design.md`为准；本次实测边界见第7节。

## 1. 这个版本实际能做什么

给每个用户准备一份独立原生 SQLite 状态和工作区，写入一个明确标为“应用准备的历史”的哨兵。这个哨兵通过真正的 graph 公开 API 写入、读出，不是创建一个空目录，也不是伪造 Agent 已经完成业务。准备阶段生成的工作区标记同样不算 Agent 业务产物。

然后运行一种明确的空闲策略：常驻策略保留真正的模型客户端、HTTP 工具桥、Deep Agents graph 和 SQLite saver；按需策略正常关闭这些对象，空闲期间没有 Worker。两边都保留原生历史。常驻对象空闲时能用公开 `aget_state` 读取原状态；代码会保存前后快照、相同对象的进程内身份、完整 Docker 身份、资源采样和真实进程清单。

当前子轮的两种策略都使用 `--network none`，不启动后端、Fixture、工具、代理或浏览器，也不向 Worker 传供应商密钥。构造 `ChatOpenAI` 对象不会主动请求模型；真正的网络封锁由 Docker 设置提供。它只用于测真实 SDK 对象的初始空闲开销，**不能把这组数字当成完整业务总成本或收益百分比**。

## 2. 各个新文件负责什么

| 文件 | 作用 |
| --- | --- |
| `adapters/deepagents_langgraph/resident_runtime.py` | 新专项入口。每进程只绑定一个用户；保留真实 history graph/model/tools/saver。可信 stdin 命令只能选择只读配置中已经登记的任务，不能指定 PID、路径、用户或凭证。不提供任务队列、资源池或故障恢复 |
| `runner/instance_cost.py` | 在 bench 镜像内准备用户材料、导出该镜像真实工具定义，并验收空闲子轮；同时给出完整实验的固定资源预算，拒绝超预算档位 |
| `scripts/run_instance_cost.py` | 宿主只调用 Docker，启动候选镜像、投递可信命令、采样、关闭和清理；不在宿主运行候选 SDK、模型或评分 |
| `tests/execution/test_deepagents_resident_runtime.py` | 合成边界校准草案：配置越界、凭证重复、历史覆盖、原生投影编码等；不会实例化真实候选，不证明能力通过 |
| `tests/test_instance_cost.py` | 固定配额和非空历史引用的正反例；不执行 Docker，不提供资源实测结果 |
| `tests/integration/deepagents_resident_probe.py` | 真实候选无模型探针：复用bench材料，启动两次烘焙resident CLI，验证公开历史准备、同状态冷读及正常关闭；002已通过，001准备期导入失败保留 |

默认 `entrypoint.py`、I22 的材料/镜像/编排、I24 文件及既有来源 stage 脚本均未修改。新入口必须被单独 stage 到新构建上下文，写进 `LAB_ADAPTER_MANIFEST.json` 的完整文件清单并重新计算 `export_sha256`；入口会拒绝自身未列入清单的镜像。沿用默认 Dockerfile 已有的 `COPY harness-lab` 即可把新入口烘焙进镜像，但不能挂宿主源码补文件。

## 3. 原生对象和任务怎样绑定

历史准备调用公开 `graph.aupdate_state(..., as_node=START)` 写入带固定 ID 的用户哨兵，再调用公开 `graph.aupdate_state(..., None, as_node=END)` 清掉这次准备输入安排的节点。固定 LangGraph 源码的 `abulk_update_state` 明确实现了这两个分支。清理仅允许发生在**全新、原本没有任何状态或任务**的准备线程；不能拿它清掉真实业务的未完成步骤。两次公开 API 的原始返回与最终原生快照均留证；该历史准备路径已在native-probe-002中用真实候选验证，仍不是业务任务完成或续跑。

每个业务任务必须另给完整 scope、session、原生状态目录、工作区、证据目录、工具 token 和模型代理 token。入口的 `rebuild_tools_graph_for_task` 显式创建新模型客户端、工具实例、graph 和 saver；先通过原 `HttpTools.load()` 读取真实工具服务，核对冻结 schema，再进入同一 HTTP 工具轨道。最新静态接线会保留最近自然完成任务的真实对象，在下一份独立任务之前或正常回收时关闭；失败任务对象立即关闭。初始历史对象仍保留。两策略共用这一套逻辑，区别在空闲时是否关闭整个运行时并回收 Worker。

不复用上个任务的工具 token，不把同一个代理预算改名当新预算；不同任务必须使用不同 variant/session 和独立目录。并未实现同进程跨用户复用。历史对象与活跃任务对象会同时占内存，这是这一最小应用接入版本的真实开销，必须双方同口径计入。

可信 stdin 是实验宿主控制通道，**不是产品身份认证边界**。I24 的 `ConnectedProductBackend` 仍负责真实用户授权和路由。当前空闲子轮只生成身份凭证材料，没有调用后端认证，不宣称 I24 已通过。

## 4. 怎样在新镜像中运行这个子轮

前置材料是两个已经构建好的确切 image ID：包含新 resident 入口和完整自身清单的候选镜像，以及包含新 runner/tests 的 bench 镜像。脚本不构建、不拉取镜像、不修改 Colima、不清理无关容器。存在其他 harness-lab 容器时直接拒绝开工。

先查看预算计划；这里的 `sha256:...` 要换成真实完整 ID，模型名使用已冻结名称：

```bash
python scripts/run_instance_cost.py plan \
  --trial-id idle-resident-2 --pair-id idle-pair-2 \
  --strategy resident --users 2 \
  --candidate-image sha256:... --bench-image sha256:... \
  --model-name SELECTED_MODEL
```

实际执行把 `plan` 改为 `run-idle`，再增加一个不存在的绝对路径 `--output-root /ABSOLUTE/DATA/runs/DATE/deepagents_langgraph/instance-cost/TRIAL`。另一策略使用新的 trial/output 路径、相同 pair ID、模型与镜像；`--strategy on-demand`。不允许覆盖失败原件。5/10 用户常驻档按完整实验 2 GiB / 0.75 CPU 每 Worker 的配额，在启动任何 Docker 容器前写入拒绝记录；它表示计划分配超限，不表示实测 OOM 或框架普遍无法承载五个用户。

一个子轮依次执行：bench 准备 N 份独立材料并退出 → 逐用户启动真实候选，准备非空原生历史并正常退出 → 常驻策略启动 N 个真实历史 Worker，按需策略保持零 Worker → 按 2 秒目标间隔采满 60 秒 → 常驻再读原状态并正常关闭 → 所有 Worker 删除后启动离线 bench 验收。原始实际采样间隔、命令失败和清理未确认项都会保存，不补采样时间戳。

`protected/` 保存宿主/bench 侧 plan、命令原响应、容器完整 ID 与 cgroup 投影、stats、`docker top` 原文、关闭与验收记录；各用户仅挂自己的 `config/`、`state/`、`workspace/`、`evidence/`。不保存 Docker Env 值。原生快照仍是源码固定的候选公开 API 投影，不能据此声称已经完成对恶意候选的独立安全验证。

## 5. 还差哪些完整实验

原空闲子轮输出永久明确 `i25_complete=false`，`savings_percent`、唤醒时延和故障影响人数为 null。以下是该子轮的范围外项目；其中双活跃、回收、唤醒和单Worker影响面已另有静态接线，具体边界及未执行项见 [instance-cost-flow.md](instance-cost-flow.md)，不能把源码存在记作实测完成：

1. I24 所需授权/隔离证据，以及新 0.75 CPU 配额下的单任务真实基线。
2. 在 I24 的两用户网络里接真实后端、两套工具和代理；两项真实工具任务实际重叠、各自独立验收，重叠内至少两次采样。
3. 正常完成之后的 60 秒回收观察；当前实现的 60 秒是最开始的空闲阶段。
4. 再发新业务任务，测热/冷唤醒且保留原用户历史。
5. 干净数据里的独立故障子轮，仅退出一个 Worker，核对另一用户继续工作以及未知写入。
6. 完整公共服务开销、独立预算/账本、三组正式种子和最终两策略比较；DeepSeek Harness 的对应实现和全部实测。

不能把本子轮的`subset_passed`登记为I25完成，也不能用常驻5/10的预算拒绝虚构节省百分比。本次100项校准和真实对象探针已执行；上述业务及资源子轮仍未执行。

## 6. 先做一次真实对象的无模型校准

先确认新入口真的能加载固定版本的框架、把历史落盘、换进程读回并关闭。探针[deepagents_resident_probe.py](../../tests/integration/deepagents_resident_probe.py)使用实际烘焙的`resident_runtime.py`公共命令，没有替换模型、graph、工具桥或saver，也没有monkeypatch。**native-probe-002已通过，执行证据见第7节。** 以下是复现模板；001和002目录均已占用，重验必须换新目录并等其他实验结束、容器清理后执行。

材料直接复用 `runner.instance_cost.prepare_idle_materials`，它也是 `runner.instance_cost_full.prepare` 的第一步，从固定 bench 导出真实 `execution.tools.server.TOOLS`。这一步不需要完整业务场景：会生成两份标准用户材料，只启动 `user-001`。两个容器都用 `--network none`，不启动 Fixture、工具服务或模型代理，不传 `.env` 或远程供应商密钥。真实 `HttpTools`、`StructuredTool` 与模型 HTTP 客户端会构造出来；本探针只需冻结 schema，不请求 `/tools` 或业务接口。真实业务入口的在线 schema 核对仍留给后续单任务基线。

候选容器里的探针依次启动两个独立子进程：A 调用 `prepare_history → state → close`；确认 A 退出 0、关闭记录齐全且原生状态锁释放后，B 只调用 `state → close`。B 沿用同一 SQLite 和工作区，不能再次准备历史。两次都必须返回实际的 `ChatOpenAI`、`HttpTools`、`CompiledStateGraph`、`AsyncSqliteSaver` 类，并保留同一 checkpoint、哨兵消息及空的 next/tasks/interrupts。原生快照有真实文件 hash；原 SQLite 只取文件清单和字节 hash，SQL 查询仅在副本上确认 checkpoint 存在。它不运行 `run_task`，也不调用 `astream`。

以下变量先设置为本轮已经冻结的**完整 image ID**和模型名；不要从 `.env` 整体导出。新候选镜像必须来自包含 resident 入口的 `external/build-context/deepagents_langgraph-instance-runtime`，不能拿 I22/I23 镜像代替，不能挂载宿主 adapter。`I25_PROBE_ROOT` 必须是新的空目录，失败后换目录保留原件。

```bash
I25_PROBE_ROOT=/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/instance-cost/native-probe-NEW_ID
mkdir -p "$I25_PROBE_ROOT"
docker run --rm --platform linux/arm64 --network none --read-only \
  --memory 512m --cpus 0.5 --pids-limit 128 --tmpfs /tmp:size=128m \
  --mount type=bind,src="$I25_PROBE_ROOT",dst=/trial \
  --mount type=bind,src=/Users/hanbowen01/repo/puff/tests/integration/deepagents_resident_probe.py,dst=/calibration/probe.py,readonly \
  --entrypoint python "$I25_BENCH_IMAGE" /calibration/probe.py prepare-materials \
  --candidate-image "$I25_CANDIDATE_IMAGE" --bench-image "$I25_BENCH_IMAGE" \
  --model-name "$I25_MODEL_NAME"
```

准备成功后，使用候选镜像内的 `/opt/venv/bin/python`。容器保留到 Docker 状态取证完成，再删除；不挂 Docker socket，不给候选整个 trial 目录。

```bash
docker create --name hl-i25-native-probe-NEW_ID --platform linux/arm64 \
  --label com.docker.compose.project=harness-lab --label harness-lab.experiment=i25-native-probe \
  --network none --read-only --memory 2g --cpus 0.75 --pids-limit 256 --tmpfs /tmp:size=128m \
  --mount type=bind,src="$I25_PROBE_ROOT/users/user-001/config",dst=/config,readonly \
  --mount type=bind,src="$I25_PROBE_ROOT/users/user-001/state",dst=/state \
  --mount type=bind,src="$I25_PROBE_ROOT/users/user-001/workspace",dst=/workspace \
  --mount type=bind,src="$I25_PROBE_ROOT/users/user-001/evidence",dst=/evidence \
  --mount type=bind,src="$I25_PROBE_ROOT/probe",dst=/probe \
  --mount type=bind,src=/Users/hanbowen01/repo/puff/tests/integration/deepagents_resident_probe.py,dst=/calibration/probe.py,readonly \
  --entrypoint /opt/venv/bin/python "$I25_CANDIDATE_IMAGE" /calibration/probe.py probe \
  > "$I25_PROBE_ROOT/protected/candidate-container.id"
I25_PROBE_CONTAINER="$(cat "$I25_PROBE_ROOT/protected/candidate-container.id")"
docker inspect --format '{{.Id}} {{.Image}} {{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.NetworkMode}}' \
  "$I25_PROBE_CONTAINER" > "$I25_PROBE_ROOT/protected/candidate-identity.txt"
docker start --attach "$I25_PROBE_CONTAINER" \
  > "$I25_PROBE_ROOT/protected/candidate-stdout.log" \
  2> "$I25_PROBE_ROOT/protected/candidate-stderr.log"
docker inspect --format '{{json .State}}' "$I25_PROBE_CONTAINER" \
  > "$I25_PROBE_ROOT/protected/candidate-state.json"
docker rm "$I25_PROBE_CONTAINER"
```

验收要同时看 `/probe/verdict.json` 和宿主保存的实际容器状态：正确完整镜像 ID、2 GiB/0.75 CPU、network none、ExitCode=0、PID=0、非 OOM、无 Docker Error；候选 `passed=true` 单独不足以确认。`prepare/cold-commands.json`、stdout、进程退出记录、两次原生对象 open/close、历史准备 API 返回、原生快照和两份物理 inventory 都保留。任一步失败先留证，禁止直接启动付费基线。即使通过，也只说明这条新入口的真实对象生命周期可用；业务完成、在线工具轨道、多租户隔离、资源收益及 I25 完成状态仍待后续实验。

## 7. 本次实际执行到哪里

[lifecycle-images-001构建](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/lifecycle-images-001/builds.json)成功。真实resident候选镜像为`sha256:46a03db9203b0facef1399eaa590ba31640e28a15c2a0410c8b037d3a95575ed`（`harness-lab-deepagents-langgraph:instance-runtime`），adapter export `1b39616294eb6895726ef22330239d85a620b10ba32281d7326d1c589128f8f2`；bench为`sha256:5ac51f8ecc0407aadb0e69cbd33bb9aa7d6e2bb20d727f398a030671a223442e`。候选入口来自镜像内固定材料，不挂宿主adapter。

[i25-cost-check-002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/i25-cost-check-002/host.json)100项校准通过（0.52秒），network none、未用远程key、退出0、无OOM且清理成功。001在收集阶段因`run_instance_cost.py`少右花括号而失败，原件保留；校准通过不提供资源均值或成本收益。

[native-probe-001](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/i25-native-probe-host-001/host.json)失败于bench准备：探针从`/calibration`执行时`runner`不在模块路径，退出1，尚未进入候选阶段。prepare补上镜像内`/app`的`sys.path`后，用新目录执行[native-probe-002](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/infrastructure/runtime/i25-native-probe-host-002/host.json)，准备及候选两个容器均退出0、PID0、非OOM、无Docker Error且确切对象清理成功；全部network none、未注入供应商key。准备bench为512 MiB/0.5 CPU，真实候选为2 GiB/0.75 CPU。

002使用的探针文件SHA-256为`d2fd79f809d1123f9b7f9349fed643cab6d165543cbe1647bf532d63f22ca2e1`。同一候选容器中，PID7准备历史后关闭，PID11从同一SQLite读回同一checkpoint后关闭；两次均为实际框架对象，未运行模型/业务命令。[verdict](/Users/hanbowen01/harness-lab-data/runs/2026-10-20/deepagents_langgraph/instance-cost/native-probe-002/probe/verdict.json) passed=true，仍明确业务未完成、未测成本/容量、未验证I24隔离、`i25_complete=false`。

现在可以说“真实对象能够准备、落盘、换进程读回并关闭”。不能说“云端常驻/按需策略已经省钱”或“两个真实用户互不干扰”。后续顺序仍是同入口单任务小配额基线，再验证双任务、结束后回收、第二次唤醒和单Worker故障影响；完整状态见[成本流程](instance-cost-flow.md#6-本次实际材料校准和断网探针)。
