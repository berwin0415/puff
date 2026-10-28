# I23 故障专项执行清单

日期：2026-10-21。依据[故障验证设计](fault-recovery-design.md)；总进度以[实施任务表](../../IMPLEMENTATION_PLAN.md)为准。**I23固定调研阶段已全部执行结束，保留业务失败。DSH正式24格完整23/24、补普通基线4/4、最终关键故障8/8；Deep Agents分别10/24、2/4、4/8。此前101普通基线DSH2/2、DA0/2分别保留，两套bench分层不合并完整镜像批次。两路线正式六条及最终四条kill均原生换容器恢复；各阶段的已提交操作重发均为0。完整业务失败均另列分页约束，不能用外层或恢复通过覆盖。** DSH两次旧采样停止批与全部失败原文保留，不拼接成功率；后文按执行顺序留档。

先说人话：先保证“制造故障和判断结果的实验台”可靠，再让两个候选做同一道任务。每个格子新建自己的账本、工作区和预算；只有同一次突然退出的 A/B 共用原任务状态。完整能力失败也保留；实验台、材料或证据坏了就停，不能换个编号重跑到成功。

**本轮最终固定阶段采用下表，现已执行结束。第1–4节的早期context、标签和命令仅保留为历史流程，不能直接拿来续跑。**

| 角色 | 当前冻结材料 |
| --- | --- |
| DSH | `harness-lab-deepseek-harness:fault-round-order`，`sha256:45a417649165b795c927c22cff98360c45a1849247d7ba9d19cc2574202af73d`；context `external/build-context/deepseek_harness-fault-round-order` |
| Deep Agents | `harness-lab-deepagents-langgraph:fault-send`，`sha256:9e06db842d8124548b4ca0792b47e1376b0f83bae584ba4ffbdd32052cc2cb6a`；context `external/build-context/deepagents_langgraph-fault-send` |
| bench | 两路线最终固定阶段使用 `harness-lab-bench:fault-remove-dsh-control-002`，`sha256:4dd8a37e4dd1d6d136c6737700a3d301cc444ef1570f44d4357211b7806a8c64`；删除与清单互斥修订。此前DSH停止批使用`fault-sampling-closed`/`3811a47d…`，均保留，不改变5秒标准 |
| services | `harness-lab-services:fault-recovery`，`sha256:1814a728fb3d9e89e48541b8df9ac7b035227f4005072226847340c00ed00efb` |

沿用`scripts/run_fault_batch.py`，每阶段用新的`--prefix`及`--batch-id`。两路线均已按`formal` → `baseline-rest` → `final`逐阶段执行并核对`batch.json`；以下规则保留本轮执行流程。每轮启动前必须等其他专项及构建完全收尾，运行中不修改批次冻结的源码、任务或镜像。业务失败可留存后继续，完整性停止必须先归因。

## 1. 启动前先核对

所有命令在仓库根目录、`harness-lab` 分支运行。I22 使用相同 Docker 项目与串行锁；**必须等 I22 当前链结束、清理完成后再开始 I23**，不能并行启动候选或改写其镜像。

| 检查项 | 本轮固定输入或处理 |
| --- | --- |
| Deep Agents 构建目录 | `external/build-context/deepagents_langgraph-fault`；adapter export `3a3891fbf96f57043767cc17d3c4d990b20dbbe5544f1c6d75c8ae204fbca9d2` |
| DSH observed轮构建目录 | `external/build-context/deepseek_harness-fault-native-notice`；adapter export `26c94e406fd5b1cb937db0024782e295161aacf1187adfa31fbeb7ef22f42c68`；旧`-fault-recovery`材料留作历史，不覆盖；后续轮次顺序修订见第10节 |
| 上游源码 export | DA：`dbd26083214e82cfd957ec6a8eaa3077e73d28cfa4f62d8e3185a6f58f81b17c`；DSH：`8b3b296786f5d436a446e589171bbe01bddb730b16184081407fe0f565cdd150` |
| 故障来源 | 各自冻结目录的`LAB_FAULT_BRIDGE_MANIFEST.json`；使用与本次候选镜像一致的source/adapter/overlay清单，不以旧目录摘要替代新材料 |
| 不得误用 | DSH 的旧 `deepseek_harness-fault`、I22 的 `-pause`、I24/I25 的其他 runtime 目录；保留原件，不覆盖 |
| 凭证和模型 | 沿用当前 `.env` 及已选远程模型；只给 model-proxy。不要打印展开的 Compose、Docker Env 或 `.env` |
| 宿主职责 | 当前 `.venv/bin/python` 仅编排 Docker、校验材料和整理索引；候选、工具、评分及下面的校准均在容器内执行 |
| 资源和其他任务 | 核对 Docker 可用资源与磁盘；没有遗留 `harness-lab` 实验容器。保留其他 Docker 项目，不全局 prune |

检查容器和静态材料的最小命令：

```sh
git branch --show-current
docker ps -a --filter label=com.docker.compose.project=harness-lab --format '{{.ID}} {{.Names}} {{.Status}}'
docker info --format '{{.NCPU}} CPUs; {{.MemTotal}} bytes memory'
docker system df
jq '{candidate,variant,export_sha256}' external/build-context/deepagents_langgraph-fault/LAB_ADAPTER_MANIFEST.json
jq '{candidate,variant,export_sha256}' external/build-context/deepseek_harness-fault-native-notice/LAB_ADAPTER_MANIFEST.json
```

这些短命令不是完整来源验证。批次入口还会校验完整 source/adapter inventory、overlay 对应仓库文件、helper、任务和宿主编排文件；每条链前后再核验一次。材料不同或运行中发生修改即停止。

已准备好的 context 无须重复 stage。以后若要改 adapter，必须先停批次、保留旧 context 和镜像，再用一个**不存在的新目录**导出上游源码、执行 `scripts/stage_fault_bridges.py`；DSH 必须加 `--goal-runtime`。`prepare_candidate_source.py --destination-root X` 实际生成 `X/<candidate>`，且可能替换其先前生成目录，不能把已冻结目录直接拿去重新导出。重新接线后从无模型校准开始，不能续接原批次。

## 2. 串行构建四个专属镜像

下列标签是本轮新标签示例。启动前确认它们没有用于已冻结批次；一旦开始验证，不再重指向。bench 包含当前仓库评分与测试，services 包含当前工具、账本和模型代理，候选镜像来自上面的专属 context。

