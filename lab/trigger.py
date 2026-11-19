"""Logon trigger: one entry in the user's Startup folder.

The entry calls `catchup-all`, so it covers every registered workspace and
keeps working after the active workspace changes or one is removed. Pausing
is a per-workspace flag, not the removal of this entry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .contract import (
    LEGACY_STARTUP_ENTRY,
    LEGACY_STARTUP_ENV,
    STARTUP_ENV,
    STARTUP_ENTRY,
    env_value,
)
from .workspace import LabError

ENTRY_NAME = STARTUP_ENTRY


def _startup_dir() -> Path:
    override = env_value(STARTUP_ENV, LEGACY_STARTUP_ENV)
    if override:
        return Path(override)
    if os.name != "nt":
        raise LabError("logon trigger is implemented for Windows only (macOS: NOT_TESTED)")
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise LabError("APPDATA is not set; cannot locate the Startup folder")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _entry() -> Path:
    return _startup_dir() / ENTRY_NAME


def _launch_prefix() -> str:
    """Frozen builds launch the exe itself; source checkouts launch groma.py."""
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    launcher = Path(__file__).resolve().parents[1] / "groma.py"
    return '"%s" "%s"' % (sys.executable or "python", launcher)


def install(root: Path | None = None, python: str | None = None) -> dict:
    target = _entry()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "@echo off\r\n"
        "set PYTHONUTF8=1\r\n"
        "set GROMA_TRIGGER=startup-folder\r\n"
        '%s catchup-all >nul 2>&1\r\n' % _launch_prefix()
    )
    target.write_text(body, encoding="utf-8", newline="")
    return {"trigger": str(target), "kind": "startup-folder", "action": "catchup-all"}


def status(root: Path | None = None) -> dict:
    target = _entry()
    return {"trigger": str(target), "kind": "startup-folder", "installed": target.is_file()}


def remove(root: Path | None = None) -> dict:
    target = _entry()
    existed = target.is_file()
    if existed:
        target.unlink()
    return {"trigger": str(target), "kind": "startup-folder", "removed": existed}


def legacy_entry() -> Path:
    return _startup_dir() / LEGACY_STARTUP_ENTRY


def remove_legacy() -> bool:
    """Delete the wb-era startup entry only; the new one is installed separately."""
    target = legacy_entry()
    existed = target.is_file()
    if existed:
        target.unlink()
    return existed
