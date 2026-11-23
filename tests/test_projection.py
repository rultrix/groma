"""载荷投影：载荷 = f(受众)，现算不是存储。

同一份资产，三受众载荷不同；净增益折叠按阈值出现/不出现两例；
投影前后 asset.json 字节不变（四件套不受影响）；块面标记形状不变。
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import tempfile
import unittest
from pathlib import Path

from lab import surface
from lab.asset import build_asset, find_asset
from lab.collect import catchup
from lab.inject import inject_theme
from lab.sources import add_source
from lab.topics import create_topic, set_pipeline_roles
from lab.workspace import init_workspace


def unit() -> dict:
    return {"unit_id": "unit_projtest", "name": "投影主题", "workspace_ref": "ws-x"}


def rows() -> list[dict]:
    return [{"path": "/tmp/home", "name": "home", "is_home": True, "sessions": 1, "events": 3}]


def asset() -> dict:
    return {
        "asset_id": "asset-projtest", "title": "投影试跑",
        "generated_at": "2026-11-22T00:00:00Z", "digest": "digest-x.json",
        "totals": {"sessions": 4, "events": 9}, "notes": "",
        "sessions": [{"session_id": "s%d" % i, "source": "qoder",
                      "path": "projects/p%d.jsonl" % i} for i in range(4)],
    }


class ProjectionTests(unittest.TestCase):
    def test_the_same_asset_renders_differently_for_three_audiences(self) -> None:
        """同资产三受众：施工/验收/无角色三份载荷不同。"""
        common = dict(viewed_from=rows()[0]["path"])
        builder = surface.project_text_for(unit(), rows(), asset(), **common)
        builder_spec = surface.project_text_for(unit(), rows(), asset(), role="施工", **common)
        acceptance = surface.project_text_for(unit(), rows(), asset(), role="验收", **common)
        self.assertIn("「施工」视角", builder_spec)
        self.assertIn("「验收」视角", acceptance)
        self.assertNotIn("视角渲染", builder, "没给受众不许自己插视角句")
        self.assertNotEqual(builder_spec, acceptance)
        self.assertNotEqual(builder_spec, builder)

    def test_the_fold_sentence_applies_by_the_threshold_two_ways(self) -> None:
        """折叠句两例：≥3 条且 ≥50% 出现；3 条但 <50% 或 2 条全是你都不出现（阈值约定）。"""
        asset_data = asset()
        mine = {"s0", "s1", "s2"}
        text = surface.project_text_for(unit(), rows(), asset_data, rows()[0]["path"],
                                        viewer_session_ids=mine)
        self.assertIn("4 条里 3 条来自你——已折叠。", text)
        fewer = dict(asset_data, sessions=asset_data["sessions"][:2])
        text = surface.project_text_for(unit(), rows(), fewer, rows()[0]["path"],
                                        viewer_session_ids={"s0", "s1"})
        self.assertNotIn("已折叠", text, "条数不足 3 不折叠")
        minority = {"s0"}
        text = surface.project_text_for(unit(), rows(), asset_data, rows()[0]["path"],
                                        viewer_session_ids=minority)
        self.assertNotIn("已折叠", text, "占比不足 50% 不折叠")

    def test_projection_leaves_asset_json_bytes_untouched(self) -> None:
        """投影前后 asset.json 字节相等——四件套与发布物不受投影影响。"""
        tmp = tempfile.TemporaryDirectory(prefix="wb-proj-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        home.mkdir()
        init_workspace(home, name="投影主题", owner="tester")
        create_topic("投影主题")
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-10T09:00:00Z", "cwd": "%s"}\n' % home.as_posix(),
            encoding="utf-8", newline="\n")
        add_source(home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(home, force=True)
        built = build_asset(home, "投影试跑", [("qoder", "projects/s.jsonl")])
        asset_path = find_asset(home, built["asset_id"]) / "asset.json"
        before = asset_path.read_bytes()

        unit_id = built["unit_id"]
        set_pipeline_roles(unit_id, {"qoder-cn": "施工"})
        inject_theme(unit_id, built["asset_id"])
        after = asset_path.read_bytes()
        self.assertEqual(before, after, "投影动了 asset.json——四件套红线被破坏")
        payload = (home / ".groma" / "injections" / "payload.md").read_text(encoding="utf-8")
        self.assertIn("「施工」视角", payload, "载荷没按角色表投影")
        book = json.loads((home / ".groma" / "injections" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(book["source"]["asset_id"], built["asset_id"])

    def test_block_markers_keep_their_shape_under_projection(self) -> None:
        """块面 `<!-- groma:inject unit= asset= -->` 标记形状不变（只改块内文本）。"""
        import re

        tmp = tempfile.TemporaryDirectory(prefix="wb-proj-face-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        home.mkdir()
        init_workspace(home, name="投影主题2", owner="tester")
        create_topic("投影主题2")
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-10T09:00:00Z", "cwd": "%s"}\n' % home.as_posix(),
            encoding="utf-8", newline="\n")
        add_source(home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(home, force=True)
        from lab.inject import inject_unit

        built = build_asset(home, "投影试跑2", [("qoder", "projects/s.jsonl")])
        set_pipeline_roles(built["unit_id"], {"codex": "验收"})
        inject_unit(home, built["asset_id"], faces=["codex-agents"], allow_project_files=True)
        agents = (home / "AGENTS.md").read_text(encoding="utf-8")
        marker = re.search(r"<!--\s*groma:inject[^>]*-->", agents)
        self.assertIsNotNone(marker)
        self.assertIn("groma:inject unit=", marker.group(0))
        self.assertIn("asset=%s" % built["asset_id"], marker.group(0))
        self.assertIn("「验收」视角", agents, "块面载荷没按该面的 harness 投影")


if __name__ == "__main__":
    unittest.main()
