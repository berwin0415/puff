# 根目录规范

仓库按**业务域**组织：`domains/<域>/{apps,services,libs,e2e}` 放业务项目，`platform/` 放跨域共享能力，`contracts/` 放接口契约。根目录只承担 workspace 编排、跨包治理与仓库级工具。

## 目录结构

```
.
├─ domains/                      ✅ 业务域，一个业务线一个目录
│  ├─ puff/
│  │  ├─ apps/                   ✅ 客户端应用（可独立构建/分发）
│  │  │  ├─ web/                 ✅ @puff/web
│  │  │  └─ desktop/             ✅ @puff/desktop
│  │  ├─ services/               ✅ 后端服务（可独立部署）
│  │  │  └─ api/                 ✅ @puff/api
│  │  ├─ libs/                   ➕ 域内共享库
│  │  ├─ e2e/                    ➕ 域内跨应用 e2e
│  │  └─ README.md               ✅ 负责团队、服务清单
│  └─ llm-lab/
│     ├─ libs/                   ✅ 域内库（本域即学习项目）
│     │  └─ mini-gpt/            ✅ llm-lab-mini-gpt（Python）
│     └─ README.md               ✅ 域定位与运行方式
├─ platform/                     ➕ 跨域共享，平台团队维护
│  ├─ libs/                      ➕ 日志、追踪、配置、RPC、鉴权、UI 组件
│  └─ services/                  ➕ 网关、鉴权中心等
├─ contracts/                    ➕ protobuf / OpenAPI 契约
├─ e2e/                          ➕ 跨域端到端测试
├─ tools/                        ✅ 边界检查、初始化脚本
├─ infra/                        ➕ 基础镜像、Helm chart、Terraform
├─ docs/                         ✅ 规范、ADR、设计文档
├─ .github/                      ✅ CI 与 CODEOWNERS
├─ nx.json                       ✅ Nx 编排配置
├─ mise.toml                     ✅ 固定工具链版本
├─ package.json                  ✅ 根脚本、packageManager、engines
├─ pnpm-workspace.yaml           ✅ workspace 成员与 pnpm catalog
├─ pnpm-lock.yaml                ✅ 唯一的锁文件
├─ tsconfig.base.json            ✅ 各包共享的 TS 严格模式基线
├─ .npmrc                        ✅ pnpm 行为配置
├─ biome.json                    ✅ 唯一的格式化 + lint 配置（Biome）
├─ .vscode/                      ✅ 只提交 extensions.json 与 settings.json
├─ .gitattributes                ✅ 行尾统一 LF
├─ .editorconfig                 ✅ 编辑器换行/缩进统一
├─ .gitignore                    ✅
└─ README.md                     ✅ 仓库入口文档
```

学习/实验属性由业务域本身表达：不新增 `labs/` 目录，也不使用 `type:lab` 标签；项目按代码形态归入 `apps` / `services` / `libs`。

## 各层职责

| 路径                        | 放什么                                             | 不放什么                                             |
| --------------------------- | -------------------------------------------------- | ---------------------------------------------------- |
| `domains/<域>/apps/*`       | 客户端应用（Web、桌面、移动端）                    | 供别人 import 的业务逻辑（下沉到 libs）              |
| `domains/<域>/services/*`   | 可独立部署的后端服务                               | 其他服务的私有代码（跨域只能走契约）                 |
| `domains/<域>/libs/*`       | 只被本域依赖的共享库、类型、工具                   | 跨域通用能力（放 `platform/libs/`）                  |
| `domains/<域>/e2e/*`        | 本域跨应用 e2e                                     | 单应用内部 e2e（留在该应用 `test/`）                 |
| `platform/libs/*`           | 跨域通用能力，多语言实现用语言后缀区分             | 具体业务逻辑                                         |
| `platform/services/*`       | 网关、鉴权等平台服务                               | 业务服务                                             |
| `contracts/**`              | 服务间接口定义与生成代码                           | 手写消费方客户端                                     |
| `tools/`                    | 仓库级检查、生成器、CI 辅助脚本                    | 单个应用的构建脚本（放该应用 `package.json`）        |
| `infra/`                    | 基础镜像、基础 Helm chart、Terraform 模块          | 单个服务的 Dockerfile/values（放服务目录）           |

