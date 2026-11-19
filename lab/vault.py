"""Where the context vault lives, and what disqualifies a location.

Nothing here reads or writes transcript bodies yet: these are the guardrails that
have to run *before* the first shard is written, because a vault in the wrong
place leaks full conversations (a stray `git add .`, another user on the box, a
sync client pushing plaintext to the cloud).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .contract import LEGACY_STATE_NAME, LEGACY_VAULT_ENV, STATE_NAME, env_value
from .digest import file_sha256
from .state import list_workspaces, normalise_path, state_base, state_root
from .workspace import LabError, read_json, write_json

VAULT_FILE = "vault.json"
VAULT_ENV = "GROMA_VAULT_DIR"

# Windows MAX_PATH is 260. The budget is spent on the deepest shape the vault
# actually writes (see check_root); PATH_WARN leaves room for one more folder.
PATH_LIMIT = 260
PATH_WARN = 240

DIR_MODE = 0o700
SHARD_MODE = 0o600

# Matched as a substring *inside* one path component, because the real folders are
# decorated: iCloud is `Mobile Documents` (a space), a business tenant is
# `OneDrive - Contoso`. Exact-component matching passes both of those.
# Still name-based, so a user-renamed sync root slips through; the authoritative
# network/removable answer on Windows is the drive-type probe below.
SYNC_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "nutstore",
    "坚果云",
    "box sync",
    "mobile documents",
    "icloud",
)
EXTERNAL_PREFIXES = ("/mnt/", "/media/", "/volumes/")


def default_root() -> Path:
    """Same platform dispatch that state uses; never a second copy of it."""
    base = state_base()
    canonical = base / STATE_NAME / "vault"
    legacy = base / LEGACY_STATE_NAME / "vault"
    if not hasattr(canonical, "is_dir"):
        return canonical
    return canonical if canonical.is_dir() or not legacy.is_dir() else legacy


def config_path() -> Path:
    return state_root() / VAULT_FILE


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        return {"schema": 1}
    try:
        return read_json(path)
    except ValueError:
        return {"schema": 1}


def vault_root() -> Path:
    override = env_value(VAULT_ENV, LEGACY_VAULT_ENV)
    if override and override.strip():
        return Path(override.strip())
    saved = load_config().get("root")
    if saved:
        return Path(saved)
    return default_root()


def _components(path: Path) -> list[str]:
    return [part.casefold() for part in path.parts]


def in_git_worktree(path: Path) -> Path | None:
    """The work-tree root that would swallow this path, or None.

    Checks for a ``.git`` entry, which is right for both a repository (a
    directory) and a linked worktree or submodule (a file). The walk is on the
    resolved path: a symlink out of a work tree still sits inside it.
    """
    cursor = Path(path).resolve()
    for candidate in [cursor] + list(cursor.parents):
        if (candidate / ".git").exists() or (candidate / ".git").is_symlink():
            return candidate
    return None


def _windows_drive_type(path: Path) -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        root = str(Path(path.drive or "C:") / "\\")
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except (OSError, ValueError):
        return None


def _windows_filesystem(path: Path) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        root = str(Path(path.drive or "C:") / "\\")
        buffer = ctypes.create_unicode_buffer(256)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, None, None, None, buffer, 256
        )
        return buffer.value.casefold() if ok else None
    except (OSError, ValueError):
        return None


def _inside_registered_workspace(path: Path) -> str | None:
    target = normalise_path(path)
    for record in list_workspaces():
        root = normalise_path(record.get("path") or "")
        if not root:
            continue
        if target == root or target.startswith(root + "/"):
            return record.get("path") or root
    return None


def check_root(path: str | Path) -> dict:
    """Every guardrail finding for a candidate vault root. Writes nothing.

    Resolved up front: `..` and symlinks must not be able to hide either a work
    tree (C1) or a sync folder (C7), and the answer is stored in `vault.json`, so
    a relative path would freeze a location that later means something else.
    """
    candidate = Path(path).expanduser().resolve()
    findings: list[dict] = []

    def reject(code: str, detail: str) -> None:
        findings.append({"code": code, "level": "reject", "detail": detail})

    def warn(code: str, detail: str) -> None:
        findings.append({"code": code, "level": "warn", "detail": detail})

    work_tree = in_git_worktree(candidate)
    if work_tree is not None:
        reject(
            "C1",
            "vault 不得建在 git 工作树内：%s 属于工作树 %s；"
            "一次 `git add .` 就会把对话全文推进仓库并共享给全组织" % (candidate, work_tree),
        )

    host = _inside_registered_workspace(candidate)
    if host is not None:
        reject("C2", "vault 落在已登记工作区 %s 内部，采集会自吞，且与 .groma/ 证据混在一处" % host)

    folded = "/" + normalise_path(candidate).lstrip("/")
    hits = sorted({part for part in _components(candidate) if any(marker in part for marker in SYNC_MARKERS)})
    if hits:
        reject(
            "C7",
            "vault 落在同步盘目录（命中 %s）：占位文件会让分片写入与哈希失真，"
            "明文对话还会被自动推上云" % ", ".join(hits),
        )
    if not hits and any(folded.startswith(prefix) for prefix in EXTERNAL_PREFIXES):
        warn("C7", "%s 看起来是外接卷/挂载点：FAT32/exFAT 上没有 POSIX 权限，归档可能被同机其他用户读到" % candidate)

    drive_type = _windows_drive_type(candidate)
    if drive_type == 4:
        reject("C7", "vault 的盘符是网络映射盘：rename 原子性与跨进程锁语义都不保证")
    elif drive_type == 2:
        warn("C7", "vault 的盘符是可移动卷：拔出即归档失踪，且多数格式无 ACL")

    filesystem = _windows_filesystem(candidate)
    if filesystem in {"fat", "fat32", "exfat"}:
        warn("C8", "卷 %s 的文件系统是 %s，没有 POSIX 权限位，0700/0600 的约束落不了地" % (candidate.drive, filesystem))

    if os.name == "nt":
        # §10 measures the real ids at 36 (uuid) to ~60 (codex `rollout-…`); 64 is
        # the budget with slack, plus the harness directory and one shard name.
        deepest = candidate / "claude-code" / ("x" * 64) / "s000001_20261105.ndjson"
        depth = len(str(deepest))
        if depth > PATH_LIMIT:
            reject(
                "LEN",
                "路径预算超限：最深分片名 %d 字符 > Windows MAX_PATH %d；"
                "换到更浅的位置，或先启用长路径支持" % (depth, PATH_LIMIT),
            )
        elif depth > PATH_WARN:
            warn("LEN", "路径已达 %d 字符，接近 MAX_PATH，搬家时可能顶到" % depth)

        if not _under_per_user_profile(candidate):
            warn(
                "C8",
                "Windows 上 vault 不在用户配置目录内：%s 的 ACL 不由该用户掌控，"
                "也没有显式权限位可断言（POSIX 上创建时会 chmod 0700）" % candidate,
            )

    verdict = "reject" if any(f["level"] == "reject" for f in findings) else (
        "warn" if findings else "ok"
    )
    return {
        "schema": 1,
        "root": str(candidate),
        "verdict": verdict,
        "findings": findings,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _under_per_user_profile(path: Path) -> bool:
    for env_name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        value = os.environ.get(env_name)
        if not value:
            continue
        root = normalise_path(value)
        target = normalise_path(path)
        if target == root or target.startswith(root + "/"):
            return True
    return False


def ensure_usable(path: str | Path) -> dict:
    """Return the check, refusing a location that any reject finding covers."""
    report = check_root(path)
    if report["verdict"] == "reject":
        reasons = "; ".join("%s: %s" % (f["code"], f["detail"]) for f in report["findings"] if f["level"] == "reject")
        raise LabError("vault 根目录不可用（%s）—— %s" % (report["root"], reasons))
    return report


def apply_dir_mode(path: Path) -> None:
    """0700 on POSIX. Windows has no chmod semantics here; the ACL is asserted, not set."""
    if os.name != "nt":
        os.chmod(path, DIR_MODE)


def safe_dir(session_key: str) -> str:
    """The on-disk directory name for one session.

    `:` is legal in a logical key and illegal in a Windows path, so the separator
    is escaped on *every* OS: one vault moved between machines must still present
    the same names, not the names this OS happens to tolerate. Case is folded
    unconditionally for the same reason — Linux would otherwise keep two spellings
    of one session, and §11 asks for one byte-identical name on all three.
    """
    raw = session_key.split(":", 1)[1] if ":" in session_key else session_key
    folded = raw.replace("\\", "-").replace("/", "-").replace(":", "-").casefold()
    folded = folded.strip(".-")
    if not folded:
        raise LabError("会话 id 无法形成安全的目录名: %r" % session_key)
    return folded


def save_root(path: str | Path) -> dict:
    """Validate then persist the vault root, and create it with its modes."""
    report = ensure_usable(path)
    root = Path(report["root"])
    root.mkdir(parents=True, exist_ok=True)
    apply_dir_mode(root)
    write_json(config_path(), {"schema": 1, "root": str(root), "last_check": report, "set_at": report["checked_at"]})
    return report


# --- writing segments into shards ----------------------------------------------------

SEGMENT_FILE = "segment.json"
SHARD_SUFFIX = ".ndjson"
SHARD_BYTES_LIMIT = 64 * 1024 * 1024

# A record without these cannot be rotated, anchored, or folded onto a later
# capture, so the writer refuses rather than filling in a plausible-looking value.
RECORD_REQUIRED = ("ts_utc", "granularity", "session_key", "source")


def session_dir(root: str | Path, harness: str, session_key: str) -> Path:
    return Path(root) / harness / safe_dir(session_key)


def shard_name(seq: int, day: str) -> str:
    """`s<000001>_<YYYYMMDD>.ndjson`: the counter is the key, the date is for eyes."""
    return "s%06d_%s%s" % (seq, day, SHARD_SUFFIX)


def canonical_bytes(record: dict) -> bytes:
    """The one JSON spelling of a record.

    Compact (no indent) because a shard is NDJSON: one record per line, so an
    indented body would no longer be one record per line. The indent-2 convention
    stays with `vault.json` / `segment.json` / `index.json`.
    """
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stored_sha256(record: dict) -> str:
    """Hash of the canonical record *without* this field, so it is recomputable."""
    body = {key: value for key, value in record.items() if key != "stored_sha256"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _day(ts_utc: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LabError("vault 记录的 ts_utc 无法解析: %r" % (ts_utc,)) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%d")


def _load_segment(path: Path) -> dict:
    if not path.is_file():
        return {"next_record_seq": 1, "next_shard": 1, "covered": {}, "shards": [], "duplicates": 0}
    try:
        segment = read_json(path)
    except ValueError:
        raise LabError("vault 的 segment.json 坏了，先修再写: %s" % path)
    segment.setdefault("next_record_seq", 1)
    segment.setdefault("next_shard", 1)
    segment.setdefault("covered", {})
    segment.setdefault("shards", [])
    return segment


def _validate(record: dict, harness: str, session_key: str) -> tuple[str, int]:
    missing = [field for field in RECORD_REQUIRED if not record.get(field)]
    if missing:
        raise LabError("vault 记录缺字段 %s，写入被拒: %s" % (missing, session_key))
    if record["session_key"] != session_key:
        raise LabError("记录属于 %s，却正要写进 %s 的分片" % (record["session_key"], session_key))
    if record["granularity"] not in ("event", "turn", "span"):
        raise LabError("未知的 granularity: %r" % record["granularity"])
    source = record["source"]
    if not source.get("path") or source.get("line_no") is None:
        raise LabError("记录没有 source.path / source.line_no，无法增量对齐: %s" % session_key)
    if not source.get("source_sha256"):
        raise LabError(
            "记录缺 source.source_sha256（DB-only 会话也要给出行的字节指纹），"
            "不给你假造一个: %s" % session_key
        )
    return str(source["path"]), int(source["line_no"])


def append_records(
    root: str | Path,
    harness: str,
    session_key: str,
    records: list[dict],
    shard_bytes_limit: int = SHARD_BYTES_LIMIT,
) -> dict:
    """Write new segments into freshly opened shards. Closed shards are never touched.

    The cursor is the source line number, so replaying a batch writes nothing and
    creates nothing. A source line at or below the cursor that now carries other
    bytes keeps the bytes the vault first saw: the shard is the capture record,
    and `source.source_sha256` is what lets an anchor notice the source drifted.
    """
    where = session_dir(root, harness, session_key)
    segment_path = where / SEGMENT_FILE
    segment = _load_segment(segment_path)
    stored_key = segment.get("session_key")
    if stored_key and stored_key != session_key:
        raise LabError(
            "%s 这个目录已经归给 %s，现在要往里写 %s —— 两个会话键折成了同一个目录名，"
            "写下去就是让两段对话互相覆盖" % (where, stored_key, session_key)
        )
    covered = segment["covered"]

    fresh: list[tuple[dict, str, int]] = []
    duplicates = 0
    for record in records:
        source_path, line_no = _validate(record, harness, session_key)
        day = _day(record["ts_utc"])
        if line_no <= covered.get(source_path, 0):
            duplicates += 1
            continue
        bucket = covered.setdefault(source_path, 0)
        covered[source_path] = max(bucket, line_no)
        fresh.append((record, day, line_no))

    if not fresh:
        return {
            "session_key": session_key,
            "harness": harness,
            "written": 0,
            "duplicates": duplicates,
            "shards": [],
        }

    if not where.is_dir():
        where.mkdir(parents=True)
        apply_dir_mode(where)

    record_seq = segment["next_record_seq"]
    shard_seq = segment["next_shard"]
    open_shard: dict | None = None
    written_shards: list[dict] = []

    def close(shard: dict) -> None:
        digest = file_sha256(Path(shard["path"]))
        shard["file_sha256"] = digest
        shard.pop("path")
        shard.pop("day")
        segment["shards"].append(shard)
        written_shards.append(dict(shard, file_sha256=digest))

    for record, day, _line_no in fresh:
        body = dict(record)
        body["seq"] = record_seq
        body["stored_sha256"] = stored_sha256(body)
        line = canonical_bytes(body) + b"\n"

        if open_shard is None or open_shard["day"] != day or open_shard["bytes"] + len(line) > shard_bytes_limit:
            if open_shard is not None:
                close(open_shard)
            name = shard_name(shard_seq, day)
            open_shard = {
                "name": name,
                "path": where / name,
                "day": day,
                "bytes": 0,
                "records": 0,
                "from_ts": record["ts_utc"],
                "to_ts": record["ts_utc"],
            }
            # "xb", not "wb": a shard name that is already on disk means the cursor
            # and the directory disagree, and reopening the file would edit history.
            try:
                with open_shard["path"].open("xb"):
                    pass
            except FileExistsError as exc:
                raise LabError(
                    "分片 %s 已经存在，但游标不知道——通常是上一轮写完分片、还没写 segment.json 时崩了。"
                    "先重建索引再继续: %s" % (open_shard["name"], where)
                ) from exc
            if os.name != "nt":
                os.chmod(open_shard["path"], SHARD_MODE)
            shard_seq += 1

        with open_shard["path"].open("ab") as handle:
            handle.write(line)
        open_shard["bytes"] += len(line)
        open_shard["records"] += 1
        open_shard["to_ts"] = record["ts_utc"]
        record_seq += 1

    if open_shard is not None:
        close(open_shard)

    segment.update(
        {
            "schema": 1,
            "session_key": session_key,
            "harness": harness,
            "next_record_seq": record_seq,
            "next_shard": shard_seq,
            "covered": covered,
            "duplicates": segment.get("duplicates", 0) + duplicates,
            "extracted_through": {"seq": record_seq - 1, "ts_utc": fresh[-1][0]["ts_utc"]},
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    write_json(segment_path, segment)
    return {
        "session_key": session_key,
        "harness": harness,
        "written": len(fresh),
        "duplicates": duplicates,
        "shards": written_shards,
    }


# --- index: the only read path upper layers may use ---------------------------------

INDEX_FILE = "index.json"
UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _line_hash_head(path: Path) -> str | None:
    with path.open("rb") as handle:
        line = handle.readline()
    return stored_sha256(json.loads(line)) if line.strip() else None


def _line_hash_tail(path: Path) -> str | None:
    # Streaming rather than a seek window: a window can land record-aligned by
    # luck, and "tail hash unavailable" leaking in as a wrong hash is worse than
    # the extra read -- reindex hashes the whole file for verification anyway.
    last: bytes | None = None
    remainder = b""
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            parts = (remainder + block).split(b"\n")
            remainder = parts.pop()
            for part in parts:
                if part.strip():
                    last = part
    if remainder.strip():
        last = remainder
    return stored_sha256(json.loads(last)) if last else None


def _read_segment(path: Path) -> dict:
    try:
        return read_json(path)
    except ValueError as exc:
        raise LabError("vault 的 segment.json 坏了，先 reindex 修或删掉再建: %s" % path) from exc


def _parent_key(harness: str, paths: list[str]) -> str | None:
    """`.../<uuid>/subagents/<file>` nests the parent right there in the path.

    Path is not identity (§5), so the relationship is resolved once here and stored;
    nothing upstream should have to parse a path to group a subagent with its parent.
    """
    for rel in paths:
        parts = Path(rel).parts
        if "subagents" not in parts:
            continue
        index = parts.index("subagents")
        if index == 0:
            continue
        candidate = parts[index - 1].lower()
        if UUID_SHAPE.match(candidate):
            return "%s:%s" % (harness, candidate)
    return None


def scan_sessions(root: str | Path) -> list[dict]:
    """Enumerate the vault tree. This walk is the index layer's own job (C5)."""
    base = Path(root)
    found: list[dict] = []
    if not base.is_dir():
        return found
    for harness_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for session_dir in sorted(p for p in harness_dir.iterdir() if p.is_dir()):
            segment_file = session_dir / SEGMENT_FILE
            if not segment_file.is_file():
                continue
            segment = _read_segment(segment_file)
            found.append({"harness": harness_dir.name, "dir": session_dir, "segment": segment})
    return found


