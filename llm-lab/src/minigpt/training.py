"""训练循环：把数据喂给模型，再用梯度改进它。

整个文件说的其实是同一件事，四行：

    前向 → 算损失 → 反向 → 更新

后面几步模型会越变越大，但这四行一步都不会变。
"""

from __future__ import annotations

import math
from pathlib import Path

import torch
import torch.nn.functional as F


def pick_device() -> torch.device:
    """有 GPU 就用 GPU，没有就退回 CPU。"""
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def sample_batch(
    data: torch.Tensor, batch_size: int, context: int, generator: torch.Generator
) -> tuple[torch.Tensor, torch.Tensor]:
    """随机取一批训练样本。

    返回 (x, y)：x 是若干段长度为 context 的文本，y 是它们各自右移一位的答案。
    上下文长度是 1 时，模型每次只看一个字符去预测下一个。
    """
    starts = torch.randint(len(data) - context - 1, (batch_size,), generator=generator)
    starts = starts.to(data.device)
    offsets = torch.arange(context, device=data.device)
    x = data[starts[:, None] + offsets]
    y = data[starts[:, None] + offsets + 1]
    return x, y


def cross_entropy_loss(logits: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
    """(B, T, vocab_size) 的分数与 (B, T) 的正确答案 → 一个数。"""
    vocab_size = logits.shape[-1]
    return F.cross_entropy(logits.reshape(-1, vocab_size), targets.reshape(-1))


@torch.no_grad()
def evaluate(model, data: torch.Tensor, *, context: int = 1, batch_size: int = 256) -> float:
    """按训练时的方式评估：取不重叠的长度为 context 的窗口，算平均损失。

    之所以不用"整段序列一次算完"：注意力模型最多只能吃 context 个 token。
    而且不重叠窗口与训练时切窗口的方式一致，量出来的就是模型真实面对的分布。
    """
    model.eval()
    n_windows = (len(data) - 1) // context
    starts = torch.arange(n_windows, device=data.device) * context
    offsets = torch.arange(context, device=data.device)
    total, count = 0.0, 0.0
    for i in range(0, n_windows, batch_size):
        positions = starts[i : i + batch_size, None] + offsets
        x, y = data[positions], data[positions + 1]
        loss = cross_entropy_loss(model(x), y)
        total += loss.item() * y.numel()
        count += y.numel()
    model.train()
    return total / count


def train(
    model,
    data: torch.Tensor,
    *,
    steps: int,
    batch_size: int,
    context: int,
    learning_rate: float,
    seed: int = 0,
    report_every: int | None = None,
) -> list[float]:
    """训练循环本尊。看这四行就够了，其余都是记账。"""
    model.train()
    generator = torch.Generator().manual_seed(seed)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    history: list[float] = []

    for step in range(steps):
        x, y = sample_batch(data, batch_size, context, generator)

        logits = model(x)                        # 1. 前向：算出预测
        loss = cross_entropy_loss(logits, y)     # 2. 算损失：量化这次错得多离谱
        optimizer.zero_grad()                    #    清掉上一轮累积的梯度
        loss.backward()                          # 3. 反向：算出每个参数的梯度
        optimizer.step()                         # 4. 更新：按梯度把参数挪一点

        history.append(loss.item())
        if report_every and (step % report_every == 0 or step == steps - 1):
            print(f"  第 {step:>5} 步   loss = {loss.item():.4f}")

    return history


def cosine_learning_rate(
    step: int, total_steps: int, *, warmup: int, base_lr: float, min_lr: float
) -> float:
    """先线性升温，再按余弦曲线降到底。"""
    if step < warmup:
        return base_lr * (step + 1) / warmup
    progress = (step - warmup) / max(1, total_steps - warmup)
    return min_lr + 0.5 * (base_lr - min_lr) * (1.0 + math.cos(math.pi * progress))


def save_checkpoint(
    path: Path,
    model: torch.nn.Module,
    *,
    config: dict,
    step: int,
    train_loss: float,
    val_loss: float,
) -> None:
    """把权重和它对应的结构参数一起存下来。

    只存权重是不够的：同一个 state_dict 配上不同的 config，加载出来会是
    完全不同（或者根本对不上）的模型。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model": model.state_dict(),
            "config": config,
            "step": step,
            "train_loss": train_loss,
            "val_loss": val_loss,
        },
        path,
    )


def train_with_schedule(
    model,
    train_data: torch.Tensor,
    val_data: torch.Tensor,
    *,
    steps: int,
    batch_size: int,
    context: int,
    base_lr: float,
    warmup: int,
    min_lr: float,
    clip: float,
    eval_every: int,
    checkpoint_path: Path | None = None,
    config: dict | None = None,
    seed: int = 0,
    log_every: int | None = None,
) -> tuple[dict, list[float]]:
    """带调度、梯度裁剪和验证集选模型的训练循环。

    和前几步相比，多出来的三件事都在"围绕训练做工程"，那四步本身没变：
    前向 → 算损失 → 反向 → 更新。
    """
    model.train()
    generator = torch.Generator().manual_seed(seed)
    optimizer = torch.optim.AdamW(model.parameters(), lr=base_lr)
    history: list[float] = []
    best = {"step": -1, "train": float("inf"), "val": float("inf"), "lr": 0.0}

    for step in range(steps):
        lr = cosine_learning_rate(
            step, steps, warmup=warmup, base_lr=base_lr, min_lr=min_lr
        )
        for group in optimizer.param_groups:
            group["lr"] = lr

        x, y = sample_batch(train_data, batch_size, context, generator)
        logits = model(x)
        loss = cross_entropy_loss(logits, y)
        optimizer.zero_grad()
        loss.backward()
        # 裁剪：把所有梯度的总长度限制在 clip 以内，防止个别批次把参数带飞
        grad_norm = float(torch.nn.utils.clip_grad_norm_(model.parameters(), clip))
        optimizer.step()
        history.append(loss.item())

        if log_every and (step % log_every == 0 or step == steps - 1):
            print(
                f"  第 {step:>5} 步  lr = {lr:.2e}  loss = {loss.item():.4f}"
                f"  grad_norm = {grad_norm:.2f}"
            )

        if (step + 1) % eval_every == 0 or step == steps - 1:
            train_ce = evaluate(model, train_data, context=context)
            val_ce = evaluate(model, val_data, context=context)
            print(f"    → 验证：训练 {train_ce:.3f} / 验证 {val_ce:.3f}")
            if val_ce < best["val"]:
                best = {"step": step, "train": train_ce, "val": val_ce, "lr": lr}
                if checkpoint_path is not None and config is not None:
                    save_checkpoint(
                        checkpoint_path,
                        model,
                        config=config,
                        step=step,
                        train_loss=train_ce,
                        val_loss=val_ce,
                    )
            model.train()

    return best, history
