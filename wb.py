#!/usr/bin/env python3
"""Deprecated launcher name; use `groma.py`. Kept for one release so old
scheduled tasks and notes keep working until `groma migrate` rewrites them.
"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab.cli import main  # noqa: E402  (path setup must happen first)

if __name__ == "__main__":
    raise SystemExit(main())
