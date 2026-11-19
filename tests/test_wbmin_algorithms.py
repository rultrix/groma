"""Tests for lab/clusters.py, lab/relay.py, lab/gamma.py."""

import statebox  # noqa: F401  -- must be first real import
import unittest
from datetime import datetime, timezone

from lab.clusters import build_loops, build_clusters
from lab.relay import build_relay_edges, build_echo_edges
from lab.gamma import compute_gamma


def make_event(loop_key, agent, ws, ts, beat_role="reply", tool_name=None, content_sha=None):
    """Helper: build a minimal event dict."""
    return {
        "loop_key": loop_key,
        "agent_key": agent,
        "workspace_key": ws,
        "harness_name": "qoder-cn",
        "ts_utc": ts,
        "beat_role": beat_role,
        "tool_name": tool_name,
        "model_name": None,
        "tokens_total": 0,
        "content_sha256": content_sha or "",
        "cwd": "/test/ws",
        "host_name": "test-host",
    }


class BuildLoopsTests(unittest.TestCase):
    def test_empty_events(self):
        loops = build_loops([])
        self.assertEqual(loops, [])

    def test_single_event_single_loop(self):
        events = [make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:00:00Z")]
        loops = build_loops(events)
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["agent"], "agt-1")
        self.assertEqual(loops[0]["ws"], "ws-1")
        self.assertEqual(loops[0]["events"], 1)

    def test_multiple_events_same_loop(self):
        events = [
            make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:00:00Z", beat_role="user"),
            make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:01:00Z", beat_role="reply"),
            make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:02:00Z", beat_role="tool", tool_name="Bash"),
        ]
        loops = build_loops(events)
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["events"], 3)
        self.assertEqual(loops[0]["roles"]["user"], 1)
        self.assertEqual(loops[0]["roles"]["reply"], 1)
        self.assertEqual(loops[0]["roles"]["tool"], 1)
        self.assertEqual(loops[0]["tools"]["Bash"], 1)

    def test_multiple_loops(self):
        events = [
            make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:00:00Z"),
            make_event("loop-2", "agt-2", "ws-1", "2026-11-09T09:00:00Z"),
        ]
        loops = build_loops(events)
        self.assertEqual(len(loops), 2)
        agents = {loop["agent"] for loop in loops}
        self.assertEqual(agents, {"agt-1", "agt-2"})

    def test_content_sha_counted(self):
        events = [
            make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:00:00Z", content_sha="abc123"),
            make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:01:00Z", content_sha="abc123"),
            make_event("loop-1", "agt-1", "ws-1", "2026-11-09T08:02:00Z", content_sha="def456"),
        ]
        loops = build_loops(events)
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0]["sha"]["abc123"], 2)
        self.assertEqual(loops[0]["sha"]["def456"], 1)


class BuildClustersTests(unittest.TestCase):
    def test_empty_loops(self):
        clusters = build_clusters([])
        self.assertEqual(clusters, [])

    def test_single_loop_single_cluster(self):
        loops = [{
            "id": "loop-1", "agent": "agt-1", "ws": "ws-1",
            "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z",
            "events": 10, "writes": 2, "harness": "qoder-cn",
        }]
        clusters = build_clusters(loops)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["ws"], "ws-1")
        self.assertEqual(clusters[0]["agents"], ["agt-1"])

    def test_loops_within_gap_same_cluster(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "ws": "ws-1",
             "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z",
             "events": 10, "writes": 2, "harness": "qoder-cn"},
            {"id": "loop-2", "agent": "agt-1", "ws": "ws-1",
             "first": "2026-11-09T08:20:00Z", "last": "2026-11-09T08:25:00Z",
             "events": 8, "writes": 1, "harness": "qoder-cn"},
        ]
        clusters = build_clusters(loops, gap=1800)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["loops"]), 2)

    def test_loops_beyond_gap_different_clusters(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "ws": "ws-1",
             "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z",
             "events": 10, "writes": 2, "harness": "qoder-cn"},
            {"id": "loop-2", "agent": "agt-1", "ws": "ws-1",
             "first": "2026-11-09T09:00:00Z", "last": "2026-11-09T09:05:00Z",
             "events": 8, "writes": 1, "harness": "qoder-cn"},
        ]
        clusters = build_clusters(loops, gap=1800)
        self.assertEqual(len(clusters), 2)

    def test_different_workspaces_different_clusters(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "ws": "ws-1",
             "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z",
             "events": 10, "writes": 2, "harness": "qoder-cn"},
            {"id": "loop-2", "agent": "agt-1", "ws": "ws-2",
             "first": "2026-11-09T08:10:00Z", "last": "2026-11-09T08:15:00Z",
             "events": 8, "writes": 1, "harness": "qoder-cn"},
        ]
        clusters = build_clusters(loops, gap=1800)
        self.assertEqual(len(clusters), 2)


