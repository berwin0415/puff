# I25 探针独立评分入口的最小修订

状态：2026-10-21 仅完成辅助脚本和文档修订，尚未测试、编译或运行新探针。probe001、probe002 的原件与失败结果保留。新 helper 无需重建 bench；后续先对 probe002 的只读副本做离线诊断，再由主线程使用全新 probe003 目录运行真实探针。

## 1. probe002 为什么没有完成评分

原实验目录：`runs/2026-10-21/infrastructure/runtime/i25-native-lifecycle-sampling-probe-002`。

| 原件 | 直接记录 |
| --- | --- |
| `protected/commands/command-000709.json` | 用固定 bench image 创建独立评分容器，入口为 `python /probe.py grade-in-container --root /trial`，没有设置 `PYTHONPATH=/app` |
| `command-000710.json` / `command-000710.stderr` | `docker start --attach` 返回 1；`/probe.py:197` 首次导入 `runner.instance_cost` 时明确报 `ModuleNotFoundError: No module named 'runner'` |
| `command-000711.stdout` | 完整 ID `d952ebd003f3437b3ca47ce8262beff9e14ba5af3ba71f6bbfc69ebffaf45c3d` 的实际终态为 exited、ExitCode1、PID0、非 OOM，运行时 Error 为空 |
| `protected/probe-host-error.json` | 宿主随后读取不存在的 `protected/probe-verdict.json`，产生次生 `FileNotFoundError` |
| `protected/rotation-sampler-status.json` / `flow-resource-samples.jsonl` | sampler 正常结束，写出 35 份采样，采样记录中没有 error；独立评分入口未成功，尚不能证明完整连续采样规则通过 |
| `protected/probe-result.json` | `probe_passed=false`、`grade_passed=false`、`cleanup_confirmed=true` |

固定 bench `sha256:863b70d9c145cd69c760e57418afbc9189054401ead63c29c073ea716e944b3d` 的源码位于 `/app`，工作目录也是 `/app`。原能力检查用 `python -c`，可从工作目录导入模块；评分用绝对脚本 `/probe.py`，Python 首先以脚本所在的 `/` 查找模块。原始 traceback 已直接证明评分入口没有找到 `runner`，能力检查通过不能替代这个入口的验证。

原 helper 使用 `check=False` 接受评分命令返回，随后立即读取 verdict，尚未记录评分进程完整性就因缺文件中止。因此本次同时修正导入路径和取证顺序，不把 35 份无错采样改写成通过结论。

## 2. 本次如何修正

只修改宿主辅助脚本 `/tmp/harness_lab_instance_lifecycle_probe.py` 的独立 grader 段落：

1. grader 创建命令显式增加 `-e PYTHONPATH=/app`，让 `/probe.py` 导入固定镜像内的模块。镜像 `/app` 不绑定宿主仓库或 trial 内容；`/probe.py` 仍是本轮新目录内冻结 helper 的只读挂载。
2. 记录创建、`start --attach` 的原始命令引用；取得原完整 ID 的实际 Docker 安全投影后，先写出 `protected/probe-grade-execution.json`。记录包含完整 ID、名称、期望 image、实际 image/终态、CLI rc、精确容器数量与原命令路径及 SHA；安全投影只保存环境变量名称，不保存值。
3. 再检查 verdict 文件。缺失时明确抛出 `probe_grade_verdict_missing`，由原失败路径保留执行证据并清理。执行记录本身没有评分或放行效力。
4. verdict 存在时，仍使用原 `bench_process_integrity()` 核对同一完整 ID、image、配额、真实终态、CLI 与 Docker 退出码，再精确清理并核对 ID/名称不存在。原 grade 与 cleanup 的通过条件保持不变。

本修订不修改生产模块、bench 源码、镜像、Worker/服务入口、采样规则或退出码规则。`probe-grade-execution.json` 是读取 verdict 前的执行证据；依赖 verdict 的 `probe-grade-process-integrity.json` 仍只在读取成功后生成。若 Docker attach 或观察本身无法完成，也仍走原失败取证和清理路径，不合成“实际退出”或评分结果。

## 3. 冻结与后续验收

新 helper SHA：`a2a615652404cbe7879cc4031beb6d63ca51145bb990c8f7701f282758366d52`。本说明和新 helper 的清单为 `docs/validation/instance-cost-probe-import-source-freeze.sha256`。

probe001、probe002 各自的 `protected/probe-source.py` 仍为原 SHA `25671fce24976ec67675b98c34c53d3b98dc8db824090a008355002c38bebc17`；已有两份源码冻结清单保持原样，不用新 helper 覆盖旧原件。002 的后续离线诊断只能写入新目录，不能追加或覆盖原 trial，也不能把诊断结果冒充原评分容器成功。

后续新 probe003 需要确认：实际 grader 使用新参数；在 verdict 读取之前已有可核对的执行记录；固定 image 内原独立评分成功执行；评分进程真实健康退出；全部连续采样和生命周期检查通过；最后精确清理通过。任一失败保留原件，不重跑同一 ID。即使无模型探针完整通过，也只证明这条基础设施验证路径，不代表 I25 业务轮换、资源节省或候选业务能力已经通过。

本次只做源码差异、原件和 SHA 的静态核对，没有执行 helper、测试、编译、Docker、模型调用或评分。
