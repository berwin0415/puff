# llm-lab

从零手写一个最简易的 LLM。目标是**理解**，不是交付。

这条学习线分八步，每一步都能单独跑出可见结果。实现在 `src/minigpt/` 里随着步骤被不断修改和扩充，讲解写在 `notes/`，进度记录在 `PROGRESS.md`。全程只依赖 PyTorch 的张量与自动求导，不使用任何现成的 Transformer 实现。

## 当前状态

| 项 | 值 |
| --- | --- |
| 已完成 | 第 1 步：环境（GPU 与自动求导均已验证） |
| 下一步 | 第 2 步：字符级分词 |

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
