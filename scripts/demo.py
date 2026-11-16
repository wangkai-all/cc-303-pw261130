#!/usr/bin/env python3
"""构建并运行两版规则 × 两种回测口径的演示报告。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risklab.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["demo", *sys.argv[1:]]))
