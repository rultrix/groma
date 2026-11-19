"""Unit-centred evidence graph, rendered server-side.

Layers: Unit -> Agent -> Session. A default agent can have sub-agents, drawn as
children of the default rather than siblings. A hierarchy, not a force layout:
a personal unit has tens of sessions, and a deterministic SVG can be asserted
with curl.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from lab import unitmatch
from lab.workspace import load_unit

COLORS = {
    "unit": "#0f766e",
    "agent": "#1d4ed8",
    "sub": "#7c3aed",
    "recent": "#15803d",
    "month": "#b45309",
    "older": "#94a3b8",
}
UNIT_BOX = (60, 150)
AGENT_BOX = (150, 38)
SESSION_BOX = (250, 26)
X_UNIT, X_AGENT, X_SESSION = 40, 300, 620
TOP = 40
ROW = 32


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _recency(last_ts: str | None) -> str:
    stamp = _parse(last_ts)
    if stamp is None:
        return "older"
    age = datetime.now(timezone.utc) - stamp
    if age <= timedelta(days=7):
        return "recent"
    if age <= timedelta(days=30):
        return "month"
    return "older"


def build_graph(root: Path) -> dict:
    """Unit-scoped: one node set for every workspace joined into this Unit."""
    unit = load_unit(root)
    roots = unitmatch.unit_workspaces(root)
    digests = unitmatch.digest_records_for(roots)
    files = unitmatch.unit_files(digests)
    stamps = [item.get("created_at") for item in digests if item.get("created_at")]

    groups: dict[str, list[dict]] = {}
    meta: dict[str, dict] = {}
    for item in files:
        agent_id = item.get("agent_id") or "unknown"
        groups.setdefault(agent_id, []).append(item)
        meta.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "name": item.get("agent_name") or "unknown",
                "kind": item.get("agent_kind") or "primary",
                "parent": item.get("parent_agent_id"),
                "harness": item.get("harness"),
            },
        )

    order = sorted(
        meta.values(),
        key=lambda item: (item["kind"] != "primary", item["harness"] or "", item["name"]),
    )

    row = 0
    session_nodes: list[dict] = []
    agent_nodes: list[dict] = []
    edges: list[dict] = []

    for info in order:
        sessions = sorted(groups.get(info["agent_id"], []), key=lambda item: item.get("last_ts") or "")
        first_row = row
        children = []
        for item in sessions:
            stem = Path(item["path"]).stem
            label = stem if len(stem) <= 24 else stem[:8] + "…"
            node = {
                "id": "s-%d" % len(session_nodes),
                "kind": "session",
                "label": label,
                "sublabel": ("%d 事件 · 无 transcript" % item.get("events", 0))
                if item.get("transcript") is False
                else ("%d 事件" % item.get("events", 0)),
                "x": X_SESSION,
                "y": TOP + row * ROW,
                "w": SESSION_BOX[0],
                "h": SESSION_BOX[1],
                "color": COLORS[_recency(item.get("last_ts"))],
                "source": item.get("source"),
                "path": item["path"],
                "events": item.get("events", 0),
                "last_ts": item.get("last_ts"),
                "agent_name": info["name"],
                "title": "%s / %s\n%d 行\n末次 %s" % (info["name"], item["path"], item.get("lines", 0), item.get("last_ts")),
            }
            session_nodes.append(node)
            children.append(node)
            row += 1

        is_sub = info["kind"] == "sub"
        agent_node = {
            "id": "a-" + info["agent_id"],
            "kind": "sub" if is_sub else "agent",
            "label": info["name"],
            "sublabel": "%s · %d 个会话" % (info["harness"] or "?", len(sessions)),
            "x": X_AGENT,
            "y": TOP + int((first_row + row - 1) / 2) * ROW,
            "w": AGENT_BOX[0],
            "h": AGENT_BOX[1],
            "color": COLORS["sub"] if is_sub else COLORS["agent"],
            "agent_id": info["agent_id"],
            "parent": info["parent"],
        }
        agent_nodes.append(agent_node)
        for child in children:
            edges.append({"from": agent_node, "to": child})

    unit_node = {
        "id": "unit",
        "kind": "unit",
        "label": unit.get("name") or "unit",
        "sublabel": Path(roots[0]).name + (" · +%d 工作区" % (len(roots) - 1) if len(roots) > 1 else ""),
        "x": X_UNIT,
        "y": TOP + max(0, int((row - 1) / 2)) * ROW,
        "w": UNIT_BOX[0],
        "h": UNIT_BOX[1],
        "color": COLORS["unit"],
        "workspace_root": unit.get("workspace_root"),
    }

    by_id = {node["id"]: node for node in agent_nodes}
    for node in agent_nodes:
        parent_id = "a-" + node["parent"] if node.get("parent") else None
        if parent_id and parent_id in by_id:
            edges.append({"from": by_id[parent_id], "to": node, "dashed": True})
        else:
            edges.append({"from": unit_node, "to": node})

    width = X_SESSION + SESSION_BOX[0] + 60
    height = TOP + max(row, 1) * ROW + 40
    return {
        "nodes": [unit_node] + agent_nodes + session_nodes,
        "edges": edges,
        "width": width,
        "height": max(height, 260),
        "unit": unit_node,
        "agents": agent_nodes,
        "sessions": session_nodes,
        "session_count": len(session_nodes),
        "event_count": sum(item["events"] for item in session_nodes),
        "workspaces": roots,
        "digest": max(stamps) if stamps else None,
    }


def _edge_path(source: dict, target: dict) -> str:
    x1 = source["x"] + source["w"]
    y1 = source["y"] + source["h"] / 2
    x2 = target["x"]
    y2 = target["y"] + target["h"] / 2
    mid = (x1 + x2) / 2
    return "M%.1f,%.1f C%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (x1, y1, mid, y1, mid, y2, x2, y2)


def render_svg(graph: dict) -> str:
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="Unit evidence graph">'
        % (graph["width"], graph["height"], graph["width"], graph["height"]),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    for edge in graph["edges"]:
        dash = ' stroke-dasharray="5 4"' if edge.get("dashed") else ""
        parts.append(
            '<path d="%s" fill="none" stroke="#cbd5e1" stroke-width="1.4"%s data-from="%s" data-to="%s"/>'
            % (_edge_path(edge["from"], edge["to"]), dash, edge["from"]["id"], edge["to"]["id"])
        )
    for node in graph["nodes"]:
        link_open, link_close = "", ""
        if node["kind"] == "session":
            href = "/session?source=%s&amp;path=%s" % (quote(node.get("source") or ""), quote(node["path"]))
            link_open, link_close = '<a href="%s">' % href, "</a>"
        parts.append(link_open + '<g id="%s" data-kind="%s">' % (escape(node["id"]), node["kind"]))
        if node.get("title"):
            parts.append("<title>%s</title>" % escape(node["title"]))
        parts.append(
            '<rect x="%d" y="%d" width="%d" height="%d" rx="7" fill="%s" fill-opacity="0.10" stroke="%s" stroke-width="1.5"/>'
            % (node["x"], node["y"], node["w"], node["h"], node["color"], node["color"])
        )
        parts.append(
            '<text x="%d" y="%d" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="13" fill="#0f172a">%s</text>'
            % (node["x"] + 10, node["y"] + 17, escape(node["label"]))
        )
        if node.get("sublabel"):
            parts.append(
                '<text x="%d" y="%d" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="10" fill="#64748b">%s</text>'
                % (node["x"] + 10, node["y"] + (26 if node["kind"] == "session" else 32), escape(node["sublabel"]))
            )
        parts.append("</g>" + link_close)
    parts.append("</svg>")
    return "".join(parts)
