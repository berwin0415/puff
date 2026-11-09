"""把上下文从 1 个字符扩到一整个窗口，中间再插一层隐藏层。

这是"从查表变成网络"的最小一步：每个位置看前面 window 个字符，
把它们拼起来过一层非线性变换，再输出下一个字符的分数。
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from torch import nn


class WindowMLP(nn.Module):
    """每个位置看前面 `window` 个字符，用一层隐藏层做预测。

    `Conv1d` 在这里就是"窗口拼接 + 线性层"：kernel_size=window 的卷积，
    在位置 t 上取的就是 (t-window+1 .. t) 这 window 个字符的向量拼接。
    权重在所有位置共享，所以参数量与序列长度无关。
    """

    def __init__(
        self, vocab_size: int, *, d_model: int = 64, hidden: int = 256, window: int = 8
    ) -> None:
        super().__init__()
        self.window = window
        self.embed = nn.Embedding(vocab_size, d_model)
        self.fc1 = nn.Conv1d(d_model, hidden, kernel_size=window)
        self.fc2 = nn.Linear(hidden, vocab_size)

    def forward(self, idx: torch.Tensor) -> torch.Tensor:
        """输入 (B, T) 的整数，输出 (B, T, vocab_size) 的分数。"""
        x = self.embed(idx).transpose(1, 2)      # 形状 (B, d_model, T)
        x = F.pad(x, (self.window - 1, 0))       # 左边补零，保证只看过去
        h = F.relu(self.fc1(x)).transpose(1, 2)  # 换回序列在前：(B, hidden, T) → (B, T, hidden)
        return self.fc2(h)                       # 形状 (B, T, vocab_size)

    @torch.no_grad()
    def generate(
        self, idx: torch.Tensor, length: int, *, seed: int | None = None, greedy: bool = False
    ) -> torch.Tensor:
        """自回归生成：每次只取最后一个位置的分数。"""
        generator = None
        if seed is not None:
            generator = torch.Generator(device=idx.device).manual_seed(seed)
        for _ in range(length):
            logits = self(idx[:, -self.window :])
            last = logits[:, -1, :]
            if greedy:
                next_id = last.argmax(dim=-1, keepdim=True)
            else:
                next_id = torch.multinomial(
                    torch.softmax(last, dim=-1), 1, generator=generator
                )
            idx = torch.cat([idx, next_id], dim=1)
        return idx
