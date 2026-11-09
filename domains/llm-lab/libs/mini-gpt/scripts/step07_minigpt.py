"""第 7 步：把位置编码、残差、LayerNorm、前馈网络、多头注意力拼成 mini-GPT。"""

from __future__ import annotations

import _bootstrap  # 把 src/ 加进 sys.path
import torch

from minigpt.data import PROCESSED_DIR, ensure_corpus
from minigpt.generation import generate
from minigpt.gpt import MiniGPT
from minigpt.tokenizer import CharTokenizer
from minigpt.training import evaluate, pick_device, train

VOCAB_PATH = PROCESSED_DIR / "vocab.json"
TRAIN_RATIO = 0.9
BLOCK_SIZE = 128
D_MODEL = 128
N_HEAD = 4
N_LAYER = 4
STEPS = 5000
BATCH_SIZE = 64
LEARNING_RATE = 3e-4


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
    model = MiniGPT(
        vocab_size,
        d_model=D_MODEL,
        n_head=N_HEAD,
        n_layer=N_LAYER,
        block_size=BLOCK_SIZE,
        dropout=0.1,
    ).to(device)

    print(f"设备           : {device}")
    print(f"结构           : {N_LAYER} 层 × {N_HEAD} 头，d_model={D_MODEL}，窗口 {BLOCK_SIZE}")
    print(f"参数量         : {count_params(model):,}")
    print(f"训练前         : 训练集 {evaluate(model, train_ids, context=BLOCK_SIZE):.3f}")

    print(f"\n训练 {STEPS} 步（每步 {BATCH_SIZE}×{BLOCK_SIZE} = {BATCH_SIZE * BLOCK_SIZE:,} 个 token）:")
    train(
        model,
        train_ids,
        steps=STEPS,
        batch_size=BATCH_SIZE,
        context=BLOCK_SIZE,
        learning_rate=LEARNING_RATE,
        report_every=STEPS // 5,
    )

    train_ce = evaluate(model, train_ids, context=BLOCK_SIZE)
    val_ce = evaluate(model, val_ids, context=BLOCK_SIZE)
    print(f"\n训练集         : {train_ce:.3f}")
    print(f"验证集         : {val_ce:.3f}")
    print("第 5 步基线    : 验证集 1.888")
    print(f"改进           : {1.888 - val_ce:+.3f}")

    prompt = torch.tensor([[tokenizer.stoi["\n"]]], device=device)
    sampled = generate(model, prompt, 500, block_size=BLOCK_SIZE, seed=0)
    print("\n采样:")
    print(tokenizer.decode(sampled[0].tolist()))


if __name__ == "__main__":
    main()
