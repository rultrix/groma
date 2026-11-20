"""Topics and their evidence: a Unit is a subject, its members are sessions.

Evidence is keyed by (source, path) because the session-key fields in the
digest only exist in code newer than the digests already on disk. The store
lives in the machine-level state directory, not in any project folder: a
subject spans workspaces, so no single workspace owns it.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import unitmatch
from .state import state_root

TOPICS = "topics.json"
EVIDENCE = "topic_evidence.jsonl"
REFORGES = "topic_reforges.jsonl"
SCHEMA = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _topics_path() -> Path:
    return state_root() / TOPICS


def _evidence_path() -> Path:
    return state_root() / EVIDENCE


def _topic_id(name: str) -> str:
    return "topic_" + hashlib.sha256(("topic:" + name).encode("utf-8")).hexdigest()[:16]


def _unit_for_name(name: str) -> dict | None:
    """The Unit that carries this display name, if any. Topics key on its id."""
    from . import unitmatch

    target = (name or "").strip()
    if not target:
        return None
    for unit in unitmatch.unit_list():
        if (unit.get("name") or "").strip() == target:
            return unit
    return None


def _decorate(record: dict) -> dict:
    """A topic is a Unit: its id is the Unit id, its name mirrors the Unit's."""
    return {**record, "intent": record.get("intent")}


