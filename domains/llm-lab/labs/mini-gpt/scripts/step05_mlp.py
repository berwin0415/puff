"""第 5 步：把上下文从 1 个字符扩到 8 个，中间加一层隐藏层。

第 4 步的模型只能看一个字符，损失卡在 2.46——那是 P(下一个|当前) 的信息上限，
换任何求法都压不下去。这一节第一次改结构：多看几个字符，并让它们先经过
一层非线性变换再决定下一个字符。
"""

from __future__ import annotations

import _bootstrap  # 把 src/ 加进 sys.path
import torch

from minigpt.data import PROCESSED_DIR, ensure_corpus
from minigpt.mlp_model import WindowMLP
from minigpt.tokenizer import CharTokenizer
from minigpt.training import evaluate, pick_device, train

VOCAB_PATH = PROCESSED_DIR / "vocab.json"
TRAIN_RATIO = 0.9
WINDOW = 8
D_MODEL = 64
HIDDEN = 256
STEPS = 3000
BATCH_SIZE = 512
LEARNING_RATE = 1e-3
BIGRAM_BASELINE = 2.458


def count_params(model: torch.nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


def main() -> None:
    device = pick_device()
    text = ensure_corpus().read_text(encoding="utf-8")
    tokenizer = CharTokenizer.load(VOCAB_PATH)
    vocab_size = tokenizer.vocab_size

    ids = torch.tensor(tokenizer.encode(text), dtype=torch.long)
    split = int(len(ids) * TRAIN_RATIO)
    train_ids, val_ids = ids[:split].to(device), ids[split:].to(device)

    torch.manual_seed(0)
    model = WindowMLP(vocab_size, d_model=D_MODEL, hidden=HIDDEN, window=WINDOW).to(device)

    print(f"设备           : {device}")
    print(f"上下文窗口     : {WINDOW} 个字符（第 4 步是 1 个）")
    print(f"d_model        : {D_MODEL}（每个字符被表示成多少个数字）")
    print(f"隐藏层         : {D_MODEL}×{WINDOW} → {HIDDEN} → {vocab_size}")
    print(f"参数量         : {count_params(model):,}（第 4 步是 4,225）")

    start_train = evaluate(model, train_ids, context=WINDOW)
    print(f"\n训练前         : 训练集 {start_train:.3f}")

    print(f"\n训练 {STEPS} 步:")
    train(
        model,
        train_ids,
        steps=STEPS,
        batch_size=BATCH_SIZE,
        context=WINDOW,
        learning_rate=LEARNING_RATE,
        report_every=STEPS // 6,
    )

    train_ce = evaluate(model, train_ids, context=WINDOW)
    val_ce = evaluate(model, val_ids, context=WINDOW)
    print(f"\n训练集         : {train_ce:.3f}")
    print(f"验证集         : {val_ce:.3f}")
    print(f"第 4 步基线    : 训练集 {BIGRAM_BASELINE:.3f}（只看 1 个字符的上限）")
    print(f"改进           : {BIGRAM_BASELINE - val_ce:+.3f}（验证集）")

    start = tokenizer.stoi["\n"]
    prompt = torch.tensor([[start]], device=device)
    sampled = tokenizer.decode(model.generate(prompt, 300, seed=0)[0].tolist())
    print("\n采样:")
    print(f"  {sampled!r}")


if __name__ == "__main__":
    main()
