"""Candidate packs for topic classification: metadata only, no content, no paths.

The pack is what a Harness-side classifier is allowed to see. Anchors are
carried as opaque tokens so the pack cannot leak a workspace location: the
relative paths that digests store for Qoder embed the absolute path as a slug
(`projects/d--workspaces-ctx-parser/...`), so shipping them verbatim would ship
the path. `apply` resolves tokens against freshly rebuilt digests -- a token
that does not resolve is refused rather than guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import qoder_db, state, topics
from .topics import key_of
from .unitmatch import digest_records_for, unit_files, unit_workspaces

KIND = "GROMA_TOPIC_CANDIDATES"
SCHEMA = 1
_SLUG = re.compile(r"^[a-z]-{1,2}.*(workspaces|users).*$", re.IGNORECASE)
_PATHISH = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'|<>]*"
    r"|/(?:workspaces|Users|users|home)/[^\s\"'|<>]*"
)
MASK = "[路径已隐藏]"


def scrub(value: str | None) -> tuple[str | None, bool]:
    """Titles are user-authored and do quote paths, so they are not safe text."""
    if not value:
        return value, False
    masked = _PATHISH.sub(MASK, value)
    return masked, masked != value


def token_of(source: str, path: str) -> str:
    return "ev-" + hashlib.sha256(("%s|%s" % (source, path)).encode("utf-8")).hexdigest()[:16]


def _read_titles(db: Path) -> dict[str, dict]:
    try:
        connection = sqlite3.connect(str(db))
    except sqlite3.Error:
        return {}
    try:
        connection.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_session)")}
        wanted = [name for name in ("session_title", "session_type", "parent_session_id") if name in columns]
        if "session_id" not in columns or not wanted:
            return {}
        out = {}
        for row in connection.execute(
            "SELECT session_id, %s FROM chat_session" % ", ".join(wanted)
        ):
            record = dict(zip(wanted, row[1:]))
            out[str(row[0])] = {k: (str(v)[:120] if v is not None else None) for k, v in record.items()}
        return out
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def qoder_roots(root: Path) -> list[Path]:
    """Declared Qoder collection roots across the Unit -- never the workspace.

    Walking the workspace can land on a staged fixture database
    (`work/_p0_stage/<other>/.qoder/local.db`) and label sessions that are not
    ours, so the DB is only taken from what `sources.json` declares.
    """
    from .sources import load_sources

    found = []
    for folder in unit_workspaces(root):
        try:
            declared = load_sources(Path(folder))
        except Exception:
            continue
        for source in declared:
            if source.get("harness") in ("qoder", "qoder-cn"):
                path = source.get("path")
                if path:
                    found.append(Path(path))
    return found


def title_index(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for folder in qoder_roots(root):
        db = qoder_db.locate_db(folder)
        if db is not None:
            out.update(_read_titles(db))
    return out


def _session_id(path: str) -> str:
    from .digest import normalise_session_id

    return normalise_session_id(path)


def _workspace_label(record: dict) -> str:
    root = (record.get("workspace_root") or "").replace("\\", "/").rstrip("/")
    return Path(root).name if root else "unknown"


def candidates(root: Path) -> dict:
    """The exportable candidate pack. Every field here is metadata by rule."""
    records = digest_records_for(unit_workspaces(root))
    rows: list[tuple[dict, str]] = []
    seen: set[tuple[str, str]] = set()
    unanchored = 0
    for record in records:
        label = _workspace_label(record)
        for row in record.get("files") or []:
            key = key_of(row)
            if key is None:
                unanchored += 1  # no source or no path: nothing to cite, refused here
                continue
            if key in seen:
                continue
            seen.add(key)
            rows.append((row, label))
    titles = title_index(root)
    items, registry = [], {}
    for row, label in rows:
        key = key_of(row)
        token = token_of(*key)
        session_id = _session_id(row["path"])
        meta = titles.get(session_id) or {}
        title, masked = scrub(meta.get("session_title"))
        signals = [
            "harness=%s" % row.get("harness"),
            "workspace=%s" % label,
            "agent_kind=%s" % row.get("agent_kind"),
            "events=%s" % row.get("events"),
        ]
        if masked:
            signals.append("title_scrubbed=true")
        if meta.get("session_type"):
            signals.append("session_type=%s" % meta["session_type"])
        if meta.get("parent_session_id"):
            signals.append("has_parent=true")
        items.append(
            {
                "token": token,
                "title": title,
                "harness": row.get("harness"),
                "workspace": label,
                "events": row.get("events"),
                "first_ts": row.get("first_ts"),
                "last_ts": row.get("last_ts"),
                "agent_kind": row.get("agent_kind"),
                "signals": signals,
            }
        )
        registry[token] = {"source": key[0], "path": key[1]}
    with_title = sum(1 for item in items if item["title"])
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "generated_at": _now(),
        "unit": state.workspace_ref(root),
        "items": items,
        "recall": {
            "candidates": len(items),
            "with_title": with_title,
            "unanchored": unanchored,
            "title_coverage": round(with_title / len(items), 3) if items else None,
            "note": "title comes from the Qoder session table; other harnesses have none yet",
        },
        "_registry": registry,
    }


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(pack: dict, tokens: list[str]) -> tuple[list[dict], list[dict]]:
    """Tokens back into anchors. Unknown tokens are refused, never guessed."""
    registry = pack.get("_registry") or {}
    known, unknown = [], []
    for token in tokens:
        anchor = registry.get(token)
        if anchor is None:
            unknown.append({"token": token, "reason": "token 不在候选包里"})
        else:
            known.append({**anchor, "confidence": None, "signals": []})
    return known, unknown


def assert_no_leak(pack: dict) -> list[str]:
    """Privacy gate for the pack: no drive letters, no absolute paths, no cwd."""
    text = json.dumps({k: v for k, v in pack.items() if k != "_registry"}, ensure_ascii=False)
    leaks = []
    for probe in (":\\\\", ":/", "/workspaces/", "/users/"):
        if probe.lower() in text.lower():
            leaks.append(probe)
    for item in pack.get("items") or []:
        value = str(item.get("token") or "")
        if _SLUG.match(value) or ".." in value:
            leaks.append("token=%s" % value)
    return leaks


def apply(root: Path, topic: str, tokens: list[str], decided_by: str = "human") -> dict:
    """Turn pack tokens into topic evidence, through both refusal gates.

    Anchors are re-derived from the live digests here, never read out of a pack
    file: a pack carries tokens only, because its anchors are relative paths and
    a Qoder relative path *is* a workspace location. So a token that the current
    digests cannot resolve is refused instead of matched by proximity -- a stale
    pack says "this used to be a session", not "cite it anyway".
    """
    pack = candidates(root)
    known, unknown = resolve(pack, tokens)
    record = topics.resolve_topic(topic)
    created = record is None
    if record is None:
        try:
            record = topics.create_topic(topic)
        except ValueError as exc:
            return {"topic_id": None, "topic": topic, "topic_created": False,
                    "decided_by": decided_by, "added": 0, "rejected": [],
                    "refused_tokens": [], "error": str(exc)}
    anchors = {(item["source"], item["path"]) for item in (pack.get("_registry") or {}).values()}
    result = topics.select(record["topic_id"], known, decided_by=decided_by, known_keys=anchors)
    return {
        "topic_id": record["topic_id"],
        "topic": record["name"],
        "topic_created": created,
        "decided_by": decided_by,
        "added": result["added"],
        "rejected": result["rejected"],
        "refused_tokens": unknown,
    }
