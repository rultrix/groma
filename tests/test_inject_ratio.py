"""注入口径统一：theme_injection_ratio 是唯一来源。

分子=injection_summary 行中 asset_id 非空的区数（paused 但有源计入）；分母=成员区总数；
送达聚合=同一批行的 delivery.state：含 stale→stale、全 match→match、一行都没有→none。

跑法：`./.venv/Scripts/python.exe -m unittest discover -s tests -p test_inject_ratio.py -v`
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import hooks, inject, surface, topics, unitmatch
from lab.asset import build_asset
from lab.workspace import init_workspace


class ThemeInjectionRatioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-ratio-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="口径主题", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import unitmatch

        self.unit_id = unitmatch.create_unit("口径主题", self.root)["unit_id"]
        self.member = base / "member"
        self.member.mkdir()
        init_workspace(self.member, name="口径成员", owner="tester")
        unitmatch.join_unit(self.unit_id, self.member, role="evidence")

    def _asset(self, title: str) -> dict:
        home = Path(unitmatch.home_path(self.unit_id) or self.root)
        folder = home / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/a.jsonl", "events": 3,
                    "created_at": "2026-11-22T01:00:00Z", "last_ts": "2026-11-22T01:00:00Z"}]
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": 1,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")
        return build_asset(home, title, [("codex", "sessions/a.jsonl")])

    def _inject_all(self, asset_id: str) -> None:
        inject.inject_theme(self.unit_id, asset_id)
        book = surface.read_manifest(self.member)
        if not (book.get("source") or {}).get("asset_id"):
            inject.inject_unit(self.member, asset_id)

    def test_paused_with_source_counts_as_injected(self) -> None:
        """两区一服役一 paused（有源）→ injected=2——暂停区确实被注入了。"""
        asset = self._asset("口径资产")
        self._inject_all(asset["asset_id"])
        topics.set_region_paused(self.unit_id, self.member.as_posix(), True)
        inject.inject_theme(self.unit_id, asset["asset_id"], decided_by="region-pause")
        book = surface.read_manifest(self.member)
        self.assertTrue((book.get("source") or {}).get("asset_id"), "夹具没造成 paused 有源")
        out = inject.theme_injection_ratio(self.unit_id)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["injected"], 2, "paused 但有源的区不计入分子——口径退回旧分裂")

    def test_empty_asset_id_row_is_not_injected(self) -> None:
        """一区有源一区无源 → injected=1/2；serving_rows 名单与计数同源。"""
        asset = self._asset("口径资产")
        self._inject_all(asset["asset_id"])
        out = inject.theme_injection_ratio(self.unit_id)
        self.assertEqual((out["injected"], out["total"]), (2, 2))
        inject.uninject(self.member, unit_id=self.unit_id)
        out = inject.theme_injection_ratio(self.unit_id)
        self.assertEqual((out["injected"], out["total"]), (1, 2),
                         "asset_id 空的行进了分子——口径不是「asset_id 非空」")
        self.assertEqual(len(out["serving_rows"]), 1)
        self.assertIn(self.root.as_posix(), out["serving_rows"])

    def test_delivery_aggregation_counts_stale_rows(self) -> None:
        """送达聚合=同一批行的 delivery.state：含 stale→stale（旧行为只扫 serving 行，
        stale 被滤掉后 hero 显示「还没有送达记录」而行内有送达——0.61.0 真机实锤）。"""
        asset = self._asset("口径资产")
        self._inject_all(asset["asset_id"])
        # 伪回执：host 取走的是一份旧载荷（sha 与盘上不符）⇒ delivery.state=stale
        receipt = hooks.receipt_dir(self.member) / "inject.log"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            "2026-11-22T00:00:00Z event=SessionStart session=fake0000 action=inject "
            "chars=10 sha256=%s\n" % ("0" * 64), encoding="utf-8", newline="\n")
        out = inject.theme_injection_ratio(self.unit_id)
        self.assertEqual(out["delivery"], "stale", "stale 行没进聚合——送达对账口径仍在滤行")

    def test_no_delivery_rows_means_never(self) -> None:
        """没有任何送达回执：聚合=none（界面显示「从未」），不装作已送达。"""
        asset = self._asset("口径资产")
        self._inject_all(asset["asset_id"])
        out = inject.theme_injection_ratio(self.unit_id)
        self.assertEqual(out["delivery"], "none")


if __name__ == "__main__":
    unittest.main()
