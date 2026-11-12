# AGENTS.md（仓库根目录）

本文件对**整个仓库**生效；`domains/*/apps/*`、`domains/*/services/*`、`domains/*/libs/*`、`platform/*` 下的项目还有各自的 `AGENTS.md`，只约束对应项目。规则冲突时，就近的文件优先。

## 项目概览

- 按业务域组织的 pnpm workspaces 单体仓库，用 Nx 编排任务、用 pnpm catalog 固定第三方版本。
- 当前有两条业务线：
  - `puff`：`domains/puff/apps/web`（`@puff/web`，React 19 + Rsbuild 2）、`domains/puff/apps/desktop`（`@puff/desktop`，Electron + DeepSeek Harness）、`domains/puff/services/api`（`@puff/api`，NestJS 12）。
  - `llm-lab`：`domains/llm-lab/libs/mini-gpt`（`llm-lab-mini-gpt`，Python），从零手写 mini-GPT 的学习/实验域。
- llm-lab 不参与 pnpm workspace，也没有 dev/build/lint/typecheck/clean 脚本契约；项目以 `type:lib` 参与边界检查，学习/实验属性由业务域本身表达。
- **puff 的 Web 端目前是一个可游玩的浏览器 FPS 垂直切片（NEON VEIL）**，由 React 外壳 + Three.js 程序化引擎组成；除后端探活/信息接口（`GET /api`、`GET /api/health`）外，后端没有自研业务功能。桌面端主要原样加载官方 DSH Web UI，不要假设存在自研聊天、鉴权、状态管理或路由。
- 工程规范在 `docs/rules/`，架构决策在 `docs/adr/`，设计依据是 `docs/monorepo-architecture-design.md`。

## 环境与安装

- Node `>= 22.12`；包管理器由根 `package.json` 的 `packageManager` 固定（当前 `pnpm@11.2.2`），只用 pnpm。
- 工具链版本固定在 `mise.toml`。首次初始化可执行 `tools/bootstrap`；Windows 使用 `powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1`。没有 mise 时两个入口都会回退到 PATH 上的 pnpm。
- 依赖只在仓库根执行 `pnpm install`。安装新依赖必须指明包：`pnpm --filter @puff/api add <pkg>`。
- **第三方版本只能写在 `pnpm-workspace.yaml` 的 catalog 里**，各 `package.json` 一律写 `"catalog:"`。不要在项目里直接写版本号。
- 不要手写或手工合并 `pnpm-lock.yaml`；不要新增第二个锁文件。
- 未经用户明确要求，不要改动 `packageManager` / `engines`，也不要升级既有依赖版本。

## 常用命令

在仓库根执行：

| 命令                                 | 作用                                          |
| ------------------------------------ | --------------------------------------------- |
| `pnpm check`                         | Biome 全仓库检查（格式 + lint + import 排序） |
| `node tools/checks/project-boundaries.mjs` | 检查项目标签与依赖边界                  |

根目录不提供单业务域的构建、测试或启动命令；这些命令直接用 `pnpm --filter` / `pnpm -r` 调用：

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

更细的全仓操作也直接用原生命令：

```bash
pnpm exec biome check --write .                       # 自动修复
pnpm exec nx run-many -t typecheck                    # 只做类型检查
pnpm exec nx run-many -t lint                         # 只跑 lint
pnpm -r run clean                                     # 递归清理
pnpm exec nx graph                                    # 项目图
pnpm exec nx affected -t lint typecheck test build    # 受影响项目
```

单项目命令用 `pnpm --filter <包名> run <脚本>`，脚本清单见各项目 `package.json` 与其 `AGENTS.md`。

## 完成标准（Definition of Done）

任何代码改动在交付前必须实际执行并贴出结果，不要声称未运行的验证：

```bash
pnpm check
pnpm -r run build                          # 改到 puff 域代码时
pnpm -r run test                           # 改到 puff 域代码时
node tools/checks/project-boundaries.mjs   # 改到项目结构或依赖时必跑
```

- 只改文档/配置时，至少跑 `pnpm check`（注意 Biome 不处理 Markdown 与 YAML，这类文件需自行保持整洁格式）。
- llm-lab 是 Python 域，构建/测试按 `domains/llm-lab/README.md` 的方式执行，不受 pnpm 递归命令影响。
- 改到 HTTP 行为时，额外跑 `pnpm --filter @puff/api test:e2e`。
- 改到前后端联调（端口、代理、请求地址）时，实际起服务验证一次请求链路。

