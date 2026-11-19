"""Structural digest of declared session files: counts and shape, never content.

The read set is derived from the same boundary used by `scan`, so the digest
cannot see a file that the scan would not have listed. Narrowing then happens
with include prefixes.

What is kept per file: bytes, line count, first/last timestamp, the set of
top-level key names, and a parse-error count. Values are never stored, except
timestamps (which are metadata). A test plants a sentinel inside message text
and asserts it does not appear anywhere in the digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .agents import agent_for_session, build_agents
from .qoder_db import session_inventory
from .scan import scan_source
from .sources import load_sources
from .state import normalise_path
from .workspace import LabError, load_unit, meta_dir, utc_stamp, write_json

DIGESTS_DIR = "digests"
SESSION_SUFFIXES = {".jsonl", ".ndjson"}
TIMESTAMP_KEYS = ("timestamp", "created_at", "updated_at", "time", "ts", "date")
MAX_HOURS_PER_FILE = 168


def _parse_include(item: str) -> tuple[str | None, str]:
    """`qoder:projects` scopes to one source; bare `projects` applies to all."""
    if ":" in item:
        source, _, prefix = item.partition(":")
        return source.strip(), prefix.strip("/")
    return None, item.strip("/")


def _under_include(source_name: str, rel: str, include: list[str]) -> bool:
    if not include:
        return True
    for item in include:
        source, prefix = _parse_include(item)
        if source is not None and source != source_name:
            continue
        if not prefix or rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False


def in_workspace(cwd: str | None, workspace: str) -> bool | None:
    """True/False when the cwd decides it; None when the session does not say."""
    if not cwd:
        return None
    current, root = normalise_path(cwd), normalise_path(workspace)
    return current == root or current.startswith(root + "/")


_UUID_TAIL = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)


def normalise_session_id(candidate: str) -> str:
    """The session's own id, stripped of the harness's file-name decoration.

    Codex writes `rollout-2026-08-10T17-34-36-<uuidv7>.jsonl`; the date part is
    when the file was made, not who the session is, and it differs between the two
    directories that already hold one session on this machine.
    """
    text = str(candidate).strip().removesuffix(".jsonl").removesuffix(".ndjson")
    found = _UUID_TAIL.search(text)
    return found.group(1).lower() if found else text


def session_key_for(harness: str, rel: str, known_id: str | None = None) -> str:
    """Stable identity for a session: harness + normalised id. Never the path."""
    return "%s:%s" % (harness, normalise_session_id(known_id or Path(rel).name))


SUBAGENTS_DIR = "subagents"
_SUBAGENT_FILE = re.compile(r"^agent-a(?P<type>.*?)-(?P<hex>[0-9a-f]{16})$", re.I)
_UUID_FULL = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def subagent_fields(harness: str, rel: str) -> dict:
    """What the path says about a transcript that lives inside another session.

    Qoder and Claude both write `<parent uuid>/subagents/agent-a<type>-<16hex>.jsonl`.
    The parent id sits right there in the path, but path is not identity
    (docs/CONTEXT-VAULT.md §5), so the relation is resolved once here and stored as
    a key instead of being re-parsed by every reader.
    """
    parts = Path(rel).parts
    if SUBAGENTS_DIR not in parts:
        return {"is_subagent": False, "parent_session_key": None}
    index = parts.index(SUBAGENTS_DIR)
    parent = parts[index - 1].lower() if index else ""
    fields = {
        "is_subagent": True,
        "parent_session_key": "%s:%s" % (harness, parent) if _UUID_FULL.match(parent) else None,
    }
    matched = _SUBAGENT_FILE.match(Path(rel).name.removesuffix(".jsonl").removesuffix(".ndjson"))
    if matched and matched.group("type"):
        # Claude names its files `agent-a<16hex>` with no type token. Absent beats
        # invented: an empty type is not an agent name.
        fields["sub_type"] = matched.group("type")
    return fields


def fill_sub_type(detail: dict, harness: str, rel: str) -> dict:
    """Give the row the sub-agent name its path states, without overriding a real one.

    A DB or transcript value is evidence about this session; the file name is
    evidence about how the harness stores it. The first wins when both exist.
    """
    if not detail.get("sub_type"):
        derived = subagent_fields(harness, rel).get("sub_type")
        if derived:
            detail["sub_type"] = derived
    return detail


def file_sha256(path: Path) -> str:
    """Raw-byte fingerprint of a transcript file; what an anchor checks against."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def identity_fields(harness: str, rel: str, source_root: str | Path | None = None,
                    known_id: str | None = None, transcript: bool = True) -> dict:
    """Who this session is, and what bytes it was read from.

    The row stays content-free; the fingerprint only proves a later anchor still
    describes the same bytes. Sessions that exist only in a database have no
    transcript to hash, so they say so rather than inventing a hash.
    """
    digest = file_sha256(Path(source_root) / rel) if transcript and source_root else None
    fields = subagent_fields(harness, rel)
    return {
        "session_key": session_key_for(harness, rel, known_id),
        "fingerprint_kind": "transcript-sha256" if transcript else "db-rows-only",
        "source_sha256": digest,
        "is_subagent": fields["is_subagent"],
        "parent_session_key": fields["parent_session_key"],
    }


