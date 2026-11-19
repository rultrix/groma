"""Metadata-only scan of declared input roots.

This module deliberately never opens a file: it uses `os.scandir` plus `stat`
so that a scan cannot leak content. Reading comes later, behind its own verb.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .sources import load_sources
from .workspace import load_unit, meta_dir, utc_stamp, write_json

SCANS_DIR = "scans"
EXCLUDED_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", ".groma", ".wb"}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_source(source: dict) -> tuple[list[dict], list[dict]]:
    """Walk one declared root. Returns (files, skipped); never reads file bytes."""
    root = Path(source["path"])
    name = source["name"]
    files: list[dict] = []
    skipped: list[dict] = []

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            skipped.append(
                {"source": name, "path": prefix or ".", "reason": "unreadable", "detail": exc.strerror or "OSError"}
            )
            return

        for entry in entries:
            rel = "%s/%s" % (prefix, entry.name) if prefix else entry.name
            try:
                if entry.is_symlink():
                    skipped.append({"source": name, "path": rel, "reason": "symlink"})
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in EXCLUDED_DIR_NAMES:
                        skipped.append({"source": name, "path": rel, "reason": "excluded_dir"})
                        continue
                    walk(Path(entry.path), rel)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    skipped.append({"source": name, "path": rel, "reason": "not_a_file"})
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                skipped.append(
                    {"source": name, "path": rel, "reason": "unreadable", "detail": exc.strerror or "OSError"}
                )
                continue
            files.append(
                {
                    "source": name,
                    "path": rel,
                    "bytes": stat.st_size,
                    "mtime": _iso(stat.st_mtime),
                }
            )

    walk(root, "")
    return files, skipped


def build_scan(root: Path) -> dict:
    unit = load_unit(root)
    sources = load_sources(root)
    files: list[dict] = []
    skipped: list[dict] = []
    for source in sources:
        source_files, source_skipped = scan_source(source)
        files.extend(source_files)
        skipped.extend(source_skipped)

    return {
        "schema": 1,
        "kind": "GROMA_LOCAL_SCAN",
        "unit_id": unit["unit_id"],
        "created_at": utc_stamp(),
        "sources": [
            {"name": source["name"], "harness": source.get("harness"), "path": source["path"]}
            for source in sources
        ],
        "file_count": len(files),
        "total_bytes": sum(entry["bytes"] for entry in files),
        "skipped_count": len(skipped),
        "files": files,
        "skipped": skipped,
    }


def save_scan(root: Path, scan: dict) -> Path:
    out = meta_dir(root) / SCANS_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = scan["created_at"].replace("-", "").replace(":", "")
    target = out / ("scan-%s.json" % stamp)
    counter = 1
    while target.exists():
        target = out / ("scan-%s-%d.json" % (stamp, counter))
        counter += 1
    write_json(target, scan)
    return target
