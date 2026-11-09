# 0002 把学习区建模为 `type:lab` 业务域

- 状态：已接受
- 日期：2026-11-09
- 依据：[ADR 0001](./0001-adopt-domain-first-monorepo.md)

## 背景

`llm-lab` 是从零手写 mini-GPT 的学习/实验项目，原先放在仓库根目录，不参与 pnpm workspace，也不在 `domains/` 的目录与依赖规则内。这样它既没有域归属和 CODEOWNERS 覆盖，也不会被边界检查发现。

## 决策

1. 新增业务域 `domains/llm-lab`，项目放在 `domains/llm-lab/labs/mini-gpt/`。
2. 新增项目类型 `type:lab`，目录类型为 `labs/`。lab 项目允许依赖同域 lib、platform lib 与 contract，但不能被除 e2e 外的项目依赖。
3. llm-lab 通过 `project.json` 声明 `domain:llm-lab`、`type:lab`、`lang:py`，参与 `tools/checks/project-boundaries.mjs` 的检查。
4. llm-lab 仍不加入 `pnpm-workspace.yaml`，不实现根 `dev` / `build` / `lint` / `typecheck` / `clean` 脚本契约，继续使用 `requirements.txt` 与自己的 Python 虚拟环境。
5. Nx 会从 `project.json` 登记 `llm-lab-mini-gpt`，但它没有 `package.json` 和 target；pnpm 递归命令与 `nx affected` 的构建目标不会执行它。

## 备选方案

- 放进 `domains/llm-lab/libs/mini-gpt` 并标记 `type:lib`：能复用既有类型，但学习项目不是共享库，语义失真。
- 继续留在仓库根目录：实现成本最低，但无法纳入域目录、CODEOWNERS 和边界检查。

## 后果

- 学习项目的归属、所有权和依赖方向与其他业务域一致。
- `type:lab` 成为新的项目类型，新增 lab 项目时必须在 `project.json` 中声明。
- llm-lab 的 Python 环境、依赖安装和运行方式保持独立，不引入 uv 或 Nx target。
- 仓库根目录不再保留 `llm-lab/`，所有学习产物都在 `domains/llm-lab` 内。
