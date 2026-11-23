"""接力回放：历史换棒只入账不投递、去重按全量账本 (workspace, at)
回放后块面文件与 hook 载荷字节不变、启动段调用被触发。

跑法：`./.venv/Scripts/python.exe -m unittest discover -s tests -p test_handoff_replay.py -v`
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import handoff, surface, topics
from lab.workspace import init_workspace


def _entry(harness: str, ts: str) -> dict:
    return {"source": harness, "path": "sessions/%s-%s.jsonl" % (harness, ts),
            "events": 3, "created_at": ts, "last_ts": ts}


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-replay-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="回放工作区", owner="tester")

    def _write_digest(self, entries: list[dict]) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        digest = {"schema": 1, "file_count": len(entries), "files": entries,
                  "unattributed_outside": 0, "unattributed_no_cwd": 0}
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps(digest, ensure_ascii=False), encoding="utf-8", newline="\n")

    def _faces_snapshot(self) -> dict:
        paths = [self.ws / "AGENTS.md", self.ws / "CLAUDE.md", self.ws / "QODER.md",
                 surface.payload_path(self.ws)]
        return {p: (p.read_bytes() if p.is_file() else None) for p in paths}

    def test_replay_produces_observed_records_and_never_delivers(self) -> None:
        """历史换棒一律 observed + decided_by=replay；块面与载荷字节不变。"""
        # 历史：codex→qoder→codex，两次换棒都该入账
        self._write_digest([_entry("codex", "2026-11-22T01:00:00Z"),
                            _entry("qoder", "2026-11-22T02:00:00Z"),
                            _entry("codex", "2026-11-22T03:00:00Z")])
        before = self._faces_snapshot()
        out = handoff.replay_handoffs(self.ws)
        self.assertEqual(out["created"], 2, "两次换棒应各入一笔")
        history = topics.handoff_history(self.ws.as_posix())
        self.assertEqual(len(history), 2)
        for item in history:
            self.assertEqual(item["status"], "observed", "回放不许产 pending/delivered")
            self.assertEqual(item["decided_by"], "replay")
        self.assertEqual({item["at"] for item in history},
                         {"2026-11-22T02:00:00Z", "2026-11-22T03:00:00Z"})
        self.assertEqual(self._faces_snapshot(), before, "回放改写了块面/载荷——违反「只入账不投递」")
        # A→A 相邻不记
        self._write_digest([_entry("codex", "2026-11-22T01:00:00Z"),
                            _entry("codex", "2026-11-22T02:00:00Z")])
        self.assertEqual(handoff.replay_handoffs(self.ws)["created"], 0)

    def test_replay_is_idempotent_on_the_full_ledger_key(self) -> None:
        """跑两遍条数不变；去重不依赖 latest_handoff——
        老时刻的 observed 行被更新的 pending 行盖住后，重放仍要跳过它。"""
        self._write_digest([_entry("codex", "2026-11-22T01:00:00Z"),
                            _entry("qoder", "2026-11-22T02:00:00Z"),
                            _entry("codex", "2026-11-22T03:00:00Z")])
        first = handoff.replay_handoffs(self.ws)
        self.assertEqual(first["created"], 2)
        # 盖住：对最近一条真实检出时刻补一笔 pending（latest_handoff 从此指向它）
        topics.record_handoff({"workspace": self.ws.as_posix(), "from_harness": "x",
                               "to_harness": "y", "at": "2026-11-22T09:00:00Z",
                               "status": "pending", "decided_by": "test"})
        self.assertEqual((topics.latest_handoff(self.ws.as_posix()) or {}).get("at"),
                         "2026-11-22T09:00:00Z", "夹具没造成 latest 被盖住")
        again = handoff.replay_handoffs(self.ws)
        self.assertEqual(again["created"], 0, "重放又入账——去重没按全量账本查")
        ats = {item.get("at") for item in topics.handoff_history(self.ws.as_posix())}
        self.assertEqual(ats, {"2026-11-22T02:00:00Z", "2026-11-22T03:00:00Z",
                               "2026-11-22T09:00:00Z"})

    def test_detection_stays_silent_for_any_ledger_status_at_the_same_moment(self) -> None:
        """detect_handoff 去重升级为全量账本任意 status：同 (workspace, at) 已有 observed
        （回放入账）就不再产出待办——「现在投/忽略」只属于全新时刻。"""
        self._write_digest([_entry("codex", "2026-11-22T01:00:00Z"),
                            _entry("qoder", "2026-11-22T02:00:00Z")])
        handoff.replay_handoffs(self.ws)
        self.assertIsNone(handoff.detect_handoff(self.ws),
                          "回放已入账的时刻又被检出——同一时刻出现了两笔")

    def test_the_startup_paths_call_the_replay(self) -> None:
        """回放只在启动段跑——cli `groma web` 与 shell 各自的 catchup 之后都要有调用。"""
        from lab import cli

        cli_src = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertIn("replay_unit_handoffs", cli_src, "cli web 启动段没接回放")
        self.assertLess(cli_src.index("cli:web-start"), cli_src.index("replay_unit_handoffs"),
                        "回放调用要落在 catchup 之后")
        import groma_shell.app as shell_app

        shell_src = Path(shell_app.__file__).read_text(encoding="utf-8")
        self.assertIn("replay_unit_handoffs", shell_src, "shell 启动段没接回放（不复用 cli 路径）")
        self.assertLess(shell_src.index("shell:start"), shell_src.index("replay_unit_handoffs"),
                        "shell 的回放调用要落在自己的 catchup 之后")

    def test_replay_unit_handoffs_covers_member_workspaces(self) -> None:
        """启动段入口：对本主题成员工作区各跑一遍，汇总 created。"""
        from lab import unitmatch

        self._write_digest([_entry("codex", "2026-11-22T01:00:00Z"),
                            _entry("qoder", "2026-11-22T02:00:00Z")])
        unitmatch.create_unit("回放主题", self.ws)
        out = handoff.replay_unit_handoffs(self.ws)
        self.assertEqual(out["created"], 1)
        self.assertIn(self.ws.as_posix(), out["workspaces"])
        # 空主题（无成员）不炸、不产账
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(handoff.replay_unit_handoffs(empty), {"created": 0, "workspaces": []})


if __name__ == "__main__":
    unittest.main()
