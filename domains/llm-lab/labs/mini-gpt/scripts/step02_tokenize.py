"""第 2 步：把文本变成整数，再变回来，中间不丢任何东西。

模型永远看不到字符。它接触到的一切都是整数 id，而中间那层映射在这里一次性
定下、然后冻结——因为 checkpoint 只有配上它训练时用的那份词表才有意义。
"""

from __future__ import annotations

import _bootstrap  # 把 src/ 加进 sys.path
from minigpt.data import PROCESSED_DIR, ROOT, ensure_corpus
from minigpt.tokenizer import CharTokenizer

VOCAB_PATH = PROCESSED_DIR / "vocab.json"
SAMPLE = "First Citizen:\nBefore we proceed"


def main() -> None:
    corpus = ensure_corpus()
    text = corpus.read_text(encoding="utf-8")
    tokenizer = CharTokenizer.from_text(text)

    print(f"corpus         : {corpus.relative_to(ROOT).as_posix()}")
    print(f"corpus size    : {len(text):,} characters")
    print(f"vocab size     : {tokenizer.vocab_size}")
    print(f"vocab          : {''.join(tokenizer.chars)!r}")

    ids = tokenizer.encode(text)
    print(f"token count    : {len(ids):,} ({len(text) / len(ids):.2f} chars/token)")

    sample_ids = tokenizer.encode(SAMPLE)
    print(f"\nencode({SAMPLE!r})")
    print(f"  ids          : {sample_ids}")
    print(f"  decode       : {tokenizer.decode(sample_ids)!r}")

    text_ok = tokenizer.decode(tokenizer.encode(text)) == text
    ids_ok = tokenizer.encode(tokenizer.decode(ids)) == ids
    print(f"\nround trip     : decode(encode(text)) == text -> {text_ok}")
    print(f"                 encode(decode(ids))  == ids  -> {ids_ok}")

    tokenizer.save(VOCAB_PATH)
    reloaded = CharTokenizer.load(VOCAB_PATH)
    saved = VOCAB_PATH.relative_to(ROOT).as_posix()
    print(f"vocab saved    : {saved} ({VOCAB_PATH.stat().st_size} bytes)")
    print(f"reload matches : {reloaded.encode(text) == ids}")

    print(f"\ndigits present : {[c for c in tokenizer.chars if c.isdigit()]}")

    print("\ncharacters outside the vocabulary have no id at all:")
    for probe in "中文🙂":
        try:
            tokenizer.encode(probe)
        except ValueError as error:
            print(f"  {error}")


if __name__ == "__main__":
    main()
