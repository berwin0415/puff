"""第 6 步：让模型自己决定该重点看前面哪些字符。

第 5 步把所有历史位置**固定地**拼在一起（或等同地，给它们固定权重）。
这一步先做一个"取平均"的对照，再换成 Q/K/V——权重不再固定，而是由内容算出来。
"""

from __future__ import annotations

import _bootstrap  # 把 src/ 加进 sys.path
import torch

from minigpt.attention_model import MeanPoolLM, SingleHeadLM
from minigpt.data import PROCESSED_DIR, ensure_corpus
from minigpt.generation import generate
from minigpt.tokenizer import CharTokenizer
from minigpt.training import evaluate, pick_device, train

VOCAB_PATH = PROCESSED_DIR / "vocab.json"
TRAIN_RATIO = 0.9
BLOCK_SIZE = 32
D_MODEL = 64
STEPS = 3000
BATCH_SIZE = 128
LEARNING_RATE = 1e-3


def count_params(model: torch.nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


def run(model: torch.nn.Module, name: str, train_ids, val_ids) -> tuple[float, float]:
    print(f"\n=== {name} ===")
    print(f"参数量         : {count_params(model):,}")
    train(
        model,
        train_ids,
        steps=STEPS,
        batch_size=BATCH_SIZE,
        context=BLOCK_SIZE,
        learning_rate=LEARNING_RATE,
        report_every=STEPS // 3,
    )
    train_ce = evaluate(model, train_ids, context=BLOCK_SIZE)
    val_ce = evaluate(model, val_ids, context=BLOCK_SIZE)
    print(f"训练集 {train_ce:.3f}   验证集 {val_ce:.3f}")
    return train_ce, val_ce


def show_attention_map(model: SingleHeadLM, tokenizer: CharTokenizer, device: torch.device) -> None:
    """看一眼真实文本里，最后一个字符把注意力放在了哪儿。"""
    snippet = "thou art my"
    ids = torch.tensor([tokenizer.encode(snippet)], device=device)
    weights = model.attention(ids)[0, -1].tolist()
    print(f"\n输入 {snippet!r} 时，最后一个字符 {snippet[-1]!r} 看向前面各位置的权重:")
    for char, weight in sorted(zip(snippet, weights), key=lambda pair: -pair[1])[:5]:
        print(f"  {char!r}  {weight:.3f}")


def main() -> None:
    device = pick_device()
    text = ensure_corpus().read_text(encoding="utf-8")
    tokenizer = CharTokenizer.load(VOCAB_PATH)
    vocab_size = tokenizer.vocab_size

    ids = torch.tensor(tokenizer.encode(text), dtype=torch.long)
    split = int(len(ids) * TRAIN_RATIO)
    train_ids, val_ids = ids[:split].to(device), ids[split:].to(device)

    print(f"设备           : {device}")
    print(f"上下文窗口     : {BLOCK_SIZE} 个字符")
    print(f"d_model        : {D_MODEL}")

    torch.manual_seed(0)
    mean_model = MeanPoolLM(vocab_size, d_model=D_MODEL).to(device)
    mean_tr, mean_val = run(mean_model, "对照组：取平均", train_ids, val_ids)

    torch.manual_seed(0)
    attn_model = SingleHeadLM(vocab_size, d_model=D_MODEL, block_size=BLOCK_SIZE).to(device)
    attn_tr, attn_val = run(attn_model, "注意力：Q/K/V", train_ids, val_ids)

    print("\n=== 对比 ===")
    print(f"取平均  训练 {mean_tr:.3f}   验证 {mean_val:.3f}")
    print(f"注意力  训练 {attn_tr:.3f}   验证 {attn_val:.3f}")
    print(f"验证集改进 {mean_val - attn_val:+.3f}")

    show_attention_map(attn_model, tokenizer, device)

    prompt = torch.tensor([[tokenizer.stoi["\n"]]], device=device)
    text_out = generate(attn_model, prompt, 300, block_size=BLOCK_SIZE, seed=0)
    print("\n注意力模型采样:")
    print(f"  {tokenizer.decode(text_out[0].tolist())!r}")


if __name__ == "__main__":
    main()
