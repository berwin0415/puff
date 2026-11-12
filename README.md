# puff

按业务域组织的 Monorepo：**puff** 业务域（Web、桌面客户端与 NestJS API）与 **llm-lab** 学习域（Python mini-GPT），使用 pnpm workspaces 管理依赖、Nx 编排任务、Biome 统一格式与 lint。

```
.
├─ domains/
│  ├─ puff/
│  │  ├─ apps/
│  │  │  ├─ web/            @puff/web       React 19 + Rsbuild 2
│  │  │  └─ desktop/        @puff/desktop   Electron 44 + DeepSeek Harness
│  │  ├─ services/
│  │  │  └─ api/            @puff/api       NestJS 12（ESM + Vitest）
│  │  ├─ libs/              域内共享库（当前为空）
│  │  ├─ e2e/               域内跨应用 e2e（当前为空）
│  │  └─ README.md
│  └─ llm-lab/
│     ├─ libs/
│     │  └─ mini-gpt/       llm-lab-mini-gpt  从零手写 mini-GPT（Python）
│     └─ README.md
├─ platform/                跨域共享库与服务（当前为空）
├─ contracts/               跨服务接口契约（当前为空）
├─ e2e/                     跨域 e2e（当前为空）
├─ tools/                   边界检查与初始化脚本
├─ infra/                   基础镜像 / Helm / Terraform（当前为空）
├─ docs/
│  ├─ rules/                工程规范
│  ├─ adr/                  架构决策记录
│  └─ monorepo-architecture-design.md
├─ .github/                 CI 与 CODEOWNERS
├─ nx.json                  Nx 编排配置
├─ mise.toml                固定 Node / pnpm 版本
├─ biome.json               唯一的格式化 + lint 配置
├─ pnpm-workspace.yaml      workspace 成员 + 第三方版本 catalog
├─ tsconfig.base.json       各包共用的 TS 严格模式基线
└─ package.json             根脚本 + 统一工具链
```

## 环境要求

- Node `>= 22.12`（`mise.toml` 固定为 24.22.0）
- pnpm `>= 10`（`packageManager` 已固定为 `pnpm@11.2.2`，执行 `corepack enable` 可自动切换版本）
- 可选：mise（执行 `tools/bootstrap` 自动安装固定工具链；Windows 使用 `powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1`）

## 快速开始

```bash
pnpm install

# 可选：自定义端口（macOS / Linux）
cp domains/puff/services/api/.env.example domains/puff/services/api/.env

# 可选：自定义端口（Windows PowerShell）
Copy-Item domains/puff/services/api/.env.example domains/puff/services/api/.env

# 同时启动 puff 域的 API(:3000)、Web(:5173) 与桌面客户端
pnpm -r --parallel run dev
```

打开 http://localhost:5173 进入 **NEON VEIL / 霓虹帷幕**：一个可直接游玩的浏览器第一人称射击垂直切片，背景是雨夜第七区反应堆天台。Web 端不再依赖 API 才能游玩；API 仍保留探活/信息接口。

> 射击、移动、波次与结算都在浏览器内完成；后端接口只用于工程链路验证。

### 游戏操作

- `WASD` 移动、`Shift` 疾跑、`Space` 跳跃、`Ctrl` / `C` 蹲伏
- 鼠标左键射击、右键瞄准、`R` 换弹、`Q` 投掷脉冲手雷
- `Esc` 退出指针锁并暂停；点击画面可重新锁定鼠标

画面预设可在开始界面切换「电影级 / 性能」。所有场景、武器、材质与音效均为程序化生成，不依赖外部美术资源。

### 视觉预览

![NEON VEIL 开始界面](docs/screenshots/neon-veil-start.jpg)

![NEON VEIL 战斗画面](docs/screenshots/neon-veil-combat.jpg)

只启动 puff 域的其中一个项目：

```bash
pnpm --filter @puff/api run dev
pnpm --filter @puff/web run dev
pnpm --filter @puff/desktop run dev
```

根目录只保留与业务域无关的全仓命令；业务域专用命令用 `pnpm --filter` 或 `pnpm -r` 直接调用。

## 根目录脚本

| 命令                                 | 作用                                                  |
| ------------------------------------ | ----------------------------------------------------- |
| `pnpm check`                         | Biome 全仓库检查（格式 + lint + import 排序）          |

根目录不提供业务域构建、测试或启动命令；各域用原生命令自行执行：

```bash
pnpm -r --parallel run dev                            # 启动 puff 域全部项目
pnpm -r run build                                     # 构建 puff 域全部项目
pnpm -r run test                                      # 测试 puff 域全部项目
pnpm --filter @puff/api run dev                       # 只启动 API
pnpm --filter @puff/web run dev                       # 只启动 Web
pnpm --filter @puff/desktop run dev                   # 只启动桌面端
pnpm --filter @puff/api run start:prod                # 生产模式启动 API
pnpm --filter @puff/desktop run package:win           # 构建 Windows x64 安装包
```

只保留最少的全仓入口。需要更细的操作时直接用原生命令：

```bash
pnpm exec biome check --write .                       # 自动修复格式/lint
pnpm exec nx run-many -t typecheck                    # 只做类型检查
pnpm -r run clean                                     # 递归清理构建产物
pnpm exec nx graph                                    # 打开项目图
pnpm exec nx affected -t lint typecheck test build    # 只跑受影响项目
```

