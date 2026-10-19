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
- 真实远程模型 API 冒烟通过：run_id 为 `deepseek-api-smoke-c4ec19d5-b783-4532-8f5c-7a6d133ea7e4`，提示词要求严格返回 `API_OK`，实际返回 `API_OK`，退出码为 0，事件数为 6，无超时或错误事件。
- 脱敏 manifest 和 events 已写入仓库外：`/Users/hanbowen01/harness-lab-data/runs/2026-10-19/deepseek_harness/api-smoke/deepseek-api-smoke-c4ec19d5-b783-4532-8f5c-7a6d133ea7e4`。

## 当前阻断与缺口

- 本次调用通过仓库 `.env` 的通用模型配置在子进程内映射为 DeepSeek Harness 所需的 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；密钥值未写入仓库或 manifest。
- 本次只验证了远程模型调用链路，尚未验证 DeepSeek Harness 的工具和浏览器任务。
- `headless` 是单次任务入口；本适配器没有原生暂停、恢复、checkpoint、审批、实时输入和取消能力。终止 subprocess 不计作任务恢复。
- subprocess 退出后 stdout/stderr 的真实交错顺序不可恢复，离线事件只按稳定顺序记录流来源。

## 下一步

下一步使用统一 T1/T2 任务运行 DeepSeek Harness，写入同一外部 artifact 根目录的 manifest、事件和结果，再决定是否通过 SDK/ACP 或外围 checkpoint 层补齐暂停恢复。
