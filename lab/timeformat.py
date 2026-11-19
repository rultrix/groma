"""UTC 存储、本机时区展示的纯格式化层。

只负责"读出来给人看"：任何写入路径都不许拿它产生时间戳（见 `docs/RULES.md` 的 W23 例外条）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import tzinfo as TZInfo


def local_tz() -> TZInfo:
    """本机时区。必须在调用时取，不在 import 时烤死——测试靠 patch 它拿确定性。"""
    return datetime.now().astimezone().tzinfo


def parse_utc(value: object) -> datetime | None:
    """把存储值解析成绝对时刻（UTC）。无偏移的串按既有内部契约当作 UTC。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_instant(value: object, tz: TZInfo | None = None) -> datetime | None:
    parsed = parse_utc(value)
    if parsed is None:
        return None
    return parsed.astimezone(tz or local_tz())


def _offset_text(local: datetime) -> str:
    """偏移按那个时刻本身算，不写死 +8。"""
    offset = local.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "%s%02d:%02d" % (sign, total // 3600, (total // 60) % 60)


def local_text(value: object, *, tz: TZInfo | None = None,
               date_only: bool = False, fallback: str = "") -> str:
    """界面展示：`2026-11-17 02:52:30 +08:00`，或只到日 `2026-11-17`。

    空值走调用方给的 fallback 文案；解析不了的值原样返回——显示层不许伪造换算结果。
    """
    if value is None or value == "":
        return fallback
    local = local_instant(value, tz)
    if local is None:
        return str(value)
    if date_only:
        return local.strftime("%Y-%m-%d")
    return "%s %s" % (local.strftime("%Y-%m-%d %H:%M:%S"), _offset_text(local))
