"""Qoder's local.db: the authoritative session inventory and sub-agent identity.

The JSONL transcript carries `key_sub_agent_type` only inside message text, so
reading it would mean parsing conversation content. The database has it as a
structured column, and it also knows the workspace (`project_id` / `project_uri`)
and per-session timestamps. We read metadata columns only, read-only.

Mirrors the package's rules (adapters/qoder.py, identity_resolver.py):
- workspace = project_id, else project_uri;
- sub-agent = chat_record.extra.key_sub_agent_type;
- timestamps are epoch milliseconds.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_DEPTH = 5
MAX_HOURS_PER_SESSION = 168
CACHE_TTL_SECONDS = 30
_CACHE: dict = {}


def locate_db(root: Path) -> Path | None:
    root = Path(root)
    direct = root / "local.db"
    if direct.is_file():
        return direct
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base_depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        if "local.db" in filenames:
            return Path(dirpath) / "local.db"
    return None


def canonical_workspace(project_id: object, project_uri: object) -> str | None:
    pid = str(project_id or "").strip()
    if pid:
        return pid
    puri = str(project_uri or "").strip()
    return puri or None


def _iso(ms: object) -> str | None:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value < 10_000_000_000:  # seconds, not milliseconds
        value *= 1000
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hour(ms: object) -> str | None:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value < 10_000_000_000:
        value *= 1000
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


def _identity(connection: sqlite3.Connection) -> dict[str, dict]:
    """session_id -> {session_type, parent_session_id, key_sub_agent_type}."""
    found: dict[str, dict] = {}
    columns = _columns(connection, "chat_session")
    wanted = [name for name in ("session_type", "mode", "parent_session_id") if name in columns]
    if "session_id" in columns and wanted:
        try:
            rows = connection.execute(
                "SELECT session_id, %s FROM chat_session" % ", ".join(wanted)
            ).fetchall()
            for row in rows:
                record = found.setdefault(str(row[0]), {})
                for name, value in zip(wanted, row[1:]):
                    if value:
                        record[name] = str(value)[:255]
        except sqlite3.Error:
            pass
    try:
        rows = connection.execute(
            "SELECT session_id, extra FROM chat_record WHERE extra LIKE '%key_sub_agent_type%'"
        ).fetchall()
        for session_id, extra in rows:
            try:
                payload = json.loads(extra or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("key_sub_agent_type"):
                found.setdefault(str(session_id), {})["key_sub_agent_type"] = str(
                    payload["key_sub_agent_type"]
                )[:255]
    except sqlite3.Error:
        pass
    return found


def _message_stats(connection: sqlite3.Connection) -> dict[str, dict]:
    """session_id -> {messages, first_ms, last_ms, hours}, via aggregates only."""
    stats: dict[str, dict] = {}
    try:
        rows = connection.execute(
            "SELECT session_id, COUNT(*), MIN(gmt_create), MAX(gmt_create) "
            "FROM chat_message GROUP BY session_id"
        ).fetchall()
    except sqlite3.Error:
        return stats
    for session_id, count, first, last in rows:
        stats[str(session_id)] = {
            "messages": int(count or 0),
            "first_ms": first,
            "last_ms": last,
            "hours": [],
        }
    # One row per session-hour instead of one row per message.
    try:
        buckets = connection.execute(
            "SELECT session_id, gmt_create / 3600000 AS bucket "
            "FROM chat_message GROUP BY session_id, bucket"
        ).fetchall()
        for session_id, bucket in buckets:
            slot = stats.get(str(session_id))
            if slot is None or len(slot["hours"]) >= MAX_HOURS_PER_SESSION:
                continue
            hour = _hour(int(bucket) * 3600000)
            if hour and hour not in slot["hours"]:
                slot["hours"].append(hour)
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return stats


def session_inventory(root: Path, with_stats: bool = True) -> list[dict]:
    """Every session Qoder knows about. Read-only; metadata columns only."""
    db = locate_db(root)
    if db is None:
        return []
    try:
        stamp = db.stat().st_mtime, db.stat().st_size
    except OSError:
        stamp = (0, 0)
    cache_key = (db.as_posix(), with_stats, stamp)
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=3)
    except sqlite3.Error:
        return []
    try:
        columns = _columns(connection, "chat_session")
        if "session_id" not in columns:
            return []
        pid_expr = "project_id" if "project_id" in columns else "NULL"
        uri_expr = "project_uri" if "project_uri" in columns else "NULL"
        created_expr = "gmt_create" if "gmt_create" in columns else "NULL"
        modified_expr = "gmt_modified" if "gmt_modified" in columns else "NULL"
        rows = connection.execute(
            "SELECT session_id, %s, %s, %s, %s FROM chat_session"
            % (created_expr, modified_expr, pid_expr, uri_expr)
        ).fetchall()
        identity = _identity(connection)
        stats = _message_stats(connection) if with_stats else {}
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    sessions: list[dict] = []
    for session_id, created, modified, project_id, project_uri in rows:
        key = str(session_id)
        info = identity.get(key, {})
        stat = stats.get(key, {})
        sessions.append(
            {
                "session_id": key,
                "workspace": canonical_workspace(project_id, project_uri),
                "created_at": _iso(created),
                "updated_at": _iso(modified),
                "first_ts": _iso(stat.get("first_ms")) or _iso(created),
                "last_ts": _iso(stat.get("last_ms")) or _iso(modified),
                "messages": stat.get("messages", 0),
                "hours": sorted(stat.get("hours", [])),
                "sub_agent_type": info.get("key_sub_agent_type"),
                "parent_session_id": info.get("parent_session_id"),
                "session_type": info.get("session_type"),
            }
        )
    _CACHE[cache_key] = (time.time(), sessions)
    return sessions


MESSAGE_BODY_COLUMNS = ("id", "session_id", "role", "content", "tool_result", "extra", "token_info", "gmt_create")


def subagent_types(root: str | Path | None = None) -> dict[str, str]:
    """session_id -> key_sub_agent_type, for callers whose keys are session ids.

    The observation page starts from vault session keys, which are ids without any
    workspace scoping; `session_inventory` is the only place that knows which of
    them are sub-agents. Default root matches discovery's qoder-cn declaration.
    """
    base = Path(root) if root is not None else Path.home() / ".qoder-cn"
    found: dict[str, str] = {}
    for session in session_inventory(base, with_stats=False):
        sub_type = session.get("sub_agent_type")
        if sub_type:
            found[session["session_id"]] = sub_type
    return found


def open_read_only(db: Path) -> sqlite3.Connection:
    """Always read-only: this file belongs to a running Qoder, not to wb."""
    connection = sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def message_rows(db: Path, session_id: str) -> list[dict]:
    """One session's message rows, **bodies included**.

    This is the only place in Groma that reads `chat_message.content` / `tool_result`:
    every other query in this module is COUNT / MIN / MAX / GROUP BY. The vault is the
    sole caller, it writes into the local archive, and nothing here may reach digest,
    asset, or export (C4). Rows come back ordered by (gmt_create, id) so the position
    can serve as a stable cursor, matching the file-based writer.
    """
    connection = open_read_only(db)
    try:
        columns = _columns(connection, "chat_message")
        if "content" not in columns:
            return []
        # Intersected with the table's own columns and drawn from a fixed tuple, so the
        # name list below can never carry anything but these eight identifiers.
        wanted = [name for name in MESSAGE_BODY_COLUMNS if name in columns]
        sql = "SELECT %s FROM chat_message WHERE session_id = ? ORDER BY gmt_create, id" % ", ".join(wanted)
        return [dict(zip(wanted, row)) for row in connection.execute(sql, (session_id,)).fetchall()]
    finally:
        connection.close()


def message_preview(db: Path, session_ids: list[str]) -> dict:
    """Rows and bytes per session, computed with LENGTH() only -- no body is read.

    Backfill has to show this before copying anything: DB-only sessions run two
    orders of magnitude bigger than the file transcripts measured so far
    (one session on this machine is 609 MB).
    """
    ids = [str(item) for item in session_ids if item]
    if not ids:
        return {"sessions": 0, "rows": 0, "content_bytes": 0, "tool_result_bytes": 0, "per_session": []}
    connection = open_read_only(db)
    try:
        rows = connection.execute(
            "SELECT session_id, COUNT(*), COALESCE(SUM(LENGTH(content)), 0), "
            "COALESCE(SUM(LENGTH(tool_result)), 0) FROM chat_message "
            "WHERE session_id IN (%s) GROUP BY session_id" % ", ".join("?" for _ in ids),
            ids,
        ).fetchall()
    finally:
        connection.close()
    per = sorted(
        ({"session_id": item[0], "rows": item[1], "content_bytes": item[2], "tool_result_bytes": item[3]}
         for item in rows),
        key=lambda item: -item["content_bytes"],
    )
    return {
        "sessions": len(per),
        "rows": sum(item["rows"] for item in per),
        "content_bytes": sum(item["content_bytes"] for item in per),
        "tool_result_bytes": sum(item["tool_result_bytes"] for item in per),
        "per_session": per,
    }