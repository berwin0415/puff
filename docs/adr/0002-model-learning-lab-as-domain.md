# 0002 把学习区纳入业务域

- 状态：已接受（2026-11-09 修订：不再使用 `labs/` 与 `type:lab`）
- 日期：2026-11-09
- 依据：[ADR 0001](./0001-adopt-domain-first-monorepo.md)

## 背景

`llm-lab` 是从零手写 mini-GPT 的学习/实验项目，原先放在仓库根目录，不参与 pnpm workspace，也不在 `domains/` 的目录与依赖规则内。这样它既没有域归属和 CODEOWNERS 覆盖，也不会被边界检查发现。

## 决策

1. 新增业务域 `domains/llm-lab`，项目放在 `domains/llm-lab/libs/mini-gpt/`。
2. 不新增 `labs/` 目录或 `type:lab` 类型。学习/实验属性由业务域本身表达，而不是再由目录或标签重复标注。
3. 项目按代码形态登记为 `type:lib`，标签为 `domain:llm-lab`、`type:lib`、`lang:py`，参与 `tools/checks/project-boundaries.mjs` 的检查。
4. llm-lab 仍不加入 `pnpm-workspace.yaml`，不实现根 `dev` / `build` / `lint` / `typecheck` / `clean` 脚本契约，继续使用 `requirements.txt` 与自己的 Python 虚拟环境。
5. Nx 会从 `project.json` 登记 `llm-lab-mini-gpt`，但它没有 `package.json` 和 target；pnpm 递归命令与 `nx affected` 的构建目标不会执行它。

## 备选方案

- 放进 `domains/llm-lab/labs/mini-gpt` 并新增 `type:lab`：多一层分类，但“是否学习/实验”已由域名表达，重复且增加类型维护成本。
- 继续留在仓库根目录：实现成本最低，但无法纳入域目录、CODEOWNERS 和边界检查。

## 后果

- `llm-lab` 是学习域；域内项目按代码形态放在 `libs/` 等目录，不再使用 `labs/`。
- 新增学习/实验域时直接建 `domains/<域>`，项目按代码形态归入 `apps` / `services` / `libs`。
- llm-lab 的 Python 环境、依赖安装和运行方式保持独立，不引入 uv 或 Nx target。
- 仓库根目录不再保留 `llm-lab/`，所有学习产物都在 `domains/llm-lab` 内。