```sh
docker buildx build --platform linux/arm64 --load -f compose/Dockerfile.bench -t harness-lab-bench:i23-fault-20261020-001 .
docker buildx build --platform linux/arm64 --load -f compose/Dockerfile.services -t harness-lab-services:i23-fault-20261020-001 .
docker buildx build --platform linux/arm64 --load -t harness-lab-deepagents-langgraph:i23-fault-20261020-001 external/build-context/deepagents_langgraph-fault
docker buildx build --platform linux/arm64 --load -t harness-lab-deepseek-harness:i23-fault-20261020-001 external/build-context/deepseek_harness-fault-recovery
docker image inspect --format '{{.RepoTags}} {{.Id}} {{.Os}}/{{.Architecture}}' harness-lab-bench:i23-fault-20261020-001 harness-lab-services:i23-fault-20261020-001 harness-lab-deepagents-langgraph:i23-fault-20261020-001 harness-lab-deepseek-harness:i23-fault-20261020-001
```

逐条执行并记录构建输出和完整 SHA256 image ID。任一构建失败先留存原因。正式批次会把标签解析为完整 ID，并用完整 ID 创建 worker、bench 和三个服务；不能仅凭标签相同认定材料相同。

## 3. 先在 Docker 内做无模型校准

Python 公共校准使用刚构建的 bench 镜像，不挂宿主源码、不传 `.env`、不挂 Docker socket。`--network none` 保留容器内回环，供测试启动自己的真实 HTTP 子进程；不能访问远程模型。

```sh
docker run --rm --platform linux/arm64 --network none --read-only --memory 512m --cpus 0.5 --pids-limit 128 --tmpfs /tmp:size=128m --entrypoint python harness-lab-bench:i23-fault-20261020-001 -m pytest -q -p no:cacheprovider tests/fixtures/test_fault_gate.py tests/test_fault_recovery_draft.py tests/test_fault_native.py tests/test_fault_dsh.py tests/test_fault_resources.py tests/test_fault_host.py tests/test_fault_batch.py tests/test_fault_outcome.py
```

其中 fake Docker/native API 用于校准拒绝条件；真实 HTTP、SQLite/WAL 和独立桥接进程用于检查写前栅栏、写后结果未知、旧 attempt 不得晚到写入等窗口。它们通过只说明实验台按约定工作，不能据此宣布候选恢复成功。

受控退出码1的补充校准和后续metadata反例已在新bench执行，结果见第8节；不能借旧242项结果通过。只有完整绑定原结果、原生状态与健康Docker退出的未成功任务，才能继续按能力失败记录。DSH包装层`incomplete`、原生轮次`completed`也属于未成功尝试；成功仍必须退出0并满足原全部检查。DA目前只接纳没有待执行任务的、可从原始checkpoint末消息核实的失败分支；缺结果、异常catch、未知停止分支、OOM及注入kill均不会仅凭退出1放行。资源采样和原五秒间隔约束继续独立核验，不能因任务失败跳过。

DSH 边界测试要检查**候选镜像中已经烘焙的 adapter**，而非宿主的另一份源码。只挂载一份测试文件供 Node 读取：

```sh
docker run --rm --platform linux/arm64 --network none --read-only --memory 512m --cpus 0.5 --pids-limit 128 --tmpfs /tmp:size=128m --workdir /opt/materials/sources/main --mount type=bind,src=$PWD/repo/puff/tests/execution/deepseek_fault_recovery.test.mjs,dst=/calibration/deepseek_fault_recovery.test.mjs,readonly -e DSH_FAULT_ADAPTER_SOURCE=/opt/materials/harness-lab --entrypoint node harness-lab-deepseek-harness:i23-fault-20261020-001 --import tsx/esm --experimental-test-module-mocks --test /calibration/deepseek_fault_recovery.test.mjs
```

该测试使用假的 native API，并禁止网络调用。若校准失败，留存失败输出、修正材料、换新镜像标签后重做；不得直接进入真实批次。

## 4. 真实模型批次怎样运行

以下步骤会真实调用当前远程模型。每条链累计最多 20 次模型请求、300 秒活跃时间；内部重试、故障等待和新 Worker 启动均计入同一链。I23 没有 I22 的十分钟等待。

先定义一个仅负责传 CLI 参数的宿主函数，后续每阶段手动发起，避免一次循环跳过检查：

```sh
i23_batch() {
  local i23_candidate i23_image i23_context i23_prefix
  case "$1" in
    da)
      i23_candidate=deepagents_langgraph
      i23_image=harness-lab-deepagents-langgraph:i23-fault-20261020-001
      i23_context=external/build-context/deepagents_langgraph-fault
      i23_prefix=i23-da-001
      ;;
    dsh)
      i23_candidate=deepseek_harness
      i23_image=harness-lab-deepseek-harness:i23-fault-20261020-001
      i23_context=external/build-context/deepseek_harness-fault-recovery
      i23_prefix=i23-dsh-001
      ;;
    *) return 2 ;;
  esac
  uv run --frozen python scripts/run_fault_batch.py \
    --candidate "$i23_candidate" --image-tag "$i23_image" \
    --bench-image harness-lab-bench:i23-fault-20261020-001 \
    --services-image harness-lab-services:i23-fault-20261020-001 \
    --materials-context "$i23_context" --prefix "$i23_prefix" \
    --phase "$2" --batch-id "$3"
}
```

| 先后 | 发起命令；同一行中的两个命令也须串行 | 每路线格数 | 进入下一步前看什么 |
| --- | --- | --- | --- |
| 无故障基线 | `i23_batch da baseline-101 i23-da-baseline101-001`；`i23_batch dsh baseline-101 i23-dsh-baseline101-001` | 2 | 两模式的正常任务、模型与工具可用；保留真实业务失败，并判断是否是任务本来波动 |
| 故障冒烟 | `i23_batch da smoke i23-da-smoke-001`；`i23_batch dsh smoke i23-dsh-smoke-001` | 4 | 四故障的非幂等 101 格均有充分注入/过程证据；定位实验台问题，真实能力缺口保留 |
| 正式三种子 | `i23_batch da formal i23-da-formal-001`；`i23_batch dsh formal i23-dsh-formal-001` | 24 | 四故障 × 两模式 × 101/202/303；每格独立证据、完整五轴结果 |
| 补普通基线 | `i23_batch da baseline-rest i23-da-baselinerest-001`；`i23_batch dsh baseline-rest i23-dsh-baselinerest-001` | 4 | 两模式 × 202/303；与最初两格组成同镜像三种子对照 |
| 最终路线补验 | `i23_batch da final i23-da-final-001` 或 `i23_batch dsh final i23-dsh-final-001`，按最终入选路线分别执行 | 8 | 写前 kill、写后丢回执 × 两模式 × 404/505；不足则明确标本轮未完成 |

冒烟和正式的 101 链编号不同，不能复用或覆盖。前四阶段每路线 34 格；最终补验后每条最终路线合计 42 格。

单链排查入口是 `scripts/run_fault_recovery.py`，参数为 `--candidate`、`--chain-id`、`--seed`、`--fault`、`--write-mode`、`--image-tag`、`--bench-image`、`--services-image`、`--materials-context`。它同样会真实调用模型。排查链必须使用全新且明确标注的 chain ID，保留旧链，不替代矩阵原件。