## 硬性规则

- **依赖边界**：项目只能按 `docs/monorepo-architecture-design.md` 4.5 的矩阵依赖。域内应用/服务只能依赖同域 lib、平台 lib 与契约；跨域只能通过 `contracts/`。禁止跨项目相对导入（如 API 里引用 `../../web/src/...`）。
- **新共享代码的位置**：只被一个域使用的放 `domains/<域>/libs/`；跨域通用能力放 `platform/libs/`。不要恢复平铺的 `packages/`。
- **项目标签**：每个项目必须有 `project.json`，声明唯一的 `domain:*`、`type:*`、`lang:*` 标签，否则边界检查失败。`type` 取 `app` / `service` / `lib` / `contract` / `e2e` / `tool`。
- **ESM 后缀**：`domains/puff/services/api` 使用 `nodenext`，相对导入必须带 `.js` 后缀（`./app.service.js`）；Web 端不写扩展名。
- **环境变量**：`.env` 一律不入库，只提交 `.env.example`；新增变量必须同步对应项目的 `.env.example`；前端只有 `PUBLIC_` 前缀会进浏览器产物，密钥绝不能用该前缀。
- **生成物**：`dist/`、`node_modules/`、`*.tsbuildinfo`、`coverage/`、`.rspack-profile-*/`、`.nx/` 一律不入库。
- **行尾**：全仓库统一 UTF-8 + LF，由根 `.gitattributes`（`* text=auto eol=lf`）保证。新增或修改文件时保持 LF，不要写 CRLF；只有 `*.bat` / `*.cmd` 例外。
- **格式与工具链**：格式化、风格校验与 import 排序统一用 Biome，配置只有根 `biome.json`（`@biomejs/biome` 也只在根 `package.json` 声明）；TS 严格模式来自根 `tsconfig.base.json`，各包通过 `extends` 继承。不要在包里新增 `.prettierrc*` / `.eslintrc*` / `.oxlintrc.json` / 本地 `biome.json`，也不要引入 Prettier、ESLint、oxlint。
- **Biome 与 Nest 的注入类型**：`domains/*/services/**` 下 `style/useImportType` 已按目录关闭。**不要把构造注入的类型改成 `import type`**：类型检查会通过，但运行时会报 `Nest can't resolve dependencies of the AppController (?)`。
- **语言**：文档、提交信息与代码注释用中文，代码标识符用英文。
- **不要在 Web 项目引入 Vite**，也不要手工添加 `index.html`（HTML 由 Rsbuild 按配置生成）。
- **不要自作主张提交**：除非用户明确要求，不要 `git commit` / `git push` / 建分支；也不要删除用户已有的改动。

## 文档同步要求

- 路由（后端接口或前端页面）有增删改 → 同步更新 `docs/rules/routing.md` 的表格。
- 新增业务域、项目或共享包 → 同步更新 `docs/rules/root.md` 与根 `README.md` 的目录结构，并补 `project.json` 标签。
- 引入约定性选择（状态管理、校验库、样式方案、测试框架等）→ 写进 `docs/rules/` 对应文档，而不是只写在回复里。
- 引入不可从代码推导的架构决策 → 在 `docs/adr/` 新增记录。

## 新增项目时

- 业务应用：`domains/<域>/apps/<name>`；业务服务：`domains/<域>/services/<name>`；域内库：`domains/<域>/libs/<name>`；平台库/服务：`platform/libs/<name>`、`platform/services/<name>`。学习/实验属性由业务域本身表达，不新增 `labs/` 目录。
- 包名 `@puff/<短名>`，补齐 `dev` / `build` / `lint` / `typecheck` / `clean` 脚本契约（可选 `test`），并 `extends` 根 `tsconfig.base.json`。Python 项目不参与 workspace，可只提供自己的运行脚本。
- 新增 `project.json`，声明 `domain:<域>`、`type:app|service|lib`、`lang:<语言>`。
- 新建自己的 `AGENTS.md`，至少包含：项目定位、脚本清单、目录约定、验证命令、与根规则的差异点。
- 跑 `node tools/checks/project-boundaries.mjs` 确认边界规则通过。