def _hour_key(value: str) -> str | None:
    """ISO timestamp -> 'YYYY-MM-DDTHH' in UTC, or None if unparseable."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def digest_file(path: Path) -> dict:
    lines = 0
    parse_errors = 0
    keys: set[str] = set()
    first: str | None = None
    last: str | None = None
    cwd: str | None = None
    hours: set[str] = set()
    sub_type: str | None = None

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            lines += 1
            try:
                record = json.loads(raw)
            except ValueError:
                parse_errors += 1
                continue
            if not isinstance(record, dict):
                continue
            keys.update(str(key) for key in record)
            if cwd is None:
                candidate = record.get("cwd")
                if not (isinstance(candidate, str) and candidate.strip()):
                    payload = record.get("payload")
                    if isinstance(payload, dict):
                        candidate = payload.get("cwd")
                if isinstance(candidate, str) and candidate.strip():
                    cwd = candidate.strip()
            if sub_type is None:
                for sub_key in ("key_sub_agent_type", "sub_agent_type"):
                    candidate = record.get(sub_key)
                    if isinstance(candidate, str) and candidate.strip():
                        sub_type = candidate.strip()
                        break
            for key in TIMESTAMP_KEYS:
                value = record.get(key)
                if isinstance(value, str):
                    if first is None or value < first:
                        first = value
                    if last is None or value > last:
                        last = value
                    hour = _hour_key(value)
                    if hour and len(hours) < MAX_HOURS_PER_FILE:
                        hours.add(hour)
                    break

    return {
        "transcript_lines": lines,
        "first_ts": first,
        "last_ts": last,
        "hours": sorted(hours),
        "keys": sorted(keys),
        "parse_errors": parse_errors,
        "cwd": cwd,
        "sub_type": sub_type,
    }


def _qoder_source_files(root: Path, source: dict, workspace: str, wanted: list[str], agent_list: list[dict]):
    """Dual source: local.db decides which sessions exist, transcripts add structure."""
    inventory = session_inventory(Path(source["path"]))
    scanned, _ = scan_source(source)
    transcripts: dict[str, dict] = {}
    skipped = 0
    for entry in scanned:
        rel = entry["path"]
        if not _under_include(source["name"], rel, wanted):
            continue
        if Path(rel).suffix.lower() not in SESSION_SUFFIXES:
            skipped += 1
            continue
        transcripts[rel] = entry

    # The same session can appear under two project directories (e.g. after a
    # rename that only changed case or -/_). Keep the largest file per stem.
    by_stem: dict[str, str] = {}
    for rel, entry in transcripts.items():
        stem = Path(rel).stem
        current = by_stem.get(stem)
        if current is None or entry["bytes"] > transcripts[current]["bytes"]:
            by_stem[stem] = rel
    by_prefix: dict[str, str] = {}
    for stem, rel in by_stem.items():
        by_prefix.setdefault(stem[:8], rel)

    files: list[dict] = []
    used: set[str] = set()
    used_stems: set[str] = set()
    outside = 0
    no_cwd = 0
    outside_cwds: Counter = Counter()

    def add(rel: str, detail: dict, entry: dict | None, known_id: str | None = None) -> None:
        harness_name = source.get("harness", "unknown")
        fill_sub_type(detail, harness_name, rel)
        agent = agent_for_session(agent_list, harness_name, detail.get("sub_type"))
        unregistered = bool(detail.get("sub_type")) and agent.get("kind") != "sub"
        files.append(
            {
                "source": source["name"],
                "path": rel,
                "bytes": entry["bytes"] if entry else 0,
                "agent_id": agent["agent_id"],
                "agent_name": agent["name"],
                "agent_kind": agent["kind"],
                "agent_unregistered": unregistered,
                "parent_agent_id": agent.get("parent_agent_id"),
                "harness": agent.get("harness"),
                **identity_fields(
                    source.get("harness", "unknown"), rel,
                    Path(source["path"]) if entry else None, known_id, bool(entry),
                ),
                **detail,
            }
        )

    for session in inventory:
        where = session.get("workspace")
        if not where:
            no_cwd += 1
            continue
        if not in_workspace(where, workspace):
            outside += 1
            outside_cwds[where] += 1
            continue
        session_id = session["session_id"]
        rel = by_stem.get(session_id) or by_prefix.get(session_id[:8])
        if rel:
            used.add(rel)
            used_stems.add(Path(rel).stem)
            detail = digest_file(Path(source["path"]) / rel)
            detail["cwd"] = where  # the DB is authoritative for the workspace
            detail["transcript"] = True
            detail["events"] = session.get("messages") or detail["transcript_lines"]
            if session.get("sub_agent_type"):
                detail["sub_type"] = session["sub_agent_type"]
            add(rel, detail, transcripts[rel], session_id)
        else:
            add(
                "db/" + session_id,
                {
                    "events": session.get("messages", 0),
                    "transcript_lines": None,
                    "first_ts": session.get("first_ts"),
                    "last_ts": session.get("last_ts"),
                    "hours": session.get("hours", []),
                    "keys": [],
                    "parse_errors": 0,
                    "cwd": where,
                    "sub_type": session.get("sub_agent_type"),
                    "transcript": False,
                },
                None,
            )

    for rel, entry in transcripts.items():
        if rel in used:
            continue
        stem = Path(rel).stem
        # A duplicate of an already-used session, or a smaller copy of the same stem.
        if stem in used_stems or by_stem.get(stem) != rel:
            continue
        detail = digest_file(Path(source["path"]) / rel)
        belongs = in_workspace(detail.get("cwd"), workspace)
        if belongs is None:
            no_cwd += 1
            continue
        if not belongs:
            outside += 1
            outside_cwds[detail.get("cwd") or "(无 cwd)"] += 1
            continue
        detail["transcript"] = True
        detail["events"] = detail["transcript_lines"]
        add(rel, detail, entry)

    return files, outside, no_cwd, skipped, outside_cwds


def build_digest(root: Path, include: list[str] | None = None) -> dict:
    unit = load_unit(root)
    sources = load_sources(root)

    if include is None:
        # The declaration is the gate: each source names its own session sub-path.
        include = [
            "%s:%s" % (source["name"], prefix)
            for source in sources
            for prefix in source.get("include", [])
        ]
        if not include:
            raise LabError(
                "no include declared; add one with `sources add <name> --path <dir> --include <subpath>`"
            )

    wanted = [item.strip() for item in include if item.strip()]
    workspace = unit.get("workspace_root") or root.as_posix()
    harnesses = sorted({source.get("harness", "unknown") for source in sources})
    agent_list = build_agents(root, harnesses)

    files: list[dict] = []
    skipped_non_session = 0
    outside = 0
    undetermined = 0
    outside_cwds: Counter = Counter()
    for source in sources:
        if source.get("harness") == "qoder-cn":
            q_files, q_outside, q_nocwd, q_skipped, q_outside_cwds = _qoder_source_files(
                root, source, workspace, wanted, agent_list
            )
            files.extend(q_files)
            outside += q_outside
            outside_cwds.update(q_outside_cwds)
            undetermined += q_nocwd
            skipped_non_session += q_skipped
            continue
        entries, _skipped = scan_source(source)
        for entry in entries:
            rel = entry["path"]
            if not _under_include(source["name"], rel, wanted):
                continue
            if Path(rel).suffix.lower() not in SESSION_SUFFIXES:
                skipped_non_session += 1
                continue
            path = Path(source["path"]) / rel
            detail = digest_file(path)
            belongs = in_workspace(detail.get("cwd"), workspace)
            if belongs is None:
                undetermined += 1
                continue
            if not belongs:
                outside += 1
                outside_cwds[detail.get("cwd") or "(无 cwd)"] += 1
                continue
            detail["events"] = detail["transcript_lines"]
            fill_sub_type(detail, source.get("harness", "unknown"), rel)
            agent = agent_for_session(agent_list, source.get("harness", "unknown"), detail.get("sub_type"))
            files.append(
                {
                    "source": source["name"],
                    "path": rel,
                    "bytes": entry["bytes"],
                    "agent_id": agent["agent_id"],
                    "agent_name": agent["name"],
                    "agent_kind": agent["kind"],
                    "parent_agent_id": agent.get("parent_agent_id"),
                    "harness": agent.get("harness"),
                    **identity_fields(source.get("harness", "unknown"), rel, Path(source["path"])),
                    **detail,
                }
            )

    by_agent: dict[str, dict] = {}
    for item in files:
        slot = by_agent.setdefault(
            item["agent_id"],
            {"agent_id": item["agent_id"], "name": item["agent_name"], "kind": item["agent_kind"], "sessions": 0, "events": 0},
        )
        slot["sessions"] += 1
        slot["events"] += item["events"]

    files.sort(key=lambda item: (item["source"], item["path"]))
    return {
        "schema": 1,
        "kind": "GROMA_LOCAL_DIGEST",
        "unit_id": unit["unit_id"],
        "created_at": utc_stamp(),
        "content_stored": False,
        "workspace_root": workspace,
        "include": wanted,
        "file_count": len(files),
        "event_count": sum(item["events"] for item in files),
        "parse_errors": sum(item["parse_errors"] for item in files),
        "skipped_non_session": skipped_non_session,
        "unattributed_outside": outside,
        "unattributed_no_cwd": undetermined,
        # 为什么本目录一条也没有：那些会话的 cwd 其实指向别处。界面靠它自解释，
        # 不必为了回答这个问题在页面上重扫一遍采集根。
        "outside_cwds": [{"cwd": cwd, "sessions": n} for cwd, n in outside_cwds.most_common(8)],
        "agents": sorted(by_agent.values(), key=lambda item: (item["kind"], item["name"])),
        "files": files,
    }


def save_digest(root: Path, digest: dict) -> Path:
    out = meta_dir(root) / DIGESTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = digest["created_at"].replace("-", "").replace(":", "")
    target = out / ("digest-%s.json" % stamp)
    counter = 1
    while target.exists():
        target = out / ("digest-%s-%d.json" % (stamp, counter))
        counter += 1
    write_json(target, digest)
    return target