## 5. 每阶段如何判读和收尾

每阶段都要查看仓库外的 `runs/<本地日期>/<candidate>/runtime/<batch-id>/batch.json`。原始日志权限为 0600，目录为 0700；公开输出仅保留固定摘要，不粘贴原始 reasoning 或异常中的敏感内容。

- `status=completed` 表示计划格子已全部有有效结论；其中可以有 `capability_failed`。退出码 1 也可能是完整的能力失败批次，不能只看退出码。
- `status=stopped` 或某格 `integrity_stopped` 表示实验台/材料/资源/证据/清理不满足条件；先查私有原件。不能自动换新链继续，不能给缺少完整判定的格子补一个能力失败标签。
- 没有原生突然退出恢复入口时，只能由绑定原始 crash boundary、精确 Docker 进程和无 B 启动标记的完整缺口证据结束。它是能力缺口，不是自动恢复通过；缺诊断或来源绑定就停止。
- 逐格报告任务结果、故障是否命中、预算/进程、提交重放和最终处理方式；幂等服务拦住了重复写，也必须报告候选确实重发了。
- 核对资源原始采样没有错误、相邻间隔不超过既定 5 秒，且 `orchestration.errors=[]`、`cleanup.remaining=[]`。SIGKILL 后的 137 只证明故障注入，绝不当作健康退出；不能补零采样掩盖间隔。

故障 host 的采样锁只覆盖确认正在运行的 exact Worker、发送 SIGKILL、独立确认该 Worker 已正常停在非 OOM 的 137。等待 attach、删除容器与确认删除均在锁外，让剩余服务继续被采样；实际间隔仍须由原始证据验证，不因代码缩锁就预先算通过。I22 sampler 不变。

阶段结论与改造投入回写候选报告及主任务表；本清单不另建一套进度状态。


## 6. 首轮历史镜像与校准索引

构建：`infrastructure/runtime/i23-images-001/builds.json`，相对2026-10-20证据根。实际使用标签为以下`:fault-recovery`，第2节的日期标签是命令模板，不是本轮已运行标签；已有I22及详细实验标签未移动。

| 角色 | 实际标签 | 完整image ID |
| --- | --- | --- |
| bench | harness-lab-bench:fault-recovery | `sha256:6ee476154162913297f11d583312a8ec65c4f75311ca20f150b597a9e17b9c67` |
| services | harness-lab-services:fault-recovery | `sha256:1814a728fb3d9e89e48541b8df9ac7b035227f4005072226847340c00ed00efb` |
| Deep Agents | harness-lab-deepagents-langgraph:fault-recovery | `sha256:44ea561ae5a135809cd23811a777b61be4f916d533957d2f2a236c88e5ec28f9` |
| DSH | harness-lab-deepseek-harness:fault-recovery | `sha256:12e6f51a90c94abb2aacca0858ef14e2e9a19b18086ff538e8e964476c8ac981` |

公共校准在`infrastructure/runtime/i23-public-check-001/`，242项；DSH在`deepseek_harness/runtime/i23-dsh-boundary-check-001/`，25项。`host.json`和`test-output.txt`保留实际命令、完整容器ID、资源配额、退出健康与清理结果。两者network-none、未传模型凭证、退出0、无OOM、确切对象删除通过。它们证明实验台及接入边界，不是候选真实故障恢复成功。


## 7. 首次基线留下的问题

DA `i23-da-baseline101-001` 两模式完整通过，分别6/8次模型请求，未重发已提交操作。DSH `i23-dsh-baseline101-001` 在首条 `i23-dsh-001-baseline-101-none-id-101` 停止，第二模式未运行：8次模型请求、独立业务正确，但候选输出status=incomplete（native_status=completed），进程1且无OOM。原Goal已complete/disarmed、最后原生turn/end completed、native_errors=0，失败来自接入层把官方tool-goal成功完成后的deferContext收尾notice当成了外部并发输入。

还暴露了实验判定的问题：所有非kill候选都被要求退出0，导致完整报出未完成/失败结果的受控退出1，也被算成基础设施坏；后续资源核对因此短路，batch缺必需检查而停止。成功仍须完整自然终态与退出0；现已把有严格原生来源的非成功退出单列为有效能力失败，OOM/未知退出/无原生结果/资源缺测继续拒验。修正后的材料和Docker校准见第8节，另起的新真实基线见第9节。旧日志、评分、8次支出均保留，不改名覆盖。

## 8. 修订后的固定材料与校准结果

先说结论：修正后的实验接线和判定已完成本轮无模型校准，并已执行第9节的新实际基线；**校准不是让真实Agent经历故障并恢复，也不能代替四种故障矩阵。** 旧DSH失败格没有因此改成通过。

i23-images-native-notice-001（`runtime/i23-images-native-notice-001`）串行构建了notice修订DSH和第一版结果判定bench；i23-evidence-image-001（`runtime/i23-evidence-image-001`）随后构建metadata错误归类修订bench。第9节observed轮实际使用的镜像如下；后续新材料见第10、11节，不能把第2、4节旧命令模板直接当作新轮固定值：

| 角色 | observed轮标签 | 完整镜像ID |
| --- | --- | --- |
| observed轮bench | `harness-lab-bench:fault-evidence` | `sha256:5cea5c30f7075e50d73935eab456ff638a4c83ff6a6fb571468a20ff2328e012` |
| 修订DSH | `harness-lab-deepseek-harness:fault-native-notice` | `sha256:4b086b132bf0a09926201177adc4fd23085a2d3d00d885b578c393696da3a43e` |
| DA候选，沿用原材料 | `harness-lab-deepagents-langgraph:fault-recovery` | `sha256:44ea561ae5a135809cd23811a777b61be4f916d533957d2f2a236c88e5ec28f9` |
| I23 services，沿用原材料 | `harness-lab-services:fault-recovery` | `sha256:1814a728fb3d9e89e48541b8df9ac7b035227f4005072226847340c00ed00efb` |

DSH新的adapter export为`26c94e406fd5b1cb937db0024782e295161aacf1187adfa31fbeb7ef22f42c68`，位于独立`deepseek_harness-fault-native-notice`上下文；旧目录、镜像及失败记录保留。修订要分清官方Goal完成时的收尾notice和真实外部并发输入，后者仍应拒绝。i23-native-notice-check-001（`runtime/i23-native-notice-check-001`）在上述DSH镜像中 **49项通过、0跳过**，network none、无模型/远程key、退出0、非OOM、确切校准容器清理通过。这里使用假的native API验证接入边界；真实基线结果见第9节，不能据这49项校准认定实际收尾问题已解决。

