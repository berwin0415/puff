# platform/services

跨业务域的平台服务目录，由平台团队维护。

当前为空。典型内容：API 网关、鉴权中心。平台服务同样需要 `project.json`，标签为 `domain:platform`、`type:service`、`lang:<语言>`，并受 `tools/checks/project-boundaries.mjs` 的依赖规则约束。
