# OpenClaw 业务工具插件

将本轮 HTTP 工具、浏览器、执行规则和业务状态接入 OpenClaw 插件接口。

主要内容：

- `index.mjs`：注册工具及插件入口。
- `browser-client.mjs`：连接浏览器并记录动作证据。
- `business-state.mjs`：从实际工具结果保存业务观察。
- `detailed-options.mjs`：校验可信配置、路径和任务作用域。
- `openclaw.plugin.json / package.json`：声明插件身份和加载方式。

工具名称和协议字段保持稳定；业务步骤由真实候选执行。
