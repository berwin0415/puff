# DeepSeek Harness I24 最小接线：源码实施交接

日期：2026-10-21。依据 [接线设计](deepseek-multi-user-design.md) 完成源码及离线测试编写。**本轮没有运行测试、编译、Docker、候选 SDK 或模型，也未生成新的 I24 能力通过结论。** 交接后先由独立评审检查源码，再由主任务安排容器校准与真实试验；I22/I23 冻结代码、上下文和结果不因本次工作重写。

## 1. 交付范围

| 文件 | 职责 |
| --- | --- |
| `execution/dsh_materials.py` | 固定 Goal argv/cwd/env、完整身份及路由、累计300秒/20次调用预算、全目录索引、原始A绑定、物理Session及Goal独立重放、展开后的原生事件摘要 |
| `execution/worker_control_dsh.py` | 单固定attempt；复用既有私网HTTP和保留Popen/PID机制；启动前核对只读材料；构造干净Node环境；信号ACK仅表示派发尝试 |
| `runner/multi_user_dsh.py` | 原生 `session.event` 活动/重叠、DSH基线来源、取消终态与副作用判定、独立零模型工具面验收 |
| `scripts/stage_deepseek_runtime.py` | 从已冻结I22上下文读取全部13个接入文件，逐字节验证；新增Python包装及manifest；复制原Dockerfile和.dockerignore；拒绝覆盖 |
| `execution/control_backend.py` | 从可信attempt选择DA/DSH，核对description、原生材料表、ACK和恢复材料；保持认证、归属及固定路由 |
| `runner/multi_user.py` | 默认仍DA；显式 `--candidate deepseek_harness` 选择DSH；prepare/bind、活动、基线、取消与资源材料分派 |
| `scripts/run_multi_user.py` | DSH系统Python和模块、固定只读挂载、先独立工具面校准再生成启动绑定；共用原资源与清理设施 |
| `tests/test_dsh_multi_user.py` | 源码级离线边界测试：固定启动/环境、ACK不确认完成、身份/预算/物理材料拒绝、真实事件格式解析、重叠负例、取消缺口、精确挂载与零模型验收 |
| `tests/test_dsh_multi_user_staging.py` | 合成材料的不可覆盖、I22原字节保护、来源不匹配与符号链接拒绝；不构成真实来源或运行证据 |

上述源码、测试和本文的SHA-256保存于 [冻结清单](deepseek-multi-user-source-freeze.sha256)。清单是本次交接时的字节快照，不证明测试通过，也不替代构建后实际镜像ID与manifest。主任务如继续修改公共编排文件，需建立新的构建清单，不能静默沿用此冻结摘要。

## 2. 固定接口与保留缺口

DSH子进程固定为 `/usr/local/bin/node --import tsx/esm /opt/materials/harness-lab/goal-runtime-entrypoint.ts --request /config/request.json`，包解析cwd固定 `/opt/materials/sources/main`，业务Session cwd仍为 `/workspace`。系统Python控制层的 `PYTHONPATH` 为 `/opt/materials/harness-lab`；模型run token只读挂载 `/budget/run.token`，远程密钥仅在模型代理。

后端先核对原A正常退出，再验证B只读绑定。B继承原材料/镜像/工具/模型/token/预算标识，并从已用活跃时间保守扣除300秒总额度；恢复请求不含prompt或followup。Backend同时重放A关闭后的原Session物理文件，B Worker校验其完整目录；原生入口再独立校验原Session前缀并执行公开恢复。包装层不实现新的Agent循环或恢复算法。

暂停使用公开Goal安全边界，取消使用公开 `Agent.cancel`。ACK始终 `immediate_cancel=false`、`native_cancel_confirmed=false`；DSH另记 `immediate_cancel_requested` 描述请求类型。只有匹配原turn/cause的aborted结束事件、原始idle/引用链、物理Session和全部副作用成立，才允许原生取消通过。I17已知缺少turn/end时继续记失败/未验证；不能用idle、pause或容器停止代替。取消诊断可保留真实子进程退出1，主任务暂停/恢复仍须退出0。

零模型工具面校准属于新镜像/完整scope，必须实际暴露11个工具，精确核对5个挂载、2GiB/0.75CPU/init、单网络、真实退出及删除；模型计数前后均0。原始工具面JSON字节直接复制并哈希，独立生成 `surface-pins.json`，不覆盖预先声明的 `pins.json`。

