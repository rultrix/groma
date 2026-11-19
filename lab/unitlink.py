"""Stable Unit identity, decoupled from where the workspace lives.

`.groma/unit.json` is machine-local metadata. `groma-unit.json` sits at the workspace
root so it can be committed: a clone on another machine keeps the same unit_id
and org_path, while the local path stays only in this machine's state registry.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import gitutil
from .contract import LEGACY_LINK_FILE, LINK_FILE
SCHEMA = 1


def link_path(root: str | Path) -> Path:
    """Where a card is (canonical first, legacy fallback for an un-migrated repo)."""
    root = Path(root)
    canonical = root / LINK_FILE
    legacy = root / LEGACY_LINK_FILE
    if canonical.is_file() or not legacy.is_file():
        return canonical
    return legacy


def _write_path(root: str | Path) -> Path:
    """Never mint a second card next to a legacy one; migrate moves it."""
    return link_path(root)


def read_link(root: str | Path) -> dict | None:
    path = link_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("unit_id"):
        return None
    return data


def normalise_org_path(value: str) -> str:
    """Accept a URL, ssh remote or host/owner/repo; return host/owner/repo."""
    text = (value or "").strip()
    if not text:
        raise ValueError("org path must not be empty")
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
    if text.startswith("git@"):
        text = text[4:].replace(":", "/", 1)
    text = re.sub(r"^[^@/]+@", "", text)
    parts = [part for part in text.split("/") if part]
    if len(parts) == 2:
        parts = ["github.com"] + parts
    if len(parts) < 3:
        raise ValueError("org path must look like host/owner/repo: %s" % value)
    parts[0] = parts[0].lower()
    return "/".join(parts)


def derive_org_path(root: str | Path) -> str | None:
    """Best-effort: read origin without a shell or a console window; accept only remote URLs."""
    result = gitutil.run(["remote", "get-url", "origin"], cwd=root)
    if result is None or result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw or Path(raw).exists() or raw.startswith((".", "/", "\\")):
        return None
    try:
        return normalise_org_path(raw)
    except ValueError:
        return None


def _write_link(root: str | Path, payload: dict) -> dict:
    path = _write_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(tmp, path)
    return payload


def ensure_link(root: str | Path, unit: dict) -> dict:
    """Create the committed identity file once; an existing file wins."""
    existing = read_link(root)
    if existing is not None:
        return existing
    payload = {
        "schema": SCHEMA,
        "unit_id": unit["unit_id"],
        "name": unit.get("name") or Path(root).name,
        "org_path": derive_org_path(root),
        "created_at": unit.get("created_at"),
        "note": "commit this file; clones on other machines resolve to the same Unit",
    }
    return _write_link(root, payload)


def adopt_link(root: str | Path, identity: dict) -> dict:
    """Point this folder at the Unit a human chose for it.

    Two callers: when a folder is opened onto a Unit the human joined it into, and
    from `unitmatch._refresh_cards` when the Unit's own name or org path changes —
    there it only reaches the home and folders that already carry a card and
    resolve to the same repository. An existing `created_at` is kept either way,
    so a rewritten card is not the same event as a newly minted one.
    """
    path = link_path(root)
    existing = read_link(root) or {}
    payload = {
        "schema": SCHEMA,
        "unit_id": identity["unit_id"],
        "name": identity.get("name") or existing.get("name") or Path(root).name,
        "org_path": identity.get("org_path") or existing.get("org_path") or derive_org_path(root),
        "created_at": existing.get("created_at") or identity.get("created_at"),
        "note": existing.get("note")
        or "commit this file; clones on other machines resolve to the same Unit",
    }
    return _write_link(root, payload)


def set_org_path(root: str | Path, value: str) -> dict:
    link = read_link(root)
    if link is None:
        raise ValueError("workspace has no %s; initialise it first" % LINK_FILE)
    link["org_path"] = normalise_org_path(value)
    return _write_link(root, link)


def clear_link(root: str | Path) -> bool:
    """Delete every card this workspace carries; False when there was none.

    Idempotent on purpose: 刀 W10 removes a workspace in one pass, and a folder
    that never had a card must not turn that pass into an error. Both names are
    removed so a half-migrated folder cannot resurrect the Unit through the old
    file.
    """
    removed = False
    for name in (LINK_FILE, LEGACY_LINK_FILE):
        path = Path(root) / name
        if path.is_file():
            path.unlink()
            removed = True
    return removed
