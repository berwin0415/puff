# AGENTS.md（apps/api）

约束 `@puff/api` 这一个包；与仓库根 `AGENTS.md` 叠加，更具体的规则以本文件为准。

## 包定位与现状

- NestJS 12 服务端，ESM（`nodenext` 解析）、Vitest、Biome（格式化 + lint）；所有接口挂在全局前缀 `/api`（`src/main.ts` 中 `setGlobalPrefix('api')`）。
- **当前只有脚手架自带的示例接口**（`GET /api`、`GET /api/health`），没有业务模块、数据库、鉴权。不要凭想象引用不存在的依赖或目录。
- 详细分层与目录规范见 `docs/rules/api.md`；路由写法与现状表见 `docs/rules/routing.md`。

## 命令

在仓库根执行（`pnpm --filter @puff/api run <脚本>` 也可）：

```bash
pnpm dev:api                                   # nest start --watch，:3000
pnpm --filter @puff/api typecheck              # tsc -p tsconfig.json --noEmit
pnpm --filter @puff/api check                  # biome check .（格式 + lint + import 排序）
pnpm --filter @puff/api check:fix              # 同上并写入修复
pnpm --filter @puff/api lint                   # 只跑 biome lint .
pnpm --filter @puff/api test                   # 单元测试 src/**/*.spec.ts
pnpm --filter @puff/api test:e2e               # e2e 测试 test/**/*.e2e-spec.ts
pnpm --filter @puff/api build                  # nest build → dist/
pnpm start:api                                 # 生产模式启动已构建产物
pnpm --filter @puff/api clean                  # 清理 dist
```

## 目录与分层

```
src/
├─ main.ts              启动入口：加载 .env、全局前缀、CORS、端口
├─ app.module.ts        根模块：只做装配
├─ app.controller.ts    ❗脚手架示例，业务路由不要往这里加
├─ modules/<domain>/    业务模块（module / controller / service / dto / entities）
├─ common/              过滤器、拦截器、管道、守卫、装饰器
└─ config/              集中读取与校验 process.env
test/                   e2e（*e2e-spec.ts）
```

依赖方向单向：`Controller → Service → Repository`；`common/`、`config/` 可被各层使用，反向依赖视为设计问题。

## 代码规则

- **新增业务模块**：`pnpm --filter @puff/api exec nest generate resource modules/<domain>`（只要骨架用 `generate module`）。这些命令会**自动修改 `src/app.module.ts`**，`resource` 还可能改 `package.json`，生成后必须 review 自动改动。
- **Controller 只做 HTTP 契约**：路由、参数绑定（`@Param`/`@Query`/`@Body` + DTO）、状态码；业务判断与数据访问放 Service，禁止在 Controller 里写查询或 SQL。
- **禁止 `any`**：入参出参都要有类型或 DTO；新接口的响应要有明确返回类型。
- **相对导入必须带 `.js` 后缀**（如 `./users.service.js`）。漏掉后缀时 `nodenext` 解析不到模块，类型检查会直接报 `error TS2307: Cannot find module './app.service'`，运行期同样失败。
- **不要用 `import type` 导入注入的类型**：Biome 的 `style/useImportType` 在本包被关闭正是为此。把 `import { AppService }` 改成 `import type { AppService }` 后 `tsc` 仍然通过，但 Nest 拿不到构造函数元数据，运行时会报 `Nest can't resolve dependencies of the AppController (?)`。
- **环境变量**：优先复用 `src/main.ts` 里的 Node 内置 `process.loadEnvFile()` 机制，不要为了读 `.env` 引入 `@nestjs/config`（除非用户明确要求）。业务代码不要在模块里散读 `process.env`，集中到 `src/config/`；新增变量必须同步 `apps/api/.env.example`。
- **CORS**：`app.enableCors()` 目前允许所有来源，仅限本地开发；改生产策略时同步更新 `docs/rules/api.md`。
- **状态码语义**：创建 `201`、删除 `204`、参数错误 `400`、不存在 `404`、冲突 `409`、业务拒绝 `422`；错误体当前是 Nest 默认形状，统一改造时必须一次性覆盖所有接口并更新 `docs/rules/routing.md`。
- **测试**：改 Controller 的方法必须补 `test/*.e2e-spec.ts`；e2e 里要用与 `main.ts` 一致的启动配置（含 `setGlobalPrefix('api')`），避免线上 404。
- **不要在这里装前端依赖**，也不要把后端依赖装到仓库根。

## 完成标准

```bash
pnpm --filter @puff/api typecheck
pnpm --filter @puff/api check
pnpm --filter @puff/api test
pnpm --filter @puff/api test:e2e     # 改到 HTTP 行为时必跑
pnpm --filter @puff/api build
```

改到接口路径、参数或响应 → 同一提交内更新 `docs/rules/routing.md`。
