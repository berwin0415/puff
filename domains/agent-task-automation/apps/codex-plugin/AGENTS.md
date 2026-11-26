# Codex 任务自动化插件

本项目通过同域核心库组织安装引导、工作区绑定和 Codex 原生计划。src 提供本地配置命令，skills 提供安装引导，scripts 装配插件，test 验证适配和产物。

dev 监听构建，build 装配 dist/plugin，lint 和 typecheck 检查，test 使用 Vitest 验证，clean 清理 dist。先构建核心库，或从根目录执行递归 build。交付时执行根目录的检查、构建、测试与项目边界验证。

遵循根规则。Codex 原生计划与外部工具调用由宿主会话处理。相对导入使用 .js 后缀。