| 公共判定校准 | 实际bench | 结果 | 如何理解 |
| --- | --- | --- | --- |
| i23-native-outcome-check-001（`runtime/i23-native-outcome-check-001`） | `sha256:96b22f3b09fd7affdfca57101d98ed6417f2fca662eab0341677828d85eea04e` | 336 passed、1 skipped，46.42秒 | metadata补充前的结果/受控退出校准；不是最终来源 |
| i23-native-outcome-check-002（`runtime/i23-native-outcome-check-002`） | `sha256:5ac51f8ecc0407aadb0e69cbd33bb9aa7d6e2bb20d727f398a030671a223442e` | 351 passed、9 skipped、2 failed，48.34秒 | 两个故意损坏物理metadata的反例被拒，但JSON解码错误没有带上约定的metadata归类文案，断言失败；退出1，原件保留 |
| i23-native-outcome-check-003（`runtime/i23-native-outcome-check-003`） | `sha256:5cea5c30f7075e50d73935eab456ff638a4c83ff6a6fb571468a20ff2328e012` | **353 passed、9 skipped，41.88秒** | 补充统一错误归类后，新bench重验通过；没有覆盖002 |

003已结束，宿主固定证据确认512 MiB/0.5 CPU、network none、未调用模型/远程key、退出0、PID0、非OOM、无Docker Error且确切容器清理成功。9项跳过是DSH参数下的1项DA物理checkpoint边界和8项DA metadata合并边界用例，不计作通过，不能称362项全部通过。`host.json` SHA-256为`463c97b8cb0fc581ab4596fb352afae96b7688f96f07c6a8aa66c9df94c94ddf`；最新构建记录SHA-256为`720bcf22819d2a7afad0ce560f071cccb49cefb88e432e1c5c02eb31e3964aaa`。

当前DSH上下文、最新bench及原services的新真实基线已经结束，结果见下一节。旧DA基线与旧DSH停止格的模型支出和判定继续单列；新基线的成功或失败由其自身完整证据决定。本文未预填任何新真实故障结果。

## 9. 新材料的真实无故障基线

先说人话：两条路线都把任务跑完并留下了完整检查结果，但“业务做对了”和“框架按约定结束”要分开看。DA两种模式中通过一种，另一种漏读分页；DSH两种模式均有收尾判定问题，其中一种业务结果正确，另一种还漏读分页。**本轮没有注入故障，不能据此评价故障恢复能力。**

| 链编号 | 真实模型请求 | 独立业务结果 | 原生终态判定 | 本格最终记录 |
| --- | ---: | --- | --- | --- |
| `i23-da-observed-baseline-101-none-id-101` | 5 | 仅`all_pages_read`未通过；3次写请求、3条实际写入 | 通过 | `capability_failed` |
| `i23-da-observed-baseline-101-none-nonid-101` | 6 | 通过；3次写请求、3条实际写入 | 通过 | `passed` |
| `i23-dsh-observed-baseline-101-none-id-101` | 9 | 通过；3次写请求、3条实际写入 | 未通过 | `capability_failed` |
| `i23-dsh-observed-baseline-101-none-nonid-101` | 7 | 仅`all_pages_read`未通过；3次写请求、3条实际写入 | 未通过 | `capability_failed` |

四格的其他8项外层检查均通过：独立账本、累计模型预算、完整工具审计、操作来源、注入窗口、宿主生命周期、精确进程/控制/挂载、资源配额。四格均未重发已经提交的操作。DSH实际原件均为包装层`status=incomplete`、`native_status=completed`；外层`native_terminal`报`fault_native_completion_missing`。后续定位到第10节的Goal入队时序误判，不把这一结果解释成DSH原生恢复能力缺陷；原来的失败记录继续保留。

批次原件为DA observed基线（`runtime/i23-da-observed-baseline-101-001`）和DSH observed基线（`runtime/i23-dsh-observed-baseline-101-001`）。两者均为`phase=baseline-101`、`status=completed`、`passed=false`；`completed`只表示两格已有效结束，不表示能力通过。对应SHA-256分别为：

- DA：`b233eb2abde2d26ea728598b5733de86d18bb9d116ac1748e13393e07750d40a`。
- DSH：`3da5810b10573c97fde65da6b537e2d373375cdbcf9566922b8e1dcd69c4619d`。

两批使用第8节同一最新bench、原services及各自候选镜像；DA和DSH的adapter export分别为`3a3891fbf96f57043767cc17d3c4d990b20dbbe5544f1c6d75c8ae204fbca9d2`、`26c94e406fd5b1cb937db0024782e295161aacf1187adfa31fbeb7ef22f42c68`。每条链的独立`verdict.json`、`business-verdict.json`及原始候选结果保存在同日期、同候选的`fault-recovery/<链编号>/`下。

本次新基线累计27次真实模型请求，包含全部失败尝试；本清单只记录来源和结果，没有改写请求用量或费用台账。DA后续独立故障冒烟已停止，结果见第10节；DSH尚未进入故障阶段。不能把这些无故障基线当作I23完成。

## 10. DA冒烟停止点与两处待验证修复

DA observed冒烟批次（`runtime/i23-da-observed-smoke-001`）实际为`status=stopped`、`passed=false`，不是四格全部完成。前三格的独立判定有效，第四格停止在实验判定环节：

| 非幂等101格 | 实际结果 | 当前可得结论 |
| --- | --- | --- |
| `model_timeout` | `passed`；业务、原生终态及九项外层检查通过 | 本格模型超时处理通过；不能外推到其他故障或种子 |
| `fail_once` | `capability_failed`；业务仅`all_pages_read`失败，九项外层检查通过 | 任务漏读分页；已提交操作未重发，不把业务失败改成通过 |
| `drop_receipt_once` | `capability_failed`；业务仅`all_pages_read`失败，九项外层检查通过 | 写后丢回执窗口已执行，已提交操作未重发；完整业务仍失败 |
| `kill_before_write` | `integrity_stopped`；已真实强杀A并完成清理，未形成完整恢复判定 | 实验判定器受阻，不能据此判候选不能恢复，也不能算恢复通过 |

写前强杀格的原件位于原链目录（`deepagents_langgraph/fault-recovery`）。断网状态结构诊断（`runtime/i23-da-checkpoint-shape-002`）确认，本次LangGraph把消息保存在DeltaChannel增量结构中；六份原始checkpoint的`channel_values`均没有直接的`messages`字段。旧判定器按完整消息快照读取，因而未生成恢复绑定所需材料。**字段不存在不等于原任务没有持久化。** 当前已补增量消息重建及快照投影处理，后续新bench校准与物理状态探针见第11节；不修改旧checkpoint、原评分或停止记录，真实故障新链仍须单独验收。

DSH的另一处问题也已定位：官方先把下一轮放入队列，再更新“已经启动的轮数”；接入观察器提前要求两个计数相等，把正常Goal轮次误判成外部并发输入。第8节49项假的native API校准没有覆盖这个真实顺序，因此旧校准不能证明此问题已解决。轮次顺序修订构建（`runtime/i23-round-order-image-001`）已成功，标签`harness-lab-deepseek-harness:fault-round-order`，完整ID为`sha256:45a417649165b795c927c22cff98360c45a1849247d7ba9d19cc2574202af73d`。**构建成功仅说明新材料可构建；新镜像边界校准及真实无故障基线仍待执行。**

