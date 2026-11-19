"""Regression tests for `state.remove_workspace`'s Unit cascade.

`remove_workspace` writes four state files in sequence. The Unit pass at the end
used to raise `NameError` on `data`, which left the earlier writes applied: the
workspace was already forgotten while every Unit still pointed at it. These
tests fail on that shape, so they pin both the exception and the half-write.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import state, unitmatch
from lab.workspace import init_workspace, load_unit

UNIT_ID = "unit_cascade0000001"


class WorkspaceRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _workspace(self, name: str, unit_id: str) -> Path:
        folder = self.base / name
        folder.mkdir(parents=True, exist_ok=True)
        card = {
            "schema": 1,
            "unit_id": unit_id,
            "name": name,
            "org_path": None,
            "created_at": "2026-11-05T00:00:00Z",
        }
        (folder / "groma-unit.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return folder

    def test_removing_the_only_workspace_drops_the_unit_instead_of_raising(self) -> None:
        home = self._workspace("home", UNIT_ID)
        record = state.register_workspace(home, "home")
        state.upsert_unit(UNIT_ID, name="home", org_path=None, home_binding=record["ref"])

        result = state.remove_workspace(home, delete_evidence=False)

        self.assertTrue(result["forgotten"])
        self.assertEqual(state.list_workspaces(), [])
        self.assertEqual([item["unit_id"] for item in state.list_units()], [])

    def test_a_sibling_workspace_keeps_the_unit_and_no_dangling_home(self) -> None:
        home = self._workspace("home", UNIT_ID)
        sibling = self._workspace("sibling", UNIT_ID)
        home_ref = state.register_workspace(home, "home")["ref"]
        state.register_workspace(sibling, "sibling")
        state.upsert_unit(UNIT_ID, name="home", org_path=None, home_binding=home_ref)

        state.remove_workspace(home, delete_evidence=False)

        units = state.list_units()
        self.assertEqual([item["unit_id"] for item in units], [UNIT_ID])
        self.assertIsNone(units[0]["home_binding"])
        surviving = {item["ref"] for item in state.list_workspaces()}
        for item in units:
            self.assertTrue(
                item.get("home_binding") is None or item.get("home_binding") in surviving,
                "a Unit must not point at a workspace that has been forgotten",
            )


    def _folder_with_card(self, name: str, unit_id: str) -> Path:
        folder = self._workspace(name, unit_id)
        init_workspace(folder, name=name)
        unitmatch.sync_units()
        return folder

    def test_a_leftover_card_revives_the_unit_when_the_folder_reopens(self) -> None:
        folder = self._folder_with_card("revive", UNIT_ID)
        self.assertIn(UNIT_ID, [item["unit_id"] for item in state.list_units()])

        state.remove_workspace(folder, delete_evidence=False)
        unitmatch.sync_units()
        self.assertNotIn(UNIT_ID, [item["unit_id"] for item in state.list_units()])

        shutil.rmtree(folder / ".groma")  # the repository is opened again later
        init_workspace(folder, name="revive")
        unitmatch.sync_units()
        # Pinned as current behaviour, not endorsed: nothing resurrects the Unit
        # except the card still lying in the folder.
        self.assertIn(UNIT_ID, [item["unit_id"] for item in state.list_units()])
        self.assertEqual(load_unit(folder)["unit_id"], UNIT_ID)

    def test_removing_the_card_stops_the_revival(self) -> None:
        folder = self._folder_with_card("nocard", UNIT_ID)

        state.remove_workspace(folder, delete_evidence=False)
        (folder / "groma-unit.json").unlink()
        shutil.rmtree(folder / ".groma")
        init_workspace(folder, name="nocard")
        unitmatch.sync_units()

        self.assertNotIn(UNIT_ID, [item["unit_id"] for item in state.list_units()])
        self.assertNotEqual(load_unit(folder)["unit_id"], UNIT_ID)


class OrphanReceiptTests(unittest.TestCase):
    """Option (b): dropping an orphaned Unit leaves a record that outlives the workspace."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _one_unit_workspace(self, name: str = "solo") -> Path:
        folder = self.base / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".groma").mkdir()
        unitlink_ = json.dumps(
            {"schema": 1, "unit_id": UNIT_ID, "name": name, "org_path": "github.com/rultrix/solo",
             "created_at": "2026-11-05T00:00:00Z"},
            ensure_ascii=False, sort_keys=True)
        (folder / "groma-unit.json").write_text(unitlink_ + "\n", encoding="utf-8", newline="\n")
        record = state.register_workspace(folder, name)
        state.upsert_unit(UNIT_ID, name=name, org_path="github.com/rultrix/solo",
                          home_binding=record["ref"])
        return folder

    def _lines(self) -> list[dict]:
        path = state.state_root() / state.RETIRED
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_receipt_records_the_dropped_unit(self) -> None:
        folder = self._one_unit_workspace()

        state.remove_workspace(folder, delete_evidence=False)

        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["unit_id"], UNIT_ID)
        self.assertEqual(lines[0]["home_binding"], state.workspace_ref(folder))
        self.assertEqual(lines[0]["org_path"], "github.com/rultrix/solo")
        self.assertTrue(lines[0]["retired_at"])

    def test_receipt_is_tamper_evident(self) -> None:
        folder = self._one_unit_workspace()
        state.remove_workspace(folder, delete_evidence=False)
        stored = self._lines()[0]
        body = {key: value for key, value in stored.items() if key != "sha256"}
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True)
        self.assertEqual(hashlib.sha256(canonical.encode("utf-8")).hexdigest(), stored["sha256"])

    def test_deleting_the_workspace_evidence_cannot_reach_the_receipt(self) -> None:
        """`同时删除 .groma/（不可恢复）` stays true while the audit survives: different objects."""
        folder = self._one_unit_workspace()

        state.remove_workspace(folder, delete_evidence=True)

        self.assertFalse((folder / ".groma").exists())  # the workspace really is unrecoverable
        self.assertTrue((state.state_root() / state.RETIRED).is_file())  # the receipt is elsewhere
        self.assertEqual(self._lines()[0]["unit_id"], UNIT_ID)

    def test_no_receipt_when_the_unit_still_has_a_home(self) -> None:
        folder = self._one_unit_workspace()
        other = self.base / "sibling"
        other.mkdir()
        state.register_workspace(other, "sibling")
        state.add_membership(UNIT_ID, state.workspace_ref(other), role="evidence",
                             signal="manual", source="manual")

        state.remove_workspace(folder, delete_evidence=False)

        self.assertFalse((state.state_root() / state.RETIRED).exists())
        self.assertIn(UNIT_ID, [item["unit_id"] for item in state.list_units()])


if __name__ == "__main__":
    unittest.main()
