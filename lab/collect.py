"""Catch-up collection: the entry point an app start / logon trigger calls.

Runs scan and digest once, but not more often than `min_interval_minutes`
unless forced. A fixed hourly schedule measured 29% coverage on a laptop
that sleeps; event-driven catch-up is the replacement.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .contract import LEGACY_TRIGGER_ENV, env_value
from .control import is_paused
from .digest import build_digest, save_digest
from .inject import refresh as refresh_injection
from .scan import build_scan, save_scan
from .sources import load_sources
from .workspace import LabError, load_unit, meta_dir, read_json, utc_stamp, write_json

# Vault collection: implemented by the vault session (2026-11-09), not the peer.
from . import vault as vault_mod

STATE_FILE = "last-catchup.json"
TRIGGER_ENV = "GROMA_TRIGGER"


def _written_by(entry: str) -> dict:
    """Who wrote this digest -- the field that makes the 15-minute rhythm nameable.

    `entry` is the command that did it; `trigger` is whoever launched us, read
    from the environment so the launcher (startup folder, scheduled task) has to
    say its name rather than being inferred. No argv, no paths: this lands in a
    file that may be read out of context.
    """
    return {"schema": 1, "pid": os.getpid(), "entry": entry,
            "trigger": env_value(TRIGGER_ENV, LEGACY_TRIGGER_ENV) or None, "at": utc_stamp()}


def _state_path(root: Path) -> Path:
    return meta_dir(root) / STATE_FILE


def last_run(root: Path) -> dict:
    path = _state_path(root)
    return read_json(path) if path.is_file() else {}


def _minutes_since(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds() / 60.0


def catchup_all(written_by: str | None = None, force: bool = False) -> dict:
    """Collect every registered workspace, each on its own interval.

    Missing directories and paused units are skipped, never errors. This is
    what the logon trigger and the in-app timer call, so switching the active
    workspace no longer stops collection of the others.
    """
    from .control import is_paused
    from .settings import collect_interval
    from .state import list_workspaces

    results: list[dict] = []
    for item in list_workspaces():
        path = Path(item["path"])
        if not path.is_dir():
            results.append({"workspace": item["path"], "skipped": True, "reason": "missing"})
            continue
        try:
            if is_paused(path):
                results.append({"workspace": item["path"], "skipped": True, "reason": "paused"})
                continue
            minutes = collect_interval(path)
        except LabError as exc:
            results.append({"workspace": item["path"], "skipped": True, "reason": str(exc)})
            continue
        try:
            outcome = catchup(path, min_interval_minutes=minutes, written_by=written_by, force=force)
        except LabError as exc:
            results.append({"workspace": item["path"], "error": str(exc)})
            continue
        results.append({"workspace": item["path"], **outcome})
    return {
        "workspaces": len(results),
        "collected": sum(1 for item in results if not item.get("skipped") and not item.get("error")),
        "results": results,
    }


def catchup(root: Path, min_interval_minutes: int = 30, force: bool = False,
            written_by: str | None = None) -> dict:
    load_unit(root)
    if is_paused(root):
        return {"skipped": True, "reason": "paused"}

    sources = load_sources(root)
    if not sources:
        return {"skipped": True, "reason": "no sources declared"}

    state = last_run(root)
    elapsed = _minutes_since(state.get("at"))
    if not force and elapsed is not None and elapsed < min_interval_minutes:
        return {
            "skipped": True,
            "reason": "within min_interval",
            "minutes_since_last": round(elapsed, 1),
            "min_interval_minutes": min_interval_minutes,
        }

    scan = build_scan(root)
    scan_path = save_scan(root, scan)

    digest_note = "written"
    try:
        digest = build_digest(root)
        if written_by:
            digest["written_by"] = _written_by(written_by)
        digest_path = save_digest(root, digest)
        digest_result = {
            "digest": digest_path.relative_to(root).as_posix(),
            "file_count": digest["file_count"],
            "unattributed_outside": digest["unattributed_outside"],
            "unattributed_no_cwd": digest["unattributed_no_cwd"],
        }
    except LabError as exc:
        # A unit may declare roots for scanning but no readable sub-path yet.
        digest_note = "skipped: %s" % exc
        digest_result = {"digest": None}

    result = {
        "skipped": False,
        "at": utc_stamp(),
        "scan": scan_path.relative_to(root).as_posix(),
        "file_count": scan["file_count"],
        "digest": digest_result.get("digest"),
        "digest_status": digest_note,
    }
    if digest_result.get("digest"):
        result.update(digest_result)
        # 证据变了就顺手刷新注入载荷（或按闸门停发）。它绝不换源、绝不注册 hook，
        # 也不能因为注入出问题而让采集失败。
        try:
            injection = refresh_injection(root)
        except (LabError, OSError, ValueError):
            injection = None
        if injection:
            result["injection"] = injection
    # Vault collection: write digest files to immutable shards.
    # Implemented by the vault session (2026-11-09), not the peer.
    if digest_result.get("digest") and digest.get("files"):
        try:
            vault_result = collect_to_vault(root, digest)
            result["vault"] = vault_result
        except (LabError, OSError, ValueError) as exc:
            result["vault"] = {"error": str(exc)}
    write_json(_state_path(root), result)
    return result


# Vault collection: implemented by the vault session (2026-11-09), not the peer.
def collect_to_vault(root: Path, digest: dict) -> dict:
    """Write digest files to vault, return stats.

    For each file in the digest:
    - If `transcript: True`: extract from the file path
    - If `transcript: False`: extract from DB (DB-only session)

    Returns a summary with attempted/written counts and per-session results.
    """
    vault_root = vault_mod.vault_root()
    sources_by_name = {s["name"]: s for s in load_sources(root)}

    results: list[dict] = []
    for file in digest.get("files", []):
        source = sources_by_name.get(file["source"])
        if not source:
            continue

        session_key = file.get("session_key")
        if not session_key:
            continue

        harness = file.get("harness", "unknown")
        agent_id = file.get("agent_id")

        try:
            if file.get("transcript"):
                abs_path = Path(source["path"]) / file["path"]
                if not abs_path.is_file():
                    continue
                result = vault_mod.extract_transcript(
                    vault_root, harness, session_key, abs_path,
                    rel=file["path"], agent_id=agent_id
                )
            else:
                from . import qoder_db
                db_path = qoder_db.locate_db(Path(source["path"]))
                if not db_path:
                    continue
                session_id = file["path"].removeprefix("db/")
                rows = qoder_db.message_rows(db_path, session_id)
                if not rows:
                    continue
                result = vault_mod.extract_db_session(
                    vault_root, session_key, rows, harness=harness, agent_id=agent_id
                )
            results.append({"session_key": session_key, **result})
        except LabError as exc:
            results.append({"session_key": session_key, "error": str(exc)})

    return {
        "vault_root": str(vault_root),
        "sessions_attempted": len(results),
        "sessions_written": sum(1 for r in results if r.get("written", 0) > 0),
        "results": results,
    }
