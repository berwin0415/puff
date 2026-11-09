# 0001 采用按业务域组织的 Monorepo

- 状态：已接受
- 日期：2026-11-09
- 依据：`docs/monorepo-architecture-design.md`（v0.3）

## 背景

仓库原本按应用类型平铺（`apps/api`、`apps/web`、`apps/desktop`），只有一个 pnpm workspace 和一套根脚本，没有编排层、依赖边界检查与 CI。设计文档要求按业务域组织目录，用工具约束跨域依赖，并保持每种语言一份锁文件、第三方依赖单一版本。

当时仓库规模较小（三个 TypeScript 项目），直接照搬面向 50～300 人组织的全部设施会引入不必要的复杂度。

## 决策

1. 目录改为 `domains/puff/{apps,services}`，并预留 `domains/puff/{libs,e2e}`、`platform/`、`contracts/`、`tools/`、`infra/`。
2. 接入 Nx 作为编排层，任务通过 `nx:run-script` 调用各项目原生命令；保留 `pnpm -r` 脚本作为直接入口。
3. 第三方依赖集中到 `pnpm-workspace.yaml` 的 catalog，所有 `package.json` 使用 `catalog:`；全仓库仍只有一份 `pnpm-lock.yaml`。
4. 依赖边界由 `tools/checks/project-boundaries.mjs` 检查，CI 每个 PR 都运行。
5. CI 使用 GitHub Actions 免费额度：PR/合并队列只恢复缓存，`main` 才保存缓存；每晚跳过缓存全量构建。
6. 暂不引入 Go/Gradle/uv 工作区、Buf、动态矩阵分片、合并队列与自建远程缓存；对应条件成熟时再按设计文档第 10 节推进。

## 与设计文档的差异

- 项目名沿用 npm 包名 `@puff/api`、`@puff/web`、`@puff/desktop`，不拼 `<域>-<目录名>`；npm scope 已保证唯一性。
- 边界检查读取 `project.json` 标签与 `package.json` 依赖，不解析 Nx 项目图。等出现跨语言项目后改为项目图校验。
- `catalogMode: strict` 在当前 pnpm 版本主要约束 `pnpm add`，历史清单里直接写版本号不会被拒绝；由检查脚本和评审兜底。

## 后果

- 新增应用/服务必须落在 `domains/<域>/{apps,services}/`，并补齐 `project.json` 标签与脚本契约。
- 第三方依赖版本只能改 catalog，不能在各项目里单独升级。
- 跨域复用只能通过 `platform/` 或 `contracts/`，否则边界检查失败。
- 2026-11-09 后续：`llm-lab` 已迁入 `domains/llm-lab`，作为 `type:lab` 业务域管理；仍不参与 pnpm workspace。见 [ADR 0002](./0002-model-learning-lab-as-domain.md)。
