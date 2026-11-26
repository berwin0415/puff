---
name: setup
description: 为任务自动化插件配置 Codex 工作区、已有任务与交付能力。用于首次设置、换绑工具或配置原生定时计划。
---

阅读 [通用配置流程](../../references/setup-workflow.md) 和 [Codex 操作说明](../../references/host.md)。先使用 list 或 config 查看已有绑定，再按用户选择配置一个当前或指定工作区，保存前展示能力、目录和动作范围。配置新工作区时使用新的工作区 ID，保存不会覆盖其他工作区。

保存绑定后，通过 task-run 的只读试跑确认任务读取和上下文。只有用户明确启用计划时，才在 Codex Scheduled 配置原生计划并回填可核对的计划引用。
