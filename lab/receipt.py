"""Byte-exact receipts: hash a unit's files, then prove them later."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .workspace import (
    META_DIR,
    RECEIPTS_DIR,
    LabError,
    load_agents,
    load_unit,
    meta_dir,
    read_json,
    utc_stamp,
    write_json,
)
from . import gitutil, unitlink

SKIP_DIRS = {META_DIR, ".git", "__pycache__", ".venv", "node_modules"}
SKIP_FILES = {unitlink.LINK_FILE}
CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def tracked_files(root: Path) -> list[str]:
    """Every regular file in the unit except metadata and tool caches.

    Paths are POSIX-style so a receipt made on Windows verifies on Linux.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.as_posix() in SKIP_FILES:
            continue
        found.append(rel.as_posix())
    return found


def git_head(root: Path) -> str | None:
    completed = gitutil.run(["rev-parse", "HEAD"], cwd=root, timeout=5)
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def build_receipt(root: Path, note: str | None = None, allow_empty: bool = False) -> dict:
    unit = load_unit(root)
    files = []
    for rel in tracked_files(root):
        path = root / rel
        files.append(
            {
                "path": rel,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not files and not allow_empty:
        raise LabError(
            "unit has no files to attest; add files or pass --allow-empty deliberately"
        )
    return {
        "schema": 1,
        "kind": "GROMA_LOCAL_RECEIPT",
        "unit_id": unit["unit_id"],
        "unit_name": unit.get("name"),
        "carrier": unit.get("carrier"),
        "git_head": git_head(root),
        "created_at": utc_stamp(),
        "note": note or "",
        "agents": load_agents(root),
        "file_count": len(files),
        "files": files,
    }


def save_receipt(root: Path, receipt: dict) -> Path:
    out = meta_dir(root) / RECEIPTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = receipt["created_at"].replace("-", "").replace(":", "")
    target = out / ("receipt-%s.json" % stamp)
    counter = 1
    while target.exists():
        target = out / ("receipt-%s-%d.json" % (stamp, counter))
        counter += 1
    write_json(target, receipt)
    return target


def verify_receipt(root: Path, receipt_path: Path) -> dict:
    receipt = read_json(receipt_path)
    entries = receipt.get("files", [])
    problems: list[dict] = []

    for entry in entries:
        rel = entry.get("path", "")
        path = root / rel
        if not path.is_file():
            problems.append({"path": rel, "problem": "missing"})
            continue
        actual_size = path.stat().st_size
        if actual_size != entry.get("bytes"):
            problems.append(
                {
                    "path": rel,
                    "problem": "size_changed",
                    "expected": entry.get("bytes"),
                    "actual": actual_size,
                }
            )
            continue
        actual_sha = sha256_file(path)
        if actual_sha != entry.get("sha256"):
            problems.append(
                {
                    "path": rel,
                    "problem": "sha256_changed",
                    "expected": entry.get("sha256"),
                    "actual": actual_sha,
                }
            )

    recorded = {entry.get("path") for entry in entries}
    extra = [rel for rel in tracked_files(root) if rel not in recorded]

    if not entries:
        # An empty receipt proves nothing; a green light here would let absence pass as evidence.
        verdict = "NOT_PROVEN"
    elif problems or extra:
        verdict = "FAIL"
    else:
        verdict = "PASS"

    return {
        "kind": "GROMA_LOCAL_VERIFICATION",
        "receipt": receipt_path.name,
        "unit_id": receipt.get("unit_id"),
        "checked": len(entries),
        "problems": problems,
        "extra_files": extra,
        "ok": verdict == "PASS",
        "verdict": verdict,
    }
