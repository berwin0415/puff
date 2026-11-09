"""完整的 Transformer block，堆成一个 mini-GPT。

第 6 步的单头注意力缺三样东西，这里一次补齐：
- 位置编码：让模型知道谁在第几位
- 残差连接：把输入原样加回去，不必靠"注意自己"来保住信息
- 前馈网络：每个位置各自再加工一次
再加 LayerNorm 让深层训练稳定，多头让模型同时关注不同类型的关系。
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F
from torch import nn


class CausalSelfAttention(nn.Module):
    """多头因果自注意力。"""

    def __init__(self, d_model: int, n_head: int, block_size: int, dropout: float) -> None:
        super().__init__()
        if d_model % n_head:
            raise ValueError("d_model 必须能被 n_head 整除")
        self.n_head = n_head
        self.head_size = d_model // n_head
        self.qkv = nn.Linear(d_model, 3 * d_model, bias=False)
        self.proj = nn.Linear(d_model, d_model)
        self.attn_dropout = nn.Dropout(dropout)
        self.resid_dropout = nn.Dropout(dropout)
        self.register_buffer("mask", torch.tril(torch.ones(block_size, block_size)))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, length, width = x.shape
        q, k, v = self.qkv(x).split(width, dim=2)
        q = q.view(batch, length, self.n_head, self.head_size).transpose(1, 2)
        k = k.view(batch, length, self.n_head, self.head_size).transpose(1, 2)
        v = v.view(batch, length, self.n_head, self.head_size).transpose(1, 2)

        scores = q @ k.transpose(-2, -1) / math.sqrt(self.head_size)
        scores = scores.masked_fill(self.mask[:length, :length] == 0, float("-inf"))
        weights = self.attn_dropout(F.softmax(scores, dim=-1))
        out = weights @ v
        out = out.transpose(1, 2).contiguous().view(batch, length, width)
        return self.resid_dropout(self.proj(out))


class FeedForward(nn.Module):
    """每个位置各自过一次两层网络，中间维度是 4 倍。"""

    def __init__(self, d_model: int, dropout: float) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(d_model, 4 * d_model),
            nn.GELU(),
            nn.Linear(4 * d_model, d_model),
            nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class Block(nn.Module):
    """先归一化再进子层，输出与输入相加。"""

    def __init__(self, d_model: int, n_head: int, block_size: int, dropout: float) -> None:
        super().__init__()
        self.norm1 = nn.LayerNorm(d_model)
        self.attn = CausalSelfAttention(d_model, n_head, block_size, dropout)
        self.norm2 = nn.LayerNorm(d_model)
        self.ff = FeedForward(d_model, dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.norm1(x))   # 残差连接
        x = x + self.ff(self.norm2(x))     # 残差连接
        return x


class MiniGPT(nn.Module):
    """token 嵌入 + 位置嵌入 + N 个 block + 输出头。"""

    def __init__(
        self,
        vocab_size: int,
        *,
        d_model: int = 128,
        n_head: int = 4,
        n_layer: int = 4,
        block_size: int = 128,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.block_size = block_size
        self.token_embedding = nn.Embedding(vocab_size, d_model)
        self.position_embedding = nn.Embedding(block_size, d_model)
        self.dropout = nn.Dropout(dropout)
        self.blocks = nn.Sequential(
            *[Block(d_model, n_head, block_size, dropout) for _ in range(n_layer)]
        )
        self.norm_final = nn.LayerNorm(d_model)
        self.lm_head = nn.Linear(d_model, vocab_size)

    def forward(self, idx: torch.Tensor) -> torch.Tensor:
        _, length = idx.shape
        if length > self.block_size:
            raise ValueError(f"序列长度 {length} 超过 block_size {self.block_size}")
        positions = torch.arange(length, device=idx.device)
        x = self.token_embedding(idx) + self.position_embedding(positions)
        x = self.dropout(x)
        x = self.blocks(x)
        return self.lm_head(self.norm_final(x))
