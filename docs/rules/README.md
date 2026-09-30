# 工程规范（docs/rules）

本目录是 puff 单体仓库的工程规范，按「中型应用」的长期可维护性编写：分层清晰、边界明确、能在半年后由新人接手。

> 当前仓库包含基础 API/Web 脚手架与 `apps/desktop` 桌面薄壳；除后端探活/信息接口外没有自研业务功能，桌面端主要复用官方 DeepSeek Harness Web UI。
> 因此规范里凡是标注「➕」的目录与文件，都是**约定位置，需要时再创建**，不要提前建空目录占位。

| 文档                       | 覆盖范围                                                              |
| -------------------------- | --------------------------------------------------------------------- |
| [root.md](./root.md)       | 仓库根目录：workspace 配置、根脚本、放什么/不放什么、新增应用与共享包 |
| [api.md](./api.md)         | `apps/api`：NestJS 分层与目录、模块内部结构、配置、测试、依赖         |
| [web.md](./web.md)         | `apps/web`：Rsbuild + React 目录结构、命名、样式、环境变量、请求封装  |
| [desktop.md](./desktop.md) | `apps/desktop`：Electron 薄宿主、DSH Host 生命周期、安全、打包与测试  |
| [routing.md](./routing.md) | 路由：后端 URL 规范与新增流程、前端路由约定、两端对应关系             |

## 通用约定

### 命名

| 对象             | 规则                                                                                      | 例子                                     |
| ---------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------- |
| 包名             | `@puff/<短名>`，与目录名一致                                                              | `apps/api` → `@puff/api`                 |
| 目录名、普通文件 | kebab-case（全小写 + 连字符）                                                             | `apps/api/src/modules/order-items/`      |
| React 组件文件   | PascalCase，一个文件一个组件                                                              | `apps/web/src/components/StatusCard.tsx` |
| React Hook       | `useXxx.ts`，具名导出 `useXxx`                                                            | `apps/web/src/hooks/useHealth.ts`        |
| Nest 文件        | 统一后缀：`*.module.ts` / `*.controller.ts` / `*.service.ts` / `*.dto.ts` / `*.entity.ts` | `users.controller.ts`                    |
| 测试             | 单元 `*.spec.ts` 与被测文件同目录；e2e `*.e2e-spec.ts` 放 `test/`                         | `app.controller.spec.ts`                 |
| 类型/接口        | PascalCase，不加 `I` 前缀                                                                 | `HealthStatus`                           |
| 常量             | SCREAMING_SNAKE_CASE                                                                      | `API_BASE_URL`                           |
| 文档文件         | 中文内容、kebab-case 文件名                                                               | `docs/rules/routing.md`                  |

### 文件与依赖边界

- **一个文件一个职责**：一个文件里不要既定义组件又定义请求封装又定义常量。
- **不在应用内部用 barrel（`index.ts`）转发**：Nest 侧是 `nodenext` + ESM，相对导入必须带 `.js` 后缀，barrel 会放大循环依赖风险；`packages/*` 只有在对外暴露时用 `exports` 字段。
- **禁止跨应用相对导入**：`apps/api` 里不允许出现 `../../web/src/x`。要共享就建 `packages/*`（见 [root.md](./root.md)）。
- **导入顺序**：Node 内置模块 → 第三方依赖 → workspace 包 → 应用内相对路径，组与组之间空一行。
- **相对导入后缀**：`apps/api` 必须写 `.js`（`./app.service.js`）；`apps/web` 由 bundler 解析，不写扩展名。
- **应用专属依赖装在应用里**：`pnpm --filter @puff/api add xxx`，不要在根 `package.json` 装业务依赖，也不要在应用目录里用 npm/yarn 安装。

### 注释与语言

- 代码标识符、代码注释统一**英文**；文档、提交信息用**中文**（与现有代码保持一致）。
- 注释解释「为什么」，不重复「做了什么」；明显的代码不写注释。

### 环境变量

- 每个应用管理自己的 `.env`（**不入库**），仓库里只提交 `.env.example`，且必须随新变量同步更新。
- 前端只把 `PUBLIC_` 前缀的变量注入浏览器代码；**任何密钥都不允许用 `PUBLIC_` 前缀**。

### 行尾与编码

- 全仓库统一 **UTF-8 + LF**：根 `.gitattributes` 用 `* text=auto eol=lf` 固定，规则优先于各人机器上的 `core.autocrlf`，所以克隆下来就是 LF，不需要每个人改本地配置。
- 唯一例外是 Windows 批处理脚本（`*.bat` / `*.cmd`），它们显式保持 CRLF；二进制资源（图片、字体、压缩包）标记为 `binary`，不做行尾转换。
- 编辑器侧由根 `.editorconfig`（`end_of_line = lf`）与 Biome（`lineEnding: "lf"`）兜底，新增文件不要在最后一行或行尾留下多余空白。

### 不入库的内容

`node_modules/`、`dist/`、`*.tsbuildinfo`、`coverage/`、`.rspack-profile-*/`、`.env*`（除 `.env.example`）、编辑器个人配置（`.vscode/extensions.json`、`.vscode/settings.json` 例外）。

### 提交前自检

在仓库根目录执行，四条全绿再提交：

```bash
pnpm format:check   # 或 pnpm format 后提交
pnpm lint
pnpm typecheck
pnpm test && pnpm build
```

### 变更文档的时机

- 新增/修改**路由** → 必须同步更新 [routing.md](./routing.md) 的表格。
- 新增**顶层目录或包** → 必须同步更新对应规范文档与根 [README.md](../../README.md) 的目录结构。
- 引入新的**约定性选择**（状态管理库、样式方案、校验库等）→ 写进对应规范文档，而不是只留在 PR 描述里。