class BuildRelayEdgesTests(unittest.TestCase):
    def test_no_relay_when_same_agent(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "ws": "ws-1", "cluster": "tc-1",
             "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z"},
            {"id": "loop-2", "agent": "agt-1", "ws": "ws-1", "cluster": "tc-1",
             "first": "2026-11-09T08:10:00Z", "last": "2026-11-09T08:15:00Z"},
        ]
        edges = build_relay_edges(loops, [], delta=900)
        self.assertEqual(len(edges), 0)

    def test_relay_when_different_agents_within_delta(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "ws": "ws-1", "cluster": "tc-1",
             "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z"},
            {"id": "loop-2", "agent": "agt-2", "ws": "ws-1", "cluster": "tc-1",
             "first": "2026-11-09T08:10:00Z", "last": "2026-11-09T08:15:00Z"},
        ]
        edges = build_relay_edges(loops, [], delta=900)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["from"], "agt-1")
        self.assertEqual(edges[0]["to"], "agt-2")
        self.assertEqual(edges[0]["kind"], "relay_candidate")

    def test_relay_when_beyond_delta_relaxed_constraint(self):
        """Delta constraint removed for observation; any gap >= 0 creates relay."""
        loops = [
            {"id": "loop-1", "agent": "agt-1", "ws": "ws-1", "cluster": "tc-1",
             "first": "2026-11-09T08:00:00Z", "last": "2026-11-09T08:05:00Z"},
            {"id": "loop-2", "agent": "agt-2", "ws": "ws-1", "cluster": "tc-1",
             "first": "2026-11-09T08:30:00Z", "last": "2026-11-09T08:35:00Z"},
        ]
        edges = build_relay_edges(loops, [], delta=900)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["from"], "agt-1")
        self.assertEqual(edges[0]["to"], "agt-2")


class BuildEchoEdgesTests(unittest.TestCase):
    def test_no_echo_when_unique_content(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "sha": {"abc": 1}},
            {"id": "loop-2", "agent": "agt-2", "sha": {"def": 1}},
        ]
        edges = build_echo_edges(loops)
        self.assertEqual(len(edges), 0)

    def test_echo_when_same_content_different_agents(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "sha": {"abc": 1}},
            {"id": "loop-2", "agent": "agt-2", "sha": {"abc": 1}},
        ]
        edges = build_echo_edges(loops)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["kind"], "echo")
        self.assertEqual(edges[0]["weight"]["repeated_units"], 1)

    def test_no_echo_when_same_agent(self):
        loops = [
            {"id": "loop-1", "agent": "agt-1", "sha": {"abc": 1}},
            {"id": "loop-2", "agent": "agt-1", "sha": {"abc": 1}},
        ]
        edges = build_echo_edges(loops)
        self.assertEqual(len(edges), 0)


class ComputeGammaTests(unittest.TestCase):
    def test_no_producers(self):
        loops = [{"id": "loop-1", "roles": {"user": 5}}]
        result = compute_gamma(loops, [])
        self.assertIsNone(result["gamma"])
        self.assertEqual(result["producers"], 0)

    def test_producers_but_no_relay(self):
        loops = [{"id": "loop-1", "roles": {"reply": 5}}]
        result = compute_gamma(loops, [])
        self.assertEqual(result["gamma"], 0.0)
        self.assertEqual(result["producers"], 1)
        self.assertEqual(result["consumed_proxy"], 0)

    def test_gamma_with_relay(self):
        loops = [
            {"id": "loop-1", "roles": {"reply": 5}},
            {"id": "loop-2", "roles": {"reply": 3}},
        ]
        relay_edges = [
            {"kind": "relay_candidate", "loop_pairs": [("loop-1", "loop-2", 60.0)]},
        ]
        result = compute_gamma(loops, relay_edges)
        self.assertEqual(result["producers"], 2)
        self.assertEqual(result["consumed_proxy"], 1)
        self.assertEqual(result["gamma"], 0.5)


if __name__ == "__main__":
    unittest.main()
