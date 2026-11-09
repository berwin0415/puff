"""单头自注意力：让模型自己决定"该重点看前面哪些字符"。

同一个窗口，两种汇总方式：
- 取平均：所有历史位置权重相同，谁也不能多看一眼；
- 注意力：用 Q/K 算出每一对位置的匹配度，softmax 之后变成权重。
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F
from torch import nn


class MeanPoolLM(nn.Module):
    """对照组：把前面所有字符的向量取平均，再预测下一个。"""

    def __init__(self, vocab_size: int, *, d_model: int = 64) -> None:
        super().__init__()
        self.embed = nn.Embedding(vocab_size, d_model)
        self.head = nn.Linear(d_model, vocab_size)

    def forward(self, idx: torch.Tensor) -> torch.Tensor:
        x = self.embed(idx)                                  # 形状 (B, T, d)
        running = x.cumsum(dim=1)                            # 累积和
        lengths = torch.arange(1, x.size(1) + 1, device=x.device).view(1, -1, 1)
        pooled = running / lengths                           # 位置 t 只看 0..t
        return self.head(pooled)


class SingleHeadLM(nn.Module):
    """单头因果自注意力：每个位置按匹配度加权前面所有位置。"""

    def __init__(self, vocab_size: int, *, d_model: int = 64, block_size: int = 32) -> None:
        super().__init__()
        self.embed = nn.Embedding(vocab_size, d_model)
        self.key = nn.Linear(d_model, d_model, bias=False)
        self.query = nn.Linear(d_model, d_model, bias=False)
        self.value = nn.Linear(d_model, d_model, bias=False)
        self.head = nn.Linear(d_model, vocab_size)
        # 下三角：位置 t 只能看 0..t
        self.register_buffer("mask", torch.tril(torch.ones(block_size, block_size)))

    def attention(self, idx: torch.Tensor) -> torch.Tensor:
        """返回 (B, T, T) 的注意力权重，供讲解时查看。"""
        x = self.embed(idx)
        q, k = self.query(x), self.key(x)
        att = q @ k.transpose(-2, -1) / math.sqrt(x.size(-1))
        att = att.masked_fill(self.mask[: x.size(1), : x.size(1)] == 0, float("-inf"))
        return F.softmax(att, dim=-1)

    def forward(self, idx: torch.Tensor) -> torch.Tensor:
        weights = self.attention(idx)
        out = weights @ self.value(self.embed(idx))
        return self.head(out)