修订材料、校准和真实新链分开留存，不能覆盖这次停止及已发生调用。第1–4节是先前执行模板，不能直接用旧context或标签启动修订轮。正式三种子、补普通基线和最终路线补验均未因此完成，I23保持进行中。

## 11. Delta修订材料、校准与新基线

recovery-lifecycle-images-001（`runtime/recovery-lifecycle-images-001`）已构建新bench `harness-lab-bench:recovery-lifecycle`，完整ID为`sha256:aa0fb8cce18aa18b9876fe6cdabaecd6e20af0519f7894cc6f6869b35463c771`。DA候选及I23 services仍沿用第8节原镜像；这里修订的是保护侧读取与判定，不把新bench冒充候选能力改造。

- i23-delta-check-001（`runtime/i23-delta-check-001`）：**376 passed、9 skipped，64.03秒**。按路线跳过的9项不算通过；断网、无模型/远程key，健康退出及确切容器清理通过。
- i23-da-delta-physical-check-001（`runtime/i23-da-delta-physical-check-001`）：只读挂载原kill状态，在私有SQLite副本中用`ancestor_delta_writes`读取6份checkpoint、5条消息和4条pending writes；原件未改，未导入候选、未调用模型，断网健康退出及清理通过。它说明判定器能读这份真实故障状态，不等于已让B恢复成功。

新无故障基线批次（`runtime/i23-da-delta-baseline101-001`）已结束，`status=completed`、`passed=false`。幂等/非幂等两模式分别4/6次真实模型请求，共10次；两格均为`capability_failed`，仅业务分页规则失败，预算、原生终态和进程等外层检查无异常。这个失败照实保留，不为取得成功结果反复重跑。

新批次`i23-da-delta-smoke-001`已开始真实故障冒烟；当前没有将活动批次预填成通过。下一步核对其完整原件，尤其真实kill后B是否用原状态、原预算空输入继续及是否重发。DSH仍须先完成轮次顺序新镜像校准和无故障基线，再进入故障阶段。


### 追加：真实冒烟的 Send 诊断接线

`i23-da-delta-smoke-001` 前三条已结束：模型超时、工具首次报错完整通过（各8条计数；超时条包含本地注入），写后丢回执条5次模型、已提交不重发，但业务分页失败。第四条已经真实杀A、从原状态绑定并启动B；B在模型前因诊断JSON不能编码原生`Send`而退出1，整批以完整性错误停止。原verdict、失败及清理记录均保留，不能把它解释为框架恢复失败。

修复仅扩展故障专属诊断投影和独立物理解码：固定serializer把无timeout的`Send(node,arg)`写为code1位置参数，候选记录完整node/arg，bench独立解码比较。原saver tuple、graph snapshot、恢复输入None和执行状态均不改；其他constructor及带timeout变体仍拒绝。新上下文为`external/build-context/deepagents_langgraph-fault-send`，adapter export `88647d200cc720ec4ca5b29ebe774b6046ba3c41c71b6c19aa6111de20f46069`。

新构建记录位于外部`infrastructure/runtime/native-diagnostics-images-001/builds.json`：bench `sha256:8a58c351654effb338e00d332b9bc1f71c49a0c25bc0c24b7fc28bb3c0bb53b5`；DA故障候选 `sha256:9e06db842d8124548b4ca0792b47e1376b0f83bae584ba4ffbdd32052cc2cb6a`。`native-diagnostics-check-001`为523 passed、9按候选路线跳过，覆盖故障/多用户/成本相关校准。`i23-real-send-encode-check-001`用真实候选Send与官方serializer生成材料，`i23-real-send-decode-check-001`在独立bench逐值核对通过；二者均断网、无key/模型调用、健康退出并清理。它们证明记录格式一致，真实杀进程续跑仍待新链验证。

DSH新轮数顺序修正镜像的真实基线批`i23-dsh-round-baseline101-001`已启动，使用上述新bench，旧误判结果不改。

DSH新基线批已结束：`i23-dsh-round-baseline101-001`两模式2/2完整通过，11/9共20次模型，完整性及清理通过。该结果只解决新入口的正常基线，四故障与正式矩阵尚待执行。

## 2026-10-21：DSH四故障冒烟完成

i23-dsh-round-smoke-001批次（`runtime/i23-dsh-round-smoke-001`）已结束，4/4完整通过，均为seed101、非幂等服务，候选为`fault-round-order`、bench为`recovery-paths`；镜像和冻结材料以批次原件为准。

| 故障 | reservation | 实际判定 |
| --- | --- | --- |
| 模型超时 | 11，含1次本地注入 | 自动完成、预算与注入有效 |
| 工具失败一次 | 13 | 自动完成，无已提交写入重发 |
| 写成功后丢回执 | 12 | 自动完成，无已提交写入重发 |
| 提交前杀Worker | 15 | A真实强杀/删除，新B从原生状态继续并完成，无已提交写入重发 |

四条链的业务、原生、过程、注入与清理检查均通过；合计51次reservation，不把注入当远程收费请求。正式双模式三种子矩阵、其余基线与最终关键种子仍待执行，不能据冒烟宣称专项完成。

## 2026-10-21：Deep Agents Send修订真实验证

新`fault-send`候选、`recovery-paths` bench下，baseline101批次（`runtime/i23-da-send-baseline101-001`）两模式0/2完整通过，各5次模型；两者均为业务失败，外层来源/预算/进程等检查有效，没有完整性中止。

四故障冒烟（`runtime/i23-da-send-smoke-001`）已结束：超时8条reservation（含本地注入）、工具失败8条均完整通过；写后丢回执6条、写前kill5条因分页业务失败。四条均无已提交操作重发、预算/注入/过程有效。最关键的kill链`original_native_crash_recovery`等11项独立检查全部通过：真实杀A、删除、绑定B、从原状态/原预算继续；Send诊断不再中止。原生恢复成立与完整业务失败同时保留，不把两者合为一个成功结论。

新冒烟合计27条reservation，四条全清理完成；正式矩阵和最终关键种子仍待执行。旧delta-smoke的完整性中止不覆盖。

## 2026-10-21：DSH正式矩阵因采样完整性停止

i23-dsh-round-formal-001（`runtime/i23-dsh-round-formal-001`）已经停止，24格中仅首格形成有效完整通过。首格`model_timeout/id/101`为12条请求计数；第二格`model_timeout/id/202`为13条，业务、注入、原生终态和预算等检查通过，但`resource_limits`因`fault_resource_sampling_gap`失败。第二格不计候选能力成败，原批次停止状态及原verdict保留。

