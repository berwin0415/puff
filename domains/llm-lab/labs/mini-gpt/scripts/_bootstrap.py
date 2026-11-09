"""给脚本准备好运行环境，做两件事。

1. 直接跑 `python scripts/foo.py` 时，`sys.path` 里是 `scripts/` 而不是 `src/`。
   先导入本模块就能把 `src/` 加进去，于是 `import minigpt` 才找得到。
2. Windows 控制台默认走 ANSI 代码页（本机是 GBK），打不出 `中` 或 emoji 这类
   字符。这里强制输出走 UTF-8，让打印出来的东西与数据本身一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
