"""Workspace preview for harnesses without a database.

Reads at most the first 20 lines of each session file to find `cwd`. Nothing
else is read, and nothing is stored. Qoder does not need this: its database
already knows the workspace for every session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .state import normalise_path

MAX_FILES = 2000
MAX_LINES = 20
EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".groma", ".wb"}


def _first_cwd(path: Path) -> str | None:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for index, line in enumerate(handle):
            if index >= MAX_LINES:
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                value = record.get("cwd")
                if not (isinstance(value, str) and value.strip()):
                    payload = record.get("payload")
                    if isinstance(payload, dict):
                        value = payload.get("cwd")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def iter_session_files(base: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]
        for name in filenames:
            if not name.lower().endswith((".jsonl", ".ndjson")):
                continue
            yield Path(dirpath) / name
            count += 1
            if count >= MAX_FILES:
                return


def cwd_counts(base: Path, workspace: str | list[str]) -> dict:
    """Count sessions by whether their cwd sits inside any of the given roots.

    `workspace` accepts a single path or a list (a Unit's member workspaces).
    `by_workspace` reports the per-root inside tally so the UI can show coverage.
    """
    roots = [workspace] if isinstance(workspace, str) else list(workspace)
    norm = [normalise_path(r) for r in roots]
    inside = outside = no_cwd = 0
    by_workspace = {r: 0 for r in norm}
    for path in iter_session_files(Path(base)):
        cwd = _first_cwd(path)
        if not cwd:
            no_cwd += 1
            continue
        current = normalise_path(cwd)
        match = next((r for r in norm if current == r or current.startswith(r + "/")), None)
        if match:
            inside += 1
            by_workspace[match] += 1
        else:
            outside += 1
    return {"inside": inside, "outside": outside, "no_cwd": no_cwd, "by_workspace": by_workspace}
