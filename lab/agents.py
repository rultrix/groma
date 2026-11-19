"""Agent identities: one default per harness, plus named sub-agents per workspace.

Mirrors the package's schema 0.3 rules without importing its code:
- a workspace is a context scope, not an agent;
- every harness has a default (primary) agent;
- a sub-agent is defined by `.qoder/agents/*.md` inside one workspace, and its
  identity includes that workspace, so the same name elsewhere is another agent;
- a sub-agent's parent is the default agent of the same harness.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .state import agent_id_for, register_workspace, upsert_agent, workspace_ref

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.S)
NAMED_SUB_AGENT_HARNESSES = {"qoder-cn"}


def _frontmatter_name(text: str) -> str | None:
    block = FRONTMATTER.search(text)
    if not block:
        return None
    for line in block.group(1).splitlines():
        if line.strip().startswith("name:"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            return value or None
    return None


def _slug(rel_path: str) -> str:
    base = rel_path.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


def sub_agent_configs(workspace_root: Path, harness: str = "qoder-cn") -> list[dict]:
    if harness not in NAMED_SUB_AGENT_HARNESSES:
        return []
    base = Path(workspace_root) / ".qoder" / "agents"
    if not base.is_dir():
        return []
    found: list[dict] = []
    seen: dict[str, int] = {}
    for path in sorted(base.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        name = _frontmatter_name(text)
        if not name:
            continue
        seen[name] = seen.get(name, 0) + 1
        rel = path.relative_to(workspace_root).as_posix()
        found.append(
            {
                "name": name,
                "config_rel_path": rel,
                "config_hash": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
            }
        )
    # Duplicate names inside one workspace are ambiguous: register none of them.
    return [item for item in found if seen[item["name"]] == 1]


def derived_sub_agent(harness: str, sub_type: str) -> dict:
    """A sub-agent type the harness names in a file path but no config declares.

    Qoder's built-in `general-purpose` has no `.qoder/agents/*.md`. Matching it to
    the primary agent is what filed thousands of sub-agent transcripts under the
    main agent, so an unmatched type gets its own stable identity instead.
    """
    key = "%s:subagent:builtin:%s" % (harness, sub_type)
    parent = default_agent(harness)
    return {
        "agent_id": agent_id_for(key),
        "external_key": key,
        "name": sub_type,
        "harness": harness,
        "kind": "sub",
        "parent_agent_id": parent["agent_id"],
        "workspace_ref": None,
        "workspace_refs": [],
        "config_rel_path": None,
        "config_hash": None,
        # Verified against the source: the name comes out of the harness's own file
        # name, not from a config the user wrote.
        "verification": "SOURCE_VERIFIED",
    }


def default_agent(harness: str) -> dict:
    key = "%s:default" % harness
    return {
        "agent_id": agent_id_for(key),
        "external_key": key,
        "name": "%s Default Agent" % harness,
        "harness": harness,
        "kind": "primary",
        "parent_agent_id": None,
        "workspace_ref": None,
        "workspace_refs": [],
        "config_rel_path": None,
        "config_hash": None,
        "verification": "DISCOVERED",
    }


def build_agents(workspace_root: Path, harnesses: list[str], persist: bool = False) -> list[dict]:
    """Build agent records. Persisting is opt-in so read paths stay pure."""
    root = Path(workspace_root)
    ref = workspace_ref(root)
    if persist:
        register_workspace(root, root.name)
    agents: list[dict] = []
    for harness in sorted(set(harnesses)):
        primary = default_agent(harness)
        agents.append(upsert_agent(primary) if persist else primary)
        for config in sub_agent_configs(root, harness):
            key = "%s:subagent:%s:%s:%s" % (harness, ref, _slug(config["config_rel_path"]), config["name"])
            record = {
                "agent_id": agent_id_for(key),
                "external_key": key,
                "name": config["name"],
                "harness": harness,
                "kind": "sub",
                "parent_agent_id": primary["agent_id"],
                "workspace_ref": ref,
                "workspace_refs": [ref],
                "config_rel_path": config["config_rel_path"],
                "config_hash": config["config_hash"],
                "verification": "DISCOVERED",
            }
            agents.append(upsert_agent(record) if persist else record)
    return agents


def sync_agents(workspace_root: Path) -> list[dict]:
    """Persist agents for the harnesses this workspace has declared."""
    from .sources import load_sources

    sources = load_sources(workspace_root)
    harnesses = [source.get("harness", "unknown") for source in sources]
    return build_agents(workspace_root, harnesses, persist=True)


def agent_for_session(agents: list[dict], harness: str, sub_type: str | None) -> dict:
    if sub_type:
        for item in agents:
            if item.get("harness") == harness and item.get("kind") == "sub" and item.get("name") == sub_type:
                return item
        # A named sub-agent that no workspace declares is still a sub-agent: sending
        # it to the primary agent is how "who did this work" gets answered wrongly.
        return derived_sub_agent(harness, sub_type)
    for item in agents:
        if item.get("harness") == harness and item.get("kind") == "primary":
            return item
    return default_agent(harness)
