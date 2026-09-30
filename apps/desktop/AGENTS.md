# AGENTS.md（apps/desktop）

约束 `@puff/desktop` 这一个包；与仓库根 `AGENTS.md` 叠加，更具体的规则以本文件为准。

## 包定位与现状

- Electron 44 桌面薄壳，负责启动、托管并展示 DeepSeek Harness 官方 Web UI。
- 运行时固定为 `@deepseek-ai/dsh@0.1.7-rc.2`，插件管理使用随包分发的 `pnpm@11.7.0`。
- 不 fork 官方 Web UI，不新增业务页面、路由、请求层或状态管理。
- 详细目录、数据位置与安全边界见 `docs/rules/desktop.md`。
- 打包踩坑与升级前检查见 `docs/desktop-packaging-troubleshooting.md`。

## 命令

在仓库根执行：

```bash
pnpm dev:desktop                              # 构建并启动 Electron
pnpm --filter @puff/desktop build             # 类型检查 + tsdown + 静态页拷贝
pnpm --filter @puff/desktop typecheck         # tsc --noEmit
pnpm --filter @puff/desktop check             # Biome 检查
pnpm --filter @puff/desktop test              # 单元测试
pnpm --filter @puff/desktop test:e2e          # 真实 DSH Host 集成测试
pnpm --filter @puff/desktop package:win:dir   # 生成未打包目录，供本地冒烟
pnpm --filter @puff/desktop package:win       # 生成 Windows x64 NSIS 安装包
pnpm --filter @puff/desktop run clean         # 清理 dist、release/desktop 与 runtime 暂存
```

## 目录约定

```
src/
├─ main.ts                  Electron 主进程入口与窗口/托盘生命周期
├─ main/                    可单测的 Host、路径、日志、偏好与安全模块
└─ static/                  仅用于本地加载页与错误页，不承载 DSH 业务界面
test/                       真实 DSH 运行时集成测试
scripts/                    构建辅助脚本
```

## 代码规则

- 使用 ESM + NodeNext；相对导入必须带 `.js` 后缀。
- 禁止跨应用相对导入；共享逻辑先留在本应用，出现第二个消费方后再按根规范抽 `packages/*`。
- 渲染进程只允许加载 `127.0.0.1` 的 DSH 认证 URL 或本包 `src/static` 下的 `file:` 页面。
- 不得把带 token 的 DSH 启动 URL、API Key、完整子进程环境写入日志或错误页。
- 不新增第二套 lint/format 配置；格式与 import 排序统一使用根 `biome.json`。
- Electron 版本与 `node-addon-require-builtin` 指纹强绑定；升级 Electron 前必须实际跑通 `test:e2e` 和 `package:win:dir`。
- 打包 runtime 必须在系统临时目录用 `pnpm install --prod --config.node-linker=hoisted` 生成，并由 `afterPack` 复制到 `resources/dsh-runtime`；不能依赖 electron-builder 直接扫描 workspace 的 pnpm 隔离依赖。

## 数据与边界

- DSH Home 默认使用用户 `~/.dsh`；显式继承 `DSH_HOME`，不得静默覆盖。
- 使用独立 `puff` profile，不直接管理官方保留的 `desktop` profile。
- 默认 workspace 为 `%LOCALAPPDATA%\puff\workspace`，应用数据位于 Electron userData。
- 首版只交付 Windows x64；macOS/Linux 适配必须单独评审。
- `package:win*` 默认使用 npmmirror 的 Electron / electron-builder 资源镜像回退；已设置对应环境变量时以环境变量为准。

## 完成标准

```bash
pnpm --filter @puff/desktop typecheck
pnpm --filter @puff/desktop check
pnpm --filter @puff/desktop test
pnpm --filter @puff/desktop test:e2e
pnpm --filter @puff/desktop build
pnpm --filter @puff/desktop package:win:dir
```
