"""第 4 步：把第 3 步的计数表换成一张学出来的表。

模型形状没变（还是 65×65），数据、损失、采样三个环节也没变，唯一换掉的是
"表里的数字从哪来"——从数出来变成学出来。所以这一步能直接和第 3 步对账，
这也是验证"梯度下降真的在工作"最干脆的方式。
"""

from __future__ import annotations

import math

import _bootstrap  # 把 src/ 加进 sys.path
import torch

from minigpt.bigram import BigramCounts
from minigpt.bigram_model import BigramLM
from minigpt.data import PROCESSED_DIR, ensure_corpus
from minigpt.tokenizer import CharTokenizer
from minigpt.training import evaluate, pick_device, train

VOCAB_PATH = PROCESSED_DIR / "vocab.json"
TRAIN_RATIO = 0.9
STEPS = 2000
BATCH_SIZE = 4096
CONTEXT = 1
LEARNING_RATE = 0.05


def main() -> None:
    device = pick_device()
    corpus = ensure_corpus()
    text = corpus.read_text(encoding="utf-8")
    tokenizer = CharTokenizer.load(VOCAB_PATH)
    vocab_size = tokenizer.vocab_size

    ids = torch.tensor(tokenizer.encode(text), dtype=torch.long)
    split = int(len(ids) * TRAIN_RATIO)
    train_ids, val_ids = ids[:split].to(device), ids[split:].to(device)

    print(f"设备           : {device}")
    print(f"tokens         : {len(ids):,}（训练 {len(train_ids):,} / 验证 {len(val_ids):,}）")
    print(f"参数量         : {vocab_size} × {vocab_size} = {vocab_size ** 2:,}（和第 3 步计数表的格子数一样）")

    torch.manual_seed(0)
    model = BigramLM(vocab_size).to(device)
    start_loss = evaluate(model, train_ids, context=CONTEXT)
    uniform = math.log(vocab_size)
    print(f"\n训练前         : 交叉熵 {start_loss:.4f}")
    print(f"                 均匀瞎猜的理论值是 ln({vocab_size}) = {uniform:.4f}"
          f"（困惑度 {math.exp(uniform):.1f}——和第 3 步的基线对上了）")
    print("                 比理论值还差一点：随机初始化的表并不是均匀分布，而是参差不齐的")

    print(f"\n训练（前向 → 算损失 → 反向 → 更新，共 {STEPS} 步）:")
    train(
        model,
        train_ids,
        steps=STEPS,
        batch_size=BATCH_SIZE,
        context=CONTEXT,
        learning_rate=LEARNING_RATE,
        report_every=STEPS // 4,
    )

    train_ce = evaluate(model, train_ids, context=CONTEXT)
    val_ce = evaluate(model, val_ids, context=CONTEXT)

    counted = BigramCounts(vocab_size, smoothing=1.0)
    counted.fit(train_ids.tolist())
    counted_ce = counted.cross_entropy(val_ids.tolist())

    print("\n和第 3 步对账:")
    print(f"  数出来的（加 1 平滑）  训练集 ——       验证集 {counted_ce:.3f}")
    print(f"  学出来的              训练集 {train_ce:.3f}   验证集 {val_ce:.3f}")
    print(f"  未平滑的计数版验证集是 inf（概率 0 导致），"
          f"学出来的这里给出 {val_ce:.3f} 而不是 inf——因为 softmax 永远给非零概率")

    learned = model.probabilities().cpu()
    counted_probs = counted.probabilities()
    agree = (learned.argmax(dim=1) == counted_probs.argmax(dim=1)).float().mean().item()
    print(f"\n逐行比较最大概率的选择：{agree:.1%} 的行一致"
          f"（一共 {vocab_size} 行）")

    no_data = int((counted.counts.sum(dim=1) == 0).sum())
    if no_data:
        print(f"  其中 {no_data} 行在训练集里从没作为「当前字符」出现过，计数版只能退回均匀分布")

    start = tokenizer.stoi["\n"]
    prompt = torch.tensor([[start]], device=device)
    sampled = model.generate(prompt, 200, seed=0)
    greedy = model.generate(prompt, 60, greedy=True)
    print("\n学出来的模型采样:")
    print(f"  {tokenizer.decode(sampled[0].tolist())!r}")
    print("\n贪心（每步取最大）：")
    print(f"  {tokenizer.decode(greedy[0].tolist())!r}")


if __name__ == "__main__":
    main()
