"""Global state: identity and paths only. Evidence lives in each workspace.

A single agent must keep one id across workspaces, so identities cannot live
inside a workspace's .groma/. Workspace paths never leave this machine; the
public handle is a sha256 prefix (ws_ref), matching the package's rule.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unicodedata
from pathlib import Path, PurePosixPath

from . import unitlink
from .contract import (
    LEGACY_META_DIR,
    LEGACY_STATE_ENV,
    LEGACY_STATE_NAME,
    META_DIR,
    STATE_ENV,
    STATE_NAME,
    env_value,
)
HARNESSES = "harnesses.json"
AGENTS = "agents.json"
WORKSPACES = "workspaces.json"
MEMBERSHIPS = "memberships.json"
UNITS = "units.json"
RETIRED = "retired-units.jsonl"
FIRST_RUN_FILE = "first_run.json"
FIRST_RUN_STEPS = {"vault": "vault_confirmed_at", "workspace": "workspace_settled_at"}


def _retire_units(dying: list[dict], refs_by_unit: dict[str, list[str]], when: str) -> None:
    """Append a tamper-evident record for every Unit dropped as an orphan.

    The log lives in the state directory, not in a workspace: the event being
    audited can be "delete this workspace", which would also delete its `.groma/`.
    Each line carries the previous line's hash so a later edit or removal of an
    older entry is detectable.
    """
    path = state_root() / RETIRED
    prev = ""
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                prev = json.loads(line)["sha256"]
            except (ValueError, KeyError):
                continue
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for item in dying:
            record = {
                "schema": 1,
                "retired_at": when,
                "unit_id": item["unit_id"],
                "name": item.get("name"),
                "org_path": item.get("org_path"),
                "home_binding": item.get("home_binding"),
                "member_refs": sorted(refs_by_unit.get(item["unit_id"], [])),
                "prev": prev,
            }
            body = json.dumps(record, ensure_ascii=False, sort_keys=True)
            record["sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
            prev = record["sha256"]
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def case_insensitive_fs() -> bool:
    """Windows and macOS (default volume) fold case; Linux does not."""
    return os.name == "nt" or sys.platform == "darwin"


def state_base() -> Path:
    """Per-OS data root, without the Groma suffix.

    macOS used to fall through to the Linux directory here, which put state
    somewhere a Mac user would never look.
    """
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def state_root() -> Path:
    override = env_value(STATE_ENV, LEGACY_STATE_ENV)
    if override:
        return Path(override)
    base = state_base()
    canonical = base / STATE_NAME / "state"
    legacy = base / LEGACY_STATE_NAME / "state"
    if not hasattr(canonical, "is_dir"):  # pure-path fakes in platform tests
        return canonical
    return canonical if canonical.is_dir() or not legacy.is_dir() else legacy


def _read(name: str, default: dict) -> dict:
    path = state_root() / name
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _write(name: str, payload: dict) -> None:
    path = state_root() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(tmp, path)


def normalise_path(value: str | Path) -> str:
    """One shape on every OS, because this string is hashed into `ws_ref`.

    macOS stores file names in NFD, and a path copied from Windows arrives with
    backslashes; either one used to hash to a different handle than the same
    folder read on the machine that wrote it.
    """
    text = str(value)
    if not text.strip():
        return ""
    folded = unicodedata.normalize("NFC", text.replace("\\", "/"))
    folded = PurePosixPath(folded).as_posix().rstrip("/")
    return folded.casefold() if case_insensitive_fs() else folded


def workspace_ref(value: str | Path) -> str:
    """De-identified workspace handle, same shape as the package: ws-<16 hex>."""
    return "ws-" + hashlib.sha256(normalise_path(value).encode("utf-8")).hexdigest()[:16]


def agent_id_for(external_key: str) -> str:
    return "agt-" + hashlib.sha256(external_key.encode("utf-8")).hexdigest()[:24]


# -- workspaces -------------------------------------------------------------


def register_workspace(path: str | Path, name: str | None = None) -> dict:
    ref = workspace_ref(path)
    link = unitlink.read_link(path) or {}
    unit_id = link.get("unit_id")
    org_path = link.get("org_path")
    data = _read(WORKSPACES, {"schema": 1, "workspaces": []})
    now = _now()
    for item in data["workspaces"]:
        if item["ref"] == ref:
            item["path"] = Path(path).as_posix()
            if unit_id:
                item["unit_id"] = unit_id
            if org_path:
                item["org_path"] = org_path
            item["last_seen"] = now
            _write(WORKSPACES, data)
            return item
    record = {
        "ref": ref,
        "path": Path(path).as_posix(),
        "name": name or Path(path).name,
        "unit_id": unit_id,
        "org_path": org_path,
        "first_seen": now,
        "last_seen": now,
    }
    data["workspaces"].append(record)
    data["workspaces"].sort(key=lambda item: item["path"])
    _write(WORKSPACES, data)
    return record


def list_workspaces() -> list[dict]:
    return _read(WORKSPACES, {"schema": 1, "workspaces": []})["workspaces"]


def find_unit(value: str) -> list[dict]:
    """Resolve a unit_id, org_path, ws_ref or local path to registry records."""
    needle = (value or "").strip().rstrip("/")
    if not needle:
        return []
    found = [
        item
        for item in list_workspaces()
        if needle in (item.get("unit_id"), item.get("org_path"), item.get("ref"), item.get("path"))
    ]
    if found:
        return found
    ref = workspace_ref(needle)
    return [item for item in list_workspaces() if item.get("ref") == ref]


def workspace_path(ref: str) -> str | None:
    for item in list_workspaces():
        if item["ref"] == ref:
            return item["path"]
    return None


def remove_workspace(value: str | Path, delete_evidence: bool = False) -> dict:
    """Forget a workspace, optionally deleting only our own .groma directory.

    Project files, harness data and any .qoder/agents config are never touched.
    """
    import shutil

    ref = workspace_ref(value)
    path = Path(value)

    workspaces = _read(WORKSPACES, {"schema": 1, "workspaces": []})
    kept = [item for item in workspaces["workspaces"] if item["ref"] != ref]
    forgotten = len(kept) != len(workspaces["workspaces"])
    workspaces["workspaces"] = kept
    _write(WORKSPACES, workspaces)

    agents = _read(AGENTS, {"schema": 1, "agents": []})
    keep_agents = [item for item in agents["agents"] if item.get("workspace_ref") != ref]
    removed_agents = len(agents["agents"]) - len(keep_agents)
    agents["agents"] = keep_agents
    _write(AGENTS, agents)

    memberships = _read(MEMBERSHIPS, {"schema": 1, "memberships": []})
    refs_by_unit: dict[str, list[str]] = {}
    for row in memberships["memberships"]:
        refs_by_unit.setdefault(row["unit_id"], []).append(row["binding_id"])
    kept_memberships = [item for item in memberships["memberships"] if item.get("binding_id") != ref]
    memberships["memberships"] = kept_memberships
    _write(MEMBERSHIPS, memberships)

    units = _read(UNITS, {"schema": 1, "units": []})
    orphaned = []
    dying: list[dict] = []
    still_used = {item["unit_id"] for item in kept_memberships}
    for item in units["units"]:
        if item["unit_id"] not in still_used and not any(
            workspace.get("unit_id") == item["unit_id"] for workspace in kept
        ):
            orphaned.append(item["unit_id"])
            dying.append(dict(item))  # snapshot the home_binding before it is cleared
        if item.get("home_binding") == ref:
            item["home_binding"] = None
    if dying:
        _retire_units(dying, refs_by_unit, _now())
    units["units"] = [item for item in units["units"] if item["unit_id"] not in orphaned]
    _write(UNITS, units)

    deleted = False
    if delete_evidence:
        from .workspace import meta_dir

        meta = meta_dir(path)
        if meta.is_dir() and meta.name in (META_DIR, LEGACY_META_DIR):
            shutil.rmtree(meta)
            deleted = True

    return {
        "ref": ref,
        "path": path.as_posix(),
        "forgotten": forgotten,
        "agents_removed": removed_agents,
        "evidence_deleted": deleted,
        # 刀 W10：界面要说出"退役了哪些主题"。退役这件事本来就在这一函数里发生，
        # 让调用方另算一遍 = 两套判据，迟早对不上（确认页与实删各说各话）。
        "retired_units": [{"unit_id": item["unit_id"], "name": item.get("name")} for item in dying],
    }


def orphan_units_if_removed(value: str | Path) -> list[dict]:
    """Themes that would be retired if this workspace were dropped -- preview only.

    Same predicate `remove_workspace()` applies below (no membership left and no
    registry row still pointing at the theme), kept next to it so the confirm page
    cannot drift from what actually happens.
    """
    ref = workspace_ref(value)
    kept_memberships = [row for row in list_memberships() if row.get("binding_id") != ref]
    still_used = {row["unit_id"] for row in kept_memberships}
    kept_rows = [item for item in list_workspaces() if item["ref"] != ref]
    return [
        {"unit_id": item["unit_id"], "name": item.get("name")}
        for item in list_units()
        if item["unit_id"] not in still_used
        and not any(row.get("unit_id") == item["unit_id"] for row in kept_rows)
    ]


def clear_workspace_unit(value: str | Path) -> bool:
    """Drop the theme pointer from one registry row; the row itself stays put.

    `remove_unit()` forgets the theme but leaves `workspaces.json` rows pointing at
    it -- and `sync_units()` rebuilds a theme from exactly that field, so a deleted
    theme would come back on the next start. 刀 W12 clears it on the way out.
    """
    ref = workspace_ref(value)
    data = _read(WORKSPACES, {"schema": 1, "workspaces": []})
    changed = False
    for item in data["workspaces"]:
        if item["ref"] != ref:
            continue
        if item.get("unit_id") or item.get("org_path"):
            changed = True
        item["unit_id"] = None
        item["org_path"] = None
    if changed:
        _write(WORKSPACES, data)
    return changed


# -- first run (two consent stamps) -----------------------------------------


def first_run() -> dict:
    """{'vault_confirmed_at': ..., 'workspace_settled_at': ...}; None means never agreed."""
    data = _read(FIRST_RUN_FILE, {})
    return {key: data.get(key) for key in FIRST_RUN_STEPS.values()}


def mark_first_run(step: str) -> dict:
    """Record agreement to one first-run step ('vault' | 'workspace').

    The first stamp is the fact, so re-marking keeps the original moment. Nothing
    here may be called from `register_workspace()`: Groma auto-registers its own boot
    folder on every start, and that is not the operator having agreed to a path.
    """
    from .workspace import LabError  # workspace imports state, so this one stays local

    key = FIRST_RUN_STEPS.get(step)
    if key is None:
        raise LabError("unknown first-run step: %s" % step)
    data = _read(FIRST_RUN_FILE, {"schema": 1})
    if not data.get(key):
        data[key] = _now()
        _write(FIRST_RUN_FILE, data)
    return first_run()


# -- memberships (Unit <-> local workspace binding) -------------------------


def _membership_key(unit_id: str, binding_id: str) -> str:
    return "%s|%s" % (unit_id, binding_id)


def list_memberships(unit_id: str | None = None) -> list[dict]:
    """Active memberships, optionally filtered to one Unit. Read-only."""
    items = _read(MEMBERSHIPS, {"schema": 1, "memberships": []})["memberships"]
    if unit_id:
        items = [m for m in items if m.get("unit_id") == unit_id and m.get("status", "active") == "active"]
    return sorted(items, key=lambda m: (m.get("unit_id", ""), m.get("role", ""), m.get("binding_id", "")))


def membership_for(unit_id: str, binding_id: str) -> dict | None:
    for item in _read(MEMBERSHIPS, {"schema": 1, "memberships": []})["memberships"]:
        if item.get("unit_id") == unit_id and item.get("binding_id") == binding_id:
            return item
    return None


def add_membership(
    unit_id: str,
    binding_id: str,
    role: str = "evidence",
    signal: str = "manual",
    source: str = "manual",
    confidence: float = 1.0,
    authorized_by: str | None = None,
) -> dict:
    if not unit_id or not binding_id:
        raise ValueError("unit_id and binding_id are required")
    if role not in ("primary", "evidence"):
        raise ValueError("role must be primary or evidence")
    data = _read(MEMBERSHIPS, {"schema": 1, "memberships": []})
    now = _now()
    record = {
        "unit_id": unit_id,
        "binding_id": binding_id,
        "role": role,
        "signal": signal,
        "source": source,
        "confidence": round(float(confidence), 3),
        "authorized_by": authorized_by,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    for index, item in enumerate(data["memberships"]):
        if _membership_key(item.get("unit_id", ""), item.get("binding_id", "")) == _membership_key(unit_id, binding_id):
            record["created_at"] = item.get("created_at", now)
            data["memberships"][index] = record
            break
    else:
        data["memberships"].append(record)
    _write(MEMBERSHIPS, data)
    return record


def remove_membership(unit_id: str, binding_id: str) -> bool:
    """Drop a membership. Never touches the workspace's own files or .groma."""
    data = _read(MEMBERSHIPS, {"schema": 1, "memberships": []})
    kept = [
        item
        for item in data["memberships"]
        if _membership_key(item.get("unit_id", ""), item.get("binding_id", "")) != _membership_key(unit_id, binding_id)
    ]
    removed = len(kept) != len(data["memberships"])
    data["memberships"] = kept
    _write(MEMBERSHIPS, data)
    return removed


