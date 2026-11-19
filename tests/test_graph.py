"""S2 graph model and SVG rendering, no browser needed."""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lab.collect import catchup
from lab.graph import build_graph, render_svg
from lab.sources import add_source
from lab.workspace import init_workspace


class GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="graph-unit")
        self.src = Path(self._tmp.name) / "src"
        (self.src / "projects").mkdir(parents=True)
        cwd = self.root.as_posix()
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.src / "projects" / "recent.jsonl").write_text(
            '{"timestamp": "%s", "cwd": "%s", "type": "user"}\n' % (recent, cwd),
            encoding="utf-8",
            newline="\n",
        )
        (self.src / "projects" / "old.jsonl").write_text(
            '{"timestamp": "%s", "cwd": "%s", "type": "user"}\n' % (old, cwd),
            encoding="utf-8",
            newline="\n",
        )
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)

    def test_three_layers_and_edges(self) -> None:
        graph = build_graph(self.root)
        self.assertEqual(graph["session_count"], 2)
        self.assertEqual(len(graph["agents"]), 1)
        self.assertEqual([node["id"] for node in graph["nodes"]][0], "unit")
        self.assertEqual(len(graph["edges"]), 3)  # unit->harness, harness->2 sessions

    def test_recency_colours_differ(self) -> None:
        graph = build_graph(self.root)
        colours = {node["path"]: node["color"] for node in graph["sessions"]}
        self.assertNotEqual(colours["projects/recent.jsonl"], colours["projects/old.jsonl"])

    def test_svg_has_nodes_edges_and_links(self) -> None:
        svg = render_svg(build_graph(self.root))
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn('id="unit"', svg)
        self.assertIn('data-kind="agent"', svg)
        self.assertIn('id="s-0"', svg)
        self.assertIn("<path", svg)
        self.assertIn("/session?source=src", svg)

    def test_empty_unit_still_renders(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            root = Path(other) / "unit"
            root.mkdir()
            init_workspace(root, name="empty")
            graph = build_graph(root)
            self.assertEqual(graph["session_count"], 0)
            self.assertIn("<svg", render_svg(graph))


if __name__ == "__main__":
    unittest.main()