def build_index(root: str | Path, units: dict[str, list[str]] | None = None) -> dict:
    """Derive `index.json` from the shards. Byte-for-byte reproducible on the fields §6 lists."""
    base = Path(root)
    sessions: list[dict] = []
    drifted: list[dict] = []
    orphans: list[dict] = []
    keys_by_fold: dict[str, set[str]] = {}

    for record in scan_sessions(base):
        harness, segment, where = record["harness"], record["segment"], record["dir"]
        session_key = segment.get("session_key") or "%s:%s" % (harness, where.name)
        expected = safe_dir(session_key)
        if expected != where.name:
            raise LabError(
                "vault 目录 %s 里记的会话键折成 %s —— 目录名与键对不上，"
                "多半是键的归一化规则变过；先处理这一条再建索引" % (where, expected)
            )
        keys_by_fold.setdefault("%s/%s" % (harness, expected), set()).add(session_key)

        shards = []
        for entry in sorted(segment.get("shards", []), key=lambda item: item.get("name", "")):
            path = where / entry["name"]
            if not path.is_file():
                raise LabError("segment.json 记了 %s，但分片不在盘上: %s" % (entry["name"], where))
            actual = file_sha256(path)
            verified = actual == entry.get("file_sha256")
            if not verified:
                drifted.append({"session_key": session_key, "shard": entry["name"],
                                "recorded": entry.get("file_sha256"), "actual": actual})
            shards.append({
                "bytes": entry.get("bytes"),
                "file_sha256": actual,
                "from_ts": entry.get("from_ts"),
                "name": entry["name"],
                "records": entry.get("records"),
                "stored_sha256_head": _line_hash_head(path),
                "stored_sha256_tail": _line_hash_tail(path),
                "to_ts": entry.get("to_ts"),
                "verified": verified,
            })

        paths = sorted(segment.get("covered", {}))
        listed = {item["name"] for item in segment.get("shards", [])}
        for extra in sorted(p for p in where.iterdir() if p.is_file() and p.name.endswith(SHARD_SUFFIX)):
            if extra.name not in listed:
                orphans.append({
                    "bytes": extra.stat().st_size,
                    "file_sha256": file_sha256(extra),
                    "harness": harness,
                    "session_key": session_key,
                    "shard": extra.name,
                })
        sessions.append({
            "bytes_total": sum(int(item["bytes"] or 0) for item in shards),
            "extracted_through": segment.get("extracted_through"),
            "harness": harness,
            "parent_session_key": _parent_key(harness, paths),
            "paths": paths,
            "safe_dir": expected,
            "session_key": session_key,
            "shards": shards,
            "unit_ids": sorted((units or {}).get(session_key, [])),
        })

    merged = [key for key, values in keys_by_fold.items() if len(values) > 1]
    if merged:
        raise LabError(
            "两个不同的会话键折叠成同一个目录 %s —— 这就是正文互相覆盖的事故，"
            "索引拒建；先把这两个键分开" % ", ".join(sorted(merged))
        )

    return {
        "schema": 1,
        "kind": "GROMA_VAULT_INDEX",
        "root": str(base),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_count": len(sessions),
        "shard_count": sum(len(item["shards"]) for item in sessions),
        "bytes_total": sum(item["bytes_total"] for item in sessions),
        # Attribution is not derivable from shard bytes: it needs cwd + memberships,
        # so the caller supplies it and says so. Empty without a lie.
        "unit_ids_source": "supplied" if units is not None else "not_supplied",
        "drifted_shards": drifted,
        # A shard on disk that no segment lists is reported, never adopted: the
        # from_ts / records counts it would need are the writer's business, and
        # quietly folding it in would rewrite what a closed session is claimed to hold.
        "orphan_shards": orphans,
        "sessions": sorted(sessions, key=lambda item: (item["harness"], item["session_key"])),
    }