# -- units (the aggregation entity itself, not a projection of a folder) -----


def list_units() -> list[dict]:
    """Every Unit this machine knows about. Read-only."""
    items = _read(UNITS, {"schema": 1, "units": []})["units"]
    return sorted(items, key=lambda item: ((item.get("name") or "").casefold(), item.get("unit_id", "")))


def unit_record(unit_id: str) -> dict | None:
    for item in _read(UNITS, {"schema": 1, "units": []})["units"]:
        if item.get("unit_id") == unit_id:
            return item
    return None


def upsert_unit(
    unit_id: str,
    name: str | None = None,
    org_path: str | None = None,
    home_binding: str | None = None,
    created_at: str | None = None,
) -> dict:
    """Create or patch a Unit record. `unit_id` is never regenerated here."""
    if not unit_id:
        raise ValueError("unit_id is required")
    data = _read(UNITS, {"schema": 1, "units": []})
    now = _now()
    record = next((item for item in data["units"] if item.get("unit_id") == unit_id), None)
    if record is None:
        record = {
            "unit_id": unit_id,
            "name": name or unit_id,
            "org_path": org_path,
            "home_binding": home_binding,
            "created_at": created_at or now,
            "updated_at": now,
        }
        data["units"].append(record)
    else:
        if name is not None:
            record["name"] = name
        if org_path is not None:
            record["org_path"] = org_path
        if home_binding is not None:
            record["home_binding"] = home_binding
        if created_at is not None:
            record["created_at"] = created_at
        record["updated_at"] = now
    _write(UNITS, data)
    return record


