# 候选原生机制探针

在固定候选镜像内验证真实原生状态和生命周期接点。

主要内容：

- `deepagents_pause_probe.py`：LangGraph drain、SQLite 保存和换进程续跑校准。
- `deepagents_resident_probe.py`：常驻入口的原生历史准备、状态读取与关闭。

按脚本约定使用独立证据目录和容器；无模型探针不算通用 Agent 业务、隔离或成本收益验证。
