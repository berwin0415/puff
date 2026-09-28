# 前端目录规范（apps/web）

技术栈：React 19 + Rsbuild 2（Rspack）+ TypeScript 严格模式 + Biome（格式化 + lint）。

> 当前只有脚手架页面（调用 `/api/health` 的自检卡片），**没有业务页面，也没有路由**。`routes/`、`components/`、`features/`、`api/` 等目录按下面的约定在需要时创建。

## 目录结构

```
apps/web/
├─ public/                     ✅ 原样拷贝到 dist 的静态资源（favicon 等）
├─ src/
│  ├─ index.tsx                ✅ 唯一入口：createRoot + 全局样式 + 路由挂载
│  ├─ App.tsx                  ✅ 当前根组件（接入路由后由 routes/ 接管，App 可删除）
│  ├─ index.css                ✅ 全局样式与 CSS 变量
│  ├─ env.d.ts                 ✅ import.meta.env 严格类型声明
│  ├─ routes/                  ➕ 路由表与页面组件（见 routing.md）
│  ├─ components/              ➕ 跨业务复用的展示型组件
│  ├─ features/                ➕ 按业务域组织的功能模块
│  │  └─ <feature>/            （内部可含 components/、hooks/、api.ts）
│  ├─ api/                     ➕ 请求封装与后端接口定义
│  │  └─ client.ts             ➕ 统一 fetch 客户端（baseURL、错误处理、序列化）
│  ├─ hooks/                   ➕ 跨业务复用的 Hook
│  ├─ stores/                  ➕ 全局状态（本地 UI 状态优先用组件内 state）
│  ├─ styles/                  ➕ 主题变量、重置样式、混用变量
│  ├─ types/                   ➕ 跨业务共享类型
│  └─ assets/                  ➕ 需要经过构建处理的图片/字体
├─ rsbuild.config.ts           ✅ 构建/开发服务配置（端口、代理、HTML、插件）
├─ tsconfig.json               ✅ 继承 ../../tsconfig.base.json
├─ （无本地 lint/format 配置） ✅ 统一使用根目录 biome.json
└─ .env.example                ✅ 环境变量样例（真实 .env 不入库）
```

## 目录职责

| 目录          | 放什么                                      | 不放什么                                    |
| ------------- | ------------------------------------------- | ------------------------------------------- |
| `routes/`     | 路由定义、页面级组件、布局组件              | 业务逻辑与请求细节                          |
| `components/` | 与业务无关的通用组件（按钮、表格、空状态）  | 只有单个页面用的组件（放 `features/`）      |
| `features/`   | 一个业务域的全部实现（组件 + hooks + 请求） | 跨业务复用的东西（上提到 components/hooks） |
| `api/`        | 请求客户端、接口函数、请求/响应类型         | React 组件、状态管理逻辑                    |
| `hooks/`      | 可复用的行为逻辑（`useXxx`）                | 只服务单个组件的 Hook（就近放在组件旁）     |
| `stores/`     | 跨页面共享的全局状态（如登录态）            | 组件局部状态、服务端数据缓存                |
| `styles/`     | 全局变量、主题、通用样式工具                | 组件私有样式（用 CSS Modules 就近放）       |
| `types/`      | 跨业务共享的类型声明                        | 组件 props 类型（与组件同文件）             |

### 组件内部结构（中型应用的推荐形态）

简单组件：单文件 `PascalCase.tsx` + 同名样式文件。

```
src/components/StatusCard/
├─ StatusCard.tsx          组件实现（具名导出 + 默认导出其一，团队内统一）
├─ StatusCard.module.css   组件样式
└─ StatusCard.test.tsx     ➕ 单测（引入测试框架后）
```

只有需要配套文件（样式、测试、子组件、Hook）时才建同名目录；**不要为了「看起来规范」给每个组件都建目录**。

## 命名与文件约定

- 组件文件 PascalCase，一个文件一个组件；组件目录名同样 PascalCase。
- Hook 文件 `useXxx.ts`，具名导出 `useXxx`。
- 样式文件 `Xxx.module.css`（CSS Modules，Rsbuild 默认按后缀识别）。
- 常量文件 `constants.ts`；类型文件 `types.ts`；工具函数 `utils.ts`，放在使用它们的目录内。
- 相对导入不写扩展名；不要用跨目录的 `../../../`（层数超过两层就该考虑上提或配置别名）。
- ➕ 需要路径别名时，在 `rsbuild.config.ts` 的 `source.alias` 配置，并**同步** `tsconfig.json` 的 `paths`，两边必须一致。

