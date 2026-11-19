"""Sub-agent attribution from the path shape (docs/CONTEXT-VAULT.md §5, docs/DATA-MODEL.md §验证).

The fact being fixed: `<parent uuid>/subagents/agent-a<type>-<16hex>.jsonl` was read as
a primary session of the default agent, because only the Qoder *database* carries
`key_sub_agent_type` and these files are not rows in it.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab.agents import agent_for_session, default_agent, derived_sub_agent
from lab.digest import (
    digest_file,
    fill_sub_type,
    identity_fields,
    subagent_fields,
)

PARENT = "fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3"
QODER_SUB = "projects/d--workspaces-ctx-parser/%s/subagents/agent-ageneral-purpose-2a43b54a5a95461d.jsonl" % PARENT
CLAUDE_SUB = "projects/D--workspaces-ctx-parser/6fcdb175-1b2e-4205-933d-49e488014106/subagents/agent-a25596ad9c5c815bb.jsonl"
ORDINARY = "projects/d--workspaces-ctx-parser/%s.jsonl" % PARENT


class PathReading(unittest.TestCase):
    def test_qoder_names_the_type_and_the_parent(self) -> None:
        fields = subagent_fields("qoder-cn", QODER_SUB)
        self.assertTrue(fields["is_subagent"])
        self.assertEqual(fields["sub_type"], "general-purpose")
        self.assertEqual(fields["parent_session_key"], "qoder-cn:" + PARENT)

    def test_claude_has_no_type_to_invent(self) -> None:
        fields = subagent_fields("claude-code", CLAUDE_SUB)
        self.assertTrue(fields["is_subagent"])
        self.assertNotIn("sub_type", fields)
        self.assertEqual(fields["parent_session_key"], "claude-code:6fcdb175-1b2e-4205-933d-49e488014106")

    def test_an_ordinary_session_is_not_a_subagent(self) -> None:
        fields = subagent_fields("qoder-cn", ORDINARY)
        self.assertEqual(fields["is_subagent"], False)
        self.assertIsNone(fields["parent_session_key"])

    def test_a_parent_that_is_not_an_id_stays_unclaimed(self) -> None:
        # Do not promote some random directory to "the parent session" because it
        # happened to sit before `subagents` in the path.
        fields = subagent_fields("qoder-cn", "projects/weird/not-a-uuid/subagents/agent-a-x-2a43b54a5a95461d.jsonl")
        self.assertTrue(fields["is_subagent"])
        self.assertIsNone(fields["parent_session_key"])

    def test_the_row_carries_the_relation_without_anyone_parsing_paths(self) -> None:
        fields = identity_fields("qoder-cn", QODER_SUB, transcript=False)
        self.assertEqual(fields["parent_session_key"], "qoder-cn:" + PARENT)
        self.assertTrue(fields["is_subagent"])


class NamePriority(unittest.TestCase):
    def test_the_database_name_beats_the_file_name(self) -> None:
        detail = {"sub_type": "qoder-ctx-parser"}
        fill_sub_type(detail, "qoder-cn", QODER_SUB)
        self.assertEqual(detail["sub_type"], "qoder-ctx-parser")

    def test_the_file_name_fills_a_blank_only(self) -> None:
        detail = {"sub_type": None}
        fill_sub_type(detail, "qoder-cn", QODER_SUB)
        self.assertEqual(detail["sub_type"], "general-purpose")

    def test_an_ordinary_row_stays_blank(self) -> None:
        detail = {}
        fill_sub_type(detail, "qoder-cn", ORDINARY)
        self.assertEqual(detail.get("sub_type"), None)

    def test_claude_leaves_the_name_absent_rather_than_guessing(self) -> None:
        detail = {}
        fill_sub_type(detail, "claude-code", CLAUDE_SUB)
        self.assertNotIn("sub_type", detail)


class AgentIdentity(unittest.TestCase):
    def test_an_unregistered_type_is_still_a_sub_agent(self) -> None:
        agent = agent_for_session([], "qoder-cn", "general-purpose")
        self.assertEqual(agent["kind"], "sub")
        self.assertEqual(agent["name"], "general-purpose")
        self.assertEqual(agent["parent_agent_id"], default_agent("qoder-cn")["agent_id"])

    def test_a_registered_sub_agent_still_wins(self) -> None:
        registered = derived_sub_agent("qoder-cn", "qoder-ctx-parser")
        agent = agent_for_session([registered], "qoder-cn", "qoder-ctx-parser")
        self.assertIs(agent, registered)

    def test_no_type_still_means_the_default_agent(self) -> None:
        agent = agent_for_session([], "codex", None)
        self.assertEqual(agent["kind"], "primary")

    def test_the_derived_identity_is_stable_and_shared_across_workspaces(self) -> None:
        first = derived_sub_agent("qoder-cn", "general-purpose")
        second = derived_sub_agent("qoder-cn", "general-purpose")
        self.assertEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(first["agent_id"], agent_for_session([], "qoder-cn", "general-purpose")["agent_id"])
        self.assertIsNone(first["workspace_ref"])
        self.assertNotEqual(first["agent_id"], derived_sub_agent("qoder-cn", "research")["agent_id"])


class RowComposition(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name).resolve()
        self.addCleanup(self._dir.cleanup)

    def transcript(self, rel: str, rows: list[dict]) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def test_reading_a_subagent_file_says_so(self) -> None:
        rel = "%s/subagents/agent-ageneral-purpose-2a43b54a5a95461d.jsonl" % PARENT
        path = self.transcript(rel, [{"cwd": str(self.root), "timestamp": "2026-11-09T08:00:00Z"}])
        detail = digest_file(path)
        fill_sub_type(detail, "qoder-cn", rel)
        agent = agent_for_session([], "qoder-cn", detail.get("sub_type"))
        row = {"path": rel, **detail, "agent_kind": agent["kind"], **identity_fields("qoder-cn", rel, self.root)}
        self.assertEqual(row["agent_kind"], "sub")
        self.assertTrue(row["is_subagent"])
        self.assertEqual(row["parent_session_key"], "qoder-cn:" + PARENT)
        self.assertEqual(row["transcript_lines"], 1)

    def test_a_parent_session_file_is_not_marked_as_a_child(self) -> None:
        rel = "%s.jsonl" % PARENT
        path = self.transcript(rel, [{"cwd": str(self.root), "timestamp": "2026-11-09T08:00:00Z"}])
        detail = digest_file(path)
        fill_sub_type(detail, "qoder-cn", rel)
        row = {**detail, **identity_fields("qoder-cn", rel, self.root)}
        self.assertFalse(row["is_subagent"])
        self.assertIsNone(row["parent_session_key"])
        self.assertIsNone(detail["sub_type"])


class RealPipeline(unittest.TestCase):
    """The helpers above are worthless if nothing calls them.

    Both digest branches are driven here, because the graded falsification showed a
    suite of helper tests that stayed green with the call sites removed.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name).resolve()
        self.addCleanup(self._dir.cleanup)
        # init_workspace / register_workspace write into the machine registry.
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)
        (self.base / "state").mkdir()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.harness_root = self.base / "harness"
        self.harness_root.mkdir()
        self.row = {"cwd": str(self.workspace), "timestamp": "2026-11-09T08:00:00Z"}

    def write(self, rel: str) -> None:
        path = self.harness_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.row, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    def test_qoder_branch_files_the_nested_transcript_as_a_sub_agent(self) -> None:
        from lab.digest import _qoder_source_files

        self.write("projects/d--probe/%s.jsonl" % PARENT)
        self.write(QODER_NESTED)
        source = {"name": "qoder-projects", "harness": "qoder-cn", "path": str(self.harness_root)}
        files, _outside, _nocwd, _skipped, _cwds = _qoder_source_files(
            self.workspace, source, str(self.workspace), [], []
        )
        by_path = {item["path"]: item for item in files}
        nested = by_path[QODER_NESTED]
        self.assertEqual(nested["agent_kind"], "sub")
        self.assertEqual(nested["sub_type"], "general-purpose")
        self.assertEqual(nested["parent_session_key"], "qoder-cn:" + PARENT)
        self.assertTrue(nested["is_subagent"])
        # The parent session itself must stay a primary row: this is not a blanket
        # "everything under projects/ is a sub-agent".
        parent_row = by_path["projects/d--probe/%s.jsonl" % PARENT]
        self.assertEqual(parent_row["agent_kind"], "primary")
        self.assertFalse(parent_row["is_subagent"])

    def test_build_digest_carries_the_attribution_end_to_end(self) -> None:
        from lab.digest import build_digest
        from lab.sources import add_source
        from lab.state import register_workspace
        from lab.workspace import init_workspace

        init_workspace(self.workspace, name="归因")
        register_workspace(self.workspace, "归因")
        add_source(self.workspace, "claude-projects", str(self.harness_root), harness="claude",
                   include=["projects"])
        self.write(CLAUDE_NESTED)
        digest = build_digest(self.workspace)
        rows = digest["files"]
        self.assertEqual(len(rows), 1, "the fake source must be reached by the real scan")
        row = rows[0]
        self.assertTrue(row["is_subagent"])
        self.assertEqual(row["parent_session_key"], "claude:6fcdb175-1b2e-4205-933d-49e488014106")
        # Claude names no type in the file name, so nothing is invented and the row
        # stays with the default agent -- the remaining gap, pinned as behaviour.
        self.assertIsNone(row["sub_type"])
        self.assertEqual(row["agent_kind"], "primary")

    def test_generic_branch_fills_a_type_bearing_name(self) -> None:
        """The non-qoder-cn branch must attribute too, or half the sources keep the bug."""
        from lab.digest import build_digest
        from lab.sources import add_source
        from lab.state import register_workspace
        from lab.workspace import init_workspace

        init_workspace(self.workspace, name="通用分支")
        register_workspace(self.workspace, "通用分支")
        add_source(self.workspace, "probe-projects", str(self.harness_root), harness="probe-cli",
                   include=["projects"])
        self.write(QODER_NESTED)
        row = build_digest(self.workspace)["files"][0]
        self.assertEqual(row["sub_type"], "general-purpose")
        self.assertEqual(row["agent_kind"], "sub")
        self.assertEqual(row["parent_session_key"], "probe-cli:" + PARENT)
        self.assertTrue(row["is_subagent"])

    def test_the_two_counts_this_change_must_not_touch(self) -> None:
        from lab.digest import build_digest
        from lab.sources import add_source
        from lab.state import register_workspace
        from lab.workspace import init_workspace

        init_workspace(self.workspace, name="计数")
        register_workspace(self.workspace, "计数")
        add_source(self.workspace, "claude-projects", str(self.harness_root), harness="claude",
                   include=["projects"])
        self.write(CLAUDE_NESTED)
        self.write("projects/d--probe/%s.jsonl" % PARENT)
        digest = build_digest(self.workspace)
        self.assertEqual(digest["file_count"], 2)
        self.assertEqual(digest["event_count"], 2)
        # Conservation, not a shape: whatever the attribution does, the rows and the
        # events must still add up under the agent list.
        self.assertEqual(sum(a["sessions"] for a in digest["agents"]), 2)
        self.assertEqual(sum(a["events"] for a in digest["agents"]), digest["event_count"])


QODER_NESTED = "projects/d--probe/%s/subagents/agent-ageneral-purpose-2a43b54a5a95461d.jsonl" % PARENT
CLAUDE_NESTED = "projects/D--probe/6fcdb175-1b2e-4205-933d-49e488014106/subagents/agent-a25596ad9c5c815bb.jsonl"


if __name__ == "__main__":
    unittest.main()