仍未验证：容器内Python/Node包解析、实际挂载和私网HTTP兼容性、并发观测窗口、0.75CPU基线、真实A→B恢复、立即取消终态，以及浏览器隔离、同Worker换用户。合成离线测试即使后续通过，也不能补齐这些证据。

## 3. 后续执行顺序与命令模板（均未运行）

执行窗口由主任务按独占锁和资源上限串行安排。先核对冻结摘要并完成独立静态评审；下列命令中的新镜像/上下文名称必须不存在，任何失败保留原目录和日志，换新ID处理，不覆盖重试。

```sh
shasum -a 256 -c docs/validation/deepseek-multi-user-source-freeze.sha256
```

1. 使用现有 `prepare_candidate_source.py` 在全新的destination root导出固定DSH提交，再把该导出目录放入 `external/build-context/deepseek_harness-multi-user`。核对SOURCE_MANIFEST的clean commit/export与原I22一致。然后只对新目录执行：

```sh
uv run --offline python scripts/stage_deepseek_runtime.py --context external/build-context/deepseek_harness-multi-user --base-context external/build-context/deepseek_harness-pause
```

stager本身不验证真实运行。其 `runtime_verified` 与 `capability_verified` 均为false；禁止改用既有DSH stager覆盖I22目录。新上下文完整来源校验留给原 `verify-source.mjs`，新adapter export与原I22不同是增加包装文件所致。

2. 串行构建新Worker、bench和services镜像并记录完整sha256 ID。Worker使用新上下文；bench/services使用当前源码。已有 Dockerfile 不需变更。先在不带凭证的容器内执行新旧相关离线用例，例如：

```sh
docker run --rm --network none --read-only --tmpfs /tmp:size=128m --memory 2g --cpus 0.75 --entrypoint python NEW_BENCH_IMAGE_ID -m pytest -q tests/test_dsh_multi_user.py tests/test_dsh_multi_user_staging.py tests/test_multi_user.py tests/test_multi_user_host.py tests/test_worker_control.py tests/test_container_control.py
```

`NEW_BENCH_IMAGE_ID` 由主任务替换为本次实际完整ID；此处不填旧镜像或伪造摘要。容器内测试仅使用合成字节/假子进程，不调用SDK或模型。独立补查Worker里 `/usr/bin/python3`、新模块、固定Node/tsx位置与describe；测试和模块导入校准均须留原始退出与输出。

3. 创建新pins：`candidate=deepseek_harness`、`worker_image_id`、`bench_image_id`、`services_image_id`、原source export、新adapter export、固定 `model_name`。DSH不使用DA的adapter helper hash。不得复制I22旧工具面通过文件；实际执行器会为每个新scope先做零模型校准并生成新绑定。

4. 同入口seed101不暂停基线，仍使用2GiB/0.75CPU。基线只启动A主任务；其他预生成任务不能出现模型或工具活动：

```sh
uv run --offline python scripts/run_multi_user.py --candidate deepseek_harness --baseline --trial-id i24-dsh-baseline-101-001 --date 2026-10-21 --seed 101 --pins-file /absolute/path/to/NEW_DSH_I24_PINS.json
```

5. 来源、预算、采样、资源和生命周期门禁成立后，执行seed101双用户接线冒烟，再决定是否冻结材料进入正式101/202/303。业务规则失败保留原判定；已知取消缺口不因其他子项通过而改成全项通过：

```sh
uv run --offline python scripts/run_multi_user.py --candidate deepseek_harness --trial-id i24-dsh-smoke-101-001 --date 2026-10-21 --seed 101 --pins-file /absolute/path/to/NEW_DSH_I24_PINS.json
```

正式轮按相同命令分别使用seed101/202/303及全新trial ID；不得挑成功重跑。双用户阶段总配额仍为5.5GiB/3CPU，bench不与两个活跃Worker并行。I25常驻成本、修复候选核心、浏览器身份恢复均不在本轮范围。

## 4. 自研成本归因及交接状态

新增DSH Worker/材料守卫、候选事件与Goal契约适配、surface校准及维护属于候选专用自研接入；认证/归属、模型计数代理、资源采样与独立验收属于公共产品/研究设施。官方负责Agent循环、Goal/Session、工具调度与原生持久化恢复。没有人工工时记录，本轮不估报实测人日。

目前完成源码静态核对和测试编写，只运行过源码读取及Git空白检查；测试、编译、镜像、容器、SDK、模型、I24实测均待主任务执行。故障或不匹配应保留原始诊断，先区分接线设施与候选原生缺口，不能为继续收费试验放宽材料、预算或终态标准。