## 样式

- 全局样式集中在 `src/styles/`（当前是 `src/index.css`）：CSS 变量定义主题色、间距、字体，支持 `prefers-color-scheme`。
- 组件样式一律用 CSS Modules（`*.module.css`），不使用全局类名，避免样式泄漏。
- 选择器尽量一层（`className` 直接命中），不写深层后代选择器；不用 `!important`。
- ➕ 如果团队决定引入 Tailwind 或 CSS-in-JS，先在 [docs/rules/README.md](./README.md) 的通用约定里登记，再统一改造，禁止两种方案并存。

## 环境变量

- Rsbuild 自动读取 `apps/web/.env`、`.env.local`、`.env.<mode>`；**只有 `PUBLIC_` 前缀的变量会注入浏览器代码**，通过 `import.meta.env.PUBLIC_XXX` 访问。
- 新增变量必须同时做三件事：更新 `.env.example`、在 `src/env.d.ts` 的 `ImportMetaEnv` 里声明（已开启 `strictImportMetaEnv`，未声明的键读不到）、在规范文档里说明用途。
- 当前使用的变量：`PUBLIC_API_BASE_URL`（浏览器请求基地址，默认 `/api`）、`API_PROXY_TARGET`（dev 代理目标，仅构建配置使用，不会进浏览器）。
- 任何密钥、token 都不得使用 `PUBLIC_` 前缀——它们会被打进前端产物。

## 与后端通信

- 所有请求走 `src/api/client.ts`（➕ 待建）：统一读取 `PUBLIC_API_BASE_URL`、统一 JSON 序列化、统一非 2xx 的错误抛出与日志。
- 组件中**禁止硬编码 URL**（如 `fetch('http://localhost:3000/...')`），只能调用 `api/` 里导出的接口函数。
- 组件里禁止直接 `fetch`：请求细节属于 `api/` 层，组件只消费函数与类型。
- 服务端状态（列表/详情/分页）推荐引入查询库（如 TanStack Query）统一缓存与重试；局部 UI 状态用 `useState`/`useReducer`，跨页面共享状态才进 `stores/`。
- 接口路径与后端的对应关系见 [routing.md](./routing.md)。

## 构建与运行

| 命令                              | 说明                                       |
| --------------------------------- | ------------------------------------------ |
| `pnpm dev:web`                    | dev server，固定 `5173`（`strictPort`）    |
| `pnpm --filter @puff/web build`   | `tsc --noEmit` + `rsbuild build` → `dist/` |
| `pnpm --filter @puff/web preview` | 预览产物，`4173`                           |

- HTML 由 Rsbuild 生成（`html.title`、`html.favicon`），仓库里**不放 `index.html`**；要改 meta/title 就改 `rsbuild.config.ts`。
- dev 与 preview 都通过 `/api` 代理到后端，浏览器只访问同源地址，因此不依赖后端 CORS。
- dev server 默认对未知路径回退到 HTML（SPA 行为）；**生产部署必须自己配置 history fallback**（如 Nginx `try_files $uri $uri/ /index.html;`），否则刷新子路由会 404。
- 产物只依赖静态文件，可由 Nginx/CDN 托管；涉及接口地址时通过构建期 `PUBLIC_*` 变量注入。

## 测试

- 现状无前端测试。引入时推荐 **rstest**（与 Rsbuild 同源配置，`pnpm create rsbuild --tools rstest` 可生成骨架）+ `@testing-library/react`。
- 命名 `*.test.tsx`，与被测组件同目录；测试文件不参与构建产物。
- 优先覆盖：纯函数与 Hook 的逻辑、关键交互路径；不要为快照而写快照测试。

## 常见反例

- ❌ 组件里直接 `fetch` 或拼 URL
- ❌ 把业务逻辑写进 `routes/` 的页面组件（应下沉到 `features/`）
- ❌ 用全局 CSS 覆盖组件样式、用 `!important`
- ❌ 把非 `PUBLIC_` 的变量名直接写进前端代码（读不到值）
- ❌ 为了加一个页面就在根目录新建 `src/pages`、`src/views` 等与规范冲突的目录
