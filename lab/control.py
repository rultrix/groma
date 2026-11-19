"""Pause and resume: one master switch for the machine, plus per-workspace flags.

The UI and the tray now expose a single switch, because "are we collecting?"
used to have as many answers as there were workspaces and the top bar could
only show one of them. The per-workspace flag stays as the lower layer: it is
what `catchup-all` skips on, and what a test can set for one folder without
silencing the rest.
"""

from __future__ import annotations

from pathlib import Path

from .state import state_root
from .workspace import meta_dir, read_json, utc_stamp, write_json

STATE_FILE = "state.json"
MASTER_FILE = "pause.json"


def _master_path() -> Path:
    return state_root() / MASTER_FILE


def load_state(root: Path) -> dict:
    path = meta_dir(root) / STATE_FILE
    return read_json(path) if path.is_file() else {}


def machine_state() -> dict:
    path = _master_path()
    data = read_json(path) if path.is_file() else {}
    return {"scope": "machine", "paused": bool(data.get("paused")), "updated_at": data.get("updated_at")}


def is_paused(root: Path) -> bool:
    return machine_state()["paused"] or bool(load_state(root).get("paused"))


def _write(root: Path, paused: bool) -> dict:
    payload = {"paused": paused, "updated_at": utc_stamp()}
    write_json(meta_dir(root) / STATE_FILE, payload)
    return payload


def _write_master(paused: bool) -> dict:
    payload = {"paused": paused, "updated_at": utc_stamp()}
    write_json(_master_path(), payload)
    return {"scope": "machine", **payload}


def pause(root: Path) -> dict:
    return _write(root, True)


def resume(root: Path) -> dict:
    return _write(root, False)


def pause_machine() -> dict:
    return _write_master(True)


def resume_machine() -> dict:
    return _write_master(False)
