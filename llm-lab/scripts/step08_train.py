"""第 8 步：训练工程化——调度、裁剪、按验证集选模型、采样策略。"""

from __future__ import annotations

import _bootstrap  # 把 src/ 加进 sys.path
import torch

from minigpt.data import PROCESSED_DIR, ROOT, ensure_corpus
from minigpt.generation import generate
from minigpt.gpt import MiniGPT
from minigpt.tokenizer import CharTokenizer
from minigpt.training import evaluate, pick_device, train_with_schedule

VOCAB_PATH = PROCESSED_DIR / "vocab.json"
CHECKPOINT = ROOT / "checkpoints" / "minigpt.pt"
TRAIN_RATIO = 0.9
BLOCK_SIZE = 128
CONFIG = {
    "d_model": 128,
    "n_head": 4,
    "n_layer": 4,
    "block_size": BLOCK_SIZE,
    "dropout": 0.1,
}
STEPS = 6000
BATCH_SIZE = 64
BASE_LR = 8e-4
WARMUP = 300
MIN_LR = 1e-5
CLIP = 1.0
EVAL_EVERY = 500
PROMPT = "ROMEO:"


def main() -> None:
    device = pick_device()
    text = ensure_corpus().read_text(encoding="utf-8")
    tokenizer = CharTokenizer.load(VOCAB_PATH)
    vocab_size = tokenizer.vocab_size

    ids = torch.tensor(tokenizer.encode(text), dtype=torch.long)
    split = int(len(ids) * TRAIN_RATIO)
    train_ids, val_ids = ids[:split].to(device), ids[split:].to(device)

    torch.manual_seed(0)
    model = MiniGPT(vocab_size, **CONFIG).to(device)
    config = {"vocab_size": vocab_size, **CONFIG}

    print(f"设备           : {device}")
    print(f"结构           : {CONFIG['n_layer']} 层 × {CONFIG['n_head']} 头，"
          f"d_model={CONFIG['d_model']}，窗口 {BLOCK_SIZE}")
    print(f"参数量         : {sum(p.numel() for p in model.parameters()):,}")
    print(f"调度           : warmup {WARMUP} 步 → 余弦降到 {MIN_LR:g}")
    print(f"梯度裁剪       : {CLIP}")

    print(f"\n训练 {STEPS} 步:")
    best, _ = train_with_schedule(
        model,
        train_ids,
        val_ids,
        steps=STEPS,
        batch_size=BATCH_SIZE,
        context=BLOCK_SIZE,
        base_lr=BASE_LR,
        warmup=WARMUP,
        min_lr=MIN_LR,
        clip=CLIP,
        eval_every=EVAL_EVERY,
        checkpoint_path=CHECKPOINT,
        config=config,
        log_every=STEPS // 6,
    )

    print("\n最佳一步:")
    print(f"  第 {best['step']} 步   训练 {best['train']:.3f}   验证 {best['val']:.3f}")
    print(f"第 7 步基线（固定学习率）: 验证集 1.651")
    print(f"改进                      : {1.651 - best['val']:+.3f}")
    print(f"checkpoint                : {CHECKPOINT.relative_to(ROOT).as_posix()}")

    # 从 checkpoint 重新加载，确认存下来的东西真的能用
    saved = torch.load(CHECKPOINT, map_location=device, weights_only=True)
    reloaded = MiniGPT(saved["config"]["vocab_size"], **{k: saved["config"][k] for k in CONFIG})
    reloaded.load_state_dict(saved["model"])
    reloaded.to(device).eval()
    print(f"重新加载       : 第 {saved['step']} 步，验证 {saved['val_loss']:.3f}")

    prompt_ids = tokenizer.encode(PROMPT)
    prompt_tensor = torch.tensor([prompt_ids], device=device)
    print(f"\n同一个开头 {PROMPT!r}，不同采样策略:")
    variants = [
        ("贪心", {"greedy": True}),
        ("温度 0.5", {"temperature": 0.5}),
        ("温度 1.0", {"temperature": 1.0}),
        ("温度 1.5", {"temperature": 1.5}),
        ("top-k 10", {"top_k": 10}),
    ]
    for name, options in variants:
        out = generate(
            reloaded, prompt_tensor, 200, block_size=BLOCK_SIZE, seed=0, **options
        )
        text_out = tokenizer.decode(out[0].tolist())
        print(f"\n[{name}]")
        print(text_out)


if __name__ == "__main__":
    main()
