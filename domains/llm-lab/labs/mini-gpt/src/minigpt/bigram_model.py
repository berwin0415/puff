"""可训练版的 bigram：把第 3 步的计数表换成一张可调的权重表。

形状完全一样，都是 65×65。区别只在这张表里的数字从哪来：
第 3 步是数出来的，这里是学出来的。
"""

from __future__ import annotations

import torch
from torch import nn


class BigramLM(nn.Module):
    """`table[i]` 是"当前字符是 i 时，下一个字符的分数"，也就是 logits。

    注意这里没有隐藏层，也没有中间向量：token 直接查到一行分数，softmax
    之后就是概率。它和第 3 步的计数表一一对应，所以两者可以直接比。
    """

    def __init__(self, vocab_size: int) -> None:
        super().__init__()
        self.vocab_size = vocab_size
        # nn.Embedding 一开始是一张随机表——这正是"训练之前什么都不会"的样子
        self.table = nn.Embedding(vocab_size, vocab_size)

    def forward(self, idx: torch.Tensor) -> torch.Tensor:
        """idx: (B, T) 的整数 → logits: (B, T, vocab_size)。"""
        return self.table(idx)

    def probabilities(self) -> torch.Tensor:
        """当前这张表给出的概率分布，形状 (vocab_size, vocab_size)。"""
        return torch.softmax(self.table.weight.detach(), dim=1)

    @torch.no_grad()
    def generate(
        self, idx: torch.Tensor, length: int, *, seed: int | None = None, greedy: bool = False
    ) -> torch.Tensor:
        """自回归生成：每一步取概率分布 → 抽一个（或取最大）→ 拼回去 → 再来一次。"""
        generator = None
        if seed is not None:
            generator = torch.Generator(device=idx.device).manual_seed(seed)
        for _ in range(length):
            logits = self(idx)
            last = logits[:, -1, :]
            if greedy:
                next_id = last.argmax(dim=-1, keepdim=True)
            else:
                next_id = torch.multinomial(
                    torch.softmax(last, dim=-1), 1, generator=generator
                )
            idx = torch.cat([idx, next_id], dim=1)
        return idx
