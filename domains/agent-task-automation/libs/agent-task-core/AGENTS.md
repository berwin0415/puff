# Agent 任务自动化核心库

本项目提供宿主无关的配置、运行条件和共享工作流资源。源码在 src，验证在 test，工作流与模板通过包入口供宿主装配。

在仓库根使用 pnpm --filter @puff/agent-task-core run <脚本>。dev 监听编译，build 生成 ESM 和类型，lint 和 typecheck 检查源码，test 运行 Vitest，clean 清理构建产物。交付时执行根目录要求的仓库检查。

遵循根规则。宿主类型、工具调用和认证由消费该库的宿主处理。相对导入使用 .js 后缀。
