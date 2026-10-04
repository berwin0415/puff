"""训练循环：把数据喂给模型，再用梯度改进它。

整个文件说的其实是同一件事，四行：

    前向 → 算损失 → 反向 → 更新

后面几步模型会越变越大，但这四行一步都不会变。
"""

from __future__ import annotations

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
def evaluate(model, data: torch.Tensor, *, context: int = 1, chunk: int = 8192) -> float:
    """在整段数据上算平均损失，分块处理以免一次吃掉太多显存。"""
    model.eval()
    total, count = 0.0, 0
    for start in range(0, len(data) - context - 1, chunk):
        # 最后一块可能不满，两端要按同一个 end 切，否则 x 与 y 长度对不上
        end = min(start + chunk, len(data) - 1)
        x = data[start:end].unsqueeze(0)
        y = data[start + 1 : end + 1].unsqueeze(0)
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
