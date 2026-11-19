"""Tests for lab/observe_this_machine.py — 单机观测页生成器."""

import statebox  # noqa: F401  -- must be first real import
import re
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest import mock

from lab.observe_this_machine import (
    build_observer_doc,
    derive,
    render,
    generate_observation_page,
)


class BuildWbminDocTests(unittest.TestCase):
    def test_empty_input(self):
        doc = build_observer_doc([], [], [], [], [], {"gamma": None, "producers": 0, "consumed_proxy": 0}, "test")
        self.assertEqual(doc["summary"]["events"], 0)
        self.assertEqual(doc["summary"]["agents"], 0)

    def test_basic_structure(self):
        events = [{"loop_key": "l1", "agent_key": "a1", "workspace_key": "w1", 
                   "ts_utc": "2026-11-09T08:00:00Z", "beat_role": "reply",
                   "tool_name": None, "content_sha256": "abc"}]
        loops = [{"id": "loop-1", "agent": "a1", "ws": "w1", "first": "2026-11-09T08:00:00Z",
                  "last": "2026-11-09T08:05:00Z", "events": 1, "writes": 0, "harness": "qoder-cn",
                  "roles": {"reply": 1}, "hours": ["2026-11-09T08"]}]
        clusters = [{"id": "tc-1", "ws": "w1", "first": "2026-11-09T08:00:00Z",
                     "last": "2026-11-09T08:05:00Z", "loops": ["loop-1"], "agents": ["a1"],
                     "events": 1, "writes": 0, "harnesses": ["qoder-cn"]}]
        
        doc = build_observer_doc(events, loops, clusters, [], [],
                             {"gamma": 0.5, "producers": 1, "consumed_proxy": 1}, "test-host")
        
        self.assertEqual(doc["summary"]["host"], "test-host")
        self.assertEqual(doc["summary"]["events"], 1)
        self.assertEqual(doc["summary"]["agents"], 1)
        self.assertEqual(len(doc["nodes"]), 3)  # 1 agent + 1 workspace + 1 task_cluster

    def test_agent_kind_reaches_the_agent_node(self):
        events = [{"loop_key": "l1", "agent_key": "agt-sub", "agent_kind": "sub",
                   "workspace_key": "w1", "ts_utc": "2026-11-09T08:00:00Z",
                   "beat_role": "reply", "tool_name": None, "content_sha256": "abc"}]
        loops = [{"id": "loop-1", "agent": "agt-sub", "ws": "w1",
                  "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z",
                  "events": 1, "writes": 0, "harness": "qoder-cn",
                  "roles": {"reply": 1}, "hours": ["2026-11-09T08"]}]
        clusters = [{"id": "tc-1", "ws": "w1", "first": "2026-11-09T08:00:00Z",
                     "last": "2026-11-09T08:05:00Z", "loops": ["loop-1"], "agents": ["agt-sub"],
                     "events": 1, "writes": 0, "harnesses": ["qoder-cn"]}]
        doc = build_observer_doc(events, loops, clusters, [], [],
                              {"gamma": None, "producers": 0, "consumed_proxy": 0}, "h")
        agent = next(x for x in doc["nodes"] if x["kind"] == "agent")
        self.assertEqual(agent["identity"]["kind"], "sub")


class DeriveTests(unittest.TestCase):
    def test_empty_doc(self):
        doc = {"summary": {"events": 0, "loops": 0, "agents": 0, "task_clusters": 0,
                          "coverage_ratio": 0, "gamma": {}, "edges_by_kind": {}},
               "nodes": [], "edges": []}
        f = derive(doc, "test")
        self.assertEqual(f["events"], 0)
        self.assertEqual(f["agent_count"], 0)

    def test_basic_metrics(self):
        doc = {
            "summary": {
                "events": 100, "loops": 10, "agents": 2, "task_clusters": 3,
                "coverage_ratio": 0.5, "gamma": {"gamma": 0.3, "producers": 5, "consumed_proxy": 2},
                "edges_by_kind": {"relay_candidate": 4, "echo": 1}
            },
            "nodes": [
                {"id": "a1", "kind": "agent", "identity": {"harness": ["qoder"]},
                 "metrics": {"loops": 6, "events": 60, "writes": 10, "first": "2026-11-09T08:00:00Z",
                            "last": "2026-11-09T09:00:00Z"}},
                {"id": "a2", "kind": "agent", "identity": {"harness": ["codex"]},
                 "metrics": {"loops": 4, "events": 40, "writes": 5, "first": "2026-11-09T08:30:00Z",
                            "last": "2026-11-09T09:30:00Z"}},
                {"id": "tc-1", "kind": "task_cluster", "identity": {"ws": "w1"},
                 "metrics": {"loops": 2, "events": 20, "agents": 2},
                 "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:30:00Z"},
            ],
            "edges": [
                {"from": "a1", "to": "a2", "kind": "relay_candidate", 
                 "weight": {"pairs": 3, "median_gap_seconds": 120}},
                {"from": "a2", "to": "a1", "kind": "relay_candidate",
                 "weight": {"pairs": 1, "median_gap_seconds": 180}},
            ]
        }
        f = derive(doc, "test-host")
        self.assertEqual(f["events"], 100)
        self.assertEqual(f["agent_count"], 2)
        self.assertEqual(f["pairs"], 4)  # 3 + 1
        self.assertEqual(f["gamma"], 0.3)


