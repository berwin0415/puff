# 进度日志

这份文件用来保证学习的连续性：隔一段时间回来，读最后一条就能接上，不需要重读代码。

每条记录固定包含：

1. **做了什么** —— 这一步实际动手的内容。
2. **代码变化** —— 新增/修改了哪些文件。
3. **现在能回答的问题** —— 学习成果，具体到能用一句话讲清的概念。
4. **还答不上来的问题** —— 诚实记录，下次优先解决。
5. **下次从这里继续** —— 明确的起点。

---

## 2026-09-30 · 初始化

**做了什么**

定下学习区的形态：单一顶层目录 `llm-lab/`，所有产物收敛在此，不参与 pnpm workspace。确认本机硬件：RTX 5070 Ti（16 GB，Blackwell / sm_120）、Ryzen 7 9700X、31 GB 内存。本机没有可用的 `uv`，改用 Python 自带的标准库建虚拟环境。

**代码变化**

- 新增 `llm-lab/` 及 `AGENTS.md`、`README.md`、`ROADMAP.md`、`PROGRESS.md`、`requirements.txt`。
- 仓库根同步改动：`.gitignore` 增加 Python 相关忽略项，`docs/rules/root.md` 与根 `README.md` 的目录结构各加一行。
- 建立虚拟环境 `llm-lab/.venv`（Python 3.11.9）。

**现在能回答的问题**

- 为什么这个学习区不放进 `apps/*`：那两个 glob 是 pnpm workspace 成员，会被 `pnpm -r` 递归执行，而 Python 目录没有对应的脚本契约。
- 为什么必须装 cu128 的 PyTorch：5070 Ti 是 sm_120，老构建里没有对应内核。

**还答不上来的问题**

- 自动求导在底层具体是怎么工作的（第 4 步会正面遇到）。

**下次从这里继续**

第 1 步的收尾：跑通 `scripts/step01_check_env.py`，确认 GPU 与自动求导可用。

---

## 2026-09-30 · 第 1 步：环境（已完成）

**做了什么**

建好 `llm-lab/.venv`（Python 3.11.9），装上 `torch 2.11.0+cu128` 与 `numpy 2.4.6`，跑通环境验证脚本。

安装过程中撞到一个真实的坑：官方源 `download.pytorch.org` 下载到 441 MB 后彻底卡死——pip 缓存十几分钟不增长，TCP 连接一直处于 Established 却不传数据。换成 SJTU 镜像后同样的内容两分钟就下完了，速度差了一个数量级。这个结论写进了 `requirements.txt` 的注释，避免下次重新踩。

**代码变化**

- 新增 `scripts/step01_check_env.py`、`src/minigpt/__init__.py`、`notes/01-环境.md`。
- `requirements.txt`：索引源改为 SJTU 镜像，补充 `numpy`（torch 缺它会在 import 时告警）。

**实际输出**

```
python         : 3.11.9 on Windows
torch          : 2.11.0+cu128
gpu            : NVIDIA GeForce RTX 5070 Ti
compute cap.   : sm_120
vram           : 15.9 GiB
bundled cuda   : 12.8
cpu vs gpu     : max diff 0.00e+00 -> ok
autograd       : d(sum(x^2))/dx = [2.0, 4.0, 6.0] -> ok
matmul 4096^3  : 4.37 ms/iter, 31.4 TFLOP/s
```

CPU 与 GPU 的矩阵乘法结果完全一致（差值 0），自动求导给出的梯度与手算的 `2x` 相符。

**现在能回答的问题**

- 驱动 / CUDA runtime / PyTorch wheel 是三层的：驱动由 NVIDIA 提供，CUDA runtime 随 wheel 分发，而 wheel 里的计算内核是按 GPU 架构编译的。
- 为什么必须装 cu128：5070 Ti 是 `sm_120`，老构建里没有对应内核，会 import 成功但一运行就报 `no kernel image is available`——这种「装上了却跑不了」的失败方式最容易让人误判。
- 为什么 benchmark 前要预热：前几次调用要承担内核选择和显存分配的开销。

**还答不上来的问题**

- 自动求导在底层是怎么实现的（第 4 步会正面遇到）。
- 31.4 TFLOP/s 相对这张卡的 fp32 峰值是什么水平（未查证，先记下）。

**下次从这里继续**

第 2 步：字符级分词。开工前先定语料——先用 tiny shakespeare 走通流程，后续再换中文语料。
