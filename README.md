# Harness Lab

用于调研各种 Agent 框架实现云端 Harness 的优劣。

需求入口：[REQUIREMENTS.md](./REQUIREMENTS.md)

实施方案：[LOCAL_VALIDATION_PLAN.md](./LOCAL_VALIDATION_PLAN.md)

当前进度：M0/M1/M2 的本地环境、统一 Runner、Fixture、Playwright 执行基线和
Compose 拓扑已完成；DeepSeek Harness、OpenClaw、Deep Agents/LangGraph 已完成来源、
依赖和最小远程模型冒烟，DeepSeek T1 工具任务和浏览器能力阻断也已留证。

本地环境检查：

```bash
./scripts/check-environment.sh
.venv/bin/pytest -q
docker compose -f compose/docker-compose.yml config
```

候选框架源码放在仓库外，适配器和统一 Runner 在本仓库维护。远程模型凭证只通过未跟踪的 `.env` 或环境变量提供。
