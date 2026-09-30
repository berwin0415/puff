# llm-lab

本目录是**个人学习区**：从零手写一个最简易的 LLM（mini-GPT），目标是理解，不是交付产品。
它**不是** `@puff/*` 应用或共享包，不参与 pnpm workspace，也不会被根脚本递归执行。

## 定位

- 只服务于一件事：把 Transformer 的每个零件亲手写出来、跑起来、讲清楚。
- 只依赖 PyTorch 的张量与自动求导，**不使用 `transformers` 等现成实现**。
- 所有产物（代码、讲解、语料、进度）都收敛在本目录内，不做跨目录引用。

## 目录约定

```
llm-lab/
├─ AGENTS.md        本文件：目录规范
├─ README.md        入口：当前进度、怎么跑
├─ ROADMAP.md       目标与步骤阶梯（含验收标准与状态）
├─ PROGRESS.md      进度日志（保证学习连续）
├─ requirements.txt Python 依赖
├─ notes/           每一步的讲解正文
├─ scripts/         可运行的步骤入口（stepNN_*.py）
├─ src/minigpt/     会随步骤演进的实现
├─ data/            语料
└─ checkpoints/     训练产物（不入库）
```

## 环境与脚本

- Python `3.11`，虚拟环境固定在 `llm-lab/.venv`（不入库）。
- 首次安装：

  ```powershell
  py -3.11 -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
  ```

- 运行任何脚本一律使用虚拟环境里的解释器：

  ```powershell
  .venv\Scripts\python scripts\step01_check_env.py
  ```

- 本目录**没有** `package.json`，不实现根 `AGENTS.md` 里 `dev` / `build` / `lint` / `typecheck` / `clean` 的脚本契约——那是 `apps/*` 的约定。

## 命名

- 目录与文件名：小写 + 连字符或下划线；步骤入口带 `stepNN_` 前缀。
- 代码标识符与注释用英文，讲解与文档用中文（与仓库根规则一致）。

## 格式

- UTF-8 + LF，与仓库根 `.gitattributes` 一致。
- Python 代码不在 Biome 的检查范围内；靠根 `.gitignore` 排除 `.venv/`、`__pycache__/`、`checkpoints/`，避免 `pnpm check` 去扫描第三方包。

## 每一步完成后必须做的事

1. 更新 `ROADMAP.md` 中对应步骤的状态。
2. 在 `PROGRESS.md` 追加一条记录，并写清「下次从这里继续」。
3. 在 `notes/` 补上这一步的讲解。
4. 在仓库根执行 `pnpm check`，确认 Biome 仍然通过。
