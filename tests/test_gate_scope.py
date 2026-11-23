"""paused 的分母口径字段化：文案由 `evidence_scope` 生成，拼句子时不再二次推断。"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import inject, topics


def _asset() -> dict:
    return {"asset_id": "asset-x", "totals": {"sessions": 1},
            "generated_at": "2026-01-01T00:00:00Z"}


def _gate(topic_id):
    with mock.patch.object(inject.unitmatch, "home_path", return_value="C:/ws"), \
            mock.patch.object(inject.unitmatch, "unit_digest_union",
                              return_value={"file_count": 10, "created_at": "2026-02-01T00:00:00Z"}), \
            mock.patch.object(topics, "topic_of_asset", return_value=topic_id), \
            mock.patch.object(topics, "active_evidence",
                              return_value=[{"source": "s", "path": "p"}] * 10):
        return inject.gate_state("unit_x", _asset())


class GateScopeTests(unittest.TestCase):
    def test_a_topic_asset_measures_against_the_topic(self) -> None:  # A4-1
        gate = _gate("unit_x")
        self.assertEqual(gate["state"], "paused")
        self.assertEqual(gate["evidence_scope"], "topic")
        self.assertIn("本协作主题", gate["reason"])
        self.assertNotIn("本 Unit", gate["reason"])

    def test_a_hand_made_asset_still_says_unit(self) -> None:  # A4-2
        gate = _gate(None)
        self.assertEqual(gate["state"], "paused")
        self.assertEqual(gate["evidence_scope"], "unit")
        self.assertIn("本 Unit 现况", gate["reason"])

    def test_a_serving_asset_carries_the_scope_too(self) -> None:
        with mock.patch.object(inject.unitmatch, "home_path", return_value=None), \
                mock.patch.object(topics, "topic_of_asset", return_value=None):
            gate = inject.gate_state("unit_x", _asset())
        self.assertEqual(gate["evidence_scope"], "unit")
        self.assertEqual(gate["state"], "serving")

class GateProjectionEquivalenceTests(unittest.TestCase):
    """B 类不变量：源升主题级后，投影 manifest 里的 gate 与 gate_state 直算相等。"""

    def setUp(self) -> None:
        import tempfile
        from unittest import mock

        from lab import unitmatch
        from lab.asset import build_asset
        from lab.collect import catchup
        from lab.sources import add_source
        from lab.workspace import init_workspace, load_unit

        self._tmp = tempfile.TemporaryDirectory(prefix="wb-gate-equiv-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="闸门等价")
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-02T09:00:00Z", "cwd": "%s"}\n' % self.home.as_posix(),
            encoding="utf-8", newline="\n")
        add_source(self.home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(self.home, force=True)
        self.asset = build_asset(self.home, "等价资产", [("qoder", "projects/s.jsonl")])
        self.unit_id = load_unit(self.home)["unit_id"]
        self._mock = mock

    def test_the_projected_manifest_gate_equals_a_direct_gate_state_call(self) -> None:
        out = inject.inject_theme(self.unit_id, self.asset["asset_id"])
        self.assertTrue(out["workspaces"])
        for workspace in out["workspaces"]:
            book = json.loads(
                (Path(workspace["workspace"]) / ".groma" / "injections" / "manifest.json")
                .read_text(encoding="utf-8"))
            direct = inject.gate_state(self.unit_id, self.asset)
            self.assertEqual(book["gate"]["state"], direct["state"])
            self.assertEqual(book["gate"]["evidence_scope"], direct["evidence_scope"])
