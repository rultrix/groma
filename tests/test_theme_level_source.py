"""Theme-level injection source: one asset serves every member, regions pause
separately, the manifest is only a projection of the topic ledger.

Legacy per-workspace manifests are migrated (migrate.run) into the ledger; the
gate's three states and the delivery reconciliation keep their semantics.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import tempfile
import unittest
from pathlib import Path

from lab import inject, migrate, surface, topics, unitmatch
from lab.asset import build_asset
from lab.collect import catchup
from lab.inject import inject_status, inject_theme, inject_unit, refresh, uninject
from lab.sources import add_source
from lab.workspace import init_workspace, load_unit


class ThemeLevelSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-theme-src-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="源主题")
        self.member = base / "member"
        self.member.mkdir()
        unitmatch.join(self.home, None, self.member, role="evidence")
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-02T09:00:00Z", "cwd": "%s"}\n' % self.home.as_posix(),
            encoding="utf-8", newline="\n",
        )
        add_source(self.home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(self.home, force=True)
        self.asset = build_asset(self.home, "主题级资产", [("qoder", "projects/s.jsonl")])
        self.asset_id = self.asset["asset_id"]
        self.unit_id = load_unit(self.home)["unit_id"]

    def _manifest(self, root: Path) -> dict:
        return json.loads((root / ".groma" / "injections" / "manifest.json").read_text(encoding="utf-8"))

    def test_one_registration_serves_every_member(self) -> None:
        out = inject_theme(self.unit_id, self.asset_id)
        self.assertEqual(sorted(Path(w["workspace"]).name for w in out["workspaces"]),
                         ["home", "member"])
        for member in (self.home, self.member):
            book = self._manifest(member)
            self.assertEqual(book["source"]["asset_id"], self.asset_id)
            self.assertEqual(book["source"]["authority"], "topic")
            self.assertEqual(book["source"]["topic_id"], self.unit_id)
            self.assertTrue(surface.payload_path(member).is_file())
        # the ledger is the authority: exactly one row, and it names the asset
        record = topics.get_injection_source(self.unit_id)
        self.assertEqual(record["asset_id"], self.asset_id)
        self.assertEqual(record["decided_by"], "human")

    def test_a_region_pause_stops_one_member_and_leaves_the_other_serving(self) -> None:
        inject_theme(self.unit_id, self.asset_id)
        member_path = self.member.as_posix()
        topics.set_region_paused(self.unit_id, member_path, True)
        out = inject_theme(self.unit_id)
        states = {Path(w["workspace"]).name: w["state"] for w in out["workspaces"]}
        self.assertEqual(states["member"], "paused")
        self.assertEqual(states["home"], "serving")
        self.assertFalse(surface.payload_path(self.member).is_file())
        self.assertTrue(surface.payload_path(self.home).is_file())
        book = self._manifest(self.member)
        self.assertEqual(book["gate"]["state"], "paused")
        self.assertIn("按区暂停", book["gate"]["reason"])
        # and the summary still tells the truth about both regions
        summary = inject.injection_summary(self.unit_id)
        self.assertEqual(summary[member_path]["state"], "paused")
        self.assertEqual(summary[self.home.as_posix()]["state"], "serving")

    def test_resuming_a_region_rebuilds_its_payload(self) -> None:
        inject_theme(self.unit_id, self.asset_id)
        member_path = self.member.as_posix()
        topics.set_region_paused(self.unit_id, member_path, True)
        inject_theme(self.unit_id)
        self.assertFalse(surface.payload_path(self.member).is_file())
        topics.set_region_paused(self.unit_id, member_path, False)
        inject_theme(self.unit_id)
        self.assertTrue(surface.payload_path(self.member).is_file())
        self.assertEqual(self._manifest(self.member)["gate"]["state"], "serving")

    def test_uninject_is_scoped_by_theme_and_keeps_other_rows_alive(self) -> None:
        inject_theme(self.unit_id, self.asset_id)
        # a second theme's row lands in the same manifest (multi-theme rows are legal shapes)
        book = self._manifest(self.home)
        book["surfaces"].append({"face": "claude-md", "path": "CLAUDE.md", "unit_id": "unit_other",
                                 "asset_id": "asset-other", "block_sha256": "x" * 64})
        surface.write_manifest(self.home, book)
        out = uninject(self.home, unit_id=self.unit_id)
        self.assertEqual(len(out["removed"]), 0)  # payload-only source: no blocks to remove
        kept = self._manifest(self.home)
        self.assertEqual([item["unit_id"] for item in kept["surfaces"]], ["unit_other"])
        # the payload survives because another theme still has a row here
        self.assertTrue(surface.payload_path(self.home).is_file())

    def test_uninject_without_a_theme_still_clears_everything(self) -> None:
        inject_unit(self.home, self.asset_id, unit_id=self.unit_id)
        uninject(self.home)
        self.assertEqual(refresh(self.home)["state"], "off")
        self.assertFalse(surface.payload_path(self.home).is_file())

    def test_the_gate_keeps_its_semantics_through_the_projection(self) -> None:
        """B invariant: gate is computed once by gate_state; the projection writes
        the same verdict the pre-migration manifest would have carried."""
        inject_theme(self.unit_id, self.asset_id)
        from lab.asset import load_asset as _load_asset  # 读侧收敛到单槽

        asset = _load_asset(self.home, self.asset_id, self.unit_id)
        direct = inject.gate_state(self.unit_id, asset)
        self.assertEqual(self._manifest(self.home)["gate"], direct)
        self.assertIn(direct["state"], ("serving", "stale", "paused"))

    def test_the_delivery_reconciliation_still_reads_the_projection(self) -> None:
        inject_theme(self.unit_id, self.asset_id)
        status = inject_status(self.home)
        self.assertEqual(status["delivery"]["state"], "none")  # nothing delivered yet
        self.assertTrue(status["delivery"]["disk_matches_manifest"])
        self.assertIn("delivery", inject.injection_summary(self.unit_id)[self.home.as_posix()])


class ManifestMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-src-migrate-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="迁移主题")
        self.unit_id = load_unit(self.home)["unit_id"]

    def test_a_legacy_manifest_source_becomes_the_topic_registration(self) -> None:
        surface.write_payload(self.home, "旧载荷")
        surface.write_manifest(self.home, {"schema": 1, "surfaces": [], "source": {
            "asset_id": "asset-legacy", "unit_id": self.unit_id, "title": "旧源",
            "set_at": "2026-11-01T00:00:00Z",
        }})
        plan = migrate.run(apply=False)
        self.assertEqual(len(plan["injection_sources"]["to_register"]), 1)
        applied = migrate.run(apply=True)
        self.assertEqual(applied["injection_sources"]["registered"][0]["asset_id"], "asset-legacy")
        record = topics.get_injection_source(self.unit_id)
        self.assertEqual(record["asset_id"], "asset-legacy")
        self.assertEqual(record["decided_by"], "manifest-migration")
        # idempotent: a second pass has nothing left to register
        self.assertEqual(migrate.run(apply=False)["injection_sources"]["to_register"], [])
        # a projection manifest (authority=topic) is never re-imported
        surface.write_manifest(self.home, {"schema": 1, "surfaces": [], "source": {
            "asset_id": "asset-other", "unit_id": self.unit_id, "authority": "topic"}})
        self.assertEqual(migrate.run(apply=False)["injection_sources"]["to_register"], [])


if __name__ == "__main__":
    unittest.main()
