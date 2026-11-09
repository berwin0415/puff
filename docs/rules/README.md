# 工程规范（docs/rules）

本目录是 puff Monorepo 的工程规范，按「中型应用」的长期可维护性编写：分层清晰、边界明确、能在半年后由新人接手。

> 仓库按业务域组织：`domains/puff/` 下是 Web、桌面端与 API；`domains/llm-lab/` 是 Python 学习域（`type:lab`，不参与 workspace）；`platform/`、`contracts/`、`infra/` 目前是约定位置。除后端探活/信息接口外没有自研业务功能，桌面端主要复用官方 DeepSeek Harness Web UI。
> 规范里凡标注「➕」的目录与文件，都是**约定位置，需要时再创建**，不要提前建空包占位。

| 文档                       | 覆盖范围                                                                          |
| -------------------------- | --------------------------------------------------------------------------------- |
| [root.md](./root.md)       | 仓库根目录：业务域目录、依赖边界、Nx、workspace/catalog、根脚本、新增项目与共享包 |
| [api.md](./api.md)         | `domains/puff/services/api`：NestJS 分层与目录、模块内部结构、配置、测试、依赖     |
| [web.md](./web.md)         | `domains/puff/apps/web`：Rsbuild + React 目录结构、命名、样式、环境变量、请求封装  |
| [desktop.md](./desktop.md) | `domains/puff/apps/desktop`：Electron 薄宿主、DSH Host 生命周期、安全、打包与测试  |
| [routing.md](./routing.md) | 路由：后端 URL 规范与新增流程、前端路由约定、两端对应关系                         |

架构决策记录见 [docs/adr](../adr/README.md)。

## 通用约定

### 命名

| 对象             | 规则                                                                                      | 例子                                                  |
| ---------------- | ----------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| 包名             | `@puff/<短名>`，与目录名一致                                                              | `domains/puff/services/api` → `@puff/api`            |
| 项目名           | 与 npm 包名一致；Nx 项目名同样是 `@puff/api`                                              | `pnpm exec nx run @puff/api:build`                    |
| 目录名、普通文件 | kebab-case（全小写 + 连字符）                                                             | `domains/puff/services/api/src/modules/order-items/`  |
| React 组件文件   | PascalCase，一个文件一个组件                                                              | `domains/puff/apps/web/src/components/StatusCard.tsx` |
| React Hook       | `useXxx.ts`，具名导出 `useXxx`                                                            | `domains/puff/apps/web/src/hooks/useHealth.ts`        |
| Nest 文件        | 统一后缀：`*.module.ts` / `*.controller.ts` / `*.service.ts` / `*.dto.ts` / `*.entity.ts` | `users.controller.ts`                                 |
| 测试             | 单元 `*.spec.ts` 与被测文件同目录；e2e `*.e2e-spec.ts` 放 `test/`                         | `app.controller.spec.ts`                              |
| 类型/接口        | PascalCase，不加 `I` 前缀                                                                 | `HealthStatus`                                        |
| 常量             | SCREAMING_SNAKE_CASE                                                                      | `API_BASE_URL`                                        |
| 文档文件         | 中文内容、kebab-case 文件名                                                               | `docs/rules/routing.md`                               |

### 文件与依赖边界

- **一个文件一个职责**：一个文件里不要既定义组件又定义请求封装又定义常量。
- **不在应用内部用 barrel（`index.ts`）转发**：Nest 侧是 `nodenext` + ESM，相对导入必须带 `.js` 后缀，barrel 会放大循环依赖风险；共享包只有在对外暴露时用 `exports` 字段。
- **禁止跨项目相对导入**：API 里不允许出现 `../../web/src/x`。共享代码放 `domains/puff/libs/` 或 `platform/libs/`，并按 [root.md](./root.md) 的流程做。
- **依赖方向**：域内应用/服务只能依赖同域 lib、平台 lib 与契约；跨域只能通过 `contracts/`。规则由 `node tools/checks/project-boundaries.mjs` 检查。
- **导入顺序**：Node 内置模块 → 第三方依赖 → workspace 包 → 项目内相对路径，组与组之间空一行。
- **相对导入后缀**：`domains/puff/services/api` 必须写 `.js`（`./app.service.js`）；Web 项目由 bundler 解析，不写扩展名。
- **应用专属依赖装在项目里**：`pnpm --filter @puff/api add xxx`，不要在根 `package.json` 装业务依赖。
- **第三方版本只写 catalog**：新增依赖时同时更新 `pnpm-workspace.yaml` 的 `catalog`，项目 `package.json` 写 `"catalog:"`。

### 注释与语言

- 代码标识符、代码注释统一**英文**；文档、提交信息用**中文**（与现有代码保持一致）。
- 注释解释「为什么」，不重复「做了什么」；明显的代码不写注释。

### 环境变量

- 每个项目管理自己的 `.env`（**不入库**），仓库里只提交 `.env.example`，且必须随新变量同步更新。
- 前端只把 `PUBLIC_` 前缀的变量注入浏览器代码；**任何密钥都不允许用 `PUBLIC_` 前缀**。

### 行尾与编码

- 全仓库统一 **UTF-8 + LF**：根 `.gitattributes` 用 `* text=auto eol=lf` 固定，规则优先于各人机器上的 `core.autocrlf`。
- 唯一例外是 Windows 批处理脚本（`*.bat` / `*.cmd`），它们显式保持 CRLF；二进制资源标记为 `binary`。
- 编辑器侧由根 `.editorconfig` 与 Biome（`lineEnding: "lf"`）兜底。

### 不入库的内容

`node_modules/`、`dist/`、`*.tsbuildinfo`、`coverage/`、`.rspack-profile-*/`、`.nx/`、`.env*`（除 `.env.example`）、编辑器个人配置（`.vscode/extensions.json`、`.vscode/settings.json` 例外）。

### 提交前自检

在仓库根目录执行，全部通过再提交：

```bash
pnpm check
pnpm build
pnpm test
node tools/checks/project-boundaries.mjs   # 改到项目结构或依赖时必跑
```

### 变更文档的时机

- 新增/修改**路由** → 必须同步更新 [routing.md](./routing.md) 的表格。
- 新增**业务域、项目或共享包** → 必须同步更新对应规范文档与根 [README.md](../../README.md) 的目录结构，并补 `project.json` 标签。
- 引入新的**约定性选择**（状态管理库、样式方案、校验库等）→ 写进对应规范文档，而不是只留在 PR 描述里。
- 引入不可从代码推导的**架构决策** → 在 [docs/adr](../adr/README.md) 新增记录。
