"""Reconcile what the vendor says exists against what we actually collected.

Three rules shape this module, all of them learned the hard way:

* The denominator must never come from our own output. `coverage` used to derive
  active hours from our digests, so a swept or relocated transcript simply left
  the set instead of raising a flag. Here the file universe is enumerated from
  the harness data directories and the session universe from the vendor
  database; our digests are only ever the *other* side of the diff.
* Root enumeration ignores `include`. An include that forgets a root -- the
  `cache/projects` conversation-history tree, `archived_sessions` -- is exactly
  the failure this module exists to surface, so it cannot also be the lens.
* Keys are not one shape. Qoder writes full uuidv7 names under `projects/`,
  8-character prefixes under `cache/projects/*/conversation-history/`, and
  subagent files as `agent-<type>-<16hex>`. Matching is prefix aware, and a
  prefix that resolves to more than one session is reported, never joined.

Nothing here reads message bodies, and no absolute path lands in the report:
harness homes appear as `ws-<16hex>` handles, roots as their relative name.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import state, topics, unitmatch
from .digest import normalise_session_id
from .sources import load_sources
from .workspace import LabError

KIND = "GROMA_TOPIC_COVERAGE"
SCHEMA = 1
MAX_EXAMPLES = 20

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PREFIX8_RE = re.compile(r"^[0-9a-f]{8}$")

# Transcript subroots per harness family, as they exist on a real machine.
# Deliberately wider than any declared include list.
SUBROOTS = {
    "qoder": ("projects", "cache/projects"),
    "codex": ("sessions", "archived_sessions"),
    "claude": ("projects",),
}
# The vendor's own session list, in preference order: the client cache database
# first, so a `local.db` sitting in a test fixture can never win.
DB_CANDIDATES = ("shared_client/cache/db/local.db", "cache/db/local.db", "local.db")
DB_COLUMNS = ("project_name", "version", "mode", "session_type", "parent_session_id")
FAMILIES = {
    "qoder": "qoder",
    "qoder-cn": "qoder",
    "codex": "codex",
    "claude": "claude",
    "claude-code": "claude",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def key_shape(token: str) -> str:
    if UUID_RE.match(token):
        return "uuid"
    if PREFIX8_RE.match(token):
        return "prefix8"
    if token.startswith("agent-"):
        return "subagent"
    return "other"


def token_of_file(path: Path) -> str:
    """The session id a transcript file name carries -- never the path itself."""
    return normalise_session_id(path.name)


def family_of(harness: object) -> str:
    text = str(harness or "").strip()
    return FAMILIES.get(text, text or "unknown")


def _declared(include: list[str], rel: str) -> bool:
    """Whether the source's own include list would have walked this subroot."""
    target = state.normalise_path(rel)
    for item in include:
        prefix = state.normalise_path(str(item))
        if not prefix:
            continue
        if prefix == target or prefix.startswith(target + "/") or target.startswith(prefix + "/"):
            return True
    return False


def workspace_folders(unit_root: Path) -> list[Path]:
    return [Path(folder) for folder in unitmatch.unit_workspaces(Path(unit_root))]


def _sources_of(folders: list[Path]) -> list[tuple[Path, dict]]:
    out = []
    for workspace in folders:
        try:
            sources = load_sources(workspace)
        except LabError:
            continue
        for source in sources:
            out.append((workspace, source))
    return out


