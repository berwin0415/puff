"""最简语言模型：一张"下一个字符出现次数"的表。

没有神经网络，没有梯度，只有数数。它是后面每一步都必须超越的基准，
也是"交叉熵、困惑度、采样"这些训练术语获得含义的地方。
"""

from __future__ import annotations

import torch


class BigramCounts:
    """用统计相邻字符对的方式估计 P(下一个 | 当前)。

    `smoothing` 会在归一化之前加到每个计数上。它的作用是让"从没见过的组合"
    不至于拿到恰好为 0 的概率——为什么这件事比看上去重要得多，见讲解。
    """

    def __init__(self, vocab_size: int, smoothing: float = 0.0) -> None:
        self.vocab_size = vocab_size
        self.smoothing = smoothing
        self.counts = torch.zeros(vocab_size, vocab_size, dtype=torch.float64)

    def fit(self, ids: list[int]) -> None:
        """统计序列里每一对相邻字符出现的次数。"""
        tokens = torch.tensor(ids, dtype=torch.long)
        current, following = tokens[:-1], tokens[1:]
        flat = current * self.vocab_size + following
        total = self.vocab_size * self.vocab_size
        self.counts = torch.bincount(flat, minlength=total).reshape(
            self.vocab_size, self.vocab_size
        ).to(torch.float64)

    def probabilities(self) -> torch.Tensor:
        """把计数按行归一化：第 i 行就是"当前字符是 i 时，下一个字符的分布"。"""
        counts = self.counts + self.smoothing
        totals = counts.sum(dim=1, keepdim=True)
        safe_totals = totals.clamp(min=1e-12)
        probs = counts / safe_totals
        # 从没作为"当前字符"出现过的行不带任何信息；用均匀分布兜底，别留下 NaN。
        empty = totals.squeeze(1) == 0
        probs[empty] = 1.0 / self.vocab_size
        return probs

    def cross_entropy(self, ids: list[int]) -> float:
        """平均每个位置的 -log P(真实的下一个字符)，单位 nats。越小越好。"""
        tokens = torch.tensor(ids, dtype=torch.long)
        current, following = tokens[:-1], tokens[1:]
        probs = self.probabilities()
        return float(-torch.log(probs[current, following]).mean())

    def perplexity(self, ids: list[int]) -> float:
        """exp(交叉熵)。大致读作"模型在多少个候选之间犹豫"。"""
        return float(torch.exp(torch.tensor(self.cross_entropy(ids))))

    def next_distribution(self, current: int) -> torch.Tensor:
        return self.probabilities()[current]

    def sample(self, start: int, length: int, seed: int = 0) -> list[int]:
        """每一步都从概率分布里随机抽一个。"""
        generator = torch.Generator().manual_seed(seed)
        probs = self.probabilities()
        out = [start]
        current = start
        for _ in range(length):
            current = int(torch.multinomial(probs[current], 1, generator=generator))
            out.append(current)
        return out

    def most_likely(self, start: int, length: int) -> list[int]:
        """每一步都取概率最大的那个字符。"""
        probs = self.probabilities()
        out = [start]
        current = start
        for _ in range(length):
            current = int(torch.argmax(probs[current]))
            out.append(current)
        return out
