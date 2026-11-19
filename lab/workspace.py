"""Local workspace primitives: one unit, declared agents, no network."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import state, unitlink
from .contract import LEGACY_META_DIR, META_DIR
UNIT_FILE = "unit.json"
AGENTS_FILE = "agents.json"
RECEIPTS_DIR = "receipts"


class LabError(Exception):
    """A user-facing failure. Never carries secrets."""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def root_path(value: str | os.PathLike[str], create: bool = False) -> Path:
    """Resolve --root. Only `init` may create it; every other command requires it."""
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        if not create:
            raise LabError("workspace root does not exist: %s" % root)
        root.mkdir(parents=True, exist_ok=True)
    return root


def meta_dir(root: Path) -> Path:
    """Canonical `.groma`, unless this folder is still carrying a legacy `.wb`.

    Falling back to the old directory keeps a pre-migration workspace writable
    as one store; `groma migrate` is what moves it, not an implicit write split.
    """
    canonical = root / META_DIR
    legacy = root / LEGACY_META_DIR
    if canonical.is_dir() or not legacy.is_dir():
        return canonical
    return legacy


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict) -> None:
    """Atomic write with LF endings so receipts survive a platform change."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def require_init(root: Path) -> None:
    if not (meta_dir(root) / UNIT_FILE).is_file():
        raise LabError(
            "not a groma workspace at %s; run `groma.py --root <dir> init` first "
            "(or `python -m lab init` from the repo root)" % meta_dir(root)
        )


def init_workspace(root: Path, name: str | None = None, owner: str | None = None) -> dict:
    """Create the local metadata; an existing `groma-unit.json` decides the identity.

    That is what makes opening a joined member safe: `unitmatch` writes the Unit's
    identity card first, and this folder inherits it instead of minting a new one.
    """
    unit_path = meta_dir(root) / UNIT_FILE
    if unit_path.exists():
        raise LabError("workspace already initialised at %s" % meta_dir(root))
    existing_link = unitlink.read_link(root)
    now = utc_stamp()
    unit = {
        "schema": 1,
        "unit_id": existing_link["unit_id"] if existing_link else "unit_" + secrets.token_hex(8),
        "name": name or (existing_link or {}).get("name") or root.name,
        "owner": owner or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        "workspace_root": root.as_posix(),
        "created_at": now,
        "carrier": "local-directory",
    }
    write_json(unit_path, unit)
    unitlink.ensure_link(root, unit)
    write_json(meta_dir(root) / AGENTS_FILE, {"schema": 1, "agents": []})
    (meta_dir(root) / RECEIPTS_DIR).mkdir(parents=True, exist_ok=True)
    # A workspace Groma knows about is a workspace the collector can sweep: register
    # it now, so Unit resolution never depends on someone having clicked "open".
    state.register_workspace(root, unit.get("name") or root.name)
    return unit


def load_unit(root: Path) -> dict:
    require_init(root)
    unit = read_json(meta_dir(root) / UNIT_FILE)
    link = unitlink.read_link(root)
    if link is not None:
        unit["unit_id"] = link["unit_id"]
        unit["org_path"] = link.get("org_path")
        unit["identity_file"] = unitlink.link_path(root).name
    else:
        unit.setdefault("org_path", None)
    return unit


def ensure_unit(root: Path, name: str | None = None) -> dict:
    """Choosing a workspace is the same act as creating its unit."""
    if (meta_dir(root) / UNIT_FILE).is_file():
        return load_unit(root)
    return init_workspace(root, name=name)


def load_agents(root: Path) -> list[dict]:
    require_init(root)
    return read_json(meta_dir(root) / AGENTS_FILE).get("agents", [])


def attach_agent(root: Path, name: str, harness: str | None = None) -> dict:
    if not name or not name.strip():
        raise LabError("agent name must not be empty")
    require_init(root)
    path = meta_dir(root) / AGENTS_FILE
    payload = read_json(path)
    agents = payload.setdefault("agents", [])
    if any(agent.get("name") == name for agent in agents):
        raise LabError("agent already attached: %s" % name)
    record = {"name": name, "harness": harness or "unknown", "attached_at": utc_stamp()}
    agents.append(record)
    agents.sort(key=lambda item: item["name"])
    write_json(path, payload)
    return record