def harness_roots_for(folders: list[Path]) -> list[dict]:
    """Every transcript subroot these workspaces point at, declared or not.

    One entry per (home, subroot): the same root reached from two workspaces
    counts once, so membership cannot inflate the numbers.
    """
    entries: dict[tuple[str, str], dict] = {}
    for workspace, source in _sources_of(folders):
        family = family_of(source.get("harness") or source.get("name"))
        home = Path(str(source.get("path") or ""))
        include = [str(item) for item in (source.get("include") or [])]
        for rel in SUBROOTS.get(family, ()):
            target = home / rel
            key = (state.normalise_path(target), family)
            entry = entries.get(key)
            if entry is None:
                entries[key] = {
                    "family": family,
                    "harness": str(source.get("harness") or family),
                    "source": str(source.get("name") or family),
                    "subroot": rel,
                    "path": target,
                    "home": home,
                    "home_ref": state.workspace_ref(home),
                    "exists": target.is_dir(),
                    "declared": _declared(include, rel),
                    "workspaces": {state.workspace_ref(workspace)},
                }
            else:
                entry["workspaces"].add(state.workspace_ref(workspace))
                entry["declared"] = entry["declared"] or _declared(include, rel)
    return sorted(entries.values(), key=lambda item: (item["family"], item["subroot"], item["home_ref"]))


def harness_roots(unit_root: Path) -> list[dict]:
    return harness_roots_for(workspace_folders(unit_root))


def stale_sources_for(folders: list[Path]) -> list[dict]:
    """Declared sources whose data directory no longer exists at all."""
    out = []
    for workspace, source in _sources_of(folders):
        home = Path(str(source.get("path") or ""))
        if not home.is_dir():
            out.append({
                "family": family_of(source.get("harness") or source.get("name")),
                "source": str(source.get("name") or ""),
                "workspace": state.workspace_ref(workspace),
            })
    return out


def stale_sources(unit_root: Path) -> list[dict]:
    return stale_sources_for(workspace_folders(unit_root))


def source_gaps_for(folders: list[Path]) -> dict[str, list[str]]:
    """Workspaces that declare no source for a harness family we otherwise know.

    A folder with no qoder source can only ever report zero Qoder sessions, and
    that reads as "nothing here" rather than as "not collected".
    """
    gaps: dict[str, set[str]] = {family: set() for family in SUBROOTS}
    for workspace in folders:
        try:
            sources = load_sources(workspace)
        except LabError:
            sources = []
        seen = {family_of(source.get("harness") or source.get("name")) for source in sources}
        for family in gaps:
            if family not in seen:
                gaps[family].add(state.workspace_ref(workspace))
    return {family: sorted(refs) for family, refs in sorted(gaps.items()) if refs}


def source_gaps(unit_root: Path) -> dict[str, list[str]]:
    return source_gaps_for(workspace_folders(unit_root))


def file_universe(roots: list[dict]) -> dict:
    """Every transcript file on disk, keyed by the id its own file name carries."""
    tokens: dict[tuple[str, str], dict] = {}
    by_root: dict[int, int] = {}
    files = 0
    for index, entry in enumerate(roots):
        by_root[index] = 0
        if not entry["exists"]:
            continue
        for path in entry["path"].rglob("*.jsonl"):
            token = token_of_file(path)
            if not token:
                continue
            files += 1
            by_root[index] += 1
            key = (entry["family"], token)
            record = tokens.get(key)
            if record is None:
                tokens[key] = {"shape": key_shape(token), "roots": {index}}
            else:
                record["roots"].add(index)
    return {"tokens": tokens, "by_root": by_root, "files": files}


def _read_chat_session(db: Path) -> dict | None:
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    except sqlite3.Error:
        return None
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_session)")}
        if "session_id" not in columns:
            return None
        wanted = [name for name in DB_COLUMNS if name in columns]
        sql = "SELECT session_id%s FROM chat_session" % (", " + ", ".join(wanted) if wanted else "")
        out: dict[str, dict] = {}
        for row in connection.execute(sql):
            meta = dict(zip(wanted, row[1:]))
            out[str(row[0])] = {key: (str(value) if value is not None else None)
                                for key, value in meta.items()}
        return out
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def vendor_universe(roots: list[dict]) -> dict:
    """The vendor's own session list: Qoder's chat_session table, identity columns only."""
    sessions: dict[str, dict] = {}
    databases: list[dict] = []
    homes: dict[str, Path] = {}
    for entry in roots:
        if entry["family"] == "qoder":
            homes.setdefault(entry["home_ref"], entry["home"])
    for home_ref in sorted(homes):
        home = homes[home_ref]
        found = next((rel for rel in DB_CANDIDATES if (home / rel).is_file()), None)
        if found is None:
            databases.append({"home": home_ref, "status": "absent"})
            continue
        rows = _read_chat_session(home / found)
        if rows is None:
            databases.append({"home": home_ref, "status": "unreadable", "rel": found})
            continue
        for session_id, meta in rows.items():
            sessions.setdefault(session_id, meta)
        databases.append({"home": home_ref, "status": "read", "rel": found, "sessions": len(rows)})
    return {"sessions": sessions, "databases": databases}


