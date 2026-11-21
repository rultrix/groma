"""Payload claims: the injected background may carry conclusions with anchors,
under a budget that exits whole quotes -- never truncates them.

Pure projection tests: `project_text` is fed hand-built units and assets, so a
red here points at the renderer, not at any pipeline upstream.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import unittest

from lab import surface


def unit() -> dict:
    return {"unit_id": "unit_claimtest", "name": "引文载荷", "workspace_ref": "ws-x"}


def rows() -> list[dict]:
    return [{"path": "/tmp/home", "name": "home", "is_home": True, "sessions": 1, "events": 3}]


def asset(claims: list[dict] | None) -> dict:
    base = {
        "asset_id": "asset-claimtest",
        "title": "引文载荷",
        "generated_at": "2026-11-21T00:00:00Z",
        "digest": "digest-x.json",
        "totals": {"sessions": 1, "events": 3},
        "notes": "",
    }
    if claims is not None:
        base["claims"] = claims
    return base


def claim(index: int, text: str | None = None) -> dict:
    body = text if text is not None else "第 %d 条结论的原文内容。" % index
    return {
        "title": "结论 %d" % index,
        "session_key": "qoder:sess-%03d" % index,
        "from_ts": "2026-11-10T08:00:00Z",
        "to_ts": "2026-11-10T08:01:00Z",
        "quote": body,
        "quote_sha256": "%064d" % index,
        "records": 1,
        "span_first_ts": "2026-11-10T08:00:00Z",
        "span_last_ts": "2026-11-10T08:01:00Z",
        "state": "verified",
    }


class PayloadClaimsTests(unittest.TestCase):
    def test_every_quote_is_preceded_by_its_anchor_line(self) -> None:
        text = surface.project_text(unit(), rows(), asset([claim(1, "唯一的那句原文。")]), "/tmp/home")
        self.assertIn("## 结论与引文", text)
        self.assertIn("唯一的那句原文。", text)
        anchor = "> 锚点：`qoder:sess-001` · 2026-11-10T08:00:00Z → 2026-11-10T08:01:00Z · sha256 `000000000000`"
        self.assertIn(anchor, text)
        self.assertLess(text.index(anchor), text.index("唯一的那句原文。"))

    def test_budget_admits_ten_claims_and_exits_the_rest_whole(self) -> None:
        claims = [claim(i, "载荷第 %d 句原文。" % i) for i in range(20)]
        text = surface.project_text(unit(), rows(), asset(claims), "/tmp/home")
        self.assertIn("## 结论与引文", text)
        for i in range(10):
            self.assertIn("载荷第 %d 句原文。" % i, text)
        for i in range(10, 20):
            self.assertNotIn("载荷第 %d 句原文。" % i, text)
        self.assertIn("另见 CONTEXT.md", text)
        self.assertLess(len(text), 60000)  # hook skip line is never reachable

    def test_no_quote_is_ever_truncated(self) -> None:
        long_quote = "长引文开头" + "很长的正文" * 3000 + "长引文结尾"
        claims = [claim(1), claim(2, long_quote)]
        text = surface.project_text(unit(), rows(), asset(claims), "/tmp/home")
        self.assertNotIn("长引文开头", text)
        self.assertNotIn("长引文结尾", text)  # too big for the budget: exits whole
        self.assertNotIn("结论 2", text)      # not truncated into a stub, simply absent

    def test_an_asset_without_claims_reads_exactly_as_before(self) -> None:
        plain = surface.project_text(unit(), rows(), asset(None), "/tmp/home")
        self.assertNotIn("结论与引文", plain)
        self.assertNotIn("锚点：", plain)
        self.assertNotIn("另见 CONTEXT.md", plain)
        # and an asset that simply lacks the key entirely (today's shape)
        legacy = surface.project_text(unit(), rows(), {k: v for k, v in asset(None).items()}, "/tmp/home")
        self.assertNotIn("结论与引文", legacy)

    def test_stale_claims_are_not_injected(self) -> None:
        stale = claim(1, "这句已经对不上源了。")
        stale["state"] = "stale"
        text = surface.project_text(unit(), rows(), asset([stale, claim(2, "这句仍然有效。")]), "/tmp/home")
        self.assertNotIn("这句已经对不上源了。", text)
        self.assertIn("这句仍然有效。", text)


if __name__ == "__main__":
    unittest.main()
