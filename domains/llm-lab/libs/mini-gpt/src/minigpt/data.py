"""语料的下载与缓存。

语料只下载一次，放在 `data/raw/`，之后一直复用。那个目录被 gitignore 了，
所以重新克隆时是重新下载，而不是把别人的数据抄一份带在仓库里。
"""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"

CORPUS_NAME = "tinyshakespeare"
CORPUS_URLS = (
    "https://cdn.jsdelivr.net/gh/karpathy/char-rnn@master/data/tinyshakespeare/input.txt",
    "https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt",
)


def corpus_path(name: str = CORPUS_NAME) -> Path:
    return RAW_DIR / f"{name}.txt"


def ensure_corpus(name: str = CORPUS_NAME, urls: tuple[str, ...] = CORPUS_URLS) -> Path:
    """返回本地语料路径；如果还没有，就下载一次。"""
    dest = corpus_path(name)
    if dest.exists():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=60) as response, dest.open("wb") as out:
                shutil.copyfileobj(response, out)
        except (urllib.error.URLError, OSError) as error:
            print(f"  download failed: {url} ({error})")
            continue
        print(f"  downloaded {dest.name} from {url}")
        return dest

    raise RuntimeError(f"could not download {name!r}; tried {len(urls)} sources")
