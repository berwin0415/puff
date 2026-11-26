# Codex 任务自动化插件

从仓库根构建插件和它依赖的同域核心库。

~~~bash
pnpm install --frozen-lockfile
pnpm exec nx run @puff/codex-plugin:build
~~~

产物位于本项目的 dist/plugin。它包含 manifest、setup 和 task-run skill、模板及 Node 配置命令。仓库的本地 marketplace 已指向该目录。

## 本地安装

用 Codex 打开仓库，在 Plugins Directory 选择 `puff-local` 并安装 `agent-task-automation`。也可以在仓库根使用 Codex CLI 注册市场和安装插件。

~~~bash
codex plugin marketplace add .
codex plugin add agent-task-automation@puff-local
~~~

插件更新后重新构建，再执行安装命令刷新本地安装。

使用 `$agent-task-automation:setup`，先查看已有绑定，再一次配置一个工作区的任务范围、允许修改路径与已有能力。使用新的工作区 ID 可继续绑定其他目录，每个工作区的能力、规则、计划和运行记录独立保存。Agent 会保存当前绑定并试跑。规则与摘要保存在当前用户的 `~/.puff/agent-task-automation` 下。

## 手动与定时

手动调用 `$agent-task-automation:task-run`。支持与工作区匹配的本地目录，或宿主已创建并能验证归属的 Git worktree。需要完整执行时，宿主需提供可核对的活动状态。同一插件工作区的活动任务使用同一个本地名额。

定时配置默认关闭。可通过 setup 生成计划参数和自然语言提示，再在 Codex Scheduled 关联已确认的工作区。存有本地任务的机器与桌面客户端需要可用。已有计划触发时重新读绑定。计划引用或工作区活动状态无法核实时，Agent 可走只读试跑并说明原因。

通过工作流入口可以读取状态、暂停和恢复。取消由宿主处理。若运行中断，请先通过宿主确认会话已停止，再恢复本地记录。交付状态未知时先使用原能力查询。具体命令见 [宿主说明](./references/host.md)。

开发验证顺序为先构建，再从根执行递归测试。真实任务、交付和通知能力由工作区用户绑定。
