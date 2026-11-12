# AGENTS.md（domains/puff/apps/web）

约束 `@puff/web` 这一个项目；与仓库根 `AGENTS.md` 叠加，更具体的规则以本文件为准。

## 包定位与现状

- React 19 + Rsbuild 2（Rspack）+ TypeScript 严格模式 + Biome（格式化 + lint）。dev server 固定 `5173`，静态预览 `4173`。
- **当前是 NEON VEIL 第一人称射击垂直切片**：React 19 负责 HUD、开始/暂停/结算界面，Three.js 负责场景、武器、敌人、雨夜灯光与后处理。没有路由、没有全局状态管理、没有请求层、没有测试框架；不要假装这些已经存在。
- 游戏引擎代码集中在 `src/features/neon-veil/engine/`，UI 与引擎通过 `GameSnapshot` / `GameEvent` 解耦；除调试外不要让 React 直接操作 Three.js 对象。
- 运行游戏不需要后端；`/api` 代理配置保留给后续接口。
- 详细目录与样式规范见 `docs/rules/web.md`；前端路由约定见 `docs/rules/routing.md`。

## 命令

在仓库根执行（`pnpm --filter @puff/web run <脚本>` 也可）：

```bash
pnpm --filter @puff/web run dev           # rsbuild dev，:5173，/api 代理到 :3000
pnpm --filter @puff/web typecheck         # tsc --noEmit
pnpm --filter @puff/web check             # biome check .（格式 + lint + import 排序）
pnpm --filter @puff/web check:fix         # 同上并写入修复
pnpm --filter @puff/web lint              # 只跑 biome lint .
pnpm --filter @puff/web build             # tsc --noEmit && rsbuild build → dist/
pnpm --filter @puff/web preview           # rsbuild preview --port 4173
pnpm --filter @puff/web clean             # 清理 dist 与缓存
pnpm exec nx run @puff/web:build          # 通过 Nx 执行
```

本项目**没有** `test` 脚本，`pnpm -r run test` 会跳过它；不要在任何回复里声称前端测试通过。

## 3D / 游戏约定

- 3D 依赖只有 `three` 与 `@types/three`，版本写在根 `pnpm-workspace.yaml` 的 catalog，项目里写 `"catalog:"`。
- 游戏资源全部程序化生成（Canvas 纹理、程序化几何、WebAudio），不要新增外部贴图/模型/音频二进制文件。
- 后处理链固定为 `RenderPass → HDR 净化 → Bloom → OutputPass → 电影调色 → SMAA`；HDR 净化 pass 不能删，Bloom 遇到 NaN/Inf 会把整屏染黑。
- 画质预设通过 `GameEngine.setQuality('cinematic' | 'performance')` 切换，不要在组件里直接改 renderer 设置。
- 指针锁必须由用户手势触发（开始/继续按钮）；失焦或退出指针锁时游戏应暂停。
- 开始界面的 `?capture=1` 只用于自动截图验证，不要把它当正式入口。

## 目录约定

```
src/
├─ index.tsx       唯一入口（createRoot + 全局样式 + 路由挂载）
├─ App.tsx         当前根组件；接入路由后由 routes/ 接管
├─ index.css       全局样式与 CSS 变量
├─ env.d.ts        import.meta.env 严格类型声明
├─ routes/         路由表与页面（router.tsx / root-layout.tsx / <segment>.tsx）
├─ components/     跨业务通用组件（PascalCase，必要时同名目录）
├─ features/       按业务域组织（组件 + hooks + 请求）
├─ api/            请求封装与接口函数（client.ts）
├─ hooks/  stores/  types/  styles/  assets/
public/            原样拷贝到 dist 的静态资源
```

只有单个页面使用的组件放 `features/`，跨业务复用的才上提到 `components/` 或 `hooks/`；不要为了「看起来规范」给每个组件都建目录。

## 代码规则

- **组件文件 PascalCase**，一个文件一个组件；组件样式用 CSS Modules（`Xxx.module.css`），不要用全局类名或 `!important` 去覆盖组件。
- **请求只在 `src/api/` 里写**：统一从 `import.meta.env.PUBLIC_API_BASE_URL ?? '/api'` 取基地址（默认 `/api`，由 dev server 代理到后端）。组件里禁止直接 `fetch`，禁止硬编码 `http://localhost:3000`。
- **环境变量**：只有 `PUBLIC_` 前缀会进浏览器产物。新增变量必须三处同步：`domains/puff/apps/web/.env.example`、`src/env.d.ts` 里的 `ImportMetaEnv` 声明、以及 `docs/rules/web.md` 的说明。密钥绝不能用 `PUBLIC_`。
- **不要添加 `index.html`**：HTML 由 Rsbuild 生成，标题与 favicon 在 `rsbuild.config.ts` 的 `html` 配置里改。
- **不要引入 Vite 或 `@vitejs/plugin-react`**；构建与 dev 一律走 Rsbuild/`rsbuild.config.ts`。
- **路由**：需要多页面时才安装 `pnpm --filter @puff/web add react-router`，路由表集中在 `src/routes/router.tsx`，页面组件用具名导出 `Component`，页面级代码分割用 `lazy`。dev 默认有 history fallback，生产部署需自行配置（如 Nginx `try_files`）。
- **端口与代理**：`5173`（strictPort）与 `/api` 代理目标写在 `rsbuild.config.ts`，代理目标从 `.env` 的 `API_PROXY_TARGET` 读取；不要改成跨域直连后端。
- **路径别名**：目前没有配置别名，跨目录最多回退两层；确需别名时同时改 `rsbuild.config.ts` 的 `source.alias` 与 `tsconfig.json` 的 `paths`，保持一致。
- **测试**：引入时推荐 rstest + `@testing-library/react`，命名 `<Component>.test.tsx` 与组件同目录；引入后同步更新 `docs/rules/web.md`。
- **依赖方向**：本项目属于 `domain:puff`、`type:app`，只能依赖同域 `libs/`、`platform/libs/` 与 `contracts/`。新增 workspace 依赖后跑 `node tools/checks/project-boundaries.mjs`。

## 完成标准

```bash
pnpm --filter @puff/web typecheck
pnpm --filter @puff/web check
pnpm --filter @puff/web build
```

改到端口、代理或请求基地址 → 实际起 `pnpm -r --parallel run dev` 验证一次请求链路，并同步 `docs/rules/routing.md`（若涉及路由）。
