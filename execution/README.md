# 执行服务与控制接点

提供容器内业务工具、模型计数代理、产品后端边界和候选 Worker 控制。

主要内容：

- `model_proxy.py`：透传模型请求、持久计数及故障注入。
- `control_backend.py`：校验产品身份并路由到已登记 Worker。
- `worker_control.py / worker_control_dsh.py`：维护单个候选子进程及控制接口。
- `dsh_materials.py`：校验 DSH 冻结绑定和原生材料。
- `tools/`：业务工具、工作区操作及写入前实验栅栏。

服务不替 Agent 规划任务；可信身份、预算和材料由编排入口绑定。
