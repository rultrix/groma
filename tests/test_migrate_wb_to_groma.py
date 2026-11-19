"""The one-release compatibility layer and the wb -> Groma migration."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import statebox  # noqa: F401  -- must land before any lab import touches state

from lab import contract, hooks, migrate, schedule, state, surface, unitlink
from lab.workspace import init_workspace, load_unit, meta_dir


class LegacyReadTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _legacy_workspace(self, unit_id="unit_legacy"):
        meta = self.root / contract.LEGACY_META_DIR
        meta.mkdir(parents=True)
        (meta / "unit.json").write_text(
            json.dumps({"schema": 1, "unit_id": unit_id, "name": "old"}), encoding="utf-8"
        )
        (self.root / contract.LEGACY_LINK_FILE).write_text(
            json.dumps({"schema": 1, "unit_id": unit_id, "name": "old"}), encoding="utf-8"
        )

    def test_legacy_meta_and_card_are_read_and_kept_as_one_store(self):
        self._legacy_workspace()
        self.assertEqual(meta_dir(self.root).name, contract.LEGACY_META_DIR)
        unit = load_unit(self.root)
        self.assertEqual(unit["unit_id"], "unit_legacy")
        self.assertEqual(unit["identity_file"], contract.LEGACY_LINK_FILE)
        # A write must not mint a second card next to the legacy one.
        unitlink.set_org_path(self.root, "github.com/owner/repo")
        self.assertTrue((self.root / contract.LEGACY_LINK_FILE).is_file())
        self.assertFalse((self.root / contract.LINK_FILE).exists())

    def test_clear_link_removes_both_names(self):
        self._legacy_workspace()
        (self.root / contract.LINK_FILE).write_text('{"unit_id": "unit_new"}', encoding="utf-8")
        self.assertTrue(unitlink.clear_link(self.root))
        self.assertFalse((self.root / contract.LINK_FILE).exists())
        self.assertFalse((self.root / contract.LEGACY_LINK_FILE).exists())

    def test_legacy_environment_names_still_select_the_roots(self):
        with mock.patch.dict(os.environ, {"GROMA_STATE_DIR": ""}):
            os.environ["WB_STATE_DIR"] = str(self.root / "old-state")
            try:
                self.assertEqual(state.state_root(), self.root / "old-state")
            finally:
                os.environ.pop("WB_STATE_DIR", None)
        with mock.patch.dict(os.environ, {"GROMA_VAULT_DIR": ""}):
            os.environ["WB_VAULT_DIR"] = str(self.root / "old-vault")
            try:
                from lab import vault

                self.assertEqual(vault.vault_root(), self.root / "old-vault")
            finally:
                os.environ.pop("WB_VAULT_DIR", None)

    def test_old_artifact_kind_is_accepted(self):
        self.assertTrue(contract.has_kind({"kind": "WB_LOCAL_DIGEST"}, "GROMA_LOCAL_DIGEST"))
        self.assertTrue(contract.has_kind({"kind": "GROMA_LOCAL_DIGEST"}, "GROMA_LOCAL_DIGEST"))
        self.assertFalse(contract.has_kind({"kind": "OTHER"}, "GROMA_LOCAL_DIGEST"))

    def test_generated_hook_falls_back_to_the_legacy_directory(self):
        meta = self.root / contract.LEGACY_META_DIR / "injections"
        meta.mkdir(parents=True)
        (meta / "payload.md").write_text("背景\n", encoding="utf-8")
        hooks.install(self.root, confirmed=True)
        result = hooks.run_hook(self.root, "SessionStart", "session-1")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["additionalContext"], "背景\n")

    def test_old_marker_block_can_be_replaced_and_removed(self):
        target = self.root / "CLAUDE.md"
        target.write_text(
            "手写一行\n<!-- wb:inject unit=u asset=a -->\n旧块\n<!-- /wb:inject -->\n",
            encoding="utf-8",
        )
        self.assertTrue(surface.has_block(target))
        surface.apply_block(self.root, "CLAUDE.md", "u", "a", "新块")
        text = target.read_text(encoding="utf-8")
        self.assertIn("groma:inject", text)
        self.assertNotIn("wb:inject", text)
        self.assertTrue(text.startswith("手写一行\n"))
        surface.remove_block(self.root, "CLAUDE.md")
        self.assertEqual(target.read_text(encoding="utf-8").strip(), "手写一行")


class LegacyLauncherTests(unittest.TestCase):
    def test_wb_py_alias_still_reaches_the_groma_cli(self):
        import subprocess
        import sys

        repo = Path(__file__).resolve().parent.parent
        done = subprocess.run(
            [sys.executable, str(repo / "wb.py"), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.strip().startswith("groma "), done.stdout)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        meta = self.root / contract.LEGACY_META_DIR
        meta.mkdir(parents=True)
        (meta / "unit.json").write_text(
            json.dumps({"schema": 1, "unit_id": "unit_legacy", "name": "legacy-space"}),
            encoding="utf-8",
        )
        (meta / "agents.json").write_text('{"schema": 1, "agents": []}', encoding="utf-8")
        (meta / "receipts").mkdir()
        (self.root / contract.LEGACY_LINK_FILE).write_text(
            json.dumps({"schema": 1, "unit_id": "unit_legacy", "name": "legacy-space"}),
            encoding="utf-8",
        )
        state.register_workspace(self.root, "legacy-space")

    def tearDown(self):
        self._tmp.cleanup()

    def test_preview_reports_without_writing(self):
        before = sorted(p.name for p in self.root.iterdir())
        report = migrate.run(apply=False)
        self.assertFalse(report["applied"])
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))
        mine = next(item for item in report["workspaces"] if item["path"] == self.root.as_posix())
        self.assertTrue(any(action["action"] == "rename" for action in mine["actions"]))

    def test_apply_moves_meta_and_card_and_keeps_the_unit(self):
        unit_id = load_unit(self.root)["unit_id"]
        report = migrate.run(apply=True)
        mine = next(item for item in report["workspaces"] if item["path"] == self.root.as_posix())
        self.assertEqual(mine["errors"], [])
        self.assertTrue((self.root / contract.META_DIR / "unit.json").is_file())
        self.assertFalse((self.root / contract.LEGACY_META_DIR).exists())
        self.assertTrue((self.root / contract.LINK_FILE).is_file())
        self.assertFalse((self.root / contract.LEGACY_LINK_FILE).exists())
        self.assertEqual(load_unit(self.root)["unit_id"], unit_id)

    def test_both_names_is_a_conflict_not_a_merge(self):
        (self.root / contract.META_DIR).mkdir()
        report = migrate.run(apply=True)
        mine = next(item for item in report["workspaces"] if item["path"] == self.root.as_posix())
        self.assertTrue(mine["conflicts"])
        self.assertTrue((self.root / contract.LEGACY_META_DIR).is_dir())
        self.assertTrue((self.root / contract.META_DIR).is_dir())

    def test_legacy_schedule_record_is_reinstalled_under_the_new_prefix(self):
        record = {
            "backend": "test-mock",
            "task_name": contract.LEGACY_TASK_PREFIX + "legacy",
            "period_minutes": 45,
            "status": "installed",
        }
        meta = self.root / contract.LEGACY_META_DIR
        (meta / "scheduler.json").write_text(json.dumps(record), encoding="utf-8")
        with mock.patch.dict(os.environ, {contract.SCHEDULER_MOCK_ENV: "1"}):
            report = migrate.run(apply=True)
        mine = next(item for item in report["workspaces"] if item["path"] == self.root.as_posix())
        self.assertEqual(mine["errors"], [])
        after = json.loads((self.root / contract.META_DIR / "scheduler.json").read_text(encoding="utf-8"))
        self.assertTrue(after["task_name"].startswith(contract.TASK_PREFIX))
        self.assertEqual(after["period_minutes"], 45)

    def test_orphan_legacy_task_without_a_record_is_rediscovered(self):
        orphan = "WBLOCAL-legacy-TOPIC"
        with mock.patch.object(schedule, "task_present", return_value=True), mock.patch.object(
            schedule, "task_period", return_value=1440
        ):
            plan = migrate._workspace_plan(self.root)
            self.assertTrue(
                any(action["action"] == "reinstall-orphan-task" for action in plan["actions"]),
                plan,
            )
            with mock.patch.object(schedule, "delete_task") as deleted, mock.patch.dict(
                os.environ, {contract.SCHEDULER_MOCK_ENV: "1"}
            ):
                report = migrate.run(apply=True)
        mine = next(item for item in report["workspaces"] if item["path"] == self.root.as_posix())
        self.assertEqual(mine["errors"], [])
        after = json.loads((self.root / contract.META_DIR / "scheduler-topic.json").read_text(encoding="utf-8"))
        self.assertTrue(after["task_name"].startswith(contract.TASK_PREFIX))
        self.assertIn(mock.call("WBLOCAL-legacy-TOPIC"), deleted.call_args_list)

    def test_manifest_rule_path_is_rewritten(self):
        old_rule = self.root / contract.LEGACY_RULE_FILE
        old_rule.parent.mkdir(parents=True)
        old_rule.write_text(
            "<!-- wb:inject unit=u asset=a -->\n旧块\n<!-- /wb:inject -->\n", encoding="utf-8"
        )
        surface.write_manifest(
            self.root,
            {
                "schema": 1,
                "source": {"asset_id": "a"},
                "surfaces": [{"face": "qoder-rule", "path": contract.LEGACY_RULE_FILE, "asset_id": "a"}],
            },
        )
        report = migrate.run(apply=True)
        mine = next(item for item in report["workspaces"] if item["path"] == self.root.as_posix())
        self.assertEqual(mine["errors"], [])
        book = surface.read_manifest(self.root)
        self.assertEqual(book["surfaces"][0]["path"], contract.RULE_FILE)
        self.assertFalse(old_rule.exists())


if __name__ == "__main__":
    unittest.main()
