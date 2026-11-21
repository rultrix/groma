"""Delivery visibility: the three sha256 values (manifest / disk payload / last
receipt) must be reconciled into a visible state, not left for a human to guess.

Three states, and "no delivery yet" must stay distinguishable from "drifted":
`none` = nothing delivered, `match` = the host took exactly what is on disk,
`stale` = what the host took is not what is stored now (either the payload moved
on, or the payload is gone while a receipt survives).
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import tempfile
import unittest
from pathlib import Path

from lab import hooks, surface
from lab.inject import inject_status, injection_summary
from lab.workspace import init_workspace, load_unit


class DeliveryVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-delivery-vis-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="送达可见")
        self.unit_id = load_unit(self.home)["unit_id"]

    def _seed(self, body: str, sha: str | None = None, registered: bool = False) -> str:
        """Write a payload plus a manifest that claims `sha` (default: real one)."""
        if registered:
            hooks.install(self.home, confirmed=True)
        payload = surface.write_payload(self.home, body)
        book = {
            "schema": 1,
            "surfaces": [],
            "payload": {"sha256": sha if sha is not None else payload["sha256"],
                        "written_at": "2026-11-21T00:00:00Z"},
            "source": {"asset_id": "asset-x", "unit_id": self.unit_id, "title": "背景",
                       "generated_at": "2026-11-20T00:00:00Z", "set_at": "2026-11-21T00:00:00Z"},
            "gate": {"state": "serving", "reason": None, "asset_sessions": 1,
                     "live_sessions": 1, "evidence_scope": "unit"},
        }
        surface.write_manifest(self.home, book)
        return payload["sha256"]

    def test_payload_and_manifest_without_any_receipt_reads_none(self) -> None:
        disk_sha = self._seed("背景正文 ALPHA")
        delivery = inject_status(self.home)["delivery"]
        self.assertEqual(delivery["state"], "none")
        self.assertTrue(delivery["disk_matches_manifest"])
        self.assertIsNone(delivery["delivered_sha256"])
        self.assertEqual(delivery["disk_sha256"], disk_sha)
        self.assertEqual(delivery["manifest_sha256"], disk_sha)

    def test_a_real_hook_delivery_matching_the_disk_reads_match(self) -> None:
        disk_sha = self._seed("背景正文 MATCH", registered=True)
        done = hooks.run_hook(self.home, "SessionStart", "sess-vis-1")
        self.assertIsNotNone(done["additionalContext"])  # the hook really served
        delivery = inject_status(self.home)["delivery"]
        self.assertEqual(delivery["state"], "match")
        self.assertEqual(delivery["delivered_sha256"], delivery["disk_sha256"])
        self.assertEqual(delivery["delivered_sha256"], disk_sha)
        self.assertTrue(delivery["disk_matches_manifest"])

    def test_payload_rolled_forward_after_delivery_reads_stale(self) -> None:
        self._seed("背景正文 FIRST", registered=True)
        hooks.run_hook(self.home, "SessionStart", "sess-vis-2")
        self.assertEqual(inject_status(self.home)["delivery"]["state"], "match")
        # refresh shape: payload and manifest move together, the receipt stays behind
        new_sha = self._seed("背景正文 SECOND")
        delivery = inject_status(self.home)["delivery"]
        self.assertEqual(delivery["state"], "stale")
        self.assertNotEqual(delivery["delivered_sha256"], delivery["disk_sha256"])
        self.assertTrue(delivery["disk_matches_manifest"])
        self.assertEqual(delivery["disk_sha256"], new_sha)

    def test_a_manifest_sha_that_lies_about_the_disk_is_visible(self) -> None:
        self._seed("背景正文 ALPHA", sha="0" * 64)
        delivery = inject_status(self.home)["delivery"]
        self.assertFalse(delivery["disk_matches_manifest"])
        self.assertEqual(delivery["manifest_sha256"], "0" * 64)
        self.assertNotEqual(delivery["disk_sha256"], "0" * 64)

    def test_payload_gone_but_receipt_alive_reads_stale_not_none(self) -> None:
        self._seed("背景正文 PAUSED", registered=True)
        hooks.run_hook(self.home, "SessionStart", "sess-vis-3")
        self.assertTrue(surface.remove_payload(self.home))
        delivery = inject_status(self.home)["delivery"]
        self.assertEqual(delivery["state"], "stale")  # what was served is not what is stored
        self.assertFalse(delivery["disk_matches_manifest"])
        self.assertIsNone(delivery["disk_sha256"])
        self.assertIsNotNone(delivery["delivered_sha256"])

    def test_summary_rows_carry_delivery_and_serving_is_untouched(self) -> None:
        self._seed("背景正文 SUMMARY", registered=True)
        rows = injection_summary(self.unit_id)
        row = rows[self.home.as_posix()]
        self.assertIn("delivery", row)
        self.assertEqual(row["delivery"]["state"], "none")
        self.assertTrue(row["delivery"]["disk_matches_manifest"])
        # serving's criteria are untouched: a chosen source + a registered hook
        self.assertTrue(row["serving"])
        self.assertTrue(row["hook_registered"])
        self.assertEqual(row["payload_sha256"], row["delivery"]["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
