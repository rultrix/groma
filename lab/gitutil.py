"""Short-lived git probes that never pop a console window.

The desktop app runs as a windowed exe (no console). A bare subprocess.run of
`git` on Windows still allocates a console window that flashes then disappears.
Passing CREATE_NO_WINDOW on win32 stops that. Non-git helpers reuse the flags.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def no_window() -> dict:
    """Extra kwargs so a spawned process does not flash a console on Windows."""
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run(args: list[str], cwd: str | Path | None = None, timeout: int = 3) -> subprocess.CompletedProcess | None:
    """Run ``git -C <cwd> <args>`` read-only; return the result or None on any failure."""
    cmd = ["git"] + (["-C", str(cwd)] if cwd is not None else []) + list(args)
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False, **no_window()
        )
    except (OSError, subprocess.SubprocessError):
        return None
