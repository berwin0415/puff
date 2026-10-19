# DeepSeek Harness：首次本地适配记录

日期：2026-10-19

## 已验证

- 外部 checkout：`../deepseek-harness`
- pinned commit：`dd6322d604e00eec1ba5e0c8541159906a21094a`
- 工作区：干净
- Node：`24.22.0`
- pnpm：`11.7.0`
- 入口：`pnpm dsh`
- `pnpm dsh --version`、`pnpm dsh --help` 和适配器 `--help` subprocess 冒烟均通过。
- 适配器能够生成候选 run ID、采集 stdout/stderr、保留候选事件 ID，并按 `event_seq` 提供断点读取。

## 当前阻断与缺口

- 当前 shell 没有远程模型 API 凭证（只确认变量未设置，不记录任何凭证值），因此尚未运行模型、工具或浏览器任务。
- `headless` 是单次任务入口；本适配器没有原生暂停、恢复、checkpoint、审批、实时输入和取消能力。终止 subprocess 不计作任务恢复。
- subprocess 退出后 stdout/stderr 的真实交错顺序不可恢复，离线事件只按稳定顺序记录流来源。

## 下一步

在受控环境通过环境变量注入远程模型 API 配置后，使用统一 T1/T2 任务运行一次 DeepSeek Harness，写入外部 artifact 根目录的 manifest、事件和结果，再决定是否通过 SDK/ACP 或外围 checkpoint 层补齐暂停恢复。
