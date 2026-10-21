# I25 服务正常停止的最小修订

状态：2026-10-21 源码修订，尚未运行本修订的测试、新镜像或 probe002。probe001 保留失败，原源码冻结清单不修改。本修订不表示真实连续采样、资源节省或业务能力通过。

## 1. 已有证据能确认什么

原实验目录：`/Users/hanbowen01/harness-lab-data/runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-001`。

| 原件 | 直接记录 |
| --- | --- |
| `protected/commands/command-000677.json` 及 stdout/stderr | 对额外 fixture 完整 ID `c114dc0b4c574ce02fa405e13ecc19ada2658722005cbd9e8329ac41dcfc1bb9` 执行 `docker stop --time 10`，耗时约 10.177 秒，命令 rc0 |
| `command-000728.stdout` | stop 返回后，同一 ID 实际 `exited`、ExitCode137、PID0、OOMKilled=false、Error为空 |
| `command-000724.stdout` / `command-000733.stdout` | 第 42 份采样前仍为 running，采样后已是同一 ID 的 exited137 |
| `command-000725.stdout` / `command-000729.stderr` | stats 虽 rc0，但资源显示 0B/PIDs0；top rc1，明确该完整 ID 已不运行 |
| `protected/flow-resource-samples.jsonl` / `rotation-sampling-failure.json` | 第 42 份记录 ValueError，首次失败锁存为 `rotation_sampling_error`；没有独立 grade 完成记录 |
| `protected/probe-result.json` | `probe_passed=false`、`grade_passed=false`、`cleanup_confirmed=true` |

冻结规则明确普通 `service_stop` 只接受 0/143。因此主线程 `terminal()` 的终态检查必然拒绝实际137，采样的 `validate_current()` 也必然拒绝它。实际 Docker 投影的 ID、名称、image、挂载等身份字段一致；这不是挂载排序变化，也不能靠忽略 top 错误解决。

原错误消息没有落盘，不能把静态推导的固定错误码冒充原日志。但已有实际退出码与冻结源码足以定位必须失败的分支。10 秒后137与服务直接以 Python 运行、未声明 `--init`、fixture 未安装 SIGTERM handler 的源码组合相符；未采集 Docker signal 事件，故不额外声称原始信号事件已经独立证明。

## 2. 本次只改什么

只对显式轮换所用的 `RotationLifecycle.service()` 增加 Docker `--init`。容器初始化进程负责转发停止信号，使 Python 服务能够接收到 TERM；Worker 保留原 native 创建和关闭入口。

`RotationLifecycle.inspect()` 保存实际 `HostConfig.Init` 原值到安全投影的 `host_config_init`。创建后、登记前，所有非 Worker 服务必须实际为布尔值 `true`；仅 argv 写了 `--init` 不够。最终独立 `validate_registry()` 再次检查该创建原件。

旧全局 `observations()` 不增加字段，`IDENTITY_FIELDS` 不扩展。`HostConfig.Init` 是该容器创建时固定的配置，后续仍用原完整 Docker ID、名称、image、role、配额、网络、环境名称和挂载核对身份。因此新创建原件中的 Init 与同 ID 后续投影绑定；不能借其他容器的 Init 记录。

普通服务仍只接受 exit0/143，正常 native Worker 仍必须 exit0；137 只允许原故障注入 A 的确切记录。既不增加停止宽限，也不放宽任一阶段的 5 秒采样标准。初始化进程产生的真实额外进程及开销，应由后续 stats/top 实际测量，不能按原数据推算。

## 3. 文件和验证边界

| 文件 | 本次用途 |
| --- | --- |
| `scripts/instance_cost_lifecycle.py` | 新轮换服务启用 `--init`，记录真实 Init，并在登记前拒绝缺失或非 true |
| `runner/instance_cost_lifecycle.py` | 独立核验非 Worker 服务的原创建 Init；原身份和退出规则保持原义 |
| `tests/test_instance_cost_service_init.py` | 合成 transport/登记反例；只证明检查边界，不证明 Docker 转发信号能力 |
| `docs/validation/instance-cost-service-init-source-freeze.sha256` | 本修订的新 SHA 清单；不覆盖原冻结清单 |

后续由主线程串行构建包含新模块的新 bench，先运行原 I25 回归及新增反例，再使用原无模型探针和全新 probe002 ID。服务 image 无需为 `--init` 修改材料；候选 Worker image 和原模型配置不改。探针必须重新观察真实服务允许终态、完整采样时序、独立评分与精确清理，任一失败仍保留原件，不重跑同一目录。

原 `/tmp/harness_lab_instance_lifecycle_probe.py` 保持 SHA `25671fce24976ec67675b98c34c53d3b98dc8db824090a008355002c38bebc17`。它的命令日志安全投影尚未额外加入 Init；本次生产创建登记会保存真实 Init，并由新 bench 独立核验。原 probe001 没有完成生命周期最终登记与独立 grade，不能补记为本修订的通过证据。
