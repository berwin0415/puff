"""第 3 步：最简语言模型——靠数数预测下一个字符。

这里没有梯度意义上的"学习"：fit() 只是对字符对做算术。这正是重点——
它把"语言模型是什么"（下一个 token 的概率分布）与"它怎么被训练"分开。
"""

from __future__ import annotations

import _bootstrap  # 把 src/ 加进 sys.path
import torch

from minigpt.bigram import BigramCounts
from minigpt.data import PROCESSED_DIR, ensure_corpus
from minigpt.tokenizer import CharTokenizer

VOCAB_PATH = PROCESSED_DIR / "vocab.json"
SAMPLE_LENGTH = 200
TRAIN_RATIO = 0.9


def load_ids() -> tuple[list[int], CharTokenizer]:
    corpus = ensure_corpus()
    text = corpus.read_text(encoding="utf-8")
    # 词表是加载的，不是重建的：这个映射在第 2 步就定下了，
    # 从今往后每一步都必须用同一套。
    tokenizer = CharTokenizer.load(VOCAB_PATH)
    return tokenizer.encode(text), tokenizer


def show_log_cost_intuition() -> None:
    print("先看看 -log(p) 长什么样（单位是 nats，即自然对数）:")
    for p in (1.0, 0.5, 0.1, 0.01):
        cost = float(-torch.log(torch.tensor(p))) + 0.0  # 加 0.0 是为了把 -0.0 变成 0.0
        print(f"  模型给正确答案的概率 p = {p:<5} -> 损失 {cost:.2f}")
    print("  p 越接近 0，惩罚增长得越快；p = 0 时损失是无穷大\n")


def main() -> None:
    ids, tokenizer = load_ids()
    vocab_size = tokenizer.vocab_size
    split = int(len(ids) * TRAIN_RATIO)
    train_ids, val_ids = ids[:split], ids[split:]

    print(f"tokens         : {len(ids):,}（训练 {len(train_ids):,} / 验证 {len(val_ids):,}）")
    print(f"vocab size     : {vocab_size}")

    model = BigramCounts(vocab_size)
    model.fit(train_ids)

    filled = int((model.counts > 0).sum())
    total = vocab_size * vocab_size
    print(f"计数表         : {vocab_size}×{vocab_size} = {total:,} 个格子，填了 {filled:,} 个"
          f"（{filled / total:.0%}）")

    space = tokenizer.stoi[" "]
    top = torch.topk(model.next_distribution(space), 3)
    readable = ", ".join(
        f"{tokenizer.decode([int(i)])!r} {p:.3f}" for p, i in zip(top.values, top.indices)
    )
    print(f"空格后面最常接 : {readable}\n")

    train_ce = model.cross_entropy(train_ids)
    uniform_ppl = float(vocab_size)
    print(f"训练集交叉熵   : {train_ce:.3f} nats")
    print(f"训练集困惑度   : {model.perplexity(train_ids):.1f}"
          f"    （瞎猜的困惑度是 {uniform_ppl:.1f}，即词表大小）\n")

    start = tokenizer.stoi["\n"]
    sampled = tokenizer.decode(model.sample(start, SAMPLE_LENGTH, seed=0))
    greedy = tokenizer.decode(model.most_likely(start, SAMPLE_LENGTH))
    print("采样（按概率抽，每次结果都不同）:")
    print(f"  {sampled!r}\n")
    print("贪心（每步都取概率最大的那个）:")
    print(f"  {greedy!r}\n")

    print("把没见过的组合拿去验证:")
    print(f"  未平滑的验证集交叉熵 = {model.cross_entropy(val_ids)}")

    smoothed = BigramCounts(vocab_size, smoothing=1.0)
    smoothed.fit(train_ids)
    print(f"  加 1 平滑后           = {smoothed.cross_entropy(val_ids):.3f} nats"
          f"（困惑度 {smoothed.perplexity(val_ids):.1f}）")
    print(f"  代价：训练集从 {train_ce:.3f} 涨到 {smoothed.cross_entropy(train_ids):.3f}"
          "——平滑是拿训练集上的自信去换对未知的容忍")


if __name__ == "__main__":
    show_log_cost_intuition()
    main()
