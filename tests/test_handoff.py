"""接力简报 · 同工作流：A→B 触发、A→A 不触发、N=5 窗口、四段齐全、
投递面=接棒方、忽略后不投（一次性）。触发=digest 硬事实，零推断。
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import tempfile
import unittest
from pathlib import Path

from lab import handoff, surface, topics
from lab.workspace import init_workspace


def _digest_entry(harness: str, ts: str) -> dict:
    return {"source": harness, "path": "sessions/%s.jsonl" % harness,
            "events": 3, "created_at": ts, "last_ts": ts}


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-handoff-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="接力工作区", owner="tester")
        topics.record_handoff = topics.record_handoff  # ledger lives in topics; keep name visible

    def _write_digest(self, entries: list[dict]) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        digest = {"schema": 1, "file_count": len(entries), "files": entries,
                  "unattributed_outside": 0, "unattributed_no_cwd": 0}
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps(digest, ensure_ascii=False), encoding="utf-8", newline="\n")

    def test_a_to_b_triggers_and_a_to_a_does_not(self) -> None:
        """触发=digest 硬事实：上一场 A、新一场 B（A≠B）⇒ 一棒；同 harness 不触发。"""
        self._write_digest([_digest_entry("codex", "2026-11-22T01:00:00Z"),
                            _digest_entry("qoder", "2026-11-22T02:00:00Z")])
        out = handoff.detect_handoff(self.ws)
        self.assertIsNotNone(out)
        self.assertEqual(out["from_harness"], "codex")
        self.assertEqual(out["to_harness"], "qoder")
        self.assertEqual(out["at"], "2026-11-22T02:00:00Z")
        self._write_digest([_digest_entry("codex", "2026-11-22T01:00:00Z"),
                            _digest_entry("codex", "2026-11-22T02:00:00Z")])
        self.assertIsNone(handoff.detect_handoff(self.ws), "A→A 不该触发接力")

    def test_the_window_is_five_sessions(self) -> None:
        """N=5：7 场会话进窗口只取最近 5 场。"""
        entries = [_digest_entry("codex" if i % 2 == 0 else "qoder",
                                 "2026-11-22T0%d:00:00Z" % (i + 1)) for i in range(7)]
        self._write_digest(entries)
        out = handoff.detect_handoff(self.ws)
        brief = handoff.build_brief(self.ws, None)
        self.assertEqual(len(brief["progress"]), handoff.WINDOW)
        self.assertEqual(handoff.WINDOW, 5)
        self.assertEqual(brief["progress"][0]["last_ts"], "2026-11-22T03:00:00Z")
        self.assertEqual(brief["progress"][-1]["last_ts"], "2026-11-22T07:00:00Z")

    def test_the_brief_has_all_four_sections(self) -> None:
        """四段齐全：进行到哪 / 最近决策 claims / 开放问题 / 交接材料。"""
        self._write_digest([_digest_entry("codex", "2026-11-22T01:00:00Z"),
                            _digest_entry("qoder", "2026-11-22T02:00:00Z")])
        text = handoff.render_brief(handoff.build_brief(self.ws, None))
        for section in ("### 进行到哪", "### 最近决策", "### 开放问题", "### 交接材料"):
            self.assertIn(section, text, "简报缺一段：%s" % section)

    def test_the_brief_is_delivered_only_to_the_successor_faces(self) -> None:
        """投递面=接棒方：codex 接棒 → AGENTS.md 有简报，CLAUDE.md 与载荷不动。"""
        self._write_digest([_digest_entry("claude-code", "2026-11-22T01:00:00Z"),
                            _digest_entry("codex", "2026-11-22T02:00:00Z")])
        record = topics.record_handoff(handoff.detect_handoff(self.ws))
        text = handoff.render_brief(handoff.build_brief(self.ws, None))
        handoff.deliver_handoff(self.ws, record, text)
        agents = (self.ws / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("接力简报", agents)
        self.assertIn("groma:inject", agents, "块标记形状被破坏")
        self.assertFalse((self.ws / "CLAUDE.md").exists(), "简报投到了非接棒方")
        self.assertFalse(surface.payload_path(self.ws).is_file(), "接棒方不走 hook，不该写载荷")
        latest = topics.latest_handoff(self.ws.as_posix())
        self.assertEqual(latest["status"], "delivered")

    def test_the_hook_face_gets_the_brief_appended_to_the_payload(self) -> None:
        """qoder 接棒 → 走 hook 载荷（追加一段，不顶掉资产载荷）。"""
        self._write_digest([_digest_entry("codex", "2026-11-22T01:00:00Z"),
                            _digest_entry("qoder", "2026-11-22T02:00:00Z")])
        surface.write_payload(self.ws, "资产载荷正文")
        record = topics.record_handoff(handoff.detect_handoff(self.ws))
        handoff.deliver_handoff(self.ws, record, "接力简报四段正文")
        payload = surface.payload_path(self.ws).read_text(encoding="utf-8")
        self.assertIn("资产载荷正文", payload)
        self.assertIn("接力简报四段正文", payload)

    def test_ignoring_once_stops_the_detection_and_the_delivery(self) -> None:
        """忽略这一次：不投，也不再检出（一次性）。"""
        self._write_digest([_digest_entry("codex", "2026-11-22T01:00:00Z"),
                            _digest_entry("qoder", "2026-11-22T02:00:00Z")])
        record = topics.record_handoff(handoff.detect_handoff(self.ws))
        handoff.ignore_handoff(record)
        self.assertIsNone(handoff.detect_handoff(self.ws), "忽略后又被检出——一次性被破坏")
        self.assertFalse(surface.payload_path(self.ws).is_file())
        self.assertFalse((self.ws / "AGENTS.md").exists())
        self.assertEqual(topics.latest_handoff(self.ws.as_posix())["status"], "ignored")


if __name__ == "__main__":
    unittest.main()


class HandoffWebTests(unittest.TestCase):
    """界面：检出时刻挂可见面，「忽略这一次」/「现在投」由人点，绝不静默投。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-handoff-web-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="接力工作区", owner="tester")
        from lab import topics as topics_mod, unitmatch
        self.unit_id = unitmatch.create_unit("接力主题", self.ws)["unit_id"]
        topics_mod.create_topic("接力主题")
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/a.jsonl", "events": 3,
                    "created_at": "2026-11-22T01:00:00Z", "last_ts": "2026-11-22T01:00:00Z"},
                   {"source": "qoder", "path": "sessions/b.jsonl", "events": 3,
                    "created_at": "2026-11-22T02:00:00Z", "last_ts": "2026-11-22T02:00:00Z"}]
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": 2,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")
        from web.app import create_app
        self.client = create_app(self.ws).test_client()

    def test_the_pending_handoff_is_visible_and_ignoring_keeps_it_silent(self) -> None:
        body = self.client.get("/units").get_data(as_text=True)
        # 预览对齐，时刻卡改「检测到换 harness」句式，
        # 主按钮=「按最近 5 会话铸简报」（直投「现在投」退场，投递只留在预览页）。
        self.assertIn("检测到换 harness", body, "待办接力没挂可见面")
        self.assertIn("按最近 5 会话铸简报", body)
        self.assertNotIn("已投", body.split("检测到换 harness", 1)[1][:400])
        # 预览页：四段 + 两个决定按钮
        preview = self.client.get("/units/handoff/preview",
                                  query_string={"path": self.ws.as_posix()}).get_data(as_text=True)
        self.assertIn("接力简报", preview)
        self.assertIn("现在投", preview)
        self.assertIn("忽略这一次", preview)
        # 忽略：入口之后不再出现待办，也没有任何投递痕迹
        response = self.client.post("/units/handoff/ignore",
                                    data={"path": self.ws.as_posix()})
        self.assertEqual(response.status_code, 303)
        body = self.client.get("/units").get_data(as_text=True)
        self.assertNotIn("检测到换 harness", body, "忽略之后还挂着待办")
        self.assertFalse(surface.payload_path(self.ws).is_file())
        self.assertFalse((self.ws / "AGENTS.md").exists())
