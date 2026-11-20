"""One-page rollup of the Unit's newest digests: activity by day and by source.

Deliberately answerable without storing any field values: only timestamps,
line counts and source names are used. "Which project" would need cwd values
and is deferred until L3 decides who reads this -- the workspaces a session
came from are named here only as local paths in `.groma/summaries`, never uploaded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import tzinfo as TZInfo
from pathlib import Path

from . import unitmatch
from .timeformat import local_tz
from .workspace import LabError, load_unit, meta_dir, utc_stamp, write_json

SUMMARIES_DIR = "summaries"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_summary(root: Path, days: int = 7, tz: TZInfo | None = None) -> dict:
    """Activity for the whole Unit: the union of its workspaces' digests.

    **存储与排序仍是 UTC 绝对时刻，日归属按传入时区（默认本机）**。
    界面把 `newest_ts` 显示成本机时间却仍按 UTC 分日，跨午夜就会自相矛盾。
    """
    unit = load_unit(root)
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise LabError("days must be a positive integer")

    roots = unitmatch.unit_workspaces(root)
    records = unitmatch.digest_records_for(roots)
    if not records:
        raise LabError("no digest yet; run `groma digest` first")
    tz = tz or local_tz()
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(tz)
    cutoff = (now_local - timedelta(days=days)).date()

    by_day: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    newest_seen: dict[str, datetime] = {}
    sessions = 0
    events = 0
    undated = 0

    for item in unitmatch.unit_files(records):
        stamp = _parse_ts(item.get("last_ts"))
        if stamp is None:
            undated += 1
            continue
        local_day = stamp.astimezone(tz).date()  # 先换算再取日，不截 UTC 串
        if local_day < cutoff:
            continue
        sessions += 1
        events += int(item.get("events", 0))

        day_key = local_day.isoformat()
        day = by_day.setdefault(day_key, {"date": day_key, "sessions": 0, "events": 0})
        day["sessions"] += 1
        day["events"] += int(item.get("events", 0))

        source = by_source.setdefault(
            item["source"], {"source": item["source"], "sessions": 0, "events": 0, "newest_ts": None}
        )
        source["sessions"] += 1
        source["events"] += int(item.get("events", 0))
        seen = newest_seen.get(item["source"])
        if seen is None or stamp > seen:  # 绝对时刻比较；裸串在不同偏移写法下会挑错
            newest_seen[item["source"]] = stamp
            source["newest_ts"] = item.get("last_ts")  # 返回给界面的仍是原 UTC 值

    return {
        "schema": 1,
        "kind": "GROMA_LOCAL_SUMMARY",
        "unit_id": unit["unit_id"],
        "created_at": utc_stamp(),
        "digests": [item["digest_name"] for item in records],
        "workspaces": roots,
        "window_days": days,
        "window_from": cutoff.isoformat(),
        "window_to": now_local.date().isoformat(),
        "session_count": sessions,
        "event_count": events,
        "active_days": len(by_day),
        "undated_files": undated,
        "by_day": [by_day[key] for key in sorted(by_day)],
        "by_source": sorted(by_source.values(), key=lambda item: item["source"]),
    }


def render_text(summary: dict) -> str:
    lines = [
        "WB summary - last %d days (%s .. %s)"
        % (summary["window_days"], summary["window_from"], summary["window_to"]),
        "sessions %d - lines %d - active days %d"
        % (summary["session_count"], summary["event_count"], summary["active_days"]),
    ]
    if summary["undated_files"]:
        lines.append("files without a usable timestamp: %d" % summary["undated_files"])
    if summary["by_source"]:
        lines.append("by source:")
        for item in summary["by_source"]:
            lines.append(
                "  %-10s %3d sessions %7d lines  newest %s"
                % (item["source"], item["sessions"], item["events"], item["newest_ts"] or "-")
            )
    if summary["by_day"]:
        lines.append("by day:")
        for item in summary["by_day"]:
            lines.append("  %s  %3d sessions %6d events" % (item["date"], item["sessions"], item["events"]))
    return "\n".join(lines)


def save_summary(root: Path, summary: dict) -> Path:
    out = meta_dir(root) / SUMMARIES_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = summary["created_at"].replace("-", "").replace(":", "")
    target = out / ("summary-%s.json" % stamp)
    counter = 1
    while target.exists():
        target = out / ("summary-%s-%d.json" % (stamp, counter))
        counter += 1
    write_json(target, summary)
    return target
