# hsi-desktop 浏览器链路迁移到云端的评估

日期：2026-10-19

参考源码：`/Users/hanbowen01/workspace/baidu/ai/hogee-hsi/packages/hsi-desktop`

这份报告只读分析 hsi-desktop 的实现，不修改该仓库。目标是判断它已经验证的
DeepSeek 浏览器接入方式能否替换成云端 Chromium，从而判断 DeepSeek Harness 的浏览器项
是否仍属于硬阻断。

## 结论

按“能否迁移到云端浏览器”这个硬门槛，DeepSeek Harness 的浏览器项不再判定为绝对阻断。
hsi-desktop 已经证明了一个可复用的连接形状：Agent 侧运行标准 `@playwright/mcp`，通过
`--cdp-endpoint` 和 `PLAYWRIGHT_MCP_CDP_HEADERS` 连接一个带鉴权的 CDP endpoint；桌面端只
负责提供浏览器页面和生命周期。云端可以把 Electron 页面宿主替换为容器内 Chromium、远程
Chromium 或 Browserless 类服务，继续使用同一条 Playwright MCP/CDP 轨道。

这不是零改造迁移。当前实现对 Electron 的浏览器宿主、用户数据分区、下载和本机进程安全
边界有依赖，适配工作量评为**中等**：协议和模型工具接入可以复用，浏览器宿主、会话持久化、
跨容器鉴权和恢复语义需要重写或重新定义。因此当前状态是“浏览器能力未阻断、云端替换待验”，
不能把 `dsh --profile headless` 直接说成已经具备浏览器能力。

## 已确认的桌面端链路

1. `src/main/index.ts` 的 `mountBrowserMcp()` 为每个任务挂载一台 stdio MCP 服务器，运行
   `@playwright/mcp`，参数包含 `--cdp-endpoint <endpoint>`；Bearer token 通过
   `PLAYWRIGHT_MCP_CDP_HEADERS` 环境变量传递，未放入 argv。服务器名和 `ownerTaskId` 绑定
   任务或子代理作用域。hsi control-plane 的 MCP 配置同时支持 `streamable-http`，因此云端
   浏览器 MCP 可以作为带 URL/header 的远程 sidecar 接入，不必强制保留 stdio。
2. `browser-relay.ts` 把页面包装成 Playwright 需要的标准 CDP 浏览器端点，补齐
   `Browser.*`、`Target.*`、多标签页、事件转发、token 鉴权和 target/frame 映射。
   `CdpPage` 与 `RelayTabHooks` 接口表明 relay 的主要协议逻辑不依赖 Electron。
3. `browser-host.ts` 负责一个任务的 host 生命周期、多标签页、下载、窗口打开和关闭顺序，
   并把 host 挂到 relay。`browser-context.ts` 额外把当前 URL、活动 tab、接管状态和登录墙
   等语义状态注入模型上下文。
4. `cdp-browser-host.mjs` 和 `cdp-browser-mcp.mjs` 是端到端判据：前者直接验证现成的
   `@playwright/mcp` 驱动 relay，后者验证产品自己的 MCP → relay → 浏览器窗口链路，包含
   导航、表单输入、多 tab、接管和 runtime 重启相关检查。
   hsi-desktop 的验收记录还保留了 `cdp-mcp-servers --pw-cached 13/13`、
   `cdp-browser-mcp 35/35` 和 `cdp-browser-context 14/14` 的桌面端结果；这些结果证明
   DSH 0.2.0-rc.2 的桌面 MCP 链路已跑通，但不等同于云 Chromium 已通过。
5. `browser-session.ts` 使用 `persist:hogee-browser` 保存登录态；代理配置通过
   `session.setProxy` 设置。runtime 重启时会先收掉任务浏览器，任务重新打开时再创建 host
   并挂回 MCP 服务器。

## 可以直接复用的部分

| 部分 | 云端复用判断 |
| --- | --- |
| `@playwright/mcp` 的工具协议和 stdio 启动方式 | 可复用。MCP 进程可放入 Execution 容器，通过环境变量拿到 endpoint 和短期 token。 |
| CDP endpoint + Bearer 鉴权约定 | 可复用。云端需要把回环地址改成同一网络内的服务地址，并增加租户/任务作用域校验。 |
| 每任务/每子代理一个 browser scope、服务器名和工具 owner | 可复用。可映射到 Runner 的 `tenant_scope`、`task_id` 和 `run_id`。 |
| 多 tab、`window.open`、活动 tab、当前页面上下文 | 语义可复用，底层改为 Playwright BrowserContext/Page 事件。 |
| 浏览器上下文文本、接管、登录等待、MCP 工具审批 | 可复用，但反向请求和权限状态要接到云端控制面。 |
| 任务工作区、下载产物、browser output 清理 | 可复用，路径改为 Execution 容器中的 run 目录并交给 artifact store。 |

## Electron 专属部分和替换点

