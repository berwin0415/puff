"""自回归生成：把模型给出的概率分布变成一段文本。

模型只负责"给前文、吐概率"，不会自己写文章。这个循环必须我们自己写。
"""

from __future__ import annotations

import torch


@torch.no_grad()
def generate(
    model,
    idx: torch.Tensor,
    length: int,
    *,
    block_size: int,
    seed: int | None = None,
    greedy: bool = False,
    temperature: float = 1.0,
    top_k: int | None = None,
) -> torch.Tensor:
    """从 idx 开始，一次生成一个 token，直到凑够 length 个。

    temperature 和 top_k 是控制随机性的两个旋钮，第 8 步会展开。
    """
    model.eval()
    generator = None
    if seed is not None:
        generator = torch.Generator(device=idx.device).manual_seed(seed)

    for _ in range(length):
        window = idx[:, -block_size:]
        logits = model(window)[:, -1, :] / temperature
        if top_k is not None:
            k = min(top_k, logits.size(-1))
            cutoff = torch.topk(logits, k).values[:, -1:]
            logits = logits.masked_fill(logits < cutoff, float("-inf"))
        if greedy:
            next_id = logits.argmax(dim=-1, keepdim=True)
        else:
            next_id = torch.multinomial(
                torch.softmax(logits, dim=-1), 1, generator=generator
            )
        idx = torch.cat([idx, next_id], dim=1)
    return idx
