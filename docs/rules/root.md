# 根目录规范

仓库根目录只承担两件事：**workspace 编排** 与 **跨包治理**。任何只被单个应用使用的东西都不属于根目录。

## 目录结构

```
.
├─ apps/                 ✅ 可独立构建、部署的应用
│  ├─ api/               ✅ @puff/api  NestJS 服务端
│  └─ web/               ✅ @puff/web  React + Rsbuild 前端
├─ packages/             ➕ 跨应用共享的库/类型包（当前为空）
├─ docs/                 ✅ 文档
│  ├─ rules/             ✅ 工程规范（本目录）
│  └─ *.md               ➕ 设计与方案文档
├─ scripts/              ➕ 被多个包复用的根级脚本（CI、代码生成、发布）
├─ package.json          ✅ 根脚本、packageManager、engines
├─ pnpm-workspace.yaml   ✅ workspace 成员与 pnpm 设置
├─ pnpm-lock.yaml        ✅ 唯一的锁文件
├─ tsconfig.base.json    ✅ 各包共享的 TS 严格模式基线
├─ .npmrc                ✅ pnpm 行为配置
├─ biome.json            ✅ 唯一的格式化 + lint 配置（Biome）
├─ .vscode/              ✅ 只提交 extensions.json 与 settings.json（默认格式化器指向 Biome）
├─ .gitattributes        ✅ 行尾统一 LF（二进制与 Windows 脚本单独声明）
├─ .editorconfig         ✅ 编辑器换行/缩进统一
├─ .gitignore            ✅
└─ README.md             ✅ 仓库入口文档
```

## 各层职责

| 路径         | 放什么                                            | 不放什么                                                 |
| ------------ | ------------------------------------------------- | -------------------------------------------------------- |
| `apps/*`     | 可独立运行/部署的应用（服务端、前端、worker）     | 供别人 `import` 的业务逻辑（应下沉到 packages）          |
| `packages/*` | 被两个以上应用复用的库、类型、工具                | 只有单个消费方的代码（先放应用内，出现第二个使用方再抽） |
| `docs/`      | 规范、设计、决策记录                              | 生成物、截图大文件、临时笔记                             |
| `scripts/`   | 跨包复用的自动化脚本（Node/pwsh 脚本 + 说明文档） | 单个应用的构建脚本（放该应用的 `package.json`）          |

## 根脚本

根脚本只做「批量转发」，不写死路径，保证新增应用后自动纳入。

| 命令                | 作用                                        | 实现方式                              |
| ------------------- | ------------------------------------------- | ------------------------------------- |
| `pnpm dev`          | 并行启动所有应用的 dev                      | `pnpm --parallel --filter "./apps/*"` |
| `pnpm dev:api`      | 只启动服务端                                | `--filter @puff/api`                  |
| `pnpm dev:web`      | 只启动前端                                  | `--filter @puff/web`                  |
| `pnpm build`        | 按依赖拓扑顺序构建全部                      | `pnpm -r run build`                   |
| `pnpm typecheck`    | 全量类型检查                                | `pnpm -r run typecheck`               |
| `pnpm check`        | Biome 全仓库检查：格式 + lint + import 排序 | `biome check .`                       |
| `pnpm check:fix`    | 同上并写入修复                              | `biome check --write .`               |
| `pnpm lint`         | 只跑 Biome lint                             | `biome lint .`                        |
| `pnpm format`       | 只跑 Biome 格式化（写入）                   | `biome format --write .`              |
| `pnpm format:check` | 只校验格式                                  | `biome format .`                      |
| `pnpm test`         | 全量测试（没有 `test` 脚本的包自动跳过）    | `pnpm -r run test`                    |
| `pnpm clean`        | 清理构建产物                                | `pnpm -r run clean`                   |
| `pnpm start:api`    | 以生产模式启动已构建的 API                  | `--filter @puff/api run start:prod`   |

### 应用必须对齐的脚本契约

根脚本依赖以下约定：每个 `apps/*` 都应实现 `dev`、`build`、`lint`、`typecheck`、`clean`，可选 `test`。
缺少某个脚本不会报错（递归执行会跳过），但会静默漏检查——所以新增应用时必须补齐。

