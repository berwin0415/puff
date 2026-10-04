"""文本与"模型真正看到的整数"之间的桥。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path


class CharTokenizer:
    """把语料里出现过的每个字符映射成 id，并且能反着映射回来。

    词表是**封闭的**：语料里没出现过的字符根本没有 id，也就无法表示。
    后面几步会换成子词分词器，那个性质保持不变，只是切分的单位变大了。
    """

    def __init__(self, chars: Sequence[str]) -> None:
        if len(set(chars)) != len(chars):
            raise ValueError("vocabulary contains duplicate characters")
        self.chars: tuple[str, ...] = tuple(chars)
        self.stoi: dict[str, int] = {c: i for i, c in enumerate(self.chars)}
        self.itos: dict[int, str] = {i: c for i, c in enumerate(self.chars)}

    @property
    def vocab_size(self) -> int:
        return len(self.chars)

    @classmethod
    def from_text(cls, text: str) -> CharTokenizer:
        # 排序让映射变得确定：同一份语料永远得到同一套 id。不排序的话，
        # 只要语料被以另一种顺序读进来，之前存下的 checkpoint 立刻失去意义。
        return cls(sorted(set(text)))

    def encode(self, text: str) -> list[int]:
        try:
            return [self.stoi[c] for c in text]
        except KeyError as error:
            raise ValueError(f"character {error.args[0]!r} is not in the vocabulary") from None

    def decode(self, ids: Iterable[int]) -> str:
        return "".join(self.itos[i] for i in ids)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"chars": list(self.chars)}, ensure_ascii=False, indent=2)
        # Windows 上必须显式写 newline="\n"：文本模式会把每个 "\n" 改写成
        # "\r\n"，破坏仓库统一的 LF 规则。
        path.write_text(payload + "\n", encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: Path) -> CharTokenizer:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(payload["chars"])

    def __repr__(self) -> str:
        return f"CharTokenizer(vocab_size={self.vocab_size})"