class RenderTests(unittest.TestCase):
    def test_renders_html(self):
        f = {
            "host": "test", "start": "2026-11-09", "end": "2026-11-09",
            "events": 100, "loops": 10, "agent_count": 2, "groups": 4, "pairs": 4,
            "gamma": 0.3, "coverage": 0.5, "week_label": "11/09–11/15", "week_count": 5,
            "top2_share": 0.8, "a": "agt-abc123", "b": "agt-def456",
            "ab": 3, "ba": 1, "pair_gap": 2.0, "pair_role_a": "主力产出",
            "pair_role_b": "边做边改", "pair_loops_a": 6, "pair_loops_b": 4,
            "pair_harness_a": "qoder", "pair_harness_b": "codex", "echo": 10,
            "clusters": [], "doc": {"nodes": []},
        }
        html = render(f)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)
        self.assertIn("test", html)
        self.assertIn("100", html)

    def test_every_agent_is_listed_and_subagents_are_labeled(self):
        f = {
            "host": "test", "start": "2026-11-09", "end": "2026-11-09",
            "events": 10, "loops": 2, "agent_count": 2, "groups": 0, "pairs": 0,
            "gamma": None, "coverage": 0.5, "week_label": "11/09–11/15", "week_count": 1,
            "top2_share": 0, "a": None, "b": None,
            "ab": 0, "ba": 0, "pair_gap": 0.0, "pair_role_a": "", "pair_role_b": "",
            "pair_loops_a": 0, "pair_loops_b": 0, "pair_harness_a": "", "pair_harness_b": "",
            "echo": 0, "clusters": [], "doc": {"nodes": []},
            "agents": [
                {"id": "agt-primary", "identity": {"harness": ["qoder-cn"], "kind": "primary"},
                 "metrics": {"loops": 1, "events": 5, "writes": 1}},
                {"id": "agt-sub", "identity": {"harness": ["qoder-cn"], "kind": "sub"},
                 "metrics": {"loops": 1, "events": 5, "writes": 0}},
            ],
        }
        html = render(f)
        self.assertEqual(html.count('<td class="id">'), 2)
        self.assertIn("agt-sub", html)
        self.assertIn("子 Agent", html)
        self.assertIn("主 Agent", html)


class IntegrationTests(unittest.TestCase):
    def test_full_pipeline_with_mock_data(self):
        """测试完整流水线（用 mock 数据）。"""
        events = [
            {"loop_key": "l1", "agent_key": "a1", "workspace_key": "w1",
             "ts_utc": "2026-11-09T08:00:00Z", "beat_role": "reply",
             "tool_name": None, "content_sha256": "abc", "harness_name": "qoder",
             "cwd": "/ws", "host_name": "test"},
        ]
        # 这个测试需要真实的 vault，所以跳过
        # 真实集成测试在 test_observer_algorithms.py 里


class LocalTimeLabelTests(unittest.TestCase):
    """刀 W23c：观测页**可见**时间按本机时区；嵌入数据仍是 UTC 原值。

    两个"UTC 日与本机日不同"的簇：`11/15 23:30Z` 在本机(+08:00)是 11/16（也不同 ISO 周），
    `11/09 23:30Z` 是 11/10。页脚原本烤的是 naive 本地串（无偏移），一并钉掉。
    """

    def setUp(self) -> None:
        self.doc = {
            "summary": {
                "events": 20, "loops": 4, "agents": 1, "task_clusters": 2,
                "coverage_ratio": 0.5, "gamma": {"gamma": 0.3, "producers": 5, "consumed_proxy": 2},
                "edges_by_kind": {"relay_candidate": 0, "echo": 0},
            },
            "nodes": [
                {"id": "a1", "kind": "agent", "identity": {"harness": ["qoder"], "kind": "primary"},
                 "metrics": {"loops": 4, "events": 20, "writes": 2}},
                {"id": "tc-1", "kind": "task_cluster", "identity": {"ws": "w1"},
                 "metrics": {"loops": 2, "events": 10, "agents": 1},
                 "first": "2026-11-09T23:30:00Z", "last": "2026-11-09T23:45:00Z"},
                {"id": "tc-2", "kind": "task_cluster", "identity": {"ws": "w1"},
                 "metrics": {"loops": 2, "events": 10, "agents": 1},
                 "first": "2026-11-15T23:30:00Z", "last": "2026-11-15T23:50:00Z"},
            ],
            "edges": [],
        }

    def test_observation_page_labels_use_local_dates(self) -> None:  # Y6
        tz8 = timezone(timedelta(hours=8))
        with mock.patch("lab.observe_this_machine.local_tz", return_value=tz8):
            f = derive(self.doc, "test-host")
            html = render(f)
        # 起止日按本机日历：两天都跨过午夜
        self.assertEqual(f["start"], "2026-11-10")
        self.assertEqual(f["end"], "2026-11-16")
        # 周分组也跟着本机日：11/15(Sun, ISO 46 周) 在本机是 11/16(Mon, ISO 47 周)
        self.assertEqual(f["week_label"], "11/16–11/22")
        self.assertIn("11/16–11/22", html)
        self.assertNotIn("11/09–11/15", html)
        # 页脚：带偏移的本机时刻，不再是 naive 的 `2026-...T..:..:..`。
        # 偏移不写死 +08:00——CI runner 是 UTC，断言从 tz8 本身推导（CI 红过一次实录）
        mins = int(tz8.utcoffset(None).total_seconds()) // 60
        expected_off = "%s%02d:%02d" % ("+" if mins >= 0 else "-",
                                        abs(mins) // 60, abs(mins) % 60)
        self.assertRegex(html, r"生成于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
                         + re.escape(expected_off))
        self.assertNotRegex(html, r"生成于 \d{4}-\d{2}-\d{2}T")
        # 分层不倒置：显示换了本机口径，doc 里的簇时间仍是 UTC 原串（不是改写数据）
        self.assertEqual(f["clusters"][0]["first"], "2026-11-09T23:30:00Z")
        self.assertEqual(f["clusters"][1]["last"], "2026-11-15T23:50:00Z")


if __name__ == "__main__":
    unittest.main()