包内脚本用 `--filter` 调用，例如：

```bash
pnpm --filter @puff/api test          # 单元测试 src/**/*.spec.ts
pnpm --filter @puff/api test:e2e      # e2e 测试 test/**/*.e2e-spec.ts
pnpm --filter @puff/web preview       # 预览前端构建产物（rsbuild preview，:4173）
pnpm --filter @puff/desktop test:e2e  # 真实验证固定版 dsh Host 与认证 URL
pnpm --filter @puff/desktop package:win:dir
```

## 依赖与版本

- 全仓库只有一份 `pnpm-lock.yaml`。
- 第三方依赖版本集中写在 `pnpm-workspace.yaml` 的 `catalog` 中，各 `package.json` 只写 `"catalog:"`。新增依赖时同时更新 catalog，不要在项目里写死版本。
- 依赖只从仓库根执行 `pnpm install`；单项目加包用 `pnpm --filter <包名> add <pkg>`。
- 项目结构与依赖方向由 `node tools/checks/project-boundaries.mjs` 检查，CI 每个 PR 都执行。

## 端口与请求代理

- API 默认监听 `3000`，可用 `domains/puff/services/api/.env` 里的 `PORT` 覆盖。
- 前端 dev server 固定 `5173`，把 `/api` 代理到 `http://localhost:3000`（`API_PROXY_TARGET` 可覆盖），浏览器始终只访问同源地址。
- 前端请求地址由 `domains/puff/apps/web/.env` 里的 `PUBLIC_API_BASE_URL` 决定，默认 `/api`。

## 桌面客户端

- `domains/puff/apps/desktop` 固定随包分发 `@deepseek-ai/dsh@0.1.7-rc.2` 与 `pnpm@11.7.0`，不需要全局 Node/pnpm/dsh。
- 首版面向 Windows x64，通过 `pnpm --filter @puff/desktop run package:win` 生成按用户安装的 NSIS 包。
- 客户端启动 `dsh --profile puff --no-open --port 0`，从 stdout 读取带一次性 token 的 loopback URL 并交给 `BrowserWindow`。
- DSH Home 默认沿用 `~/.dsh`，使用独立 `puff` profile；默认 workspace 为 `%LOCALAPPDATA%\puff\workspace`。
- 托盘常驻，关闭窗口只隐藏；开机启动默认关闭，Host 崩溃按 1s/2s/4s 最多重启三次。
- 首版不做代码签名、自动更新与崩溃上报；详情见 [docs/rules/desktop.md](./docs/rules/desktop.md)。
- Windows 打包问题与排障步骤见 [docs/desktop-packaging-troubleshooting.md](./docs/desktop-packaging-troubleshooting.md)。

## CI

`.github/workflows/ci.yml` 使用 GitHub Actions 免费额度：

- PR 与合并队列只恢复缓存，`main` 才保存 pnpm store 与 `.nx/cache`。
- 每个 PR 都跑项目边界检查，然后执行 `nx affected -t lint typecheck test build`。
- 每晚在 `main` 上跳过缓存全量跑一次，用于发现输入声明不完整导致的缓存错误。

## 约定

- 目录、命名、分层与依赖边界见 [docs/rules](./docs/rules/README.md)：[根目录](./docs/rules/root.md)、[服务端](./docs/rules/api.md)、[前端](./docs/rules/web.md)、[桌面端](./docs/rules/desktop.md)、[路由](./docs/rules/routing.md)。
- 架构决策记录见 [docs/adr](./docs/adr/README.md)。
- 面向 AI 编码助手的仓库级指令见根 [AGENTS.md](./AGENTS.md)，各项目另有 `AGENTS.md`。
- puff 的 TypeScript 项目都是 ESM（`"type": "module"`），API 因为用 `nodenext` 解析，相对导入要写 `.js` 后缀。
- 类型严格模式来自根目录 `tsconfig.base.json`，各包 `tsconfig` 通过 `extends` 继承（新增项目在 `domains/**` 下需要写 `../../../../tsconfig.base.json`）。
- 代码格式化、风格校验、import 排序统一走 Biome：配置只有根目录 `biome.json`。Biome 不处理 Markdown 与 YAML，文档与 `pnpm-workspace.yaml` 需手写保持整洁。
- API 环境变量由 Node 内置的 `process.loadEnvFile()` 读取 `domains/puff/services/api/.env`，没有额外依赖。
- 前端环境变量由 Rsbuild 自动读取 `domains/puff/apps/web/.env*`：只有 `PUBLIC_` 前缀的变量会注入浏览器代码（`import.meta.env.PUBLIC_*`），`src/env.d.ts` 里开了 `strictImportMetaEnv`，没声明的键读不到。
- 前端入口约定为 `src/index.tsx`，HTML 由 Rsbuild 按 `html.title` / `html.favicon` 生成，仓库里不再放 `index.html`。
- 共享代码：只被 puff 域使用的放 `domains/puff/libs/`；跨域通用能力放 `platform/libs/`。源码直出的纯 TS 包对 Nest 的 `tsc` 构建不友好，推荐先构建出 `dist` 再被引用。
- `domains/llm-lab` 是 Python 学习域（`type:lib` 项目），不参与 pnpm workspace，也没有 Nx target；学习/实验属性由域本身表达，运行方式见 [domains/llm-lab/README.md](./domains/llm-lab/README.md)。
