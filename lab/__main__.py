"""Entry point: python -m lab ..."""

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