| 当前实现 | Electron 依赖 | 云端替换 |
| --- | --- | --- |
| `browser-adapter.ts` | `<webview>`、`did-attach-webview`、`webContents.debugger`、窗口销毁事件 | `BrowserHost` 直接持有 Playwright `BrowserContext/Page` 或远程 CDP page；不再等待 renderer 挂载 guest。 |
| `browser-session.ts` | `session.fromPartition()`、`session.setProxy()`、Electron cookie/cache API | Chromium persistent context 或独立 profile volume；网络策略用容器代理/NetworkPolicy 实现。 |
| `browser-host.ts` 下载与窗口打开 | `will-download`、`setWindowOpenHandler`、`webContents.executeJavaScript` | Playwright `page.on('download')`、context `page` 事件、locator/DOM 查询。 |
| `browser-relay.ts` 的进程校验 | 本机回环、peer-PID、pin 文件、macOS SBPL | 服务间 mTLS/短期 token、任务级 ACL、网络命名空间或 Unix socket；不能照搬 peer-PID。 |
| renderer 面板和可见性 | React `<webview>`、guestId 与 UI tab 对齐 | 云端只保存语义 tab 状态；如果需要用户接管，另提供远程浏览器画面/控制通道。 |

relay 的抽象接口降低了替换成本，但云端不应机械保留 Electron relay。如果 Chromium 服务
已经暴露受保护的标准 CDP endpoint，可以直接让 Playwright MCP 连接；只有在需要隐藏多租户
路由、统一鉴权或协议修补时才保留 relay/gateway。

## 对暂停、恢复和状态的影响

桌面端的持久 partition 只保证 cookie/cache 等登录态落盘，并不等价于“杀掉进程后恢复完整
浏览器任务”。Playwright MCP 的 element ref、当前 tab 和部分语义状态在 MCP 进程内存中；
重启后必须重新 snapshot，并重建 tab/context 映射。云端验证必须分别记录：

- Chromium profile 或 `storageState` 是否能恢复 cookie、localStorage 和登录态；
- tab、URL、下载和接管状态是否能重建；
- MCP 进程重启后旧 element ref 是否被明确作废；
- Agent checkpoint 与浏览器恢复之间是否存在安全点，以及外部副作用是否带幂等键。

## 最小云端验证

在把浏览器项标为“通过”前，补一条不依赖 Electron 的验证轨道：

1. 用 Docker 启动 Chromium 和 Playwright/CDP endpoint，为每个 `task_id` 创建隔离的
   BrowserContext，并把 endpoint、短期 token 和任务作用域交给 `@playwright/mcp`。
2. 用与 T2 相同的本地站点验证导航、表单填写、截图、第二个 tab、`window.open` 和下载；
   判据取页面 DOM、文件和 context 状态，不采用模型复述。
3. 杀掉并重启 MCP 进程，确认工具表能重建，旧 ref 会被拒绝或明确要求重新 snapshot。
4. 保存 profile/storageState 后重建 Chromium，确认登录态和语义上下文按约定恢复；再杀掉
   Runner/Execution 容器，验证 checkpoint 恢复不会重复 Fixture 副作用。
5. 把跨容器鉴权、租户隔离和浏览器 endpoint 暴露面写入运行 manifest；本地回环和 peer-PID
   只能作为桌面端证据，不能作为云端安全结论。

## 对 DeepSeek Harness 结论的修正

- `dsh --profile headless` 仍然是无浏览器的一次性入口；原有 T2 结果仍只能记为该入口的
  原生浏览器阻断。
- hsi-desktop 证明了 DSH 生态可以通过标准 Playwright MCP/CDP 轨道接入浏览器，且核心
  浏览器协议层具备替换宿主的边界。因此在候选矩阵中，DeepSeek 的浏览器能力改记为
  **“未阻断（可迁移候选，待云端最小验证）”**。
- DeepSeek 仍未因此成为主路线：headless/SDK 的暂停恢复、checkpoint、审批、实时输入和
  长生命周期任务尚未形成统一证据；这些是独立于浏览器的生命周期缺口。

## 证据位置

- hsi-desktop relay：`packages/hsi-desktop/src/main/core/browser-relay.ts`
- Electron 适配层：`packages/hsi-desktop/src/main/core/browser-adapter.ts`
- host/session/context：`packages/hsi-desktop/src/main/core/browser-host.ts`、
  `browser-session.ts`、`browser-context.ts`
- MCP 挂载：`packages/hsi-desktop/src/main/index.ts` 的 `mountBrowserMcp()`
- 端到端判据：`packages/hsi-desktop/scripts/cdp-browser-host.mjs`、
  `cdp-browser-mcp.mjs`
- DSH headless 限制：`/Users/hanbowen01/repo/deepseek-harness/apps/cli/reference/README.md:33`、
  `packages/bundle/headless/cordis.patch.yml:1-4`

## 后续验证状态（2026-10-19 更新）

无 Electron 的 Chromium + `@playwright/mcp` 轨道已独立验证通过：导航、表单、
accessibility snapshot、双 tab + `window.open`、截图和 Chromium 重启后的
localStorage 恢复均有证据（`execution/browser/mcp_cdp_smoke.py`）。这证明
“桌面宿主替换为容器内 Chromium”的浏览器/MCP 层可行，但尚未把 DSH 的
BrowserHost/HostSession、任务 scope、审批和 artifact 路径接到该 endpoint，
因此 DeepSeek 的浏览器项仍是“可迁移、DSH 接入待验证”。
