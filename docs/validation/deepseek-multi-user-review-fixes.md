# DSH I24 首次源码评审修订

日期：2026-10-21。本次仅修改源码与离线测试，**未执行测试、编译、Docker、SDK或模型**。初交付 [冻结清单](deepseek-multi-user-source-freeze.sha256) 原样保留；修订文件摘要另存 [修订清单](deepseek-multi-user-review-fixes.sha256)。主任务报告的初交付镜像校准结果应独立保留，不能改写为本修订已通过。

| 根因 | 最小修订 | 新增/修订验证代码（未运行） |
| --- | --- | --- |
| `DSHAttemptWorker._bound_request` 使用 `immutable_bytes` 默认1MiB读取SOURCE_MANIFEST；I22真实原件为2,226,959字节，正常启动将先被大小守卫拒绝 | 仅 `/opt/materials/SOURCE_MANIFEST.json` 显式使用有界8MiB；请求、lifecycle和adapter manifest仍使用原默认1MiB；共享读取函数不改 | 生成与source exporter相同schema/per-file字段的大于1MiB清单，实际执行 `_bound_request`、`verify_binding`、`immutable_bytes`、`read_bound_file` 的完整读取路径；另拒绝source超8MiB及其他三文件超1MiB，并确认可写控制挂载仍被拒绝 |
| 新恢复材料测试的临时目录可写，生产只读守卫在目标物理重放断言之前先拒绝 | 仅合成测试fixture模拟 `statvfs().f_flag & ST_RDONLY`；保留实际文件读取、大小/硬链接/路径及生产只读检查 | 原正例、文件/预算负例和6类物理材料定向反例继续执行原验证器；fixture明确说明没有实测只读Docker挂载。chmod改变权限，不能设置statvfs只读文件系统标志 |
| 新policy候选检查直接比较缺省值None，拒绝此前使用省略candidate的DA旧policy材料 | policy字段省略时仅按DA解析，与已注册可信spec比较；明确的null、未知候选和候选不匹配仍拒绝，DSH省略字段不会落入DSH | 基于现有fake Worker HTTP/真实policy文件fixture测试DA省略/显式通过；DA↔DSH错配、未知/null和DSH省略拒绝；材料核对阶段不派发HTTP |

本次修改为 `execution/worker_control_dsh.py`、`execution/control_backend.py`、`tests/test_dsh_multi_user.py`、`tests/test_container_control.py`。SOURCE_MANIFEST测试只映射固定绝对路径和模拟只读挂载标志，未stub `_bound_request`、`verify_binding` 或实际bounded-file reader；清单文件条目是合成材料，不是上游来源真实性证明。

实际镜像必须由主任务在独占窗口重新构建并运行容器校准；本修订不触及I23冻结材料、候选核心、I25或旧结果。新导出的DSH source上下文仍待stage/build，不能把已有导出等同于Worker可用。