def reindex(root: str | Path, units: dict[str, list[str]] | None = None) -> dict:
    index = build_index(root, units)
    write_json(Path(root) / INDEX_FILE, index)
    return index


def read_index(root: str | Path) -> dict:
    """The only way upper layers may see the vault. A missing index is an error, not an empty vault."""
    path = Path(root) / INDEX_FILE
    if not path.is_file():
        raise LabError(
            "vault 索引不存在: %s —— 先 `reindex`。索引是派生品，"
            "没有它不等于没有内容，也不许改成遍历目录树去找分片" % path
        )
    try:
        return read_json(path)
    except ValueError as exc:
        raise LabError("vault 索引坏了，先 reindex 再读: %s" % path) from exc

# --- turning a harness transcript into records ---------------------------------------

EXTRACT_MODES = ("verbatim", "normalized")
NOISE_WRAPPERS = ("system-reminder", "local-command-caveat", "task-reminder")
# Qoder and Claude Code share the Anthropic block vocabulary; Codex wraps everything
# in a `response_item` whose own `payload.type` is the taxonomy.
CLAUDE_FAMILY_TYPES = ("user", "assistant", "attachment", "system")
REASONING_BLOCK_TYPES = ("thinking", "redacted_thinking")
CODEX_CONTENT_OUTER = "response_item"
TOOL_CALL_SUFFIX = "_call"
TOOL_RESULT_SUFFIX = "_call_output"