def remove_unit(unit_id: str) -> bool:
    """Forget a Unit and its memberships. Workspace evidence and files stay put."""
    data = _read(UNITS, {"schema": 1, "units": []})
    kept = [item for item in data["units"] if item.get("unit_id") != unit_id]
    removed = len(kept) != len(data["units"])
    data["units"] = kept
    _write(UNITS, data)
    memberships = _read(MEMBERSHIPS, {"schema": 1, "memberships": []})
    left = [item for item in memberships["memberships"] if item.get("unit_id") != unit_id]
    if len(left) != len(memberships["memberships"]):
        memberships["memberships"] = left
        _write(MEMBERSHIPS, memberships)
    return removed


# -- harnesses --------------------------------------------------------------


def load_harnesses() -> list[dict]:
    return _read(HARNESSES, {"schema": 1, "harnesses": []})["harnesses"]


def save_harnesses(items: list[dict]) -> None:
    _write(HARNESSES, {"schema": 1, "harnesses": items})


# -- agents -----------------------------------------------------------------


def load_agents() -> list[dict]:
    return _read(AGENTS, {"schema": 1, "agents": []})["agents"]


def upsert_agent(record: dict) -> dict:
    data = _read(AGENTS, {"schema": 1, "agents": []})
    now = _now()
    for item in data["agents"]:
        if item["agent_id"] == record["agent_id"]:
            item.update({key: value for key, value in record.items() if value is not None})
            item["last_seen"] = now
            _write(AGENTS, data)
            return item
    record.setdefault("first_seen", now)
    record["last_seen"] = now
    data["agents"].append(record)
    data["agents"].sort(key=lambda item: (item.get("kind", ""), item.get("name", "")))
    _write(AGENTS, data)
    return record


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