def identity_of(row: dict) -> tuple[str, str]:
    """(harness family, session token) -- the pair a ledger key and a digest row must agree on.

    The source name is a display label a user may rename (`qoder` ->
    `qoder-projects`); it is never half of an identity. The digest's own
    `session_key` is preferred, because a file name alone can be a capture
    timestamp (see `token_of_file`).
    """
    key = str(row.get("session_key") or "")
    if ":" in key:
        family, token = key.split(":", 1)
        return family_of(family), token
    token = token_of_file(Path(str(row.get("path") or "x")))
    return family_of(row.get("harness") or row.get("source")), token


def _collected_from_records(records: list[dict]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for record in records:
        for row in record.get("files") or []:
            family, token = identity_of(row)
            if token:
                out[token] = (family, str(row.get("source") or ""))
    return out


def citation_index_for(folders: list[Path]) -> dict[str, list[dict]]:
    """What may be cited, keyed by the session id -- never by the source label.

    A row's `source` survives into the citation as the label the digest holds
    *today* -- after a rename the asset must say `qoder-projects`, not the
    ledger's older `qoder`. Their `family` rides along so a lookup can refuse a
    cross-harness collision. Records arrive newest-first, so the first row seen
    for an identity wins.
    """
    out: dict[str, list[dict]] = {}
    records = unitmatch.digest_records_for([str(Path(folder)) for folder in folders])
    for record in records:
        for row in record.get("files") or []:
            family, token = identity_of(row)
            if not token:
                continue
            bucket = out.setdefault(token, [])
            if any(item.get("family") == family for item in bucket):
                continue
            bucket.append({**row, "digest": record.get("digest_name"), "family": family})
    return out


def resolve_citation(index: dict[str, list[dict]], key: tuple[str, str]) -> dict | None:
    """A ledger key `(source, path)` resolved against the citation index.

    The family comes from the source *only when the source is a known harness*;
    a user may name a collection source anything (`src`), and that name cannot
    decide an identity. In that case the session id alone is the identity -- and
    only when it is unambiguous.
    """
    source, path = key
    family = family_of(source)
    candidates = index.get(token_of_file(Path(str(path)))) or []
    for row in candidates:
        if row.get("family") == family:
            return row
    if family not in set(FAMILIES.values()) and len(candidates) == 1:
        return candidates[0]
    return None


def collected_tokens(unit_root: Path) -> dict[str, tuple[str, str]]:
    """What our digests claim, used only as the diff's other side.

    The digest's own `session_key` is preferred; a row without one falls back to
    its file name, which is the shape the vendor itself writes.
    """
    return _collected_from_records(digest_records(unit_root))


def digest_records(unit_root: Path) -> list[dict]:
    return unitmatch.digest_records_for(unitmatch.unit_workspaces(Path(unit_root)))


def _db_matches(token: str, shape: str, db_ids: list[str]) -> list[str]:
    """Which vendor sessions this file token is the transcript of."""
    if shape == "prefix8":
        return [session_id for session_id in db_ids if session_id.startswith(token + "-")]
    return [token] if token in db_ids else []


# --- 证据键存活判定------------------------------------------------
# 这里问的是"这条键还能不能被 `build_asset` 引到"，不是"世界上还有多少会话"：
# 引用索引就是 digest，用它是为了回答能不能引，**不是**把它当分母（见上「三规则」）。
# 厂商两栏（DB 与 transcript）只在键不可引时回答"为什么"。
LIVE = "live"
VENDOR_LISTS = "vendor_lists_not_digested"
GONE = "gone"
LIVENESS_STATES = (LIVE, VENDOR_LISTS, GONE)
LIVENESS_TEXT = {
    LIVE: "仍可引用",
    VENDOR_LISTS: "厂商侧仍在、本机未采进 digest",
    GONE: "厂商侧也查不到",
}


def evidence_liveness_for(folders: list[Path],
                          keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """每个证据键落在哪一档。

    `live`                      — 仍在 digest 引用索引里，`build_asset` 引得到；
    `vendor_lists_not_digested` — 索引里没有，但厂商的 DB 或 transcript 还在；
    `gone`                      — 索引没有、厂商侧也查不到。
    后两档都不能被引用，差别只在"为什么"，供界面与账本如实报数。
    """
    folders = [Path(folder) for folder in folders]
    roots = harness_roots_for(folders)
    files = file_universe(roots)
    vendor = vendor_universe(roots)
    collected = _collected_from_records(unitmatch.digest_records_for([str(f) for f in folders]))
    db_ids = sorted(vendor["sessions"])
    file_tokens: dict[str, set[str]] = {}
    for family, token in files["tokens"]:
        file_tokens.setdefault(family, set()).add(token)

    out: dict[tuple[str, str], str] = {}
    for source, path in keys:
        token = normalise_session_id(Path(str(path)).name)
        family = family_of(source)
        shape = key_shape(token)
        tokens = file_tokens.get(family, set())
        hits = _db_matches(token, shape, db_ids) if family == "qoder" else []
        vendor_seen = (bool(hits) or token in vendor["sessions"] or token in tokens
                       or any(len(item) == 8 and token.startswith(item + "-") for item in tokens))
        if token in collected:
            out[(source, path)] = LIVE
        elif vendor_seen:
            out[(source, path)] = VENDOR_LISTS
        else:
            out[(source, path)] = GONE
    return out


def evidence_liveness(unit_root: Path,
                      keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    return evidence_liveness_for(workspace_folders(Path(unit_root)), keys)


def build_reconciliation(unit_root: Path) -> dict:
    return build_reconciliation_for(workspace_folders(Path(unit_root)))


def build_reconciliation_for(folders: list[Path]) -> dict:
    folders = [Path(folder) for folder in folders]
    roots = harness_roots_for(folders)
    files = file_universe(roots)
    vendor = vendor_universe(roots)
    digests = unitmatch.digest_records_for([str(folder) for folder in folders])
    collected = _collected_from_records(digests)
    classified = topics.classification_counts(unitmatch.unit_files(digests))
    db_ids = sorted(vendor["sessions"])

    records: dict[str, dict] = {}

    def place(family: str, session_id: str) -> dict:
        record = records.get(session_id)
        if record is None:
            record = {"id": session_id, "family": family, "in_db": False, "in_files": False,
                      "collected": False, "file_shapes": [], "sources": []}
            records[session_id] = record
        return record

    for session_id, meta in vendor["sessions"].items():
        record = place("qoder", session_id)
        record["in_db"] = True
        record["db_version"] = meta.get("version")
        record["has_parent"] = bool(meta.get("parent_session_id"))

    ambiguous: dict[str, list[str]] = {}
    for (family, token), info in files["tokens"].items():
        hits = _db_matches(token, info["shape"], db_ids) if family == "qoder" else []
        if len(hits) > 1:
            # One prefix, several candidate sessions: saying which file belongs
            # to which session would be a guess, so no session claims the file.
            ambiguous[token] = hits
            continue
        for session_id in hits or [token]:
            record = place(family, session_id)
            record["in_files"] = True
            if info["shape"] not in record["file_shapes"]:
                record["file_shapes"].append(info["shape"])
            if not hits and info["shape"] == "prefix8":
                record["unanchored_prefix"] = True

    for token, (family, source) in collected.items():
        record = place(family, token)
        record["collected"] = True
        if source and source not in record["sources"]:
            record["sources"].append(source)

    rows = sorted(records.values(), key=lambda row: (row["family"], row["id"]))
    missing = [row for row in rows if not row["collected"]]
    unverifiable = [row for row in rows if row["collected"] and not row["in_files"] and not row["in_db"]]

    by_harness = {}
    for family in sorted({row["family"] for row in rows}):
        group = [row for row in rows if row["family"] == family]
        by_harness[family] = {
            "sessions": len(group),
            "with_transcript": sum(1 for row in group if row["in_files"]),
            "vendor_listed": sum(1 for row in group if row["in_db"]),
            "transcript_only": sum(1 for row in group if row["in_files"] and not row["in_db"]),
            "vendor_only": sum(1 for row in group if row["in_db"] and not row["in_files"]),
            "collected": sum(1 for row in group if row["collected"]),
            "missing": sum(1 for row in group if not row["collected"]),
            "unverifiable": sum(1 for row in group
                                if row["collected"] and not row["in_files"] and not row["in_db"]),
        }

    shapes: dict[str, int] = {}
    for info in files["tokens"].values():
        shapes[info["shape"]] = shapes.get(info["shape"], 0) + 1

    stale = stale_sources_for(folders)
    denominator = {
        "files": "enumerated from harness data directories; the include list was NOT consulted",
        "sessions": "vendor chat_session table, identity columns only",
        "digests": "the diff's other side only -- never a denominator",
    }
    counts = {
        "workspaces": len({ref for entry in roots for ref in entry["workspaces"]}),
        "transcript_files": files["files"],
        "transcript_tokens": len(files["tokens"]),
        "vendor_db_sessions": len(vendor["sessions"]),
        "vendor_databases": sum(1 for db in vendor["databases"] if db["status"] == "read"),
        "sessions": len(rows),
        "with_transcript": sum(1 for row in rows if row["in_files"]),
        "transcript_only": sum(1 for row in rows if row["in_files"] and not row["in_db"]),
        "vendor_only": sum(1 for row in rows if row["in_db"] and not row["in_files"]),
        "collected": sum(1 for row in rows if row["collected"]),
        "missing_from_collection": len(missing),
        "collected_unverifiable": len(unverifiable),
        "ambiguous_prefix8": len(ambiguous),
        "undeclared_roots": sum(1 for entry in roots if entry["exists"] and not entry["declared"]),
        "missing_roots": sum(1 for entry in roots if not entry["exists"]),
        "stale_sources": len(stale),
    }
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "generated_at": _now(),
        "denominator_source": denominator,
        "coverage": {
            "collected": counts["collected"],
            "in_vendor_list": counts["vendor_db_sessions"],
            "missing_from_collection": counts["missing_from_collection"],
            "unclassified": classified["unclassified"],
            "denominator_source": denominator,
        },
        "counts": counts,
        "by_harness": by_harness,
        "key_shapes": shapes,
        "roots": [
            {
                "family": entry["family"],
                "harness": entry["harness"],
                "subroot": entry["subroot"],
                "exists": entry["exists"],
                "declared_in_include": entry["declared"],
                "files": files["by_root"].get(index, 0),
                "home": entry["home_ref"],
                "workspaces": len(entry["workspaces"]),
            }
            for index, entry in enumerate(roots)
        ],
        "databases": vendor["databases"],
        "stale_sources": stale,
        "source_gaps": source_gaps_for(folders),
        "ambiguous_prefix": {token: hits[:4] for token, hits in sorted(ambiguous.items())[:MAX_EXAMPLES]},
        "examples": {
            "missing_from_collection": [
                {"id": row["id"], "family": row["family"], "in_files": row["in_files"],
                 "in_db": row["in_db"], "file_shapes": row["file_shapes"]}
                for row in missing[:MAX_EXAMPLES]
            ],
            "collected_unverifiable": [
                {"id": row["id"], "family": row["family"], "sources": row["sources"]}
                for row in unverifiable[:MAX_EXAMPLES]
            ],
        },
    }