def strip_noise(text: str) -> tuple[str, list[str]]:
    """Remove harness-injected wrapper blocks and record what was removed."""
    removed: list[str] = []
    for tag in NOISE_WRAPPERS:
        open_tag, close_tag = "<%s>" % tag, "</%s>" % tag
        while open_tag in text:
            start = text.index(open_tag)
            end = text.find(close_tag, start)
            if end == -1:
                break
            text = text[:start] + text[end + len(close_tag):]
            removed.append(tag)
        if open_tag in text:
            # An unterminated block is still harness noise; drop it and say so.
            text = text[:text.index(open_tag)]
            removed.append(tag + "-unterminated")
    return text.strip(), sorted(set(removed))


def _clean(value: str, mode: str, stripped: list[str]) -> str:
    if mode != "normalized":
        return value
    cleaned, names = strip_noise(value)
    stripped.extend(names)
    return cleaned


def _blocks_of(container) -> list[dict]:
    if isinstance(container, str):
        return [{"type": "text", "text": container}]
    if not isinstance(container, list):
        return []
    return [item for item in container if isinstance(item, dict)]


def _message_blocks(parsed: dict) -> list[dict]:
    message = parsed.get("message")
    if isinstance(message, dict):
        return _blocks_of(message.get("content"))
    attachment = parsed.get("attachment")
    if isinstance(attachment, dict):
        return _blocks_of(attachment.get("content"))
    return []


