# Codex 操作

使用 Node 运行插件根目录下 scripts/automation.mjs，参数依次为命令名和可选 JSON 输入文件。以插件实际安装目录解析脚本位置。将工作区作为命令运行目录。JSON 输入文件可由宿主文件工具生成，使用绝对路径传给命令，避免将任务文本拼接到 shell 命令。

## 配置和计划

- config 读取完整配置，list 返回每个已绑定工作区的状态摘要。configure 每次接收一个工作区对象，字段见 [配置模板](../templates/config.example.json)，会追加新 ID 或更新同 ID 的配置，不会覆盖其它工作区。Codex 宿主填写 type 为 codex、adapterVersion 为 0.1。绑定必须从用户选定的目录与实际可用的能力生成。配置及摘要保存在用户目录下 .puff/agent-task-automation。
- 每个工作区独立保存任务范围、能力、允许路径、运行窗口、暂停状态、计划引用和活动记录。重复绑定同一真实目录到不同 ID 会被拒绝，避免宿主上下文无法判断应使用哪套规则。
- 保存后，用 prepare 的 preview 模式试跑，并在宿主实际读取选定任务源。用户随后可独立请求手动执行。
- plan 接收 workspaceId，返回原生计划所需的工作区、模式、窗口、间隔和提示。用户确认后，使用 Codex Scheduled 支持的入口关联相同项目，按模式选择本地目录或宿主 worktree。用户确认且能读取宿主返回的计划引用后，以 attach-plan 保存 workspaceId 与 hostPlanRef。改绑规则时更新已有计划。
- pause 和 resume 接收 workspaceId。暂停时同步调整宿主计划。宿主取消需单独完成。已有活动记录在宿主确认结束后，可用 recover 记录 cancelled 或 unknown-delivery。无法取得可靠结束证据时保留活动记录。

## 运行

- prepare 接收 workspaceId、trigger、activity、availableRefs，以及定时入口的 hostPlanRef。trigger 取 preview、manual 或 scheduled。availableRefs 只列当前已确认能调用的绑定引用。activity 取 idle、busy 或 unknown，由 Codex 当前会话可取得的工作区运行信息决定。只有能核实该工作区的相关会话、排除当前检查并确认其余均未活动时才可填写 idle。若无法核实计划引用，使用 preview 并说明原因。
- prepare 返回 execute 时，可按只读上下文读取并选择任务。开始执行前用 start 传同样字段加 taskRef，只有返回 runId 才开始写入。任务、计划或工作区条件发生变化时重新核对。
- check-action 接收 workspaceId、runId、action、availableRefs。对于绑定操作传 role。对于修改传相对于当前目录的 paths。每次修改或业务写入前执行检查。允许结果只反映插件的配置，Codex 和能力的权限仍需满足。
- finish 接收 workspaceId、runId、outcome，以及可用的 verification、deliveryRef、detail。outcome 可取 completed、blocked、failed、cancelled、unknown-delivery。只读检查通过会话返回结果。
- status 接收 workspaceId。存在待核对交付时，按原绑定使用只读查询。调用 reconcile 时传 workspaceId、taskRef 和包含原能力的 availableRefs。已确认交付时提供 deliveryRef。原能力明确确认未交付时，以 deliveryAbsent 为 true 并在 detail 中写明依据，后续触发可重新检查该任务。仍未确认时保留待核对状态。
