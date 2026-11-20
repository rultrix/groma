"""paused 的分母口径字段化：文案由 `evidence_scope` 生成，拼句子时不再二次推断。"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import unittest
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