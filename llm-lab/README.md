# llm-lab

从零手写一个最简易的 LLM。目标是**理解**，不是交付。

这条学习线分八步，每一步都能单独跑出可见结果。实现在 `src/minigpt/` 里随着步骤被不断修改和扩充，讲解写在 `notes/`，进度记录在 `PROGRESS.md`。全程只依赖 PyTorch 的张量与自动求导，不使用任何现成的 Transformer 实现。

**先读 [notes/00-总览.md](./notes/00-总览.md)**：一个最简易的 LLM 由哪些组件构成、它们怎么串起来、每个组件对应第几步。每一步开工前先回那里定位。

术语查表用 [notes/glossary.md](./notes/glossary.md)：按首次出现顺序排列，含 `vocab_size`、`d_model` 这类跨步骤概念的专门说明。

零基础补课从 [notes/01b-梯度.md](./notes/01b-梯度.md) 开始：梯度是什么、为什么"沿着它下降"就能训练、这条学习线到底需要多少数学基础。

## 当前状态

| 项 | 值 |
| --- | --- |
| 已完成 | 第 1 步：环境、第 2 步：字符级分词、第 3 步：bigram（统计版）、第 4 步：bigram（梯度版） |
| 下一步 | 第 5 步：加隐藏层 |

目标、验收标准与完整步骤表见 [ROADMAP.md](./ROADMAP.md)；逐次做了什么见 [PROGRESS.md](./PROGRESS.md)。

## 环境

Python `3.11` + 独立虚拟环境 `llm-lab/.venv`，不参与根仓库的 pnpm workspace。

```powershell
cd llm-lab
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

`requirements.txt` 指向 PyTorch 的 `cu128` 源：本机显卡是 Blackwell 架构（sm_120），只有 CUDA 12.8 及以上的构建才带对应内核，装老版本会 import 成功但一运行就报 `no kernel image is available`。索引用的是 SJTU 镜像，因为官方源在这台机器上实测会卡住（原因见文件内注释）。

## 怎么跑

```powershell
.venv\Scripts\python scripts\step01_check_env.py
.venv\Scripts\python scripts\step01_grad_demo.py
.venv\Scripts\python scripts\step01_gradient_descent.py
.venv\Scripts\python scripts\step02_tokenize.py
.venv\Scripts\python scripts\step03_bigram_counts.py
.venv\Scripts\python scripts\step04_bigram_gradient.py
```

## 目录

```
llm-lab/
├─ AGENTS.md        目录规范（本目录的规则与约定）
├─ README.md        本文件：入口
├─ ROADMAP.md       目标与步骤阶梯
├─ PROGRESS.md      进度日志
├─ requirements.txt Python 依赖
├─ notes/           每一步的讲解
├─ scripts/         步骤入口脚本
├─ src/minigpt/     会演进的实现
├─ data/            语料
└─ checkpoints/     训练产物（不入库）
```