def _payload_value(value, mode: str, stripped: list[str]):
    """Tool input and result stay whole (decision 3); strings get the wrapper accounting."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return _clean(value, mode, stripped)
        if isinstance(parsed, (dict, list)):
            # Codex stores `arguments` as a JSON string. Keeping the exact bytes
            # would make one call look different after any re-serialization, so the
            # value is normalised to canonical key order before hashing time.
            return _clean(json.dumps(parsed, ensure_ascii=False, sort_keys=True), mode, stripped)
        return _clean(value, mode, stripped)
    if isinstance(value, (dict, list)):
        return json.loads(json.dumps(value, ensure_ascii=False))
    return value


def _claude_family_parts(parsed: dict, mode: str, stripped: list[str]) -> tuple[list, list, list]:
    texts: list[str] = []
    reasoning: list[str] = []
    tools: list[dict] = []
    for block in _message_blocks(parsed):
        kind = block.get("type")
        if kind == "tool_use":
            tools.append({"call_id": block.get("id"), "name": block.get("name"),
                          "input": _payload_value(block.get("input"), mode, stripped), "result": None})
        elif kind == "tool_result":
            tools.append({"call_id": block.get("tool_use_id"), "name": None, "input": None,
                          "result": _payload_value(block.get("content"), mode, stripped)})
        elif kind in REASONING_BLOCK_TYPES:
            body = _clean(str(block.get("thinking") or block.get("text") or ""), mode, stripped)
            if body:
                reasoning.append(body)
        else:
            body = str(block.get("text") or block.get("input_text") or block.get("output_text") or "")
            body = _clean(body, mode, stripped)
            if body:
                texts.append(body)
    return texts, reasoning, tools


def _codex_parts(payload: dict, mode: str, stripped: list[str], stats: dict):
    kind = payload.get("type")
    texts: list[str] = []
    reasoning: list[str] = []
    tools: list[dict] = []
    encrypted = False
    if kind == "message":
        for block in _blocks_of(payload.get("content")):
            body = _clean(str(block.get("text") or block.get("input_text")
                              or block.get("output_text") or ""), mode, stripped)
            if body:
                texts.append(body)
    elif kind == "reasoning":
        for block in _blocks_of(payload.get("summary")):
            body = _clean(str(block.get("text") or block.get("summary_text") or ""), mode, stripped)
            if body:
                reasoning.append(body)
        encrypted = bool(payload.get("encrypted_content"))
        if encrypted and not reasoning:
            # Empty summary + ciphertext is not "the model did not think". Recording
            # the line with a flag is what keeps a later reader from reading the gap
            # as an absence.
            stats["reasoning_encrypted"] += 1
    elif isinstance(kind, str) and kind.endswith(TOOL_RESULT_SUFFIX):
        tools.append({"call_id": payload.get("call_id"), "name": payload.get("name"),
                      "input": None, "result": _payload_value(payload.get("output"), mode, stripped)})
    elif isinstance(kind, str) and kind.endswith(TOOL_CALL_SUFFIX):
        tools.append({"call_id": payload.get("call_id") or payload.get("id"),
                      "name": payload.get("name") or kind[: -len(TOOL_CALL_SUFFIX)],
                      "input": _payload_value(payload.get("arguments")
                                              if payload.get("arguments") is not None
                                              else payload.get("input"), mode, stripped),
                      "result": None})
    else:
        stats["skipped_payload"][str(kind)] = stats["skipped_payload"].get(str(kind), 0) + 1
        return None
    role = payload.get("role") or ("tool" if tools and not texts else "assistant")
    return role, kind, texts, reasoning, tools, encrypted


def scan_transcript(path: str | Path, harness: str, session_key: str, rel: str | None = None,
                    mode: str = "normalized", agent_id: str | None = None) -> tuple[list[dict], dict]:
    """One record per content-bearing source line, plus the skip accounting.

    `verbatim` keeps every byte; `normalized` only removes the wrapper blocks named in
    NOISE_WRAPPERS and lists them in `strip`, so "shorter" is never read as "changed".
    One line is one record even when it carries several blocks: the cursor is a
    per-file line number, so a second record on the same line would look like a
    duplicate next round and vanish.
    """
    if mode not in EXTRACT_MODES:
        raise LabError(
            "抽取模式 %r 还没实现（现有 %s）；redacted 涉及替换已存内容，不做半吊子"
            % (mode, " / ".join(EXTRACT_MODES))
        )
    source = Path(path)
    relative = rel or source.name
    is_codex = harness == "codex"
    stats: dict = {"lines": 0, "records": 0, "skipped_sidechain": 0, "empty": 0,
                   "reasoning_encrypted": 0, "skipped_outer": {}, "skipped_payload": {}}
    records: list[dict] = []

    with source.open("rb") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            stats["lines"] += 1
            # Hash the record's own bytes without the line terminator: a transcript
            # copied through a text-mode tool changes CRLF/LF, and an anchor that
            # breaks on that is an anchor nobody trusts.
            line_bytes = raw.rstrip(b"\r\n")
            try:
                parsed = json.loads(line_bytes.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue

            if is_codex:
                if parsed.get("type") != CODEX_CONTENT_OUTER:
                    outer = str(parsed.get("type"))
                    stats["skipped_outer"][outer] = stats["skipped_outer"].get(outer, 0) + 1
                    continue
                payload = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else {}
                stripped: list[str] = []
                parts = _codex_parts(payload, mode, stripped, stats)
                if parts is None:
                    continue
                role, harness_type, texts, reasoning, tools, encrypted = parts
            else:
                outer_type = parsed.get("type")
                if outer_type not in CLAUDE_FAMILY_TYPES:
                    stats["skipped_outer"][str(outer_type)] = stats["skipped_outer"].get(str(outer_type), 0) + 1
                    continue
                if parsed.get("isSidechain"):
                    # A sidechain line is another conversation embedded in this file;
                    # it belongs to the sub-agent's own key, not to this shard.
                    stats["skipped_sidechain"] += 1
                    continue
                stripped = []
                texts, reasoning, tools = _claude_family_parts(parsed, mode, stripped)
                role, harness_type, encrypted = str(outer_type), str(outer_type), False

            if not (texts or reasoning or tools) and not encrypted:
                stats["empty"] += 1
                continue
            if tools and not texts:
                kind_label = "tool"
            elif harness_type == "reasoning":
                # A reasoning item stays its own kind even when its text is not
                # exposed: filtering on `text != ""` must not erase the fact that
                # the model reasoned here.
                kind_label = "reasoning"
            else:
                kind_label = "message"
            records.append({
                "ts_utc": parsed.get("timestamp") or "",
                "granularity": "event",
                "session_key": session_key,
                "agent_id": agent_id,
                "role": role,
                "type": kind_label,
                "harness_type": harness_type,
                "text": "\n".join(texts),
                "reasoning": "\n".join(reasoning) or None,
                "reasoning_encrypted": encrypted,
                "tool": tools or None,
                "strip": sorted(set(stripped)),
                "profile": mode,
                "source": {
                    "path": relative,
                    "line_no": line_no,
                    "bytes": len(line_bytes),
                    "source_sha256": hashlib.sha256(line_bytes).hexdigest(),
                },
            })
            stats["records"] += 1
    return records, stats


def transcript_records(path: str | Path, harness: str, session_key: str, rel: str | None = None,
                       mode: str = "normalized", agent_id: str | None = None) -> list[dict]:
    return scan_transcript(path, harness, session_key, rel=rel, mode=mode, agent_id=agent_id)[0]


def extract_transcript(root: str | Path, harness: str, session_key: str, path: str | Path,
                       rel: str | None = None, mode: str = "normalized",
                       agent_id: str | None = None) -> dict:
    """Read one transcript and append whatever the cursor has not seen."""
    records, stats = scan_transcript(path, harness, session_key, rel=rel, mode=mode, agent_id=agent_id)
    for record in records:
        if not record["ts_utc"]:
            raise LabError("记录没有 ts_utc，无法轮转: %s 第 %d 行"
                           % (rel or path, record["source"]["line_no"]))
    result = append_records(root, harness, session_key, records)
    result["lines_read"] = stats["lines"]
    result["skipped"] = {key: value for key, value in stats.items() if key not in ("lines", "records")}
    return result


# --- DB-only sessions: the messages live in local.db, not in a transcript -------------

DB_SOURCE_TABLE = "chat_message"
# One locator per session stream, and `line_no` is the row position within it: the
# writer's cursor is keyed by source path, so giving every row its own path would
# make a replay look like all-new content and double-write the session.
DB_STREAM = "local.db#%s" % DB_SOURCE_TABLE


def _row_bytes(row: dict) -> bytes:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def db_records(rows: list[dict], harness: str, session_key: str, mode: str = "normalized",
               agent_id: str | None = None, first_seq: int = 1) -> tuple[list[dict], dict]:
    """Map `chat_message` rows onto §4 records, keeping the row's own bytes provable.

    `source.path` is a stream locator (`local.db#chat_message`) rather than a file path,
    because there is no file to go back to; `source.row_id` names the individual row so
    an anchor can still be re-checked against the database.
    `source_sha256` hashes the whole row as stored, so a later reader can tell a
    rewritten row from an unchanged one. `line_no` is the row's position ordered by
    (gmt_create, id) -- that is what the cursor compares against.
    """
    if mode not in EXTRACT_MODES:
        raise LabError("抽取模式 %r 还没实现（现有 %s）" % (mode, " / ".join(EXTRACT_MODES)))
    records: list[dict] = []
    stats = {"rows": 0, "records": 0, "empty": 0, "tool_rows": 0}
    for position, row in enumerate(rows, first_seq):
        stats["rows"] += 1
        text = row.get("content") or ""
        raw_tool = row.get("tool_result")
        tool = None
        if isinstance(raw_tool, str) and raw_tool.strip():
            try:
                tool = json.loads(raw_tool)
            except ValueError:
                tool = {"raw": raw_tool}
        elif isinstance(raw_tool, dict):
            tool = raw_tool
        if isinstance(tool, dict) and tool:
            stats["tool_rows"] += 1

        stripped: list[str] = []
        if mode == "normalized":
            text, names = strip_noise(str(text))
            stripped.extend(names)
        if not str(text).strip() and not tool:
            stats["empty"] += 1
            continue

        row_id = str(row.get("id") or "row-%d" % position)
        created = row.get("gmt_create")
        records.append({
            "ts_utc": _iso_utc(created),
            "granularity": "event",
            "session_key": session_key,
            "agent_id": agent_id,
            "role": str(row.get("role") or "unknown"),
            "type": "tool" if tool and not str(text).strip() else "message",
            "harness_type": DB_SOURCE_TABLE,
            "text": str(text),
            # `chat_record.reasoning_content` holds thinking; `chat_message` does not,
            # so a DB-only session says "no reasoning column here" instead of claiming
            # the model produced none.
            "reasoning": None,
            "reasoning_encrypted": False,
            "tool": _tool_payload(tool),
            "strip": sorted(set(stripped)),
            "profile": mode,
            "source": {
                "path": DB_STREAM,
                "row_id": row_id,
                "line_no": position,
                "bytes": len(_row_bytes(row)),
                "source_sha256": hashlib.sha256(_row_bytes(row)).hexdigest(),
            },
        })
        stats["records"] += 1
    return records, stats


def _tool_payload(tool):
    """Keep the vendor shape and lift the three fields §4 names."""
    if not isinstance(tool, dict):
        return None
    result = tool.get("results") if tool.get("results") is not None else (
        tool.get("content") if tool.get("content") is not None else tool.get("rawData"))
    return {
        "call_id": tool.get("toolCallId") or tool.get("call_id"),
        "name": tool.get("name"),
        "input": tool.get("parameters") if tool.get("parameters") is not None else tool.get("input"),
        "result": result,
        "raw": tool,
    }


def _iso_utc(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if isinstance(value, str) and value.strip():
        return value
    raise LabError("DB 行没有可用的 gmt_create，无法定时戳：%r" % (value,))


def extract_db_session(root: str | Path, session_key: str, rows: list[dict], harness: str = "qoder-cn",
                       mode: str = "normalized", agent_id: str | None = None) -> dict:
    """Append one DB-only session's messages, continuing from the stored cursor."""
    where = session_dir(root, harness, session_key)
    segment = _load_segment(where / SEGMENT_FILE)
    stored_key = segment.get("session_key")
    if stored_key and stored_key != session_key:
        raise LabError("%s 已归给 %s，不能改写进 %s" % (where, stored_key, session_key))
    # Positions come from the row list itself, never from the stored cursor: that is
    # what makes a replay of the same rows land on line numbers the cursor already
    # covers, and get counted as duplicates instead of written twice.
    records, stats = db_records(rows, harness, session_key, mode=mode, agent_id=agent_id)
    result = append_records(root, harness, session_key, records)
    result["rows_read"] = stats["rows"]
    result["skipped"] = {key: value for key, value in stats.items() if key != "rows"}
    return result


