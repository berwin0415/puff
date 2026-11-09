# puff 业务域

puff 是面向用户的业务域，包含 Web/桌面客户端与后端 API 服务。

## 负责团队

待补充。建议在此写明负责团队、值班方式与升级路径。

## 项目清单

| 项目 | 路径 | 类型 | 说明 |
| --- | --- | --- | --- |
| `@puff/web` | `apps/web` | 前端应用 | React 19 + Rsbuild 2，dev server `:5173`，`/api` 代理到后端 |
| `@puff/desktop` | `apps/desktop` | 桌面应用 | Electron 44 薄宿主，托管 DeepSeek Harness Web UI |
| `@puff/api` | `services/api` | 后端服务 | NestJS 12，全局前缀 `/api`，默认 `:3000` |

## 目录约定

- `apps/`：可独立构建、分发的客户端应用。
- `services/`：可独立部署的后端服务。服务独占自己的数据库 schema，其他服务不能直连。
- `libs/`：只允许本域项目依赖的共享库。跨域复用请下沉到 `platform/`。
- `e2e/`：本域的跨应用端到端测试。单个应用内部的 e2e 仍留在该应用的 `test/` 目录。

## 边界

- 域内应用/服务只能依赖同域 `libs/`、`platform/libs/` 与 `contracts/`。
- 其他域不能直接引用本域代码，只能通过 `contracts/` 中的接口契约交互。
- 规则由 `tools/checks/project-boundaries.mjs` 检查，CI 与本地都会执行。

## 常用命令

在仓库根执行：

```bash
pnpm -r --parallel run dev                 # 并行启动三个项目
pnpm --filter @puff/web run dev            # 只启动 Web
pnpm --filter @puff/desktop run dev        # 只启动桌面端
pnpm --filter @puff/api run dev            # 只启动 API
pnpm exec nx affected -t lint typecheck test build
```
