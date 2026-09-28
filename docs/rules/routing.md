# 路由文档

本文档同时约束**后端接口路径**与**前端页面路由**，并给出两端的对应关系。任何路由的增删改都必须同步更新这里的表格。

> 当前仓库没有任何业务路由：后端只有脚手架自带的两个探活/信息接口，前端是单页无路由。下面的业务示例是**约定形态**。

## 总览

| 维度         | 约定                                                                    |
| ------------ | ----------------------------------------------------------------------- |
| 后端前缀     | 所有接口统一挂在 `/api` 下（`main.ts` 中 `app.setGlobalPrefix('api')`） |
| 后端资源路径 | 复数名词 + kebab-case，最多两层（`/api/order-items/:id`）               |
| 前端页面路径 | 由 React Router 接管，与后端资源同名但**不带 `/api` 前缀**              |
| 两端对应     | 页面 `/users` ↔ 接口 `/api/users`                                       |
| 版本化       | 当前不做版本号；对外契约需要并存时再引入（见下文「版本化」）            |

## 后端路由规范

### URL 形态

- 资源用**复数名词**：`/api/users`、`/api/order-items`。
- 动作用 HTTP 方法表达，**不要**在路径里写动词（`/api/getUserList` ❌）。
- 层级不超过两层，更深的关联用查询参数：`/api/orders?userId=1` 优于 `/api/users/1/orders`。
- 路径小写 + 连字符；查询参数 camelCase（`?pageSize=20`）。

### 方法与状态码

| 方法     | 用途        | 路径示例                       | 成功状态码        |
| -------- | ----------- | ------------------------------ | ----------------- |
| `GET`    | 列表 / 详情 | `/api/users`、`/api/users/:id` | `200`             |
| `POST`   | 创建        | `/api/users`                   | `201`             |
| `PATCH`  | 局部更新    | `/api/users/:id`               | `200`             |
| `PUT`    | 全量替换    | `/api/users/:id`               | `200`             |
| `DELETE` | 删除        | `/api/users/:id`               | `204`（无响应体） |

错误码约定：`400` 参数不合法、`401` 未认证、`403` 无权限、`404` 资源不存在、`409` 冲突（唯一键/并发）、`422` 业务规则拒绝、`500` 服务端异常。

### 控制器与前缀

- 每个业务模块一个控制器，装饰器前缀即资源名：`@Controller('users')` → `/api/users`。
- 参数一律通过 `@Param()` / `@Query()` / `@Body()` 绑定到 DTO，禁止直接使用裸对象或 `any`。
- 路径参数只放**资源标识**（id、slug），过滤/排序/分页走查询参数。

### 列表接口约定

请求：`?page=1&pageSize=20&sort=-createdAt&keyword=xxx`（`-` 表示倒序）。

响应统一带分页元信息，避免后期破坏性改动：

```json
{
  "items": [],
  "page": 1,
  "pageSize": 20,
  "total": 0
}
```

### 错误响应

- 现状：Nest 默认形状（实测 `GET /api/nope`）：

  ```json
  { "message": "Cannot GET /api/nope", "error": "Not Found", "statusCode": 404 }
  ```

- 约定：前端只依据**状态码**判断成败，`message` 仅用于展示与排错。
- ➕ 引入统一异常过滤器后，错误体形状在此处更新，并一次性覆盖所有接口（禁止新旧形状并存）。

### 版本化

当前不加版本号。当出现「外部消费方需要旧契约继续可用」时二选一，并在此文档记录决定：

1. Nest 内置版本控制（`app.enableVersioning()` + `@Version()`），路径形如 `/api/v1/users`；
2. 或者新增业务前缀（`/api/v2/...`），旧前缀保留一个下线周期。

### 探活接口

`GET /api/health` 用于部署探活与前端连通性自检（当前脚手架页面的数据来源），**不属于业务契约**，字段可随运维需要调整；接入正式探活方案时替换为 `@nestjs/terminus`。

## 当前后端路由（实测）

| 方法 | 路径          | 实现                      | 说明                                           |
| ---- | ------------- | ------------------------- | ---------------------------------------------- |
| GET  | `/api`        | `AppController.getRoot`   | 返回服务名与版本，脚手架示例                   |
| GET  | `/api/health` | `AppController.getHealth` | 返回 `status/service/version/uptime/timestamp` |

以上两个都是脚手架自带的示例接口；**业务接口从 `src/modules/<domain>/` 开始**，不要再往 `app.controller.ts` 里加。

## 新增后端路由的流程

