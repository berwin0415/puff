# 独立验收与证据处理

准备任务输入、读取可信原件并独立判断业务、恢复、作用域和资源结果。

主要内容：

- `grading.py / detailed.py`：公共任务判定与详细实验准备。
- `pause_recovery*.py / fault*.py`：暂停续跑与故障恢复验收。
- `multi_user*.py / shared_*.py`：产品身份、双用户控制和运行边界。
- `instance_cost*.py / metrics.py`：实例生命周期、采样及资源评价。
- `config.py / process_observation.py`：配置路径及原生进程证据校验。

验收程序运行在 Docker 中；宿主 scripts/ 只做编排和采样。Runner 不替候选执行缺失的业务步骤。
