# 服务端目录规范（apps/api）

技术栈：NestJS 12、ESM（`nodenext`）、Vitest、Biome（格式化 + lint）。所有接口统一挂在全局前缀 `/api` 下（见 [routing.md](./routing.md)）。

> 当前只有脚手架示例（`AppController` 的探活与信息接口），**没有任何业务模块**。下面的 `modules/`、`common/`、`config/` 是按中型应用分层约定的位置，需要时再创建。

## 目录结构

```
apps/api/
├─ src/
│  ├─ main.ts                  ✅ 启动入口：环境变量、全局前缀、CORS、监听端口
│  ├─ app.module.ts            ✅ 根模块：只做模块装配
│  ├─ app.controller.ts        ✅ 脚手架示例（探活/信息），业务路由不要写在这里
│  ├─ app.service.ts           ✅ 脚手架示例
│  ├─ app.controller.spec.ts   ✅ 与实现同目录的单元测试
│  ├─ modules/                 ➕ 业务模块，一个业务域一个目录
│  │  └─ <domain>/
│  │     ├─ <domain>.module.ts
│  │     ├─ <domain>.controller.ts
│  │     ├─ <domain>.service.ts
│  │     ├─ dto/               ➕ 入参出参 DTO
│  │     └─ entities/          ➕ 领域模型/持久化模型
│  ├─ common/                  ➕ 跨模块基础设施
│  │  ├─ filters/              异常过滤器（统一错误响应）
│  │  ├─ interceptors/         响应拦截器、日志、耗时
│  │  ├─ pipes/                校验与转换管道
│  │  ├─ guards/               鉴权/权限
│  │  └─ decorators/           自定义参数装饰器
│  ├─ config/                  ➕ 配置读取与校验（集中管理 process.env）
│  └─ database/                ➕ 数据访问层（ORM/连接/迁移），用到时再建
├─ test/                       ✅ e2e 测试（*e2e-spec.ts）
├─ nest-cli.json               ✅ Nest CLI 配置
├─ tsconfig.json               ✅ 继承 ../../tsconfig.base.json
├─ tsconfig.build.json         ✅ 构建用（排除 test 与 spec）
├─ vitest.config.ts            ✅ 单元测试
├─ vitest.config.e2e.ts        ✅ e2e 测试
└─ .env.example                ✅ 环境变量样例（真实 .env 不入库）
```

## 分层职责

| 层                    | 允许做的事                                         | 禁止做的事                                    |
| --------------------- | -------------------------------------------------- | --------------------------------------------- |
| Controller            | HTTP 契约：路由、参数绑定、状态码、调用 Service    | 写业务判断、直接访问数据库、拼 SQL            |
| Service               | 业务规则、编排、事务边界、调用数据访问层           | 直接读写 `Request`/`Response`、处理 HTTP 细节 |
| DTO                   | 入参与出参结构、校验装饰器、`@nestjs/swagger` 注解 | 放业务逻辑                                    |
| Repository/DataSource | 数据访问、查询构造                                 | 写业务规则                                    |
| common/               | 跨模块复用的管道、守卫、过滤器、装饰器             | 依赖具体业务模块                              |
| config/               | 读取、校验、导出配置对象                           | 业务逻辑                                      |

依赖方向单向：`Controller → Service → Repository`，`common/` 与 `config/` 可被各层使用，反向依赖一律视为设计问题。

## 新增业务模块

业务代码统一建在 `src/modules/<domain>/`（domain 用复数名词，kebab-case）：

```bash
# 只要模块骨架
pnpm --filter @puff/api exec nest generate module modules/users

# 完整 CRUD 骨架（module + controller + service + dto + entity + spec）
pnpm --filter @puff/api exec nest generate resource modules/users
```

两条命令都会**自动修改 `src/app.module.ts`**，`resource` 还可能给 `package.json` 增加依赖，生成后必须 review 这些自动改动。

## 配置与环境变量

