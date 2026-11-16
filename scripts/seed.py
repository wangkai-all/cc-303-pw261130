#!/usr/bin/env python3
"""一键构建演示数据库：虚构数据 + 两种回测模式 × 两版规则。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risklab.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["init", "--seed", *sys.argv[1:]]))