第二格原始`resource-samples.jsonl`第9→10行，时间从`03:43:25.070916Z`到`03:43:30.186996Z`，相隔**5.116080秒**，超过5秒标准。两行均处于`worker-a_active`，四容器stats完整、error为null、无生命周期过渡；Worker直到`03:43:46.425Z`才正常退出。当前证据无法把这次间隔归因于退出、删除或某一条Docker命令。

此入口正常每轮依次查询容器清单、批量inspect、stats及后置Worker清单；已有按本轮实际耗时扣减的等待，并非每轮额外固定sleep两秒。旧证据没有逐命令耗时，不能据此认定是哪条命令慢或宿主调度暂停。下一步仅增加I23局部的monotonic分阶段诊断，记录锁等待、固定命令阶段和原始采样文件写入，不保存命令参数、环境值、模型内容或原始inspect。新材料先在Docker内校准，再用新运行留真实采样证据；不改变原顺序、锁、采样节奏或5秒判定，不改写旧数据。

### 新诊断校准及四容器连续采样

诊断第一版已构建但未执行模型实验；代码复核发现独立诊断文件写入失败可能让采样线程退出而未上报。第二版补上固定错误类型的内存标记及主线程收尾核验：诊断IO错误不覆盖原始采样写入异常，任何采样线程异常或未结束都使主链完整性失败。新bench为`harness-lab-bench:fault-sampling-closed`，完整ID `sha256:3811a47d6f11a337cf604579ac14285361de9c5aa118c32bab28f6f00d389cbc`，见构建记录（`runtime/fault-sampling-closed-images-001`）。

fault-sampling-closed-check-001（`runtime/fault-sampling-closed-check-001`）在Docker内断网、无key运行五个相关模块，**279 passed、9按路线跳过**。覆盖正常顺序、四个命令分别迟延、timeout、锁等待、原文件写入延迟、诊断文件open/write/close失败、原采样异常、线程异常及泄密反例；超过5秒仍由原判定拒绝。

fault-sampling-probe-001（`runtime/fault-sampling-probe-001`）随后用相同`FaultLab.sample_with_reporting`观察四个断网、无挂载、无key的空闲Python容器60秒，配额合计2.75 GiB/1.75 CPU，没有运行候选或模型。形成30条原始采样，覆盖59.342秒；完整ID正常终止及确切删除均通过。独立bench断网只读验收也通过，见原判定（`fault-sampling-probe-001/grade-proof`）。

| 实测项目 | 均值 | 最大值 |
| --- | ---: | ---: |
| 原采样时间间隔 | 2.046秒 | 2.167秒 |
| 清单ps | 0.017秒 | 0.033秒 |
| 批量inspect | 0.017秒 | 0.026秒 |
| Docker stats | 1.986秒 | 2.119秒 |
| 后置Worker ps | 0.025秒 | 0.038秒 |

本轮未复现5秒缺口，只证明新诊断在四个空闲进程下可用。不能用它倒推旧5.116秒的原因，也不能推断真实Agent活动时不会再次出现。后续付费批次使用新bench和新prefix；原正式首批停止及所有失败继续保留。

新i23-dsh-timing-formal-001（`runtime/i23-dsh-timing-formal-001`）按原24格完整计划串行执行，候选与services不变，bench为上表的新诊断镜像。它是新材料下的独立正式阶段，没有从旧批次挑剩余格续跑；本批后来在第19格停止，结果见下。

### 新正式批：18格有效结束，第19格采样错误停止

前18格涵盖模型超时、工具首次失败和写后丢回执，各含两种写入模式及三个种子；17格完整通过，`fail_once/id/303`仅业务`all_pages_read`未通过。该失败格报告、实际写入、预算、原生终态及其他外层检查通过，原业务失败保留。18格均无已提交操作重发，不将幂等服务防重视为框架独有能力。

第19格kill_before_write/id/101原判定（`fault-recovery/i23-dsh-timing-001-formal-kill_before_write-id-101`）业务通过、原生换容器恢复通过，但`resource_limits`为`fault_resource_sample_error`，本批按完整性规则停止。该格不计候选能力成败；余5格未运行，不能补成失败或零消耗。batch中该格verdict指针为null，实际独立原verdict存在，应以原件留存。19条链均结束并清理，当前无本实验残留容器。

安全耗时原件显示第9次采样的`inventory_ps`返回0，随后`inventory_batch_inspect`返回1、错误类型为`CalledProcessError`；不是本次诊断文件IO失败，`sampling_failures=[]`。源码中kill后的`docker rm(A)`在采样锁外，可能与采样的ps→inspect交叉；实际删除时间与本次inspect失败窗口高度吻合。诊断未记录命令对象ID及stderr，不能严格认定该返回1必定由A消失造成，更不能据此重判旧原件。

前瞻修订已把确切A的删除及不存在核验放回同一采样锁，等待attach结束仍放锁外；其余采样顺序和5秒标准不变。原失败不重判，不自动重跑挑成功，也不从本批剩余5格续接为完整正式阶段。

### 删除互斥修订与后续窗口

宿主I23脚本SHA-256为`921acb1bb7ba0e88bd1a3ad5f0812f5ff0900346179606f958baaf72d4e9ae77`。新bench `4dd8a37e…` 已构建；I23独立校准（`runtime/fault-remove-isolated-check-001`）为287项通过、9项依赖DA物理解码材料的用例按路线跳过，断网、无凭证、健康退出且确切清理通过。先前组合校准有14项DSH新接线或兼容性用例失败，原件保留；不能把本次I23独立通过扩大为DSH多用户校准通过。

真实删除探针（`runtime/i23-exact-remove-probe-001`）复用未替换的生产采样/清单方法，使用已固定的旧bench `3811a47d…` 承载无模型Python进程；20次均实际完成ps→批量inspect→rm→确切ID及名称不存在核验。22个样本最长相邻间隔2.010177秒，无采样错误，正常exit0/非OOM及全部清理通过。探针最多1 GiB/1 CPU，未运行Agent、模型、SIGKILL或原生恢复，也没有执行独立I23评分；它只验证这处删除互斥，不证明真实Agent正式阶段稳定。

随后在新bench与原DA Send候选/services下启动DA首次正式矩阵（`runtime/i23-da-removal-formal-001`），固定24格、prefix `i23-da-removal-001`。本批已结束，结果见下；DSH停止批保留原状态。


### Deep Agents首次正式24格结束

本批原件（`runtime/i23-da-removal-formal-001`）状态为`completed`，24/24格全部结束，完整通过10/24；14格业务失败均仅`all_pages_read`，不覆盖原失败。batch SHA-256为`7ee77a1ccb1cad5578f275aebf1df71e5052cb019f3803a88815a132262b85a1`。候选、bench及services为上表固定镜像，本批不与任何旧冒烟或停止批拼接成功率。

