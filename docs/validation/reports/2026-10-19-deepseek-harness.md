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
- 统一 T1 工具任务通过：run_id `b14909c9-5758-49c5-b8b5-c0766806599e`。DeepSeek
  Harness 通过 bash/curl 访问 Fixture、得到 `sum=60`，并以固定幂等键创建一条
  side effect（HTTP `201`）；Fixture 状态和请求记录作为脱敏证据留存。
- 统一 T2 的 Execution Playwright 基线通过：run_id
  `37cd25bc-fe6a-4740-80de-e7c498a7d97f` 的 `artifacts/browser/t2-browser.png`
  记录了页面访问、表单提交和 `accepted:harness-lab`。

## 当前阻断与缺口

- 本次调用通过仓库 `.env` 的通用模型配置在子进程内映射为 DeepSeek Harness 所需的 `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`；密钥值未写入仓库或 manifest。
- T1 已验证候选工具调用和幂等副作用；T2 的统一 Execution Playwright
  基线已验证，但候选 headless 轨道不具备浏览器能力，不能把基线结果归为
  DeepSeek Harness 的原生浏览器成功。
- T2 候选轨道明确阻断：headless profile 的官方说明声明不挂载 browser
  Connection、HTTP server、Web runtime 或 browser client。运行脚本仍发起了一次
  真实模型调用，候选在 30 秒内尝试了非浏览器 fallback 后超时；没有把直接 HTTP
  请求计作浏览器成功。该阻断和候选输出见
  `~/harness-lab-data/runs/2026-10-19/deepseek_harness/t2-browser/37cd25bc-fe6a-4740-80de-e7c498a7d97f/`。
- `headless` 是单次任务入口；本适配器没有原生暂停、恢复、checkpoint、审批、实时输入和取消能力。终止 subprocess 不计作任务恢复。
- subprocess 退出后 stdout/stderr 的真实交错顺序不可恢复，离线事件只按稳定顺序记录流来源。

## 下一步

运行入口为 `scripts/run_deepseek_p0.py --task all`，每次运行把脱敏
`manifest.json`、`events.jsonl`、`result.json`、Fixture 状态和浏览器截图写入
`HARNESS_LAB_ARTIFACT_ROOT`。下一步应优先评估 DeepSeek Harness SDK/ACP 是否能承载
持久会话和浏览器接入，再决定是否通过外围 checkpoint 层补齐暂停恢复。