Biome 是例外：`pnpm check` / `pnpm format` / `pnpm format:check` 由根脚本一次性扫描整个仓库（配置只有根 `biome.json`，不随包分裂）。各包内的 `lint` / `check` / `format` 脚本是同样的 Biome 命令、只作用于本包目录，方便用 `pnpm --filter <包> run check` 只查一个应用。

## workspace 配置

- `pnpm-workspace.yaml` 的 `packages` 固定为 `apps/*` 与 `packages/*`，新增应用/包只要落在对应目录即可被识别。
- `minimumReleaseAgeExclude`：pnpm 默认会等待一段时间才信任刚发布的版本（供应链保护）。目前只有 `@rsbuild/plugin-react@2.1.1` 需要豁免；今后安装依赖被该策略拦下时，**先确认版本来源可信**，再把具体版本加进这个列表，不要整体关掉保护。
- `.npmrc` 中 `engine-strict=true` 会让 Node 版本不满足 `engines` 时直接安装失败；`save-workspace-protocol=rolling` 让 `pnpm add` 自动写成 `workspace:` 协议。
- 根 `package.json` 的 `packageManager` 与 `engines` 是唯一的版本声明处，用 corepack 保证团队一致，不要在文档里另写一套版本号。
- **全仓库只有一个 `pnpm-lock.yaml`**：应用目录里不允许出现 `package-lock.json`、`yarn.lock` 或第二个 `pnpm-lock.yaml`。

## 根目录禁止出现的内容

- ❌ 业务代码与页面/接口实现（属于 `apps/*`）
- ❌ 应用专属依赖（`react`、`@nestjs/*` 等写进应用 `package.json`）
- ❌ 应用的 `tsconfig.json`、构建配置（前端 `rsbuild.config.ts`、后端 `nest-cli.json`）
- ❌ 第二套格式化/lint 配置（`.prettierrc*`、`.eslintrc*`、`.oxlintrc.json`、包内 `biome.json`）：配置只有根 `biome.json`
- ❌ `.env` 等真实环境变量文件
- ❌ 构建产物与缓存（`dist/`、`node_modules/`、`*.tsbuildinfo`、`.rspack-profile-*/`）
- ❌ 为了「方便」在根目录新建的散装示例代码、调试脚本（用 `scripts/` 或应用内目录，并配套说明）

## 新增一个应用

1. 用官方脚手架生成到目标目录，并跳过安装与 git 初始化，例如前端：`pnpm create rsbuild apps/<name> --template react-ts --no-git`。
2. 把 `package.json` 的 `name` 改成 `@puff/<name>`，设置 `"private": true`。
3. 删除脚手架自带的 `.gitignore`、`README.md`、锁文件，以及自带的格式化/lint 配置（不要引入第二套工具链，统一用根 `biome.json`），改为复用根配置（`biome.json`、`tsconfig.base.json`、`.gitignore`）。
4. 补齐脚本契约 `dev` / `build` / `lint` / `typecheck` / `clean`（必要时 `test`），并在应用 `tsconfig.json` 里 `extends: "../../tsconfig.base.json"`。
5. 在仓库根执行 `pnpm install`，然后跑 `pnpm check && pnpm typecheck && pnpm build`。
6. 新增 `docs/rules/<name>.md` 规范文档，更新本文件与根 `README.md` 的目录结构；涉及路由时同时更新 [routing.md](./routing.md)。

## 新增一个共享包（packages/*）

1. 目录命名 kebab-case，包名 `@puff/<短名>`，`"private": true`，并在 `package.json` 中用 `exports` 声明对外入口。
2. 只放**无框架耦合**的内容（纯类型、工具函数、常量、DTO 契约）起步；带 UI 或带 Nest 依赖的包单独评估。
3. 引用方用 `--workspace` 安装：`pnpm --filter @puff/web add @puff/shared --workspace`。
4. 注意后端消费方：Nest 用 `tsc` 构建，直接引用「源码直出的 TS 包」会因为 `rootDir` 之外的文件而报错。推荐让共享包产出 `dist` 再被引用；若坚持源码直连，需要在 `apps/api/tsconfig.json` 里配 `paths` **并**解决运行时解析，属于需要专门评审的改动。