| 故障 | 完整通过 / 已结束 | 其余结果 |
| --- | ---: | --- |
| 模型超时 | 2/6 | 4格仅业务分页失败 |
| 工具首次失败 | 5/6 | 1格仅业务分页失败 |
| 写成功后丢回执 | 2/6 | 4格仅业务分页失败 |
| 提交前杀Worker | 1/6 | 5格仅业务分页失败；6条均从原生状态恢复 |

24格原独立`checks`均通过，host `errors=0`，每格`cleanup.remaining=[]`；6条kill均真实换Worker原生恢复，24格已提交操作重发均为0。这里的恢复、预算和实验完整性成立，与原业务未读全分页是两个独立结论，不能把业务失败记成完整成功。

本批axes累计154条reservation。后续009已在断网Docker完成独立SQL拆分，确认为148条远程200、6条本地注入，不能称为154次远程调用。009库存295项，token已知部分为7,769,302；未登记材料、未知usage、请求关联和金额仍有缺口，不表示I26完成。

本条完成时仍缺DSH完整formal、两路线各4条`baseline-rest`和各8条`final`。DSH新正式结果追加如下；旧停止批不重判，也不把它们与本批合并计算恢复或完整成功率；I23保持进行中。

### DSH删除互斥修订后正式24格结束

i23-dsh-removal-formal-001原件（`runtime/i23-dsh-removal-formal-001`）已`completed`，24/24格全部有效结束，**完整通过23/24**。批次SHA-256为`acc141a648168dc40e9dbb0f21d7646f96ad9412ffa1406ebab3c9d445c45ac8`。这是删除互斥修订后的独立固定正式阶段，不是把旧停止批的成功格留下、再补剩余格。

| 故障 | 完整通过 / 已结束 | 原判定说明 |
| --- | ---: | --- |
| 模型超时 | 5/6 | 非幂等服务/101仅业务分页检查失败 |
| 工具首次失败 | 6/6 | 业务及原外层检查通过 |
| 写成功后丢回执 | 6/6 | 业务及原外层检查通过，无已提交操作重发 |
| 提交前杀Worker | 6/6 | 六条均原生换容器恢复，业务及原外层检查通过 |

唯一失败为model_timeout/non_idempotent/101原业务判定（`fault-recovery/i23-dsh-removal-001-formal-model_timeout-nonid-101`）的`all_pages_read=false`；报告可读且内容正确、实际写入准确、scope与审计等其余业务项均通过。外层`checks`全过不能覆盖这项业务失败，原批次`passed=false`保留。

逐格核对既有原件：24格外层`checks`全部通过，host `errors=[]`、`sampling_failures=[]`、`timed_out=false`；每格`cleanup.remaining=[]`。六条kill的`original_native_crash_recovery`均通过；每格`reissued_after_commit=0`。这证明本固定任务和入口下的恢复、预算、原生终态及观测链有效，不把幂等服务防重算成候选独有能力，也不外推生产可靠性。旧两批采样停止结果及其失败原文保持不变，成功率不合并。

本批固定模型为`deepseek-v4-flash`，候选镜像`sha256:45a417649165b795c927c22cff98360c45a1849247d7ba9d19cc2574202af73d`，bench `sha256:4dd8a37e4dd1d6d136c6737700a3d301cc444ef1570f44d4357211b7806a8c64`，services `sha256:1814a728fb3d9e89e48541b8df9ac7b035227f4005072226847340c00ed00efb`。本次仅读取已结束索引、原判定、host和清理记录并核SHA，没有重新评分、读取SQLite或合计reservation；本批尚未纳入有效009用量截点。

截至该DSH正式批完成时，两路线各4条`baseline-rest`和各8条`final`仍待完成；DA补基线随后结束，结果追加如下。正常基线用于区分常见业务约束失败与故障后的额外损失，最终种子用于补充关键写入窗口的证据。当时I23继续进行中，最终收尾见末节。

### Deep Agents补普通基线202/303结束

i23-da-removal-baseline-rest-001原件（`runtime/i23-da-removal-baseline-rest-001`）已`completed`，四格有效结束，**完整通过2/4**。批次SHA-256为`e736012dddfb51b63cfae5c2901f93abcffaad20829416552c2976817aadb90c`，均不注入故障。

| 写入服务 | 种子 | 原判定 |
| --- | --- | --- |
| 幂等 | 202 | 完整通过 |
| 幂等 | 303 | 仅业务`all_pages_read`失败 |
| 非幂等 | 202 | 仅业务`all_pages_read`失败 |
| 非幂等 | 303 | 完整通过 |

两个失败格的报告、实际写入、scope、审计及其余业务检查均通过，分页要求仍必须满足。四格外层`checks`全部通过、`cleanup.remaining=[]`，没有完整性中止，不用正确报告或外层通过覆盖原业务失败。

本批沿用`fault-send`候选`sha256:9e06db842d8124548b4ca0792b47e1376b0f83bae584ba4ffbdd32052cc2cb6a`及services `sha256:1814a728fb3d9e89e48541b8df9ac7b035227f4005072226847340c00ed00efb`，bench为`sha256:4dd8a37e4dd1d6d136c6737700a3d301cc444ef1570f44d4357211b7806a8c64`。此前101两模式普通基线0/2的原失败保留，其bench为`sha256:8bd7f754a6de5b422a3cd4c43c3724e7427d56bc89034f4a99c5f03df2762da4`；可描述同候选下六条普通基线完整2/6，但保留两套bench分层，不拼成同一完整镜像组合批次，也不与故障矩阵合并成功率。

这些普通基线表明：没有故障时也会出现相同的分页约束失败，因此正式故障组的同类失败不能全部归于恢复机制；恢复、写入一致性和完整业务仍分别评价。本条记录时DSH补普通基线正在执行、两路线最终关键故障种子尚未完成；后续结束结果追加于下文。本文本次仅读取上述已结束索引、既有判定和清理记录并核SHA，没有重新评分、SQL或调用模型；本补基线尚未纳入009用量截点。

### DSH补普通基线202/303结束

i23-dsh-removal-baseline-rest-001原件（`runtime/i23-dsh-removal-baseline-rest-001`）已`completed`且`passed=true`，四条无故障任务**完整4/4通过**。批次SHA-256为`a7aa22808d1c6d88d69c30ce9748d5e81d74468d67ef6b2ffd1444b39167951e`。

| 写入服务 | 种子 | 原模型计数记录 | 原判定 |
| --- | --- | ---: | --- |
| 幂等 | 202 | 9 | 完整通过 |
| 幂等 | 303 | 9 | 完整通过 |
| 非幂等 | 202 | 14 | 完整通过 |
| 非幂等 | 303 | 9 | 完整通过 |

四格外层`checks`全部通过、`cleanup.remaining=[]`；这些模型计数来自既有batch记录，不等于已完成SQL核对的远程账单。本批尚未纳入009用量截点，未合计费用或远程调用总量。

