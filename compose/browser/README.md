# 容器浏览器材料

构建和校验 Linux Chromium、Playwright 与 MCP 浏览器环境。

主要内容：

- `launcher.mjs`：启动浏览器及 MCP 服务。
- `acceptance.mjs`：校验浏览器基础动作、页面及产物。
- `package.json / package-lock.json`：固定 Node 依赖和安装版本。

用于容器内浏览器轨道；基础验收不算候选自主完成网页任务。
