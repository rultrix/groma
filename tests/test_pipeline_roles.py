"""流水线角色表：harness→站位人工配一次；棒位 = 最近会话查表、下一站即棒位。

三站固定（任务书/施工/验收）；非法表拒配；未配表的主题零配置可用
（角色接力不启用，只有同工作流触发）。
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import tempfile
import unittest
from pathlib import Path

from lab import pipeline, topics
from lab.pipeline import derive_baton, normalise_roles
from lab.workspace import LabError


def _topic(self, name: str) -> str:
    base = Path(self._tmp.name)
    home = base / name
    home.mkdir(parents=True)
    from lab import unitmatch
    from lab.workspace import init_workspace
    init_workspace(home, name=name, owner="tester")
    unit_id = unitmatch.create_unit(name, home)["unit_id"]
    topics.create_topic(name)
    return unit_id


class RoleTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-pipeline-")
        self.addCleanup(self._tmp.cleanup)

    def test_a_saved_table_persists_into_the_topic_record_and_derives_the_baton(self) -> None:
        """配表→持久化进主题登记；最近会话 harness 查表得已完成站、下一站即棒位。"""
        unit_id = _topic(self, "配表主题")
        saved = topics.set_pipeline_roles(unit_id, {"codex": "任务书", "qoder": "施工"})
        stored = topics.pipeline_roles(unit_id)
        self.assertEqual(stored["roles"], {"codex": "任务书", "qoder": "施工"})
        self.assertEqual(stored["decided_by"], "human")
        self.assertEqual(saved["name"], "配表主题")  # 表挂进主题记录，主题本体不动
        # 棒位：最近会话是 qoder → 施工站已完成 → 下一棒 = 验收
        baton = derive_baton(stored["roles"], "qoder")
        self.assertTrue(baton["enabled"])
        self.assertTrue(baton["mapped"])
        self.assertEqual(baton["completed_station"], "施工")
        self.assertEqual(baton["next_station"], "验收")
        # 最近会话是 codex → 任务书完成 → 下一棒 = 施工
        self.assertEqual(derive_baton(stored["roles"], "codex")["next_station"], "施工")
        # 已到最后一站：没有下一棒（流水线走完）
        done = derive_baton(stored["roles"], "claude") if "claude" in stored["roles"] else None
        if done:
            self.assertIsNone(done["next_station"])

    def test_an_unmapped_table_keeps_the_topic_zero_config(self) -> None:
        """未配表→无角色触发：derive 报 enabled=False，零配置可用；清表回到同一状态。"""
        unit_id = _topic(self, "零配置主题")
        self.assertIsNone(topics.pipeline_roles(unit_id), "没配表却读出了表")
        baton = derive_baton(None, "qoder")
        self.assertFalse(baton["enabled"])
        self.assertIsNone(baton["next_station"])
        # 配了再清：回到零配置
        topics.set_pipeline_roles(unit_id, {"qoder": "施工"})
        topics.set_pipeline_roles(unit_id, {})
        self.assertIsNone(topics.pipeline_roles(unit_id))

    def test_an_illegal_table_is_refused_loudly(self) -> None:
        """表非法→拒配：站位不在三站内、一站多 harness，都拒（fail-loud，不静默清洗）。"""
        with self.assertRaises(LabError):
            normalise_roles({"codex": "摸鱼"})
        with self.assertRaises(LabError):
            normalise_roles({"codex": "施工", "qoder": "施工"})
        unit_id = _topic(self, "非法表主题")
        with self.assertRaises(LabError):
            topics.set_pipeline_roles(unit_id, {"codex": "八站"})
        self.assertIsNone(topics.pipeline_roles(unit_id), "拒配之后账里不许留半张表")

    def test_the_three_stations_are_the_fixed_pipeline_order(self) -> None:
        """第一期固定三站，顺序即流水线；加站另立一期。"""
        self.assertEqual(pipeline.STATIONS, ("任务书", "施工", "验收"))


if __name__ == "__main__":
    unittest.main()
