"""刀 W23a：`lab/timeformat.py` 的换算契约（不依赖开发机时区）。"""

from __future__ import annotations

import re
import unittest
from datetime import timezone, timedelta

from lab.timeformat import local_text, parse_utc

TZ8 = timezone(timedelta(hours=8))
TZ_MINUS5 = timezone(timedelta(hours=-5))


class TimeFormatTests(unittest.TestCase):
    def test_local_text_formats_offset_and_date_only_values(self) -> None:
        """Y1：UTC 原串在固定 +08:00 下换成本机时刻，日期格只到日。"""
        self.assertEqual(local_text("2026-11-09T00:00:00Z", tz=TZ8),
                         "2026-11-09 08:00:00 +08:00")
        self.assertEqual(local_text("2026-11-09T00:00:00Z", tz=TZ8, date_only=True),
                         "2026-11-09")
        # 负偏移与跨日归属：同一个 instant 在西五区是前一天晚上
        self.assertEqual(local_text("2026-11-09T00:00:00Z", tz=TZ_MINUS5),
                         "2026-11-08 19:00:00 -05:00")
        self.assertEqual(local_text("2026-11-09T00:00:00Z", tz=TZ_MINUS5, date_only=True),
                         "2026-11-08")

    def test_local_text_handles_offsets_naive_contract_and_unparseables(self) -> None:
        """Y2：带偏移的先换算、naive 按 UTC 契约解释、空值走 fallback、乱串原样。"""
        self.assertEqual(local_text("2026-11-09T12:00:00+05:00", tz=TZ8),
                         "2026-11-09 15:00:00 +08:00")
        self.assertEqual(local_text("2026-11-09T00:00:00", tz=TZ8),
                         "2026-11-09 08:00:00 +08:00")
        self.assertEqual(local_text(None, tz=TZ8, fallback="从未"), "从未")
        self.assertEqual(local_text("", tz=TZ8, fallback="还没有跑过"), "还没有跑过")
        self.assertEqual(local_text("昨天吧", tz=TZ8), "昨天吧")
        self.assertIsNone(parse_utc("昨天吧"))
        self.assertIsNone(parse_utc(None))
        # 不传 tz 时走本机时区：只钉形状，不钉具体偏移（本仓测试不许依赖开发机时区）
        self.assertRegex(local_text("2026-11-09T00:00:00Z"),
                         r"^2026-11-09 \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
