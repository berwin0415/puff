# 桌面端规范（apps/desktop）

技术栈：Electron 44 + TypeScript 严格模式 + tsdown + electron-builder + Biome。

## 定位

`@puff/desktop` 是 DeepSeek Harness 的 Windows 薄宿主，不是第二套 AI 客户端界面。

- 客户端固定随包分发 `@deepseek-ai/dsh@0.1.7-rc.2`，用户不需要预装 Node、pnpm 或 dsh。
- 主进程启动 `dsh --profile puff --no-open --port 0`，从 stdout 读取带认证 token 的 loopback URL。
- `BrowserWindow` 加载该 URL，不修改、不代理、不重写官方 Web UI。
- API、Web 与桌面端之间禁止跨应用相对导入；当前三者互不依赖。

## 目录职责

| 路径                | 放什么                                                  | 不放什么                               |
| ------------------- | ------------------------------------------------------- | -------------------------------------- |
| `src/main.ts`       | Electron 主进程、窗口、托盘、单实例、应用级退出流程     | 可独立测试的复杂逻辑（下沉到 `main/`） |
| `src/main/`         | DSH 路径、运行时解析、Host 监督、日志、偏好、shim       | Electron UI 组件或业务界面             |
| `src/static/`       | 本地加载页与错误页                                      | 官方 Web UI 的替代实现                 |
| `test/`             | 真实 dsh 运行时或打包链路的集成测试                     | 单元测试（与被测文件同目录）           |
| `scripts/`          | 构建辅助脚本，例如把静态页复制到 `dist/`                | 业务代码或运行时逻辑                   |

## 运行时与数据

- DSH Home 默认是 `~/.dsh`；若用户显式设置 `DSH_HOME`，客户端原样继承。
- Profile 固定为 `puff`，首次启动通过 `--from-default-profile web` 从官方 Web 模板初始化。
- 不直接使用或管理官方保留的 `desktop` profile。
- 默认 workspace：Windows 为 `%LOCALAPPDATA%\puff\workspace`，macOS 为 `Application Support`，Linux 为 `XDG_DATA_HOME` 或 `~/.local/share`。
- Electron userData 保存偏好与日志；DSH 的会话、凭据、设置仍由共享 Home 管理。

## Host 生命周期

- 正常启动参数：`--profile puff --no-open --port 0`。
- 只接受 `dsh web:` 行中以 `http://127.0.0.1:<port>` 开头的 URL。
- 启动 URL 只保存在内存；日志、错误页和诊断信息必须只保留 `http://127.0.0.1:<port>/`。
- Host 意外退出时按 1s、2s、4s 退避重启，最多三次；主动退出或重启不计入崩溃次数。
- 显式退出前必须提示可能正在运行的任务；确认后先请求进程退出，超时再终止进程树。

## Electron 安全

- `contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`、`webSecurity: true`。
- 不允许导航到 DSH loopback origin 之外；外部 http/https 链接交给系统浏览器。
- 拒绝新窗口；不为第三方页面暴露 preload、IPC 或 Electron 能力。
- `puff-action://` 仅用于本地加载/错误页的重试、重启、打开日志和退出。

## 托盘与偏好

- 关闭主窗口只隐藏到托盘，后台任务继续运行。
- 托盘菜单包含打开、重启 Host、开机启动、打开日志、退出。
- 开机启动默认关闭，偏好写入 Electron userData 的 `preferences.json`。
- 第二实例不创建新窗口，只显示并聚焦已有实例。

## 打包

- electron-builder 输出目录为 `apps/desktop/release/desktop`。
- `clean` 清理 `dist`、`release/desktop`、`release/app` 与 `release/runtime` 暂存目录。
- Windows 目标为 x64 NSIS，按用户安装，不要求管理员权限。
- 产物名：`puff-Setup-<version>-x64.exe`。
- 打包前在系统临时目录用 `pnpm install --prod --config.node-linker=hoisted` 生成自包含的 DSH runtime 与 pnpm；electron-builder 的 `afterPack` 钩子再把它复制到 `resources/dsh-runtime`，`app.asar` 只包含 Electron shell 代码。
- 首版不做代码签名、自动更新和崩溃上报；卸载不删除 `~/.dsh` 与 workspace。
- 打包脚本在未显式设置时回退到 npmmirror 的 Electron 与 electron-builder 资源镜像，避免 GitHub 下载不可达导致打包失败。
- Windows 打包的完整问题清单与排查步骤见 [docs/desktop-packaging-troubleshooting.md](../desktop-packaging-troubleshooting.md)。

## 测试与验收

- 单元测试覆盖 URL 解析与脱敏、Host 参数与环境、profile 初始化、路径、偏好、pnpm shim、重启状态机。
- `test:e2e` 使用临时 `DSH_HOME`，启动真实固定版 dsh，验证认证 URL 返回 HTTP 200 后正常停止。
- 打包后用 `package:win:dir` 产物手工验证：首启、加载官方 UI、第二实例、关闭到托盘、开机启动、插件安装、Host 崩溃恢复、退出提醒。
- 改到 Host 参数、端口、数据目录、IPC/导航策略或打包配置时，必须重新执行 `test:e2e` 与 `package:win:dir`。

## 常见反例

- 把 DSH token URL 或 API Key 写入日志、错误页或崩溃报告。
- 在 `BrowserWindow` 中开启 `nodeIntegration`、关闭 `contextIsolation` 或允许任意页面导航。
- 直接杀掉 Host 进程树而不给用户任务中断提示。
- 修改官方 Web UI 业务代码，或在桌面端重复实现聊天、设置和路由。
- 用应用级 shim 覆盖 `DSH_HOME`，导致客户端绕过用户既有的凭据与会话。
