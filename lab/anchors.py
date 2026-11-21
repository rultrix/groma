"""Claim anchors: locate a human-chosen span in the vault and hash the quote.

The vault is the only store of conversation content, and upper layers may only
see it through its index (`vault.read_index`) -- never by globbing the tree.
So locating a span means: read the index, find the session's shard entries,
read exactly those shard files, keep the records inside the span.

A claim is human-supplied (conclusion sentence + session key + ts window); this
module only locates, hashes and renders. The anchor hash is the sha256 of the
quote's full UTF-8 text -- the consuming side can verify it without knowing
anything about the vault's normalisation rules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import vault
from .workspace import LabError, utc_stamp


def quote_sha256(quote: str) -> str:
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def _session_entry(index: dict, session_key: str) -> dict:
    for entry in index.get("sessions") or []:
        if entry.get("session_key") == session_key:
            return entry
    raise LabError("会话不在 vault 索引里：%s（先采集并 reindex）" % session_key)


def _shard_records(index: dict, entry: dict) -> list[dict]:
    """Read the shard files the index lists, refusing a drifted shard."""
    root = Path(index["root"])
    records: list[dict] = []
    for shard in entry.get("shards") or []:
        path = root / entry["harness"] / entry["safe_dir"] / shard["name"]
        if not path.is_file():
            raise LabError("索引列出的分片不在盘上：%s" % path)
        actual = vault.file_sha256(path)
        if actual != shard.get("file_sha256"):
            raise LabError("分片已漂移（索引记的 sha 与盘上不符）：%s" % shard["name"])
        for line in path.read_bytes().split(b"\n"):
            if line:
                records.append(json.loads(line))
    return records


def _instant(ts: str) -> datetime:
    # bare string compare would sort "…16.245Z" before "…16Z" ('.' < 'Z') and
    # silently cut sub-second records out of the window; compare instants instead
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _inside(record: dict, from_ts: str, to_ts: str) -> bool:
    raw = str(record.get("ts_utc") or "")
    if not raw:
        return False
    try:
        ts = _instant(raw)
    except ValueError:
        return False
    return _instant(from_ts) <= ts <= _instant(to_ts)


def locate(vault_root: Path, session_key: str, from_ts: str, to_ts: str) -> dict:
    """Resolve one claim window to a quote. Fail loud: no session, no records,
    or a drifted shard are all errors -- never an empty quote wearing an anchor."""
    if not session_key or not from_ts or not to_ts:
        raise LabError("claim 三要素缺一不可：session_key、from_ts、to_ts")
    if from_ts > to_ts:
        raise LabError("claim 区间倒置：%s > %s" % (from_ts, to_ts))
    index = vault.read_index(vault_root)
    entry = _session_entry(index, session_key)
    records = [item for item in _shard_records(index, entry) if _inside(item, from_ts, to_ts)]
    texts = [str(item.get("text") or "").strip() for item in records]
    quote = "\n".join(text for text in texts if text)
    if not quote:
        raise LabError("claim 区间里没有可引用的正文：%s %s → %s" % (session_key, from_ts, to_ts))
    return {
        "session_key": session_key,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "quote": quote,
        "quote_sha256": quote_sha256(quote),
        "records": len(records),
        "span_first_ts": min(str(item["ts_utc"]) for item in records),
        "span_last_ts": max(str(item["ts_utc"]) for item in records),
    }


def make_claim(found: dict, title: str) -> dict:
    """The asset-side shape of one claim. `quote` is the only field carrying
    conversation text -- every other field is structure or provenance."""
    label = (title or "").strip()
    if not label:
        raise LabError("claim 的结论句不能为空")
    return {
        "title": label,
        "session_key": found["session_key"],
        "from_ts": found["from_ts"],
        "to_ts": found["to_ts"],
        "quote": found["quote"],
        "quote_sha256": found["quote_sha256"],
        "records": found["records"],
        "span_first_ts": found["span_first_ts"],
        "span_last_ts": found["span_last_ts"],
        "state": "verified",
        "extracted_at": utc_stamp()[:10],
        "extractor": "human",
    }


def anchor_line(claim: dict) -> str:
    """The one line every quote must be preceded by (session + real span + hash head)."""
    return "> 锚点：`%s` · %s → %s · sha256 `%s`" % (
        claim["session_key"], claim.get("span_first_ts") or claim["from_ts"],
        claim.get("span_last_ts") or claim["to_ts"], claim["quote_sha256"][:12],
    )


def render_claims(claims: list[dict], max_items: int | None = None,
                  max_chars: int | None = None) -> tuple[list[str], int]:
    """Render claims as payload/export lines. Budget is first-come-first-served:
    a claim that does not fit exits whole (never truncated mid-quote). Returns
    (lines, omitted_count); a section header is the caller's business."""
    lines: list[str] = []
    items = 0
    chars = 0
    omitted = 0
    for claim in claims:
        if claim.get("state") != "verified":
            omitted += 1
            continue
        block = [claim["title"], anchor_line(claim)] + ["> " + l for l in claim["quote"].split("\n")]
        cost = sum(len(line) + 1 for line in block)
        if max_items is not None and items >= max_items:
            omitted += 1
            continue
        if max_chars is not None and items and chars + cost > max_chars:
            omitted += 1
            continue
        if lines:
            lines.append("")
        lines.extend(block)
        items += 1
        chars += cost
    return lines, omitted
