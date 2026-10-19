# DeepSeek Harness adapter

这个适配器把外部的 DeepSeek Harness checkout 映射到本仓库的候选适配器协议，当前只接入它的 `headless` 一次性入口。

## 能力

- `preflight()` 检查源码目录、Git checkout、当前 `HEAD` 与配置中的 pinned commit，以及工作区是否干净。适配器不修改外部 checkout。
- `build_headless_command(prompt)` 生成 `dsh --profile headless <prompt>` 命令。也支持传入本地可执行文件或 `node apps/cli/lib/bin.js` 形式的命令前缀。
- `run_once()` 可选地启动一次 subprocess，采集 stdout、stderr、退出码和超时信息，并映射成统一事件。`submit()` 默认只分配 run id 和排队事件；设置 `execute_on_submit=True` 才会启动进程。
- `stream_events()` 支持按 `event_seq` 游标读取当前进程的事件；事件同时保留 `candidate_event_id`、原始输出流和文本。

模型调用仍由 DeepSeek Harness 自己负责。API key、base URL 和模型配置只能通过进程环境变量或外部 secret 注入，适配器不会把凭证放进命令行、配置文件或事件 payload。

## 当前限制

- `headless` 是单次任务入口，没有可供本适配器调用的原生暂停、恢复、审批、实时输入和 checkpoint API；相应协议方法会显式抛出 `NotImplementedError`。这项缺口需要在 M3 评估，不能把终止 subprocess 当成可恢复暂停。
- subprocess 结束后 stdout/stderr 已经分离，离线事件按 stdout 后 stderr 的稳定顺序重建，不能代表两个流的真实交错顺序。需要实时顺序时应接入候选事件流或 SDK。
- 适配器不 vendoring DeepSeek Harness 源码，也不替外部仓库安装依赖；运行前必须先完成候选仓库自身的构建/依赖准备。
- 真实模型、工具和浏览器任务需要远程模型 API，离线单测只验证命令、预检、进程采集和事件映射，不验证模型效果。

## 最小用法

```python
from adapters.deepseek_harness import DeepSeekHarnessAdapter

adapter = DeepSeekHarnessAdapter(
    source_path="../deepseek-harness",
    expected_commit="dd6322d604e00eec1ba5e0c8541159906a21094a",
    # 本地 checkout 的可复现入口；也可以使用：("node", "apps/cli/lib/bin.js")
    command=("pnpm", "dsh"),
)
adapter.preflight()
result = adapter.run_once({"prompt": "inspect the local fixture"}, timeout=120)
print(result.ok, result.stdout)
```

凭证示例（值不要写入仓库）：

```bash
export DEEPSEEK_API_KEY='...'
export DEEPSEEK_BASE_URL='https://...'
```
