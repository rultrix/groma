"""Known harness locations, probed read-only.

Discovery is a preview, never a read of conversation content:
- Qoder's database knows each session's workspace and sub-agent, so we ask it;
- other harnesses get a light cwd preview (first 20 lines per file);
- nothing is stored until the user adds the harness.
"""

from __future__ import annotations

import os
from pathlib import Path

from .agents import build_agents
from .preview import cwd_counts
from .qoder_db import session_inventory
from .sources import load_sources
from .state import normalise_path
from .workspace import load_unit

EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".groma", ".wb"}
MAX_FILES = 5000

CANDIDATES = [
    {"name": "qoder", "harness": "qoder-cn", "root": "~/.qoder-cn", "include": "projects"},
    {"name": "codex", "harness": "codex", "root": "~/.codex", "include": "sessions"},
    {"name": "claude", "harness": "claude-code", "root": "~/.claude", "include": "projects"},
    {"name": "cursor", "harness": "cursor", "root": "~/.cursor", "include": None},
]


def _probe(base: Path) -> dict:
    if not base.is_dir():
        return {"exists": False, "sessions": 0, "bytes": 0, "truncated": False}
    sessions = 0
    total = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRS]
        for filename in filenames:
            if not filename.lower().endswith((".jsonl", ".ndjson")):
                continue
            try:
                total += os.stat(os.path.join(dirpath, filename)).st_size
            except OSError:
                continue
            sessions += 1
            if sessions >= MAX_FILES:
                truncated = True
                break
        if truncated:
            break
    return {"exists": True, "sessions": sessions, "bytes": total, "truncated": truncated}


def _roots(workspace: str | list[str]) -> list[str]:
    return [workspace] if isinstance(workspace, str) else list(workspace)


def _unit_agents(roots: list[str]) -> list[dict]:
    """Agent rows for the whole Unit: primary once, sub-agents merged by name."""
    order: list[tuple[str, str]] = []
    seen = set()
    for r in roots:
        for record in build_agents(Path(r), ["qoder-cn"]):
            key = (record["name"], record["kind"])
            if key not in seen:
                seen.add(key)
                order.append(key)
    return [{"name": name, "kind": kind} for (name, kind) in order]


def _qoder_breakdown(unit_root: Path, root: Path, workspace: str | list[str]) -> dict | None:
    inventory = session_inventory(root, with_stats=False)
    if not inventory:
        return None
    norm = [normalise_path(r) for r in _roots(workspace)]
    inside = outside = no_cwd = 0
    per_agent: dict[str, int] = {}
    for session in inventory:
        where = session.get("workspace")
        if not where:
            no_cwd += 1
            continue
        current = normalise_path(where)
        if not any(current == r or current.startswith(r + "/") for r in norm):
            outside += 1
            continue
        inside += 1
        label = session.get("sub_agent_type") or "__default__"
        per_agent[label] = per_agent.get(label, 0) + 1

    agents = []
    for record in _unit_agents(norm):
        if record["kind"] == "primary":
            count = per_agent.pop("__default__", 0)
        else:
            count = per_agent.pop(record["name"], 0)
        agents.append({**record, "sessions": count})
    for leftover, count in per_agent.items():
        agents.append({"name": leftover, "kind": "unregistered", "sessions": count})
    return {
        "total": len(inventory),
        "inside": inside,
        "outside": outside,
        "no_cwd": no_cwd,
        "agents": agents,
        "breakdown": "database",
    }


def _file_breakdown(unit_root: Path, base: Path, workspace: str | list[str]) -> dict:
    counts = cwd_counts(base, workspace)
    primary = {"name": "Default Agent", "kind": "primary", "sessions": counts["inside"]}
    return {
        "total": counts["inside"] + counts["outside"] + counts["no_cwd"],
        "inside": counts["inside"],
        "outside": counts["outside"],
        "no_cwd": counts["no_cwd"],
        "agents": [primary],
        "breakdown": "cwd-preview",
    }


def discover(unit_root: Path, workspaces: list[str] | None = None) -> list[dict]:
    unit = load_unit(unit_root)
    roots = workspaces or [unit.get("workspace_root") or str(unit_root)]
    declared = {source["path"] for source in load_sources(unit_root)}
    found: list[dict] = []
    for candidate in CANDIDATES:
        root = Path(candidate["root"]).expanduser()
        base = root / candidate["include"] if candidate["include"] else root
        info = _probe(base)
        entry = {
            "name": candidate["name"],
            "harness": candidate["harness"],
            "root": root.as_posix(),
            "include": candidate["include"],
            "declared": root.as_posix() in declared,
            **info,
            "total": info["sessions"],
            "inside": 0,
            "outside": 0,
            "no_cwd": 0,
            "agents": [],
            "breakdown": None,
        }
        if info["exists"]:
            breakdown = None
            if candidate["harness"] == "qoder-cn":
                breakdown = _qoder_breakdown(unit_root, root, roots)
            if breakdown is None:
                breakdown = _file_breakdown(unit_root, base, roots)
            entry.update(breakdown)
        found.append(entry)
    return found
