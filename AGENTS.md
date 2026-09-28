# AGENTS.md（仓库根目录）

本文件对**整个仓库**生效；`apps/*` 下还有各自的 `AGENTS.md`，只约束对应应用。规则冲突时，就近的文件优先。

## 项目概览

- pnpm workspaces 单体仓库，成员是 `apps/api`（`@puff/api`，NestJS 12）与 `apps/web`（`@puff/web`，React 19 + Rsbuild 2）。
- **当前是脚手架状态**：除了后端的探活/信息接口（`GET /api`、`GET /api/health`）外没有任何业务功能。不要假设存在数据库、鉴权、状态管理、路由等功能。
- 工程规范在 `docs/rules/`（根目录 / 服务端 / 前端 / 路由），动手前先读相关那篇：改动路由看 `docs/rules/routing.md`，改动目录结构看 `docs/rules/root.md` 等。

## 环境与安装

- Node `>= 22.12`；包管理器由根 `package.json` 的 `packageManager` 固定（当前 `pnpm@11.2.2`），只用 pnpm。
- 依赖只在仓库根执行 `pnpm install`。安装新依赖必须指明包：`pnpm --filter @puff/api add <pkg>` / `pnpm --filter @puff/web add -D <pkg>`。
- 不要手写或手工合并 `pnpm-lock.yaml`；不要新增第二个锁文件。
- 未经用户明确要求，不要改动 `packageManager` / `engines`，也不要升级既有依赖版本。

## 常用命令

在仓库根执行：

| 命令                            | 作用                                          |
| ------------------------------- | --------------------------------------------- |
| `pnpm dev`                      | 并行启动 api(:3000) 与 web(:5173)             |
| `pnpm dev:api` / `pnpm dev:web` | 只启动其中一端                                |
| `pnpm build`                    | 递归构建全部                                  |
| `pnpm typecheck`                | 递归类型检查                                  |
| `pnpm check`                    | Biome 全仓库检查（格式 + lint + import 排序） |
| `pnpm check:fix`                | 同上并写入自动修复                            |
| `pnpm lint` / `pnpm format`     | 只跑 Biome 的 lint / 格式化                   |
| `pnpm format:check`             | 只校验格式                                    |
| `pnpm test`                     | 递归测试                                      |
| `pnpm clean`                    | 清理构建产物                                  |
| `pnpm start:api`                | 以生产模式启动已构建的 API                    |

单包命令用 `pnpm --filter <包名> run <脚本>`，脚本清单见各包 `package.json` 与其 `AGENTS.md`。

## 完成标准（Definition of Done）

任何代码改动在交付前必须实际执行并贴出结果，不要声称未运行的验证：

```bash
pnpm check        # Biome：格式 + lint + import 排序
pnpm typecheck
pnpm test
pnpm build
```

- 只改文档/配置时，至少跑 `pnpm check`（注意 Biome 不处理 Markdown 与 YAML，这类文件需自行保持整洁格式）。
- 改到 HTTP 行为时，额外跑 `pnpm --filter @puff/api test:e2e`。
- 改到前后端联调（端口、代理、请求地址）时，实际起服务验证一次请求链路。

## 硬性规则

- **依赖边界**：禁止跨应用相对导入（如 `apps/api` 里引用 `../../web/src/...`）。要共享就建 `packages/*`，并按 `docs/rules/root.md` 的流程做。
- **ESM 后缀**：`apps/api` 使用 `nodenext`，相对导入必须带 `.js` 后缀（`./app.service.js`）；`apps/web` 不写扩展名。
- **环境变量**：`.env` 一律不入库，只提交 `.env.example`；新增变量必须同步 `apps/*/.env.example`；前端只有 `PUBLIC_` 前缀会进浏览器产物，密钥绝不能用该前缀。
- **生成物**：`dist/`、`node_modules/`、`*.tsbuildinfo`、`coverage/`、`.rspack-profile-*/` 一律不入库。
- **行尾**：全仓库统一 UTF-8 + LF，由根 `.gitattributes`（`* text=auto eol=lf`）保证。新增或修改文件时保持 LF，不要写 CRLF；只有 `*.bat` / `*.cmd` 例外。
- **格式与工具链**：格式化、风格校验与 import 排序统一用 Biome，配置只有根 `biome.json`（`@biomejs/biome` 也只在根 `package.json` 声明）；TS 严格模式来自根 `tsconfig.base.json`，各包通过 `extends` 继承。不要在包里新增 `.prettierrc*` / `.eslintrc*` / `.oxlintrc.json` / 本地 `biome.json`，也不要引入 Prettier、ESLint、oxlint。
- **Biome 与 Nest 的注入类型**：`apps/api` 下 `style/useImportType` 已按目录关闭。**不要把构造注入的类型改成 `import type`**：类型检查会通过，但运行时会报 `Nest can't resolve dependencies of the AppController (?)`。
- **语言**：文档与提交信息用中文，代码标识符与代码注释用英文。
- **不要在 `apps/web` 引入 Vite**，也不要给 `apps/web` 手工添加 `index.html`（HTML 由 Rsbuild 按配置生成）。
- **不要自作主张提交**：除非用户明确要求，不要 `git commit` / `git push` / 建分支；也不要删除用户已有的改动。

## 文档同步要求

- 路由（后端接口或前端页面）有增删改 → 同步更新 `docs/rules/routing.md` 的表格。
- 新增顶层目录、应用或共享包 → 同步更新 `docs/rules/root.md` 与根 `README.md` 的目录结构。
- 引入约定性选择（状态管理、校验库、样式方案、测试框架等）→ 写进 `docs/rules/` 对应文档，而不是只写在回复里。

## 新增应用或共享包时

- 新应用：落在 `apps/<name>`，先读 `docs/rules/root.md` 的「新增一个应用」清单；包名 `@puff/<name>`；必须补齐 `dev` / `build` / `lint` / `typecheck` / `clean` 脚本契约并 `extends` 根 tsconfig。
- 新共享包：落在 `packages/<name>`，注意 Nest 用 `tsc` 构建时不能直接引用源码直出的 TS 包（详见 `docs/rules/root.md`）。
- **两者都必须新建自己的 `AGENTS.md`**，内容至少包含：包定位、脚本清单、目录约定、验证命令、与根规则的差异点。
