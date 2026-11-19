#!/usr/bin/env python3
"""Launcher that works from any working directory.

    python C:\\path\\to\\local-lab\\groma.py --root <dir> <command> ...

`python -m lab` still works, but only from the repository root.
"""

import sys
from pathlib import Path

# This tool promises not to modify a repository it inspects; bytecode caches would.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab.cli import main  # noqa: E402  (path setup must happen first)

if __name__ == "__main__":
    raise SystemExit(main())