## 依赖边界

每个项目在 `project.json` 中声明三个标签：`domain:<域>`、`type:app|service|lib|contract|e2e|tool`、`lang:<语言>`。

| 项目类型                   | 允许依赖                                     |
| -------------------------- | -------------------------------------------- |
| `type:app` / `type:service` | 同域 lib、platform lib、任意 contract        |
| 域内 `type:lib`            | 同域 lib、platform lib、任意 contract        |
| platform lib               | platform lib、platform contract              |
| contract                   | contract（仅用于引用公共类型）               |
| e2e                        | 任意应用、服务、contract、平台 lib           |
| tool                       | 任意项目；生产代码不能依赖 tool              |

补充：

- 应用和服务不能被任何项目依赖（e2e 除外）。
- 一个域要复用另一个域的东西时：通用能力下沉到 `platform/`；业务逻辑通过调用对方服务的契约获得。
- 每个服务独占自己的数据库 schema，其他服务不能直连。

规则由 `node tools/checks/project-boundaries.mjs` 检查，CI 每个 PR 都运行。

## 根脚本

根脚本只做与业务域无关的批量转发，不写死项目路径，也不放单业务域专用命令。

| 命令                          | 作用                                        | 实现方式                              |
| ----------------------------- | ------------------------------------------- | ------------------------------------- |
| `pnpm check`                  | Biome 全仓库检查：格式 + lint + import 排序 | `biome check .`                       |

单业务域命令直接用 `pnpm --filter` / `pnpm -r` 调用：

| 需求 | 命令 |
| --- | --- |
| 启动 puff 域全部项目 | `pnpm -r --parallel run dev` |
| 构建 puff 域全部项目 | `pnpm -r run build` |
| 测试 puff 域全部项目 | `pnpm -r run test` |
| 只启动服务端 | `pnpm --filter @puff/api run dev` |
| 只启动前端 | `pnpm --filter @puff/web run dev` |
| 只启动桌面客户端 | `pnpm --filter @puff/desktop run dev` |
| 生产模式启动 API | `pnpm --filter @puff/api run start:prod` |
| 构建 Windows 安装包 | `pnpm --filter @puff/desktop run package:win` |

Windows 首次初始化使用 `powershell -ExecutionPolicy Bypass -File tools/bootstrap.ps1`；没有 mise 时两个 bootstrap 入口都会回退到 PATH 上的 `pnpm install`。

更细的全仓操作也使用原生命令：

| 需求 | 命令 |
| --- | --- |
| 自动修复格式/lint | `pnpm exec biome check --write .` |
| 只做类型检查 | `pnpm exec nx run-many -t typecheck` |
| 只跑 lint | `pnpm exec nx run-many -t lint` |
| 递归清理 | `pnpm -r run clean` |
| 项目图 | `pnpm exec nx graph` |
| 只跑受影响项目 | `pnpm exec nx affected -t lint typecheck test build` |

### 项目必须对齐的脚本契约

每个可构建项目都应实现 `dev`、`build`、`lint`、`typecheck`、`clean`，可选 `test` / `test:e2e`。缺少某个脚本不会报错（递归执行会跳过），但 Nx affected 和 CI 会静默漏检查。

Biome 是例外：根目录只有 `pnpm check` 一个全仓入口，自动修复用 `pnpm exec biome check --write .`；各项目内的 `lint` / `check` / `format` 是同样的 Biome 命令、只作用于本项目目录。

## workspace 与依赖版本