1. 建模块骨架：`pnpm --filter @puff/api exec nest generate resource modules/users`，review 它对 `app.module.ts` 与 `package.json` 的自动改动。
2. 在 `users.controller.ts` 用 HTTP 方法装饰器声明路由，路径遵循上文 URL 形态；`@Controller('users')` 已提供 `/api/users` 前缀。
3. 参数绑定到 DTO（`dto/`），出参定义返回类型，避免 `any`。
4. 业务逻辑写进 `users.service.ts`，控制器只做契约转换。
5. 补测试：`test/users.e2e-spec.ts`（真实 HTTP 断言）+ 必要分支的单元测试。
6. 更新本文档「当前后端路由」表格，并在前端 `src/api/` 增加对应请求函数与类型。

检查清单：路径是否复数名词、是否只在 `/api` 前缀下、状态码是否符合语义、是否有 DTO 校验、是否有 e2e、文档是否更新。

## 前端路由规范

### 现状

单页无路由：`src/index.tsx` 直接渲染 `App.tsx`。在出现第二个页面之前**不要**引入路由库。

### 引入路由（React Router）

```bash
pnpm --filter @puff/web add react-router
```

约定目录与文件：

```
src/routes/
├─ router.tsx        路由表（唯一出口，具名导出 router）
├─ root-layout.tsx   顶层布局（导航 + <Outlet />）
├─ home.tsx          页面组件（route 模块，具名导出 Component）
├─ users.tsx
└─ not-found.tsx     兜底 404（path: '*'）
```

`src/index.tsx` 改为挂载 `RouterProvider`：

```tsx
import { RouterProvider } from 'react-router';
import { router } from './routes/router';
import './index.css';

const rootEl = document.getElementById('root');

if (rootEl) {
  createRoot(rootEl).render(
    <StrictMode>
      <RouterProvider router={router} />
    </StrictMode>,
  );
}
```

`src/routes/router.tsx` 用 data router 集中声明路由，并用 `lazy` 做页面级代码分割：

```tsx
import { createBrowserRouter } from 'react-router';
import RootLayout from './root-layout';

export const router = createBrowserRouter([
  {
    path: '/',
    Component: RootLayout,
    children: [
      { index: true, lazy: () => import('./home') },
      { path: 'users', lazy: () => import('./users') },
      { path: '*', lazy: () => import('./not-found') },
    ],
  },
]);
```

页面模块用**具名导出** `Component` 作为路由组件（也可同时导出 `loader` / `action` / `ErrorBoundary`）：

```tsx
// src/routes/users.tsx
export function Component() {
  return <h1>Users</h1>;
}
```

### 路径与文件对应

| 页面 URL     | 页面文件                     | 依赖接口             |
| ------------ | ---------------------------- | -------------------- |
| `/`          | `src/routes/home.tsx`        | —                    |
| `/users`     | `src/routes/users.tsx`       | `GET /api/users`     |
| `/users/:id` | `src/routes/user-detail.tsx` | `GET /api/users/:id` |
| `*`          | `src/routes/not-found.tsx`   | —                    |

约定：URL 用 kebab-case；文件名与 URL 段一致（`/user-settings` → `user-settings.tsx`）；动态段用 `:id`，在组件里通过 `useParams()` 读取。

### 导航

- 用 `<Link>` / `<NavLink>` 跳转，**不要用 `<a href>`**（会整页刷新，丢失 SPA 状态）。
- 页面级数据加载优先用路由 `loader`，交互触发的请求用组件内调用 `src/api/` 的函数。
- 需要标题/面包屑/权限等元信息时，写在路由对象的 `handle` 字段里，由布局统一读取，避免各页面重复实现。

### 深链与部署

- dev 与 preview 已默认对未知路径回退到 HTML（实测 `/users/deep` 返回 `200` + HTML），直接刷新子路由可用。
- 生产静态托管必须自行配置 history fallback（Nginx：`try_files $uri $uri/ /index.html;`），否则刷新 `/users` 会 404。
- 前端不区分「接口路由」和「页面路由」的 base：页面走根路径，接口统一 `/api` 前缀。

## 前后端对应关系示例

| 页面路由     | 页面文件                     | 调用的接口           | 后端实现位置（约定）                    |
| ------------ | ---------------------------- | -------------------- | --------------------------------------- |
| `/`          | `src/routes/home.tsx`        | `GET /api/health`    | `app.controller.ts`（探活，示例）       |
| `/users`     | `src/routes/users.tsx`       | `GET /api/users`     | `src/modules/users/users.controller.ts` |
| `/users/:id` | `src/routes/user-detail.tsx` | `GET /api/users/:id` | 同上                                    |

## 变更时同步更新

新增、改名、删除任何路由（后端或前端），同一个提交里必须完成：

1. 更新本文档的「当前后端路由」与「路径与文件对应」表格；
2. 后端补 e2e，前端补对应的接口函数与类型；
3. 若路由涉及新的权限或鉴权策略，在 [api.md](./api.md) 的安全章节与前端路由守卫说明中同步记录。
