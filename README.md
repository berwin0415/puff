# puff

pnpm workspaces 单体仓库：**NestJS** 服务端 + **React (Rsbuild / Rspack)** 前端。

```
.
├─ apps/
│  ├─ api/            NestJS 12 服务端（ESM + vitest + Biome）
│  └─ web/            React 19 + Rsbuild 2（Rspack）前端
├─ packages/          预留：共享库／类型包放这里
├─ docs/
│  └─ rules/          工程规范：根目录 / 服务端 / 前端 / 路由
├─ biome.json         唯一的格式化 + lint 配置（Biome）
├─ pnpm-workspace.yaml
├─ tsconfig.base.json 各包共用的 TS 严格模式基线
└─ package.json       根脚本 + 统一工具链（Biome）
```

## 环境要求

- Node `>= 22.12`（Rsbuild 2 与 Vite 8 的同款要求，当前环境 v24）
- pnpm `>= 10`（`packageManager` 已固定为 `pnpm@11.2.2`，执行 `corepack enable` 可自动切换版本）

## 快速开始

```bash
pnpm install

# 可选：自定义端口
Copy-Item apps/api/.env.example apps/api/.env

# 同时启动服务端(:3000) 与前端(:5173)
pnpm dev
```

打开 http://localhost:5173 ，页面会请求 `/api/health` 并显示后端状态，用来确认前后端已经打通。

> 目前是脚手架状态：除了后端的探活/信息接口外没有任何业务功能。

只启动其中一个：

```bash
pnpm dev:api
pnpm dev:web
```

## 根目录脚本

| 命令                | 作用                                                  |
| ------------------- | ----------------------------------------------------- |
| `pnpm dev`          | 并行启动 `apps/*` 的 dev 任务                         |
| `pnpm build`        | 递归构建（按依赖拓扑顺序）                            |
| `pnpm typecheck`    | 递归类型检查                                          |
| `pnpm check`        | Biome 全仓库检查（格式 + lint + import 排序，CI 用）  |
| `pnpm check:fix`    | 同上，并直接写入修复                                  |
| `pnpm lint`         | 只跑 Biome lint                                       |
| `pnpm format`       | 只跑 Biome 格式化（写入）                             |
| `pnpm format:check` | 只校验格式                                            |
| `pnpm test`         | 递归测试（api 用 vitest；没有 test 脚本的包自动跳过） |
| `pnpm clean`        | 递归清理 `dist` 等构建产物                            |
| `pnpm start:api`    | 以生产模式启动已构建的 API                            |

包内脚本用 `--filter` 调用，例如：

```bash
pnpm --filter @puff/api test          # 单元测试 src/**/*.spec.ts
pnpm --filter @puff/api test:e2e      # e2e 测试 test/**/*.e2e-spec.ts
pnpm --filter @puff/web preview       # 预览前端构建产物（rsbuild preview，:4173）
```

## 端口与请求代理

- API 默认监听 `3000`，可用 `apps/api/.env` 里的 `PORT` 覆盖。
- 前端 dev server 固定 `5173`，把 `/api` 代理到 `http://localhost:3000`（`API_PROXY_TARGET` 可覆盖），浏览器始终只访问同源地址。
- 前端请求地址由 `apps/web/.env` 里的 `PUBLIC_API_BASE_URL` 决定，默认 `/api`。

## 约定

- 目录、命名、分层、注释与提交自检等工程规范见 [docs/rules](./docs/rules/README.md)：[根目录](./docs/rules/root.md)、[服务端](./docs/rules/api.md)、[前端](./docs/rules/web.md)、[路由](./docs/rules/routing.md)。
- 面向 AI 编码助手的仓库级指令见根 [AGENTS.md](./AGENTS.md)，各应用另有 [apps/api/AGENTS.md](./apps/api/AGENTS.md) 与 [apps/web/AGENTS.md](./apps/web/AGENTS.md)。
- 两个包都是 ESM（`"type": "module"`），API 因为用 `nodenext` 解析，相对导入要写 `.js` 后缀。
- 类型严格模式来自根目录 `tsconfig.base.json`，各包 `tsconfig` 通过 `extends` 继承。
- 代码格式化、风格校验、import 排序统一走 Biome：配置只有根目录 `biome.json`，各包不再单独维护格式化/lint 配置。Biome 不处理 Markdown 与 YAML，文档与 `pnpm-workspace.yaml` 需手写保持整洁。
- API 环境变量由 Node 内置的 `process.loadEnvFile()` 读取 `apps/api/.env`，没有额外依赖。
- 前端环境变量由 Rsbuild 自动读取 `apps/web/.env*`：只有 `PUBLIC_` 前缀的变量会注入浏览器代码（`import.meta.env.PUBLIC_*`），`src/env.d.ts` 里开了 `strictImportMetaEnv`，没声明的键读不到。
- 前端入口约定为 `src/index.tsx`，HTML 由 Rsbuild 按 `html.title` / `html.favicon` 生成，仓库里不再放 `index.html`。
- 共享代码建议放 `packages/`：源码直出的纯 TS 包对 Nest 的 `tsc` 构建不友好，推荐先构建出 `dist` 再被 `apps/*` 引用（或在 API 里配 path alias 并同步处理运行时解析）。

## 相对模板的调整

- 包名改为 `@puff/api`、`@puff/web`，各自补齐 `typecheck` / `clean` 脚本，公共配置上提到仓库根目录。
- 前端按 create-rsbuild 的 `react-ts` 模板重建：Rsbuild 2 + `@rsbuild/plugin-react` 替换掉 Vite 与 `@vitejs/plugin-react`。
- 格式化与风格校验从 Prettier + oxlint 统一换成 Biome（根目录 `biome.json` + `@biomejs/biome` 单一依赖）：移除 `.prettierrc.json`、`.prettierignore` 与两份 `.oxlintrc.json`，`pnpm lint` / `pnpm format` / `pnpm check` 全部由 Biome 提供；其中 `style/useImportType` 在 `apps/api` 下关闭（Nest 的 `emitDecoratorMetadata` 依赖值导入，改成 `import type` 会让依赖注入在运行期报 `Nest can't resolve dependencies`）。
- 修掉 Nest 12 脚手架 e2e 模板里的 `import { App } from 'supertest/types'`：在 `nodenext` 解析下该子路径无法解析（supertest 本身不带 types），会让 `tsc --noEmit` 直接报错。
- API 的 vitest 配置改用 Vite 8 原生的 `resolve.tsconfigPaths`，去掉 `vite-tsconfig-paths` 插件（同时消除了它对 TypeScript < 7 的 peer 冲突）。
- 前端替换掉模板演示页，改成一个调用 `/api/health` 的最小页面。