- `pnpm-workspace.yaml` 的 `packages` 固定为 `domains/*/{apps,services,libs}/*` 与 `platform/{libs,services}/*`。
- 第三方依赖统一写在 `catalog` 中，所有 `package.json` 使用 `"catalog:"`。同一依赖在仓库内只有一个版本。
- `minimumReleaseAgeExclude`：pnpm 默认会等待一段时间才信任刚发布的版本。当前只有 `@rsbuild/plugin-react@2.1.1` 需要豁免；被策略拦下时先确认版本来源可信，再加具体版本，不要整体关掉保护。
- `allowBuilds`：只有明确需要的依赖才允许执行安装脚本（`nx`、`electron`、`koffi`、`node-pty` 等）；新增依赖被拦下时先确认脚本用途和来源，再按包精确放行，禁止 `--all`。
- `overrides`：`@electron/get` 固定到 `5.1.0`，匹配 electron-builder 26 的 cache-mode API。
- **全仓库只有一个 `pnpm-lock.yaml`**：项目目录里不允许出现 `package-lock.json`、`yarn.lock` 或第二个 `pnpm-lock.yaml`。

## Nx

- `nx.json` 只声明输入、缓存和任务默认值；具体任务通过各项目 `project.json` 的 `nx:run-script` 调用原生脚本。
- 项目标签只写在 `project.json`，用于边界检查与统计。
- 本地常用：`pnpm exec nx graph`、`pnpm exec nx run <项目>:<target>`、`pnpm exec nx affected -t lint typecheck test build`。
- 远程缓存暂不接入；先使用 GitHub Actions 的 `.nx/cache` 缓存。

## 根目录禁止出现的内容

- ❌ 业务代码、页面或接口实现（属于 `domains/`）
- ❌ 应用专属依赖（写进对应项目 `package.json`）
- ❌ 项目的 `tsconfig.json`、构建配置（`rsbuild.config.ts`、`nest-cli.json`、`tsdown.config.ts`）
- ❌ 第二套格式化/lint 配置（`.prettierrc*`、`.eslintrc*`、`.oxlintrc.json`、包内 `biome.json`）
- ❌ `.env` 等真实环境变量文件
- ❌ 构建产物与缓存（`dist/`、`node_modules/`、`*.tsbuildinfo`、`.rspack-profile-*/`、`.nx/`）
- ❌ 散装示例代码、调试脚本（放 `tools/` 或项目内目录，并配套说明）

## 新增一个项目

1. 选择位置：业务应用 `domains/<域>/apps/<name>`，业务服务 `domains/<域>/services/<name>`，域内库 `domains/<域>/libs/<name>`，平台项目 `platform/libs/<name>` 或 `platform/services/<name>`。学习/实验属性由业务域本身表达，不新增 `labs/` 目录。
2. 包名 `@puff/<短名>`，设置 `"private": true`。
3. 补齐脚本契约 `dev` / `build` / `lint` / `typecheck` / `clean`（必要时 `test`），并 `extends` 根 `tsconfig.base.json`。`domains/**` 下的项目路径深度为四层，写 `../../../../tsconfig.base.json`；Python 项目不参与 workspace，可只提供自己的运行脚本。
4. 新增 `project.json`，声明 `domain:*`、`type:*`、`lang:*` 标签和 `nx:run-script` targets。
5. 在仓库根执行 `pnpm install`，然后跑 `pnpm check && pnpm -r run build && node tools/checks/project-boundaries.mjs`。
6. 新增 `docs/rules/<name>.md` 规范文档，更新本文件与根 `README.md` 的目录结构；涉及路由时同时更新 [routing.md](./routing.md)。

## 新增一个共享包

1. 只被一个域使用 → `domains/<域>/libs/`；跨域通用 → `platform/libs/`。不要恢复平铺的 `packages/`。
2. 目录命名 kebab-case，包名 `@puff/<短名>`，在 `package.json` 中用 `exports` 声明对外入口。
3. 只放无框架耦合的内容（纯类型、工具函数、常量、DTO 契约）起步；带 UI 或带 Nest 依赖的包单独评估。
4. 引用方用 workspace 协议安装：`pnpm --filter @puff/web add @puff/shared --workspace`。
5. 注意后端消费方：Nest 用 `tsc` 构建，直接引用源码直出的 TS 包会因 `rootDir` 之外的文件报错。推荐让共享包产出 `dist` 再被引用。
