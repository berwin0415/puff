# 宿主编排与材料准备

提供实验命令入口、源码材料准备、容器生命周期控制和离线统计入口。

主要内容：

- `lab.py`：统一基础实验入口。
- `stage_*.py / prepare_candidate_source.py`：固定候选材料、依赖和构建上下文。
- `run_*.py`：按专项启动容器、采样、收尾和保存原件。
- `summarize_*.py / audit_*.py`：离线统计与采样审计程序，按约定放到验收容器执行。

宿主 Python 使用 uv run --frozen python；候选、业务、评分和统计均按实施计划在 Docker 内执行。不要直接把统计脚本在宿主运行当作正式验收。
