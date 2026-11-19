"""Groma: a local-first receipts and observability runtime for AI coding agents.

No network, no credentials, no third-party dependencies.
"""

from pathlib import Path
import sys

FALLBACK_VERSION = "0.32.0"


def _read_version() -> str:
    candidates = []
    here = Path(__file__).resolve()
    candidates.extend([here.parent, here.parent.parent])
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    for base in candidates:
        version_file = base / "VERSION"
        try:
            if version_file.is_file():
                value = version_file.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            pass
    return FALLBACK_VERSION


__all__ = ["__version__"]
__version__ = _read_version()