# --- move & purge: implemented by the vault session (2026-11-09), not the peer ------

def move_vault(to_path: str | Path) -> dict:
    """Move the vault root to a new location.

    Validates the new path, moves all content, updates the config.
    Returns a receipt with old/new roots and content stats.
    """
    import shutil

    old_root = vault_root()
    new_root = Path(to_path).expanduser().resolve()

    if old_root == new_root:
        return {"moved": False, "reason": "same path", "root": str(old_root)}

    check = ensure_usable(new_root)
    if check["verdict"] != "ok":
        raise LabError("新 vault 根不可用: %s" % check["findings"])

    new_root.mkdir(parents=True, exist_ok=True)
    apply_dir_mode(new_root)

    moved_dirs = []
    moved_bytes = 0
    if old_root.is_dir():
        for item in old_root.iterdir():
            if item.name == VAULT_FILE:
                continue
            dest = new_root / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
                shutil.rmtree(item)
            else:
                shutil.copy2(item, dest)
                item.unlink()
            moved_dirs.append(item.name)
            moved_bytes += sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())

    write_json(config_path(), {
        "schema": 1,
        "root": str(new_root),
        "last_check": check,
        "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "moved_from": str(old_root),
        "moved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    return {
        "moved": True,
        "old_root": str(old_root),
        "new_root": str(new_root),
        "dirs_moved": len(moved_dirs),
        "bytes_moved": moved_bytes,
        "health": check,
    }


def purge_vault(confirm: bool = False) -> dict:
    """Delete all vault content (shards, index, segments), keep the config.

    This is a dangerous operation. Requires explicit confirmation.
    Returns a receipt with what was deleted.
    """
    if not confirm:
        raise LabError("purge 需要 confirm=True，这是不可逆操作")

    root = vault_root()
    if not root.is_dir():
        return {"purged": False, "reason": "vault root does not exist", "root": str(root)}

    deleted_dirs = []
    deleted_files = 0
    deleted_bytes = 0

    for item in root.iterdir():
        if item.name == VAULT_FILE:
            continue
        if item.is_dir():
            for f in item.rglob("*"):
                if f.is_file():
                    deleted_bytes += f.stat().st_size
                    deleted_files += 1
            shutil.rmtree(item)
            deleted_dirs.append(item.name)
        else:
            deleted_bytes += item.stat().st_size
            deleted_files += 1
            item.unlink()

    return {
        "purged": True,
        "root": str(root),
        "dirs_deleted": len(deleted_dirs),
        "files_deleted": deleted_files,
        "bytes_deleted": deleted_bytes,
    }