- 启动时由 `src/main.ts` 用 Node 内置的 `process.loadEnvFile()` 加载 `apps/api/.env`，因此**无需引入 `@nestjs/config`**；文件不存在时静默跳过，真实环境变量优先。
- 新变量必须同时更新 `apps/api/.env.example`，并在 [root.md](./root.md) 的脚本/部署说明或本文件里写清用途与默认值。
- 业务代码**不要散读 `process.env`**：统一在 `src/config/`（➕）读取、给默认值、做校验后导出类型化对象；`main.ts` 只读启动必需的变量（如 `PORT`）。
- 禁止把密钥、数据库口令写进入库文件或日志。

## 错误与响应

- 现状只使用 Nest 默认行为，未接入统一响应/异常层。默认 404 响应体已实测为：

  ```json
  { "message": "Cannot GET /api/nope", "error": "Not Found", "statusCode": 404 }
  ```

- 约定：**失败一律返回非 2xx 状态码 + 明确 message**，前端只按状态码判断成败，不解析自然语言 message。
- ➕ 需要统一时，在 `common/filters/` 加全局异常过滤器（映射业务异常 → 响应体），在 `common/interceptors/` 加统一包装（如 `{ data, meta }`）；一旦引入，**必须同步更新 [routing.md](./routing.md) 的响应约定并一次性统一所有已有接口**，不要一半包装一半裸返回。
- 参数校验（`ValidationPipe` + DTO 装饰器）属于接入业务前的必做项：引入 `class-validator`/`class-transformer` 后，在 `main.ts` 全局启用，并把错误体收敛成统一形状。

## 日志、安全与运维

- 开发环境 `main.ts` 里 `app.enableCors()` 等价于允许所有来源，**只适合本地联调**；上线前改成白名单来源。
- 生产环境建议清单（按需接入，接入后更新本节）：结构化日志（`nestjs-pino` 等）、`helmet`、限流、健康检查改用 `@nestjs/terminus`、优雅停机 `app.enableShutdownHooks()`。
- `PORT` 默认 `3000`；端口、CORS、日志级别等启动相关配置只从环境读取，不写死在代码里。

## 测试规范

| 类型 | 位置与命名                           | 命令                               |
| ---- | ------------------------------------ | ---------------------------------- |
| 单元 | `src/**/*.spec.ts`，与被测文件同目录 | `pnpm --filter @puff/api test`     |
| e2e  | `test/*.e2e-spec.ts`                 | `pnpm --filter @puff/api test:e2e` |

- 新增/修改 Controller 的方法 → **必须**补对应 e2e（真实 HTTP 断言状态码与响应体形状）。
- Service 的业务分支用单元测试覆盖；测试里不要直连真实外部服务，用 mock 或测试替身。
- e2e 内用 `app.setGlobalPrefix('api')` 等与 `main.ts` 一致的启动配置，避免「测试通过但线上 404」。

## 依赖与脚本

```bash
pnpm --filter @puff/api add @nestjs/swagger          # 生产依赖
pnpm --filter @puff/api add -D @nestjs/testing       # 开发依赖
```

- 不要在这里安装前端依赖；不要把后端依赖装到根目录。
- 保持 `typecheck`（`tsc -p tsconfig.json --noEmit`）与 `check`（Biome，`biome check .`）在提交前全绿；`pnpm --filter @puff/api check:fix` 可自动修复格式与 import 排序。
- Biome 的 `style/useImportType` 在 `apps/api` 下被显式关闭（见根 `biome.json` 的 overrides）。原因：Nest 依赖 `emitDecoratorMetadata` 生成构造函数参数元数据，把 `AppService` 这类注入类型改成 `import type` 后类型检查仍然通过，但启动时会报 `Nest can't resolve dependencies of the AppController (?)`。**不要为了消警告把注入的类型改成 `import type`。**

## 常见反例

- ❌ 在 `app.controller.ts` 里继续堆业务路由（应下沉到 `modules/<domain>/`）
- ❌ Controller 里直接 `await this.repo.find()` 或写 SQL
- ❌ 相对导入漏掉 `.js` 后缀（`./app.service`），`nodenext` 下运行时会解析失败
- ❌ 用 `any` 承接请求体或三方返回值；用 DTO + 类型守卫
- ❌ 在 `main.ts` 里写路由、写业务分支、写与启动无关的逻辑