本批沿用`fault-round-order`候选`sha256:45a417649165b795c927c22cff98360c45a1849247d7ba9d19cc2574202af73d`及services `sha256:1814a728fb3d9e89e48541b8df9ac7b035227f4005072226847340c00ed00efb`，bench为删除互斥修订后的`sha256:4dd8a37e4dd1d6d136c6737700a3d301cc444ef1570f44d4357211b7806a8c64`。此前101两模式普通基线2/2使用同候选及services、bench为`sha256:8a58c351654effb338e00d332b9bc1f71c49a0c25bc0c24b7fc28bb3c0bb53b5`。可描述同候选材料下普通基线完整6/6，但保留两套bench分层，不拼为同一完整镜像组合批次，也不与故障矩阵合并成功率。

本条记录时两路线补普通基线均已结束，DA最终批正在执行；两路线最终8格的结束结果追加于下文。本次仅读取已结束索引、既有判定及清理记录并核SHA，没有重新评分、SQL或模型调用。

### Deep Agents最终关键故障404/505结束

i23-da-removal-final-001原件（`runtime/i23-da-removal-final-001`）已`completed`，8/8格有效结束，**完整通过4/8**；批次SHA-256为`92d2a6b70b7341fd4780f4e8879c699af56f0ae33895043f5c045280bac88306`。候选、bench、services沿用DA补基线及首次正式矩阵的固定镜像，最终种子单列，不与前述基线或正式组拼成一个通过率。

| 故障 | 写入服务 | 种子 | 原完整判定 |
| --- | --- | --- | --- |
| 提交前杀Worker | 幂等 | 404 | 仅业务`all_pages_read`失败 |
| 提交前杀Worker | 幂等 | 505 | 仅业务`all_pages_read`失败 |
| 提交前杀Worker | 非幂等 | 404 | 通过 |
| 提交前杀Worker | 非幂等 | 505 | 仅业务`all_pages_read`失败 |
| 写成功后丢回执 | 幂等 | 404 | 通过 |
| 写成功后丢回执 | 幂等 | 505 | 通过 |
| 写成功后丢回执 | 非幂等 | 404 | 仅业务`all_pages_read`失败 |
| 写成功后丢回执 | 非幂等 | 505 | 通过 |

写前强杀四格完整1/4，写后丢回执四格完整3/4。四个失败格的报告内容、实际写入、scope、审计等其他业务检查均通过，分页约束失败原样保留。逐原件核对：八格外层`checks`全部通过，四条kill的`original_native_crash_recovery`均通过，每格已提交操作重发为0、`cleanup.remaining=[]`。原生恢复和写入一致性成立，不等于完整业务通过；不能将相同分页约束失败都归为恢复失败。

本批预算计数不在宿主合计，远程请求/注入拆分等待后续Docker SQL；尚未纳入有效009用量截点，不报完整账单。本条记录时DSH最终8格仍在执行，未读取活动目录或预填结果；现已结束，收尾见末节。本次只读取上述已结束的既有索引、判定、清理及SHA，没有重新评分、Docker、SQL或模型调用。

### DSH最终关键故障404/505结束；I23调研收尾

i23-dsh-removal-final-001原件（`runtime/i23-dsh-removal-final-001`）已`completed`且`passed=true`，8/8格有效、完整通过：写前强杀和写后丢回执各四格，覆盖幂等/非幂等服务及404/505。批次SHA-256为`84a57b8942df276f2f8bd05df874e66aa07259675deb82b625baf20b2da689e1`，候选、bench及services沿用本路线正式矩阵与补基线的固定镜像。

逐格原件确认：八格外层`checks`全部通过，host `errors=[]`、`sampling_failures=[]`、`timed_out=false`，每格`cleanup.remaining=[]`；四条kill的`original_native_crash_recovery`均通过，每格`reissued_after_commit=0`。本次只读取已结束原件和SHA，没有重新评分、SQL、模型调用或在宿主合计预算。

| 本轮固定阶段 | DSH完整业务 | Deep Agents完整业务 | 评价边界 |
| --- | --- | --- | --- |
| 正式101/202/303四故障×两服务 | 23/24 | 10/24 | 各自独立24格；业务失败均单列分页检查，原生恢复及实验完整性另验 |
| 补普通基线202/303×两服务 | 4/4 | 2/4 | 早先101基线分别2/2、0/2保留；可描述同候选普通基线6/6、2/6，但bench分层 |
| 最终404/505写前kill/写后丢回执×两服务 | 8/8 | 4/8 | DSH两故障各4/4；DA分别1/4、3/4，四失败仅分页检查 |

**I23约定调研已结束，能力结论包含失败与限制。** 两路线均有原生恢复和不重发已提交操作的证据；DSH在本固定任务中的完整业务更稳定，DA没有故障时也会出现分页失败，因此不能把其故障组同类失败都归于恢复机制。不据这些小样本给出生产可靠性承诺，不将不同阶段或旧停止批合为一个成功率。

010已完成独立断网Docker SQL验收（`runtime/research-usage-check-010`）及token验收（`runtime/token-usage-check-010`），均无key、退出0、清理通过。当前343库共2485条reservation：2416条远程200、36条模拟、28条本地注入、4条远程未确认、1条在途未知；未闭合及别名列表为空，仍有17个未登记来源未查询、不补0。token覆盖307项/285目录，2266份观察输入/输出/总量已知、13份未知；已知输入9,590,870、输出662,533、总量10,253,403，11个材料单元无可用usage。没有请求级join、完整账单或金额结论，I26本轮未完成。

| 已结束阶段 | 010安全分组中的请求分类 |
| --- | --- |
| DSH正式24格 | 293条reservation，其中287条远程200、6条本地注入 |
| DA补普通基线4格 | 21条远程200 |
| DSH补普通基线4格 | 41条远程200 |
| DA最终8格 | 50条远程200 |
| DSH最终8格 | 95条远程200 |

上述分类来自已通过并清理的Docker分组验收，只读原SQL安全摘要，未在宿主打开数据库或合计；分组原件（`research-usage-enrollment-010/proof`）。010清单SHA-256为`3d424d716689b10179dc59f16ca95bd11c7066aa3140e2d68652d0c53f1d14c3`，SQL安全摘要为`69d2a174beb4f73f8defc9b154ad4313968eb8a63d5a038354f732326c0c44b1`，token安全摘要为`ee30a30d4914e74a5716372bcc7aa26adb003345bda063dd381aaf43bf981131`。009及更早原件全部保留；前述各阶段“未纳入009/待SQL”是当时截点记录，现由010追加，不改写历史。

本轮I24/I25的未完成项另按真实缺口收尾，不为了全部通过继续扩建实验台；I27条件化建议已交付，不证明生产迁移或线上省钱。