def _clean_intent(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def create_topic(name: str, org_path: str | None = None, origin: str = "local",
                 intent: str | None = None) -> dict:
    """A name is how you refer to a subject; `intent` is the rule for what belongs.

    Intent is optional at creation on purpose -- a label can exist before its
    definition -- but a classification run refuses to guess without it.
    """
    label = (name or "").strip()
    if not label:
        raise ValueError("主题名称不能为空")
    payload = _read_json(_topics_path(), {"schema": SCHEMA, "topics": []})
    unit = _unit_for_name(label)
    if unit is None:
        raise ValueError("没有叫「%s」的主题；先在主题页新建主题（需要一个工作区作它的家）" % label)
    key = unit["unit_id"]
    for item in payload["topics"]:
        if item["name"] == label or item.get("topic_id") == key:
            if item["name"] == label and unit is not None and item.get("topic_id") != key:
                # the Unit now exists: adopt its id so one subject has one key
                item["topic_id"] = key
                _atomic_write(_topics_path(), payload)
            return _decorate(item)
    record = {
        "topic_id": key,
        "name": label,
        "org_path": (org_path or "").strip() or None,
        "intent": _clean_intent(intent),
        "intent_updated_at": _now() if _clean_intent(intent) else None,
        "intent_by": "human" if _clean_intent(intent) else None,
        "origin": origin,
        "created_at": _now(),
    }
    payload["topics"].append(record)
    _atomic_write(_topics_path(), payload)
    return _decorate(record)


def set_intent(topic: str, intent: str | None, decided_by: str = "human") -> dict:
    """Rewrite the one-line definition. Only the next classification reads it.

    Already-selected evidence is untouched: changing what a subject means does
    not silently re-decide the past, it decides the next pass.
    """
    record = resolve_topic(topic)
    if record is None:
        raise ValueError("主题不存在：%s" % topic)
    payload = _read_json(_topics_path(), {"schema": SCHEMA, "topics": []})
    updated = None
    for item in payload["topics"]:
        if item["topic_id"] == record["topic_id"]:
            item["intent"] = _clean_intent(intent)
            item["intent_updated_at"] = _now()
            item["intent_by"] = decided_by
            updated = dict(item)
            break
    _atomic_write(_topics_path(), payload)
    return updated or {**record, "intent": _clean_intent(intent)}


def list_topics() -> list[dict]:
    payload = _read_json(_topics_path(), {"schema": SCHEMA, "topics": []})
    rows = (_decorate(item) for item in payload["topics"])
    return sorted(rows, key=lambda item: item["name"])


def topic_record(topic_id: str) -> dict | None:
    for item in list_topics():
        if item["topic_id"] == topic_id:
            return item
    return None


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def events(topic_id: str | None = None) -> list[dict]:
    path = _evidence_path()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if topic_id is None or record.get("topic_id") == topic_id:
            out.append(record)
    return out


def _append(record: dict) -> None:
    path = _evidence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def key_of(item: dict) -> tuple[str, str] | None:
    source = (item.get("source") or "").strip()
    path = (item.get("path") or "").strip()
    if not source or not path:
        return None
    return (source, path)


def active_evidence(topic_id: str) -> list[dict]:
    """The current membership: the last event per key, undropped."""
    latest: dict[tuple[str, str], dict] = {}
    for event in events(topic_id):
        key = (event.get("source"), event.get("path"))
        latest[key] = event
    return [record for record in latest.values() if record.get("action") == "select"]


def select(topic_id: str, items: list[dict], decided_by: str,
           confidence: float | None = None, signals: list[str] | None = None,
           known_keys: set[tuple[str, str]] | None = None) -> dict:
    """Record evidence for a topic. An item without an anchor is refused.

    `known_keys` is the set of sessions the digests actually hold: letting an
    unanchored key in is how an auto-classifier invents a past.
    """
    if topic_record(topic_id) is None:
        raise ValueError("主题不存在：%s" % topic_id)
    added, rejected = [], []
    for item in items:
        key = key_of(item)
        if key is None:
            rejected.append({"item": item, "reason": "无锚点：缺 source 或 path"})
            continue
        if known_keys is not None and key not in known_keys:
            rejected.append({"item": item, "reason": "锚点不存在于任何 digest"})
            continue
        record = {
            "schema": SCHEMA,
            "topic_id": topic_id,
            "action": "select",
            "source": key[0],
            "path": key[1],
            "fingerprint_kind": item.get("fingerprint_kind"),
            "source_sha256": item.get("source_sha256"),
            "decided_by": decided_by,
            "confidence": confidence if item.get("confidence") is None else item.get("confidence"),
            "signals": item.get("signals") if item.get("signals") is not None else list(signals or []),
            "at": _now(),
        }
        _append(record)
        added.append(record)
    return {"topic_id": topic_id, "added": len(added), "rejected": rejected}


def undo(topic_id: str, keys: list[tuple[str, str]], decided_by: str, reason: str | None = None) -> int:
    """Undo is an event, not a deletion: the audit trail keeps both calls."""
    done = 0
    for source, path in keys:
        _append({
            "schema": SCHEMA,
            "topic_id": topic_id,
            "action": "undo",
            "source": source,
            "path": path,
            "decided_by": decided_by,
            "reason": reason,
            "at": _now(),
        })
        done += 1
    return done


def all_active_keys() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for record in active_evidence_all():
        out.add((record["source"], record["path"]))
    return out


def active_evidence_all() -> list[dict]:
    latest: dict[tuple[str, str, str], dict] = {}
    for event in events():
        latest[(event.get("topic_id"), event.get("source"), event.get("path"))] = event
    return [r for r in latest.values() if r.get("action") == "select"]


def unclassified(rows: list[dict], classified: set[tuple[str, str]] | None = None) -> list[dict]:
    """Sessions collected but belonging to no topic -- the only proof of a miss.

    Without this number an auto-classifier reports success while quietly
    dropping everything it did not recognise.
    """
    chosen = all_active_keys() if classified is None else classified
    return [row for row in rows if key_of(row) not in chosen]


def classification_counts(rows: list[dict]) -> dict:
    keys = {key_of(row) for row in rows if key_of(row)}
    active = all_active_keys()
    return {
        "collected_sessions": len(keys),
        "classified": len({k for k in active if k in keys}),
        "unclassified": len({k for k in keys if k not in active}),
    }


def resolve_topic(value: str) -> dict | None:
    """By id or by name: the CLI and the UI each hold whichever they were given."""
    text = (value or "").strip()
    if not text:
        return None
    found = topic_record(text) or next((item for item in list_topics() if item["name"] == text), None)
    if found is not None:
        return found
    unit = _unit_for_name(text)
    return topic_record(unit["unit_id"]) if unit else None


def evidence_split(topic_id: str) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """活跃证据按"还能不能引"拆成两半。

    存活判定只住 `lab.reconcile`，这里只调用。撤销过的键在更早一步就没了；
    这一步管的是另一种语义：键还在账上，但它对应的会话已不在 digest 引用索引里，
    `build_asset` 引不到它——不剔除就会把整条重铸链打死。
    """
    keys = sorted({(item["source"], item["path"]) for item in active_evidence(topic_id)})
    if not keys:
        return [], {}
    home = unitmatch.home_path(topic_id)
    if not home:
        return keys, {}
    from . import reconcile  # reconcile 顶层 import 本模块，函数内 import 防成环

    states = reconcile.evidence_liveness(home, keys)
    kept: list[tuple[str, str]] = []
    skipped: dict[str, int] = {}
    for key in keys:
        state = states.get(key, reconcile.LIVE)
        if state == reconcile.LIVE:
            kept.append(key)
        else:
            skipped[state] = skipped.get(state, 0) + 1
    return kept, skipped


def asset_selection(topic_id: str) -> list[tuple[str, str]]:
    """What a re-forge is allowed to cite: active evidence that can still be quoted.

    Two filters stay apart on purpose: an undo changes the context; a key whose
    session left the digest index cannot be written into an asset at all.
    """
    return evidence_split(topic_id)[0]


def _reforges_path() -> Path:
    return state_root() / REFORGES


def last_reforge(topic_id: str) -> dict | None:
    """The most recent re-forge event of one topic, if any."""
    path = _reforges_path()
    if not path.is_file():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("topic_id") == topic_id:
            latest = record
    return latest


def reforge(root: str | Path, topic: str, title: str | None = None, note: str = "") -> dict:
    """Rebuild the topic's asset from live evidence, and record what was cited.

    引不到的键在选中前就被剔除并计数（`skipped_stale`），计数同时进返回值与账本：
    只进返回值等于事后无法取证。
    """
    from .asset import build_asset

    record = resolve_topic(topic)
    if record is None:
        raise ValueError("主题不存在：%s" % topic)
    selection, skipped = evidence_split(record["topic_id"])
    skipped_stale = sum(skipped.values())
    if not selection and not skipped_stale:
        raise ValueError("主题「%s」没有活跃证据，重铸只会写出一个空资产" % record["name"])
    label = (title or record["name"]).strip()
    if not selection:
        return {"topic_id": record["topic_id"], "topic": record["name"], "title": label,
                "asset_id": None, "sessions": 0, "skipped_stale": skipped_stale,
                "skipped": skipped,
                "reason": "证据集已整体失效，没有可重铸的依据"}
    asset = build_asset(Path(root), label, selection, note)
    _append_reforge({
        "schema": SCHEMA,
        "topic_id": record["topic_id"],
        "asset_id": asset["asset_id"],
        "title": label,
        "sessions": len(selection),
        "skipped_stale": skipped_stale,
        "skipped": skipped,
        "selection": [[source, path] for source, path in selection],
        "at": _now(),
    })
    return {"topic_id": record["topic_id"], "topic": record["name"], "asset_id": asset["asset_id"],
            "title": label, "sessions": len(selection), "skipped_stale": skipped_stale,
            "skipped": skipped, "asset": asset}


def _append_reforge(record: dict) -> None:
    path = _reforges_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def stale_after_undo(topic_id: str) -> dict:
    """Keys the last re-forge served that this topic has since undone.

    An asset is immutable and content-addressed, so undoing evidence cannot
    change an asset already written; naming what is still being cited is the
    honest half of the contract.
    """
    served = last_reforge(topic_id)
    if served is None:
        return {"reforged": False, "topic_id": topic_id, "dropped": []}
    active = set(asset_selection(topic_id))
    dropped = sorted(
        (str(entry[0]), str(entry[1]))
        for entry in (served.get("selection") or [])
        if (entry[0], entry[1]) not in active
    )
    return {"reforged": True, "topic_id": topic_id, "asset_id": served.get("asset_id"),
            "served": len(served.get("selection") or []), "dropped": dropped}


def asset_ids_for(topic_id: str) -> list[str]:
    """Asset ids this topic has ever re-forged. Used to recognise our own source."""
    path = _reforges_path()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("topic_id") == topic_id and record.get("asset_id"):
            out.append(str(record["asset_id"]))
    return out


def topic_of_asset(asset_id: str) -> str | None:
    """Reverse lookup: a re-forged asset belongs to exactly one topic."""
    path = _reforges_path()
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if str(record.get("asset_id")) == str(asset_id):
            found = record.get("topic_id")
    return found
