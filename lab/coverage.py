"""Active-hour coverage: did we scan during the hours when work happened?

Wall-clock coverage punished the collector for hours the machine was
asleep, when there was nothing to collect. This measures the hours that
actually contain session activity, and asks whether a scan landed in them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import unitmatch
from .workspace import LabError, read_json

MAX_HOURS_PER_FILE = 48


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _hours(start: datetime, end: datetime) -> set[str]:
    cursor = start.replace(minute=0, second=0, microsecond=0)
    out: set[str] = set()
    while cursor <= end and len(out) < MAX_HOURS_PER_FILE:
        out.add(cursor.strftime("%Y-%m-%dT%H"))
        cursor += timedelta(hours=1)
    return out


def build_coverage(root: Path, days: int = 7) -> dict:
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise LabError("days must be a positive integer")

    roots = unitmatch.unit_workspaces(root)
    records = unitmatch.digest_records_for(roots)
    if not records:
        raise LabError("no digest yet; run `groma digest` or `groma catchup` first")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    cutoff_hour = cutoff.strftime("%Y-%m-%dT%H")
    active: set[str] = set()
    for item in unitmatch.unit_files(records):
        recorded = [hour for hour in item.get("hours") or [] if hour >= cutoff_hour]
        if recorded:
            active.update(recorded)
            continue
        # Older digests have no per-hour histogram; fall back to the file span.
        start = _parse(item.get("first_ts"))
        end = _parse(item.get("last_ts"))
        if not start or not end or end < cutoff:
            continue
        active |= _hours(max(start, cutoff), min(end, now))

    scanned: set[str] = set()
    earliest: datetime | None = None
    for path in unitmatch.scan_paths_for(roots):
        stamp = _parse(read_json(path).get("created_at"))
        if stamp and stamp >= cutoff:
            scanned.add(stamp.strftime("%Y-%m-%dT%H"))
            if earliest is None or stamp < earliest:
                earliest = stamp

    # Hours before collection ever ran cannot be covered; not counting them
    # avoids a metric that is permanently dragged down by history.
    window_from = max(cutoff, earliest) if earliest else cutoff
    since_hour = window_from.strftime("%Y-%m-%dT%H")
    active = {hour for hour in active if hour >= since_hour}

    covered = active & scanned
    missing = sorted(active - scanned)
    return {
        "schema": 1,
        "kind": "GROMA_LOCAL_COVERAGE",
        "window_days": days,
        "since": window_from.isoformat(),
        "active_hours": len(active),
        "scan_hours": len(scanned),
        "covered_hours": len(covered),
        "coverage": round(len(covered) / len(active), 3) if active else None,
        "missing_hours": missing[:60],
        "note": "active hours count from the first scan; wall-clock sleep is not counted",
    }
