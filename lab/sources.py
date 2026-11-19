"""Declared input roots. Nothing is discovered automatically; the user names it."""

from __future__ import annotations

from pathlib import Path

from .workspace import LabError, meta_dir, read_json, require_init, utc_stamp, write_json

SOURCES_FILE = "sources.json"


def normalise(value: str) -> Path:
    return Path(value).expanduser().resolve()


def load_sources(root: Path) -> list[dict]:
    require_init(root)
    path = meta_dir(root) / SOURCES_FILE
    if not path.is_file():
        return []
    return read_json(path).get("sources", [])


def add_source(
    root: Path,
    name: str,
    path_value: str,
    harness: str | None = None,
    include: list[str] | None = None,
) -> dict:
    require_init(root)
    label = (name or "").strip()
    if not label:
        raise LabError("source name must not be empty")

    prefixes: list[str] = []
    for item in include or []:
        raw = item.strip()
        if not raw:
            continue
        if raw.startswith("/") or ":" in raw or "\\" in raw:
            raise LabError("include must be a plain sub-path, got: %s" % item)
        prefix = raw.strip("/")
        if ".." in prefix.split("/"):
            raise LabError("include must stay inside the root, got: %s" % item)
        if prefix not in prefixes:
            prefixes.append(prefix)

    target = normalise(path_value)
    if not target.is_dir():
        raise LabError("source path is not a directory: %s" % target)

    meta = meta_dir(root).resolve()
    if target == meta or meta in target.parents:
        raise LabError("source path must not be inside the unit metadata directory: %s" % target)

    existing = load_sources(root)
    if any(source["name"] == label for source in existing):
        raise LabError("source already declared: %s" % label)
    for source in existing:
        other = Path(source["path"])
        if target == other or other in target.parents or target in other.parents:
            raise LabError(
                "source overlaps with %s (%s); declare one root, not both" % (source["name"], other)
            )

    record = {
        "name": label,
        "harness": harness or "unknown",
        "path": target.as_posix(),
        "include": prefixes,
        "added_at": utc_stamp(),
    }
    existing.append(record)
    existing.sort(key=lambda item: item["name"])
    write_json(meta_dir(root) / SOURCES_FILE, {"schema": 1, "sources": existing})
    return record


def remove_source(root: Path, name: str) -> dict:
    require_init(root)
    existing = load_sources(root)
    kept = [source for source in existing if source["name"] != name]
    if len(kept) == len(existing):
        raise LabError("source not declared: %s" % name)
    write_json(meta_dir(root) / SOURCES_FILE, {"schema": 1, "sources": kept})
    return {"removed": name, "remaining": len(kept)}
