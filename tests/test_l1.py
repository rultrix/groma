"""L1 acceptance tests: unit creation, agent registry, byte-exact receipts."""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest import mock

from lab import __version__, schedule as schedule_mod
from lab import hooks
from lab import surface
from lab import trigger as trigger_mod
from lab import unitlink
from lab import unitmatch
from lab import state
from lab.cli import main as cli_main
from lab.agents import build_agents
from lab.asset import build_asset, find_asset, list_assets
from lab.graph import build_graph
from lab.inject import export_context, inject_status, inject_unit, refresh, uninject
from lab.control import pause
from lab.settings import collect_interval, load_settings, save_settings
from lab.collect import catchup, catchup_all
from lab.coverage import build_coverage
from lab.digest import build_digest, save_digest
from lab.receipt import build_receipt, save_receipt, verify_receipt
from lab.scan import build_scan, save_scan, scan_source
from lab.sources import add_source, load_sources, remove_source
from lab.state import find_unit, list_workspaces, register_workspace, remove_workspace, workspace_ref
from lab.state import load_agents as load_global_agents
from lab.summary import build_summary, render_text
from lab.workspace import LabError, attach_agent, ensure_unit, init_workspace, load_agents, load_unit

REPO_ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_version_file_matches_package_version(self) -> None:
        self.assertEqual((REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__)

    def test_cli_reports_version(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as exit_error:
                cli_main(["--version"])
        self.assertEqual(exit_error.exception.code, 0)
        self.assertIn(__version__, buffer.getvalue())


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_init_creates_unit(self) -> None:
        unit = init_workspace(self.root, name="demo", owner="tester")
        self.assertTrue(unit["unit_id"].startswith("unit_"))
        self.assertEqual(unit["name"], "demo")
        self.assertEqual(unit["owner"], "tester")
        self.assertEqual(unit["carrier"], "local-directory")
        self.assertTrue((self.root / ".groma" / "unit.json").is_file())
        self.assertTrue((self.root / ".groma" / "receipts").is_dir())

    def test_init_twice_is_rejected(self) -> None:
        init_workspace(self.root)
        with self.assertRaises(LabError):
            init_workspace(self.root)

    def test_attach_agent_is_idempotent_by_name(self) -> None:
        init_workspace(self.root)
        record = attach_agent(self.root, "qoder", harness="qoder")
        self.assertEqual(record["harness"], "qoder")
        self.assertEqual(len(load_agents(self.root)), 1)
        with self.assertRaises(LabError):
            attach_agent(self.root, "qoder", harness="qoder")

    def test_unit_survives_reload(self) -> None:
        created = init_workspace(self.root, name="reload")
        self.assertEqual(load_unit(self.root)["unit_id"], created["unit_id"])

    def test_ensure_unit_creates_then_reuses(self) -> None:
        # Picking a workspace in the installer is the same act as creating its unit.
        created = ensure_unit(self.root, name="fresh")
        self.assertTrue((self.root / ".groma" / "unit.json").is_file())
        self.assertEqual(ensure_unit(self.root)["unit_id"], created["unit_id"])


class UnitIdentityTests(unittest.TestCase):
    """A Unit must be findable by id without leaking or depending on a local path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.root = self.base / "space"
        self.root.mkdir()
        self.unit = init_workspace(self.root, name="space")

    def test_identity_file_matches_unit(self) -> None:
        link = unitlink.read_link(self.root)
        self.assertIsNotNone(link)
        self.assertEqual(link["unit_id"], self.unit["unit_id"])
        self.assertEqual(load_unit(self.root)["unit_id"], self.unit["unit_id"])
        self.assertTrue((self.root / "groma-unit.json").is_file())

    def test_clone_keeps_unit_id_and_resolves_both_paths(self) -> None:
        clone = self.base / "clone"
        clone.mkdir()
        shutil.copy2(self.root / "groma-unit.json", clone / "groma-unit.json")
        cloned = ensure_unit(clone)
        self.assertEqual(cloned["unit_id"], self.unit["unit_id"])
        register_workspace(self.root, "space")
        register_workspace(clone, "clone")
        records = find_unit(self.unit["unit_id"])
        self.assertEqual({item["path"] for item in records}, {self.root.as_posix(), clone.as_posix()})

    def test_org_path_is_normalised_and_resolved(self) -> None:
        link = unitlink.set_org_path(self.root, "git@github.com:example/upstream-harness.git")
        self.assertEqual(link["org_path"], "github.com/example/upstream-harness")
        self.assertEqual(load_unit(self.root)["org_path"], "github.com/example/upstream-harness")
        register_workspace(self.root, "space")
        records = find_unit("github.com/example/upstream-harness")
        self.assertEqual([item["unit_id"] for item in records], [self.unit["unit_id"]])

    def test_cli_unit_show_reports_local_and_org_paths(self) -> None:
        unitlink.set_org_path(self.root, "https://github.com/example/upstream-harness.git")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(self.root), "unit", "show"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["unit_id"], self.unit["unit_id"])
        self.assertEqual(payload["org_path"], "github.com/example/upstream-harness")
        self.assertIn(self.root.as_posix(), payload["local_paths"])

    def test_cli_unit_show_by_id_is_path_free(self) -> None:
        register_workspace(self.root, "space")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(self.base), "unit", "show", self.unit["unit_id"]])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["local_paths"], [self.root.as_posix()])


class UnitMembershipTests(unittest.TestCase):
    """Scattered workspaces of the same repo can be confirmed into one Unit (D1-D8)."""

    REMOTE = "https://github.com/projX/thing.git"

    def setUp(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git not available")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.base = base
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.primary = self._repo(base / "primary")
        init_workspace(self.primary, name="thing")  # ensure_link derives org_path from git origin
        self.worktree = self._repo(base / "worktree")
        self.scratch = base / "random-notes"
        self.scratch.mkdir()
        (self.scratch / "x.md").write_text("scratch", encoding="utf-8")
        for extra in (self.primary, self.worktree, self.scratch):
            register_workspace(extra, extra.name)  # the app registers every folder it opens

    def _repo(self, path: Path) -> Path:
        path.mkdir()
        args = ["-c", "user.email=a@b", "-c", "user.name=t", "-C", str(path)]
        subprocess.run(["git", *args, "init"], check=True, capture_output=True)
        subprocess.run(["git", *args, "remote", "add", "origin", self.REMOTE], check=True, capture_output=True)
        (path / "f.txt").write_text("1", encoding="utf-8")
        subprocess.run(["git", *args, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", *args, "commit", "-m", "x"], check=True, capture_output=True)
        return path

    def _candidate(self, result, path):
        binding = unitmatch.binding_id_for(path)
        return next((c for c in result["candidates"] if c["binding_id"] == binding), None)

    def test_detect_high_confidence_for_same_remote_and_branch(self) -> None:
        result = unitmatch.detect(self.primary)
        found = self._candidate(result, self.worktree)
        self.assertIsNotNone(found)
        self.assertEqual(found["signal"], "remote+branch")
        self.assertGreaterEqual(found["confidence"], 0.9)
        self.assertEqual(found["suggest"], "join_needs_confirm")
        self.assertFalse(found["is_member"])  # detected, not silently joined

    def test_detect_ignores_unrelated_scratch(self) -> None:
        result = unitmatch.detect(self.primary)
        self.assertIsNone(self._candidate(result, self.scratch))

    def test_join_writes_membership_without_touching_workspace(self) -> None:
        membership = unitmatch.join(self.primary, None, self.worktree)
        self.assertEqual(membership["role"], "evidence")
        self.assertEqual(membership["signal"], "remote+branch")
        self.assertTrue(state.list_memberships(membership["unit_id"]))
        self.assertFalse((self.worktree / ".groma").exists())  # join records state only, no project files
        self.assertFalse((self.worktree / "groma-unit.json").exists())

    def test_leave_is_reversible(self) -> None:
        membership = unitmatch.join(self.primary, None, self.worktree)
        self.assertTrue(unitmatch.leave(self.primary, self.worktree))
        self.assertEqual(state.list_memberships(membership["unit_id"]), [])
        self.assertTrue(self.worktree.is_dir())  # files remain

    def test_manual_merge_without_git_or_org_path(self) -> None:
        # The reported blocker: two plain local folders, no git, no org_path.
        home = self.base / "non GIT"; home.mkdir()
        init_workspace(home, name="no-remote")  # org_path stays None (no git origin)
        sibling = self.base / "sibling notes"; sibling.mkdir()
        (sibling / "a.md").write_text("x", encoding="utf-8")
        self.assertIsNone(load_unit(home).get("org_path"))
        result = unitmatch.detect(home)  # must not raise just because there is no remote
        self.assertFalse(result["has_org_path"])
        membership = unitmatch.join(home, None, sibling, role="evidence")  # explicit human merge
        self.assertEqual(membership["signal"], "manual")
        self.assertEqual(membership["confidence"], 1.0)
        self.assertFalse((sibling / ".groma").exists())  # still touches no project files
        self.assertEqual(len(unitmatch.unit_workspaces(home)), 2)

    def test_many_to_many_same_binding_different_units(self) -> None:
        binding = unitmatch.binding_id_for(self.worktree)
        state.add_membership("unit_alpha", binding, role="evidence", signal="manual")
        state.add_membership("unit_beta", binding, role="evidence", signal="manual")
        self.assertEqual(len(state.list_memberships("unit_alpha")), 1)
        self.assertEqual(len(state.list_memberships("unit_beta")), 1)

    def test_cli_detect_and_join_roundtrip(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cli_main(["--root", str(self.primary), "unit", "detect"]), 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn(unitmatch.binding_id_for(self.worktree), [c["binding_id"] for c in payload["candidates"]])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(self.primary), "unit", "join", "--path", str(self.worktree)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["signal"], "remote+branch")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(["--root", str(self.primary), "unit", "show"])
        self.assertTrue(json.loads(buffer.getvalue())["members"])

    def test_cli_create_home_and_list_operate_on_the_unit(self) -> None:
        lone = self.base / "cli-lone"
        lone.mkdir()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(self.primary), "unit", "create", "--name", "另一件事", "--home", str(lone)])
        self.assertEqual(code, 0)
        created = json.loads(buffer.getvalue())
        self.assertEqual(created["name"], "另一件事")
        self.assertTrue((lone / "groma-unit.json").is_file())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(["--root", str(self.primary), "unit", "list"])
        listed = json.loads(buffer.getvalue())["units"]
        self.assertIn(created["unit_id"], [item["unit_id"] for item in listed])
        # moving a Unit's home is a Unit operation, not a folder switch
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(self.primary), "unit", "home", "--path", str(self.worktree)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["home_binding"], unitmatch.binding_id_for(self.worktree))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(["--root", str(self.worktree), "unit", "rename", "--name", "换了名字"])
        self.assertEqual(state.unit_record(load_unit(self.primary)["unit_id"])["name"], "换了名字")

    def test_sync_keeps_the_claim_but_does_not_import_the_card_name(self) -> None:
        """退役复活语义（原为「同远端卡直接折成带卡名/org 的行」）。

        卡只证工作区身份——sync 折出的行用注册表行的名字，
        不再抄卡上的 `name`/`org_path`；同远端 clone 的**身份认领**不受影响。
        """
        clone = self._repo(self.base / "clone")
        init_workspace(clone, name="thing")  # same remote, so the same Unit is claimed
        unitmatch.join(self.primary, None, self.worktree)
        unitmatch.sync_units()
        unit_id = load_unit(self.primary)["unit_id"]
        record = state.unit_record(unit_id)
        self.assertIsNotNone(record)
        self.assertNotEqual(record["name"], "thing", "卡上的名字不该被抄进折出来的行（卡只证身份）")
        self.assertIn(unit_id, [item["unit_id"] for item in unitmatch.unit_list()])
        # the Unit's home is a real property now, not "wherever the mouse is"
        self.assertEqual(record["home_binding"], unitmatch.binding_id_for(self.primary))
        self.assertFalse((clone / ".groma" / "digests").exists())

    def test_sync_units_is_idempotent_and_does_not_resurrect_a_left_folder(self) -> None:
        unitmatch.join(self.primary, None, self.worktree)
        unitmatch.sync_units()
        unitmatch.leave(self.primary, self.worktree)
        unitmatch.sync_units()
        self.assertEqual(state.list_memberships(load_unit(self.primary)["unit_id"]), [])

    def test_set_home_moves_the_unit_and_keeps_the_folder_it_leaves(self) -> None:
        unit_id = load_unit(self.primary)["unit_id"]
        unitmatch.sync_units()
        result = unitmatch.set_home(unit_id, self.worktree)
        self.assertEqual(result["home_binding"], unitmatch.binding_id_for(self.worktree))
        paths = unitmatch.workspaces_for_unit(unit_id)
        self.assertEqual(paths[0], self.worktree.as_posix())
        self.assertIn(self.primary.as_posix(), paths)  # the old home stays a member
        self.assertEqual(unitlink.read_link(self.worktree)["unit_id"], unit_id)

    def test_rename_applies_to_members_not_to_a_stranger_card(self) -> None:
        """退役复活语义（原为「改名外溢到每张同卡并折它进成员」）。

        卡不再折成员——陌生 clone 挂着同身份的卡也只是身份相同，
        改名只写**成员**的卡；身份认领本身不受影响。
        """
        unit_id = load_unit(self.primary)["unit_id"]
        clone = self._repo(self.base / "clone")
        # a second clone of the same repo carries the same committed card
        unitlink.adopt_link(clone, unitmatch.identity_of(unit_id))
        init_workspace(clone, name="thing")
        init_workspace(self.worktree, name="worktree")
        unitmatch.join(self.primary, None, self.worktree)
        unitmatch.sync_units()
        self.assertEqual(len(unitmatch.workspaces_for_unit(unit_id)), 2,
                         "陌生卡把自己折成了成员（卡只证身份）")
        unitmatch.rename_unit(unit_id, "ACPE 采集改造")
        self.assertEqual(state.unit_record(unit_id)["name"], "ACPE 采集改造")
        self.assertEqual(unitlink.read_link(self.primary)["name"], "ACPE 采集改造")
        # 成员带的是关系不是卡：worktree 的卡不跟改名（_refresh_cards 走注册表指针，不走成员关系）
        self.assertEqual(unitlink.read_link(self.worktree)["name"], "worktree")
        # 同仓 clone 的注册表行仍指向这条 Unit（登记时抄的卡），改名照旧跟到它——
        # 改的不是改名半径，是"卡能不能折出成员/主题行"
        self.assertEqual(unitlink.read_link(clone)["name"], "ACPE 采集改造")
        self.assertEqual(load_unit(clone)["name"], "ACPE 采集改造")

    def test_create_unit_refuses_a_folder_that_is_still_a_home(self) -> None:
        unit_id = load_unit(self.primary)["unit_id"]
        unitmatch.sync_units()
        unitmatch.join(self.primary, None, self.worktree)
        with self.assertRaises(LabError) as error:
            unitmatch.create_unit("抢家", self.primary)
        self.assertIn("另一个 Unit", str(error.exception))
        self.assertEqual(state.unit_record(unit_id)["home_binding"], unitmatch.binding_id_for(self.primary))

    def test_creating_a_unit_names_a_plain_folder_without_a_second_id(self) -> None:
        lone = self.base / "lone-folder"
        lone.mkdir()
        record = unitmatch.create_unit("一件事", lone)
        self.assertEqual(record["name"], "一件事")
        self.assertTrue((lone / ".groma" / "unit.json").is_file())
        self.assertEqual(unitlink.read_link(lone)["unit_id"], record["unit_id"])
        self.assertEqual(unitmatch.workspaces_for_unit(record["unit_id"]), [lone.as_posix()])
        again = unitmatch.create_unit("改名一下", lone)  # alone in its Unit: rename, not a new id
        self.assertEqual(again["unit_id"], record["unit_id"])
        self.assertEqual(again["name"], "改名一下")


class UnitScopedEvidenceTests(unittest.TestCase):
    """关系图 / 活动 / 资产 read the union of a Unit's workspaces (D9)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="两件事")
        register_workspace(self.home, "home")
        self.member = base / "member"
        self.member.mkdir()
        unitmatch.join(self.home, None, self.member, role="evidence")
        self.yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._digest(self.home, [("qoder", "projects/home.jsonl", self.yesterday)])
        self._digest(self.member, [("codex", "sessions/member.jsonl", self.yesterday)])

    def _digest(self, root: Path, rows: list[tuple[str, str, str]]) -> None:
        """A hand-made digest: the collector is tested elsewhere, shape is what matters."""
        files = [
            {
                "source": source,
                "path": path,
                "bytes": 120,
                "lines": 3,
                "events": 3,
                "transcript": True,
                "transcript_lines": 3,
                "first_ts": ts,
                "last_ts": ts,
                "hours": [ts[:13]],
                "keys": ["cwd", "timestamp"],
                "parse_errors": 0,
                "cwd": root.as_posix(),
                "sub_type": None,
                "agent_id": "agt-" + source,
                "agent_name": "Default Agent",
                "agent_kind": "primary",
                "parent_agent_id": None,
                "harness": source,
            }
            for source, path, ts in rows
        ]
        save_digest(
            root,
            {
                "schema": 1,
                "kind": "GROMA_LOCAL_DIGEST",
                "unit_id": load_unit(self.home)["unit_id"],
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "content_stored": False,
                "workspace_root": root.as_posix(),
                "include": [],
                "file_count": len(files),
                "event_count": sum(item["events"] for item in files),
                "parse_errors": 0,
                "skipped_non_session": 0,
                "unattributed_outside": 0,
                "unattributed_no_cwd": 0,
                "agents": [],
                "files": files,
            },
        )

    def test_graph_spans_the_unit(self) -> None:
        graph = build_graph(self.home)
        self.assertEqual(graph["session_count"], 2)
        self.assertEqual(graph["event_count"], 6)
        self.assertEqual(len(graph["workspaces"]), 2)

    def test_a_session_shared_by_two_workspaces_counts_once(self) -> None:
        self._digest(
            self.member,
            [("codex", "sessions/member.jsonl", self.yesterday), ("qoder", "projects/home.jsonl", self.yesterday)],
        )
        self.assertEqual(build_graph(self.home)["session_count"], 2)
        self.assertEqual(build_summary(self.home, days=7)["session_count"], 2)

    def test_activity_spans_the_unit(self) -> None:
        summary = build_summary(self.home, days=7)
        self.assertEqual(summary["session_count"], 2)
        self.assertEqual(summary["event_count"], 6)
        self.assertEqual(len(summary["digests"]), 2)
        self.assertEqual(build_coverage(self.home, days=7)["active_hours"], 1)

    def test_an_asset_can_cite_a_members_session(self) -> None:
        asset = build_asset(self.home, "跨工作区", [("codex", "sessions/member.jsonl")])
        self.assertEqual(asset["totals"]["sessions"], 1)
        self.assertEqual(asset["workspace_count"], 2)
        self.assertEqual([item["name"] for item in asset["digests"]], [asset["sessions"][0]["digest"]])
        readme = (self.home / ".groma" / "assets" / "current" / "README.md").read_text(encoding="utf-8")
        self.assertIn("覆盖工作区：2 个", readme)
        self.assertNotIn(self.member.as_posix(), readme)  # the join stays local, no paths leak
        context = Path(export_context(self.home, asset["asset_id"])["context"]).read_text(encoding="utf-8")
        self.assertIn("覆盖 2 个工作区", context)

    def test_same_second_digest_suffix_is_still_the_newest(self) -> None:
        # save_digest writes `digest-<stamp>-1.json` when two land in one second;
        # plain name order would pick the older one, so aggregation would stall.
        workspace = Path(self._tmp.name) / "suffix-case"
        folder = workspace / ".groma" / "digests"
        folder.mkdir(parents=True)
        for name, count in (("digest-20261104T103130Z.json", 1), ("digest-20261104T103130Z-1.json", 5)):
            (folder / name).write_text(
                json.dumps({"schema": 1, "created_at": name[7:-5], "file_count": count, "event_count": count}),
                encoding="utf-8",
                newline="\n",
            )
        self.assertEqual(unitmatch.newest(sorted(folder.glob("digest-*.json"))).name, "digest-20261104T103130Z-1.json")
        self.assertEqual(unitmatch._latest_counts(workspace)["sessions"], 5)

    def test_unit_rows_show_home_members_and_what_each_collects(self) -> None:
        unit_id = load_unit(self.home)["unit_id"]
        unitmatch.sync_units()
        asset = build_asset(self.home, "两处证据", [("qoder", "projects/home.jsonl")])
        rows = unitmatch.unit_rows(unit_id)
        self.assertEqual([row["name"] for row in rows], ["home", "member"])
        self.assertTrue(rows[0]["is_home"])
        self.assertFalse(rows[1]["is_home"])
        self.assertEqual(rows[0]["sessions"], 1)
        self.assertEqual(rows[1]["sessions"], 1)  # joined folders contribute their own evidence
        self.assertTrue(rows[0]["initialised"])
        self.assertFalse(rows[1]["initialised"])  # never opened: no .groma/unit.json, nothing declared
        self.assertEqual([item["asset_id"] for item in list_assets(self.home)], [asset["asset_id"]])
        self.assertEqual(unitmatch.asset_dirs_for([self.member.as_posix()]), [])  # the member holds none


class InjectionSurfaceTests(unittest.TestCase):
    """The marked-block writer: what we promise about other people's files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="两件事")
        self.member = base / "member"
        self.member.mkdir()
        unitmatch.join(self.home, None, self.member, role="evidence")
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-02T09:00:00Z", "cwd": "%s"}\n' % self.home.as_posix(),
            encoding="utf-8",
            newline="\n",
        )
        add_source(self.home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(self.home, force=True)
        self.asset = build_asset(self.home, "全景", [("qoder", "projects/s.jsonl")], note="先看 L3 采集")
        self.asset_id = self.asset["asset_id"]

    def _face_text(self, relative: str = "CLAUDE.md") -> str:
        return (self.home / relative).read_text(encoding="utf-8")

    def _seed(self, relative: str = "CLAUDE.md") -> str:
        original = "# 我的项目说明\n\n手写段落。\n\n最后一行。\n"
        (self.home / relative).write_text(original, encoding="utf-8", newline="\n")
        return original

    def test_apply_touches_only_our_block(self) -> None:
        original = self._seed()
        result = inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        text = self._face_text()
        self.assertTrue(result["surfaces"][0]["untouched_outside"])
        self.assertEqual(surface._outside(text).rstrip("\n") + "\n", original)
        self.assertIn("# 项目全景（Groma 生成", text)
        self.assertIn("member", text)  # the Unit's other workspace is visible in the map

    def test_second_apply_writes_nothing(self) -> None:
        self._seed()
        inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        before = (self.home / "CLAUDE.md").read_bytes()
        again = inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        self.assertFalse(again["surfaces"][0]["changed"])
        self.assertEqual((self.home / "CLAUDE.md").read_bytes(), before)

    def test_block_carries_no_machine_fingerprint(self) -> None:
        self._seed()
        inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        block = self._face_text()[surface._slice(self._face_text())[0] : surface._slice(self._face_text())[1]]
        self.assertNotIn("ws-", block)  # ws_ref stays out of a committed file
        self.assertNotIn(self.home.as_posix(), block)
        self.assertNotRegex(block, r"\d{4}-\d{2}-\d{2}T\d{2}")  # no write timestamps either

    def test_project_faces_are_refused_without_the_flag(self) -> None:
        self._seed()
        with self.assertRaises(LabError) as error:
            inject_unit(self.home, self.asset_id, faces=["claude-md"])
        self.assertIn("--allow-project-file", str(error.exception))
        self.assertEqual(self._face_text(), "# 我的项目说明\n\n手写段落。\n\n最后一行。\n")  # untouched

    def test_removal_restores_the_file_byte_for_byte(self) -> None:
        original = self._seed()
        inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        self.assertNotEqual(self._face_text(), original)
        uninject(self.home)
        self.assertEqual(self._face_text(), original)

    def test_removal_keeps_lines_the_user_added_afterwards(self) -> None:
        self._seed()
        inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        edited = self._face_text() + "\n我后来又写了一段。\n"
        (self.home / "CLAUDE.md").write_text(edited, encoding="utf-8", newline="\n")
        uninject(self.home)
        text = self._face_text()
        self.assertIn("我后来又写了一段", text)
        self.assertNotIn("groma:inject", text)

    def test_a_face_we_created_is_deleted_again(self) -> None:
        result = inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        face = self.home / "CLAUDE.md"
        self.assertTrue(result["surfaces"][0]["created"])
        self.assertTrue(face.is_file())
        outcome = uninject(self.home)
        self.assertFalse(face.exists())
        self.assertEqual(outcome["removed"][0]["mode"], "deleted")
        self.assertFalse(surface.payload_path(self.home).is_file())  # the hook's payload goes too

    def test_faces_follow_the_harnesses_that_ran_here(self) -> None:
        # Qoder is served by the hook, so it must not add a file to the project
        self.assertEqual(surface.surfaces_for({"qoder-cn"}), [])
        self.assertTrue(surface.uses_hook({"qoder-cn"}))
        self.assertEqual(surface.surfaces_for({"codex", "claude-code"}, True), ["claude-md", "codex-agents"])
        self.assertEqual(surface.surfaces_for(set()), [])
        result = inject_unit(self.home, self.asset_id)  # evidence says: qoder-cn only
        self.assertEqual(result["surfaces"], [])
        self.assertEqual(result["hook_serves"], ["qoder-cn"])
        self.assertTrue(result["payload"]["changed"])  # the payload result, not the manifest
        self.assertEqual(len(result["payload"]["sha256"]), 64)
        self.assertTrue(surface.payload_path(self.home).is_file())  # payload written anyway
        self.assertIn("项目全景", surface.payload_path(self.home).read_text(encoding="utf-8"))
        again = inject_unit(self.home, self.asset_id)
        self.assertFalse(again["payload"]["changed"])  # idempotent payload too

    def test_unknown_face_is_rejected(self) -> None:
        with self.assertRaises(LabError):
            inject_unit(self.home, self.asset_id, faces=["not-a-face"])

    def test_status_shows_hand_editing_as_drift(self) -> None:
        self._seed()
        inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        self.assertEqual(inject_status(self.home)["changed"], [])
        text = self._face_text().replace("先看 L3 采集", "被我改过了")
        (self.home / "CLAUDE.md").write_text(text, encoding="utf-8", newline="\n")
        self.assertEqual(inject_status(self.home)["changed"], ["CLAUDE.md"])

    def test_cli_inject_roundtrip(self) -> None:
        self._seed()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                ["--root", str(self.home), "inject", "apply", "--asset", self.asset_id,
                 "--surface", "claude-md", "--allow-project-file"]
            )
        self.assertEqual(code, 0)
        self.assertIn("项目全景", self._face_text())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(["--root", str(self.home), "inject", "status"])
        self.assertEqual(len(json.loads(buffer.getvalue())["surfaces"]), 1)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(["--root", str(self.home), "inject", "remove"])
        self.assertIn("restart", json.dumps(json.loads(buffer.getvalue())))


    def _backdate_asset(self, asset_id: str, stamp: str = "2020-01-01T00:00:00Z") -> None:
        folder = find_asset(self.home, asset_id)
        path = folder / "asset.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["generated_at"] = stamp
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")

    def _grow_sessions(self, count: int = 4) -> None:
        src = Path(next(s["path"] for s in load_sources(self.home) if s["name"] == "qoder")) / "projects"
        start = getattr(self, "_extra", 0)
        for offset in range(count):
            index = start + offset
            (src / ("extra-%d.jsonl" % index)).write_text(
                '{"timestamp": "2026-11-03T09:%02d:00Z", "cwd": "%s"}\n' % (index, self.home.as_posix()),
                encoding="utf-8",
                newline="\n",
            )
        self._extra = start + count

    def _digest_keys(self) -> list[tuple[str, str]]:
        paths = unitmatch.digest_paths_for([self.home.as_posix()])
        digest = json.loads(paths[-1].read_text(encoding="utf-8"))
        return sorted({(f["source"], f["path"]) for f in digest["files"]})

    def test_refresh_tracks_live_numbers_and_never_changes_the_source(self) -> None:
        self._grow_sessions()
        catchup(self.home, force=True)
        full = build_asset(self.home, "全覆盖", self._digest_keys())
        inject_unit(self.home, full["asset_id"])
        before = surface.payload_path(self.home).read_text(encoding="utf-8")
        self._grow_sessions(2)  # one more round of real work
        outcome = catchup(self.home, force=True)
        self.assertIn("injection", outcome)  # collection refreshed the payload on its own
        after = surface.payload_path(self.home).read_text(encoding="utf-8")
        self.assertNotEqual(before, after)
        self.assertIn("| home | 家 | 7 |", after)
        book = surface.read_manifest(self.home)
        self.assertEqual(book["source"]["asset_id"], full["asset_id"])  # refresh never changes the source
        self.assertNotEqual(book["gate"]["state"], "paused")  # 覆盖仍然完整，闸门不该误停

    def test_gate_pauses_injection_when_the_asset_covers_too_little(self) -> None:
        inject_unit(self.home, self.asset_id)
        self.assertTrue(surface.payload_path(self.home).is_file())
        self._grow_sessions()
        catchup(self.home, force=True)
        self._backdate_asset(self.asset_id)
        outcome = refresh(self.home)
        self.assertEqual(outcome["state"], "paused")
        self.assertIn("会话", outcome["reason"])
        self.assertFalse(surface.payload_path(self.home).is_file())  # 宁可不注入，也不发过期结论
        state = inject_status(self.home)
        self.assertFalse(state["payload_present"])
        self.assertEqual(state["gate"]["state"], "paused")

    def test_gate_stale_still_serves_when_coverage_is_still_wide(self) -> None:
        inject_unit(self.home, self.asset_id)
        self._backdate_asset(self.asset_id)
        outcome = refresh(self.home)
        self.assertEqual(outcome["state"], "stale")  # 资产旧但视野仍覆盖全局：继续投，只标注
        self.assertTrue(surface.payload_path(self.home).is_file())
        self.assertEqual(inject_status(self.home)["gate"]["state"], "stale")

    def test_refresh_is_quiet_until_a_source_is_chosen(self) -> None:
        self.assertEqual(refresh(self.home)["state"], "off")
        inject_unit(self.home, self.asset_id)
        uninject(self.home)
        self.assertEqual(refresh(self.home)["state"], "off")  # uninject forgets the source
        self.assertFalse(surface.payload_path(self.home).is_file())


    def test_digest_records_where_the_outside_sessions_came_from(self) -> None:
        # A 0-session workspace must be explainable without re-scanning on the page.
        src = Path(next(s["path"] for s in load_sources(self.home) if s["name"] == "qoder")) / "projects"
        (src / "elsewhere.jsonl").write_text(
            '{"timestamp": "2026-11-03T11:00:00Z", "cwd": "D:/workspaces/somewhere-else"}\n',
            encoding="utf-8",
            newline="\n",
        )
        catchup(self.home, force=True)
        row = next(item for item in unitmatch.unit_rows(load_unit(self.home)["unit_id"]) if item["is_home"])
        self.assertEqual(row["outside_cwds"][0]["name"], "somewhere-else")
        self.assertEqual(row["outside_cwds"][0]["sessions"], 1)


class ProjectionWordbookTests(unittest.TestCase):
    """装载面上那句主语：词与值必须同改，且不许出现工程口径的 `Unit`。

    钉在真写过注入块的树上（CLAUDE.md 与 payload 两处渲染态），不钉 `project_text()`
    的返回值——只钉函数返回，这一行漏多久都不会有人知道。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="两件事")
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-02T09:00:00Z", "cwd": "%s"}\n' % self.home.as_posix(),
            encoding="utf-8",
            newline="\n",
        )
        add_source(self.home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(self.home, force=True)
        self.asset_id = build_asset(self.home, "全景", [("qoder", "projects/s.jsonl")])["asset_id"]

    def _rendered(self) -> list[str]:
        """两处装载面渲染态：项目里的标记块 ＋ hook 读的 payload。"""
        inject_unit(self.home, self.asset_id, faces=["claude-md"], allow_project_files=True)
        face = (self.home / "CLAUDE.md").read_text(encoding="utf-8")
        payload = surface.payload_path(self.home).read_text(encoding="utf-8")
        return [face, payload]

    def test_no_projection_line_names_the_engineering_word(self) -> None:
        for index, text in enumerate(self._rendered()):
            with self.subTest(rendered=index):
                self.assertNotIn("Unit：", text, "装载面上还有裸 `Unit`")
                self.assertRegex(text, r"(协作主题|本机登记)：\S+ ｜ \d+ 个工作区")

    def test_the_word_and_its_value_come_from_the_same_ledger(self) -> None:
        # 无标签这一支：读 units 登记名，并显式说是"本机登记"，不冒充协作主题
        face = self._rendered()[0]
        self.assertIn("本机登记：两件事", face)
        self.assertNotIn("协作主题：", face)

        # 建了人建标签之后再改登记名：词换成协作主题，值必须跟着换成标签名
        from lab import topics

        unit_id = load_unit(self.home)["unit_id"]
        topics.create_topic("两件事", intent="测试用判据")
        unitmatch.rename_unit(unit_id, "改过的登记名")
        again = self._rendered()[0]
        self.assertIn("协作主题：两件事", again, "词改了值还从 units.json 取 ⇒ 两份账又错位")
        self.assertNotIn("本机登记：改过的登记名", again)


class InjectScopeFollowsTheThemeTests(unittest.TestCase):
    """注入的资产寻址与工作区表跟"正在操作的协作主题"走，不跟落点目录自己的卡片走。

    真机形状（Owner 0.55 实测 @12:2x）：成员区自己有一张身份卡时，主题资产的扇出
    与单投都撞 `asset not found`——因为解析作用域取自落点目录的卡。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.theme_home = base / "theme-home"
        self.theme_home.mkdir()
        self.member = base / "member"
        self.member.mkdir()
        init_workspace(self.theme_home, name="主题家")
        init_workspace(self.member, name="别人的区")  # 关键：成员自带一张卡
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        for folder, name in ((self.theme_home, "home.jsonl"), (self.member, "member.jsonl")):
            (src / "projects" / name).write_text(
                '{"timestamp": "2026-11-19T01:00:00Z", "cwd": "%s", "type": "user"}\n'
                % folder.as_posix(),
                encoding="utf-8", newline="\n",
            )
        add_source(self.theme_home, "qoder", str(src), "qoder-cn", ["projects"])
        add_source(self.member, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(self.theme_home, force=True)
        catchup(self.member, force=True)
        self.theme_id = load_unit(self.theme_home)["unit_id"]
        unitmatch.join_unit(self.theme_id, self.member, role="evidence")
        self.asset = build_asset(self.theme_home, "主题资产", [("qoder", "projects/home.jsonl")])
        self.asset_id = self.asset["asset_id"]

    def test_the_asset_of_a_theme_is_addressable_from_a_member_with_its_own_card(self) -> None:
        self.assertIsNotNone(find_asset(self.theme_home, self.asset_id, self.theme_id))
        self.assertIsNotNone(find_asset(self.member, self.asset_id, self.theme_id))

    def test_injecting_into_a_member_writes_the_themes_map(self) -> None:
        outcome = inject_unit(self.member, self.asset_id, unit_id=self.theme_id)
        self.assertEqual(outcome["asset_id"], self.asset_id)
        self.assertEqual(outcome["unit_id"], self.theme_id)
        source = surface.read_manifest(self.member)["source"]
        self.assertEqual((source["asset_id"], source["unit_id"]), (self.asset_id, self.theme_id))
        self.assertTrue((self.member / ".groma" / "injections" / "payload.md").is_file())
        body = (self.member / ".groma" / "injections" / "payload.md").read_text(encoding="utf-8")
        self.assertIn("主题家", body)  # 全景表是主题的地图，不是落点目录自己那张

    def test_refresh_after_a_theme_injection_does_not_pause_the_member(self) -> None:
        inject_unit(self.member, self.asset_id, unit_id=self.theme_id)
        outcome = refresh(self.member)
        self.assertNotEqual(outcome["state"], "paused", outcome.get("reason"))
        self.assertTrue((self.member / ".groma" / "injections" / "payload.md").is_file())


class QoderHookTests(unittest.TestCase):
    """The generated hook is driven with the host's own contract: stdin JSON in."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.root = base / "proj"
        self.root.mkdir()
        init_workspace(self.root, name="注入测试")
        self.payload = self.root / ".groma" / "injections" / "payload.md"
        self.settings = self.root / ".qoder" / "settings.local.json"

    def _foreign(self) -> str:
        text = json.dumps(
            {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "other.exe", "timeout": 5}]}]}},
            ensure_ascii=False,
        )
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(text, encoding="utf-8", newline="\n")
        return text

    def test_first_registration_refuses_without_confirmation(self) -> None:
        with self.assertRaises(LabError) as error:
            hooks.install(self.root)
        self.assertIn("确认", str(error.exception))
        self.assertFalse(hooks.hook_file(self.root).exists())
        self.assertFalse(self.settings.exists())

    def test_install_registers_both_events_with_the_documented_schema(self) -> None:
        result = hooks.install(self.root, confirmed=True)
        self.assertTrue(result["script_created"])
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        command = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(command.endswith("/.qoder/hooks/groma_inject.py"))
        self.assertTrue(command.split()[0] in ("py", "python3"))
        self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["timeout"], 10)
        self.assertEqual(sorted(result["events_present"]), ["SessionStart", "UserPromptSubmit"])
        again = hooks.install(self.root, confirmed=True)
        self.assertEqual(again["events_added"], [])  # idempotent

    def test_other_tools_hooks_survive(self) -> None:
        before = self._foreign()
        hooks.install(self.root, confirmed=True)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "other.exe")
        hooks.remove(self.root)
        after = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(after["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "other.exe")
        self.assertFalse(hooks.hook_file(self.root).exists())
        self.assertNotIn("SessionStart", after.get("hooks", {}))
        self.assertEqual(before.count("other.exe"), 1)

    def test_corrupt_settings_is_never_overwritten(self) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text("{ not json", encoding="utf-8", newline="\n")
        with self.assertRaises(LabError):
            hooks.install(self.root, confirmed=True)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ not json")
        self.assertFalse(hooks.hook_file(self.root).exists())

    def test_generated_hook_delivers_the_payload_and_keeps_our_ledger(self) -> None:
        hooks.install(self.root, confirmed=True)
        surface.write_payload(self.root, surface.block_of("unit_x", "asset-x", "项目全景 ZEBRA"))
        expected = hooks.payload_file(self.root).read_text(encoding="utf-8")
        done = hooks.run_hook(self.root, "SessionStart", "sess-A")
        self.assertEqual(done["returncode"], 0)
        self.assertIn("项目全景 ZEBRA", done["additionalContext"])
        self.assertEqual(done["additionalContext"].strip(), expected.strip())
        log = (self.root / ".groma" / "injections" / "receipts" / "inject.log").read_text(encoding="utf-8")
        self.assertIn("event=SessionStart session=sess-A action=inject", log)
        self.assertIn("sha256=", log)

    def test_user_prompt_submit_injects_once_per_session(self) -> None:
        hooks.install(self.root, confirmed=True)
        surface.write_payload(self.root, "背景内容")
        self.assertIsNotNone(hooks.run_hook(self.root, "UserPromptSubmit", "sess-B")["additionalContext"])
        self.assertIsNone(hooks.run_hook(self.root, "UserPromptSubmit", "sess-B")["additionalContext"])
        self.assertIsNotNone(hooks.run_hook(self.root, "UserPromptSubmit", "sess-C")["additionalContext"])

    def test_one_session_gets_the_background_once_across_events(self) -> None:
        # 真机实测的形状：SessionStart 带 source，UserPromptSubmit 不带，两者同秒触发
        hooks.install(self.root, confirmed=True)
        surface.write_payload(self.root, "背景内容")
        self.assertIsNotNone(
            hooks.run_hook(self.root, "SessionStart", "sess-G", source="startup")["additionalContext"]
        )
        self.assertIsNone(hooks.run_hook(self.root, "UserPromptSubmit", "sess-G")["additionalContext"])
        self.assertIsNone(
            hooks.run_hook(self.root, "SessionStart", "sess-G", source="startup")["additionalContext"]
        )
        log = (self.root / ".groma" / "injections" / "receipts" / "inject.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("session=sess-G action=inject"), 1)
        self.assertIn("reason=already-injected", log)
        # 上下文被重建（clear/compact）是新的装载时机
        self.assertIsNotNone(
            hooks.run_hook(self.root, "SessionStart", "sess-G", source="clear")["additionalContext"]
        )

    def test_hook_fails_open_and_never_breaks_a_session(self) -> None:
        import subprocess
        import sys as _sys

        hooks.install(self.root, confirmed=True)
        done = subprocess.run(
            [_sys.executable, str(hooks.hook_file(self.root))],
            input="not json at all",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "")
        # no payload file yet: skip, don't crash, don't inject
        empty = hooks.run_hook(self.root, "SessionStart", "sess-D")
        self.assertEqual(empty["returncode"], 0)
        self.assertIsNone(empty["additionalContext"])
        log = (self.root / ".groma" / "injections" / "receipts" / "inject.log").read_text(encoding="utf-8")
        self.assertIn("reason=no-context-file", log)
        unknown = hooks.run_hook(self.root, "PreToolUse", "sess-F")
        self.assertEqual(unknown["returncode"], 0)
        self.assertIsNone(unknown["additionalContext"])

    def test_status_reports_registration_and_receipts(self) -> None:
        hooks.install(self.root, confirmed=True)
        surface.write_payload(self.root, "背景内容")
        hooks.run_hook(self.root, "SessionStart", "sess-E")
        state = hooks.status(self.root)
        self.assertTrue(state["script"])
        self.assertEqual(state["registered"], ["SessionStart", "UserPromptSubmit"])
        self.assertTrue(state["payload"])
        self.assertIn("action=inject", " ".join(state["receipts"]))
        self.assertIn("重启", state["restart_hint"])

    def test_cli_hook_roundtrip(self) -> None:
        buffer = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            code = cli_main(["--root", str(self.root), "inject", "hook-install"])
        self.assertEqual(code, 2)  # EXIT_USAGE: it refuses to register without --confirm
        self.assertIn("确认", errors.getvalue())
        self.assertFalse(hooks.hook_file(self.root).is_file())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(self.root), "inject", "hook-install", "--confirm"])
        self.assertEqual(code, 0)
        self.assertTrue(hooks.hook_file(self.root).is_file())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(["--root", str(self.root), "inject", "hook-status"])
        self.assertEqual(json.loads(buffer.getvalue())["registered"], ["SessionStart", "UserPromptSubmit"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(["--root", str(self.root), "inject", "hook-remove"])
        self.assertTrue(json.loads(buffer.getvalue())["restart_required"])
        self.assertFalse(hooks.hook_file(self.root).is_file())


class ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        init_workspace(self.root, name="lab")
        (self.root / "notes.md").write_text("hello\n", encoding="utf-8", newline="\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "a.txt").write_text("alpha\n", encoding="utf-8", newline="\n")

    def _receipt(self) -> Path:
        return save_receipt(self.root, build_receipt(self.root, note="t"))

    def test_receipt_lists_only_unit_files(self) -> None:
        receipt = build_receipt(self.root)
        paths = [entry["path"] for entry in receipt["files"]]
        self.assertEqual(paths, ["notes.md", "sub/a.txt"])

    def test_verify_passes_on_clean_tree(self) -> None:
        result = verify_receipt(self.root, self._receipt())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["extra_files"], [])

    def test_verify_detects_same_size_edit(self) -> None:
        receipt_path = self._receipt()
        (self.root / "notes.md").write_text("HELLO\n", encoding="utf-8", newline="\n")
        result = verify_receipt(self.root, receipt_path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["problems"][0]["problem"], "sha256_changed")

    def test_verify_detects_missing_file(self) -> None:
        receipt_path = self._receipt()
        (self.root / "sub" / "a.txt").unlink()
        result = verify_receipt(self.root, receipt_path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["problems"][0]["problem"], "missing")

    def test_verify_detects_extra_file(self) -> None:
        receipt_path = self._receipt()
        (self.root / "extra.md").write_text("new\n", encoding="utf-8", newline="\n")
        result = verify_receipt(self.root, receipt_path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["extra_files"], ["extra.md"])

    def test_receipt_ignores_metadata_dir(self) -> None:
        receipt_path = self._receipt()
        paths = [entry["path"] for entry in json.loads(receipt_path.read_text(encoding="utf-8"))["files"]]
        self.assertFalse(any(path.startswith(".groma/") for path in paths))

    def test_verify_reports_pass_verdict(self) -> None:
        result = verify_receipt(self.root, self._receipt())
        self.assertEqual(result["verdict"], "PASS")

    def test_verify_reports_fail_verdict(self) -> None:
        receipt_path = self._receipt()
        (self.root / "notes.md").write_text("HELLO\n", encoding="utf-8", newline="\n")
        self.assertEqual(verify_receipt(self.root, receipt_path)["verdict"], "FAIL")


class EmptyUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        init_workspace(self.root, name="empty")

    def test_empty_receipt_is_refused(self) -> None:
        with self.assertRaises(LabError):
            build_receipt(self.root)

    def test_empty_receipt_allowed_on_purpose(self) -> None:
        receipt = build_receipt(self.root, allow_empty=True)
        self.assertEqual(receipt["file_count"], 0)

    def test_empty_receipt_is_not_proven(self) -> None:
        receipt_path = save_receipt(self.root, build_receipt(self.root, allow_empty=True))
        result = verify_receipt(self.root, receipt_path)
        self.assertEqual(result["verdict"], "NOT_PROVEN")
        self.assertFalse(result["ok"])

    def test_empty_receipt_with_later_file_stays_not_proven(self) -> None:
        # Documented precedence: no baseline means NOT_PROVEN, even when files appeared later.
        receipt_path = save_receipt(self.root, build_receipt(self.root, allow_empty=True))
        (self.root / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
        result = verify_receipt(self.root, receipt_path)
        self.assertEqual(result["verdict"], "NOT_PROVEN")
        self.assertEqual(result["extra_files"], ["new.txt"])


class CliExitCodeTests(unittest.TestCase):
    """Exit codes are the contract: 0 pass, 2 usage, 3 integrity, 4 not proven."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def run_cli(self, *argv: str) -> int:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            return cli_main(["--root", str(self.root), *argv])

    def _prepare(self) -> str:
        (self.root / "a.txt").write_text("alpha\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.run_cli("init", "--name", "cli"), 0)
        self.assertEqual(self.run_cli("receipt"), 0)
        receipts = sorted((self.root / ".groma" / "receipts").glob("*.json"))
        return ".groma/receipts/" + receipts[-1].name

    def test_clean_verify_is_zero(self) -> None:
        rel = self._prepare()
        self.assertEqual(self.run_cli("verify", rel), 0)

    def test_drift_is_three(self) -> None:
        rel = self._prepare()
        (self.root / "a.txt").write_text("ALPHA\n", encoding="utf-8", newline="\n")
        self.assertEqual(self.run_cli("verify", rel), 3)

    def test_missing_receipt_is_two(self) -> None:
        self._prepare()
        self.assertEqual(self.run_cli("verify", ".groma/receipts/nope.json"), 2)

    def test_empty_receipt_is_four(self) -> None:
        self.assertEqual(self.run_cli("init", "--name", "cli"), 0)
        self.assertEqual(self.run_cli("receipt"), 2)  # refused without --allow-empty
        self.assertEqual(self.run_cli("receipt", "--allow-empty"), 0)
        receipts = sorted((self.root / ".groma" / "receipts").glob("*.json"))
        rel = ".groma/receipts/" + receipts[-1].name
        self.assertEqual(self.run_cli("verify", rel), 4)

    def test_emit_survives_a_windowed_exe(self) -> None:
        with mock.patch("sys.stdout", None), mock.patch("sys.stderr", None):
            self.assertEqual(cli_main(["--root", str(self.root), "init", "--name", "win"]), 0)
            self.assertEqual(cli_main(["--root", str(self.root), "receipt"]), 2)

    def test_root_after_subcommand_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli_main(["init", "--root", other, "--name", "after"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(other) / ".groma" / "unit.json").is_file())

    def test_init_creates_a_missing_root(self) -> None:
        # `init` is the only command allowed to create --root (like `git init <dir>`).
        with tempfile.TemporaryDirectory() as parent:
            missing = Path(parent) / "brand-new"
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli_main(["--root", str(missing), "init", "--name", "new"])
            self.assertEqual(code, 0)
            self.assertTrue((missing / ".groma" / "unit.json").is_file())

    def test_non_init_command_rejects_a_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            missing = Path(parent) / "nope"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = cli_main(["--root", str(missing), "receipt"])
            self.assertEqual(code, 2)
            self.assertFalse(missing.exists())


class DeclaredSourceTests(unittest.TestCase):
    """L2 boundary: nothing is discovered, only declared. Nested roots are ambiguous."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="boundary")
        self.src = Path(self._tmp.name) / "src"
        (self.src / "sub").mkdir(parents=True)
        (self.src / "a.txt").write_text("alpha\n", encoding="utf-8", newline="\n")
        (self.src / "sub" / "b.txt").write_text("beta\n", encoding="utf-8", newline="\n")

    def test_scan_lists_only_declared_root(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("nope\n", encoding="utf-8", newline="\n")
        add_source(self.root, "src", str(self.src), harness="test")
        scan = build_scan(self.root)
        listed = sorted("%s:%s" % (entry["source"], entry["path"]) for entry in scan["files"])
        self.assertEqual(listed, ["src:a.txt", "src:sub/b.txt"])
        self.assertEqual(scan["file_count"], 2)

    def test_scan_never_opens_a_file(self) -> None:
        add_source(self.root, "src", str(self.src), harness="test")
        source = load_sources(self.root)[0]

        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("a metadata scan must never open a file")

        with mock.patch("builtins.open", boom), mock.patch("io.open", boom), mock.patch("pathlib.Path.open", boom):
            files, skipped = scan_source(source)
        self.assertEqual(len(files), 2)
        self.assertEqual(skipped, [])

    def test_scan_skips_excluded_dirs(self) -> None:
        (self.src / ".git").mkdir()
        (self.src / ".git" / "config").write_text("x\n", encoding="utf-8")
        add_source(self.root, "src", str(self.src))
        scan = build_scan(self.root)
        reasons = {entry["path"]: entry["reason"] for entry in scan["skipped"]}
        self.assertEqual(reasons.get(".git"), "excluded_dir")
        self.assertNotIn(".git/config", [entry["path"] for entry in scan["files"]])

    def test_scan_skips_symlink_entries(self) -> None:
        # Windows often forbids creating symlinks, so stub the directory entry:
        # the branch must be proven on every host, not skipped where it matters.
        class FakeEntry:
            name = "link.txt"
            path = str(self.src / "link.txt")

            def is_symlink(self) -> bool:
                return True

            def is_dir(self, follow_symlinks: bool = False) -> bool:
                raise AssertionError("a symlink must be skipped before any stat")

            def is_file(self, follow_symlinks: bool = False) -> bool:
                raise AssertionError("a symlink must be skipped before any stat")

            def stat(self, follow_symlinks: bool = False) -> object:
                raise AssertionError("a symlink must never be followed")

        with mock.patch("lab.scan.os.scandir", return_value=iter([FakeEntry()])):
            files, skipped = scan_source({"name": "src", "path": str(self.src)})
        self.assertEqual(files, [])
        self.assertEqual([entry["reason"] for entry in skipped], ["symlink"])

    def test_source_must_exist(self) -> None:
        with self.assertRaises(LabError):
            add_source(self.root, "ghost", str(Path(self._tmp.name) / "missing"))

    def test_duplicate_name_is_rejected(self) -> None:
        add_source(self.root, "src", str(self.src))
        with self.assertRaises(LabError):
            add_source(self.root, "src", str(self.src))

    def test_nested_roots_are_rejected(self) -> None:
        add_source(self.root, "outer", str(self.src))
        with self.assertRaises(LabError):
            add_source(self.root, "inner", str(self.src / "sub"))

    def test_remove_source_then_missing(self) -> None:
        add_source(self.root, "src", str(self.src))
        self.assertEqual(remove_source(self.root, "src")["remaining"], 0)
        self.assertEqual(load_sources(self.root), [])
        with self.assertRaises(LabError):
            remove_source(self.root, "src")

    def test_include_must_be_a_plain_subpath(self) -> None:
        for index, bad in enumerate(["/abs", "../up", "a:b"]):
            with self.assertRaises(LabError):
                add_source(self.root, "bad%d" % index, str(self.src), include=[bad])

    def test_scan_with_no_sources_is_empty_not_an_error(self) -> None:
        scan = build_scan(self.root)
        self.assertEqual(scan["file_count"], 0)
        self.assertEqual(scan["skipped_count"], 0)


class ScanCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        self.src = Path(self._tmp.name) / "src"
        self.src.mkdir()
        (self.src / "a.txt").write_text("alpha\n", encoding="utf-8", newline="\n")

    def run_cli(self, *argv: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli_main(["--root", str(self.root), *argv])

    def test_sources_add_then_scan_writes_a_manifest(self) -> None:
        self.assertEqual(self.run_cli("init", "--name", "scan-cli"), 0)
        self.assertEqual(self.run_cli("sources", "add", "src", "--path", str(self.src), "--harness", "test"), 0)
        self.assertEqual(self.run_cli("scan"), 0)
        manifests = sorted((self.root / ".groma" / "scans").glob("scan-*.json"))
        self.assertEqual(len(manifests), 1, manifests)
        payload = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "GROMA_LOCAL_SCAN")
        self.assertEqual(payload["file_count"], 1)
        self.assertEqual(payload["sources"][0]["name"], "src")

    def test_scan_before_sources_is_allowed(self) -> None:
        self.assertEqual(self.run_cli("init", "--name", "scan-cli"), 0)
        self.assertEqual(self.run_cli("scan"), 0)


class ScheduleTests(unittest.TestCase):
    """Install/status/remove. Every test runs against the mock backend on purpose:
    a test suite must never create or delete a real OS task."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        init_workspace(self.root, name="sched")
        patcher = mock.patch.dict(os.environ, {"GROMA_TEST_MOCK_SCHEDULER": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_install_writes_record_and_wrapper(self) -> None:
        result = schedule_mod.install(self.root, 5)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["period_minutes"], 5)
        record = json.loads((self.root / ".groma" / "scheduler.json").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "installed")
        self.assertEqual(record["backend"], "test-mock")
        wrapper = Path(record["wrapper"])
        self.assertTrue(wrapper.is_file())
        body = wrapper.read_text(encoding="utf-8")
        self.assertIn("scan", body)
        self.assertIn("digest", body)
        self.assertIn(str(self.root), body)

    def test_task_name_is_stable_and_unit_specific(self) -> None:
        installed = schedule_mod.install(self.root)
        self.assertTrue(installed["task_name"].startswith("GROMA-"))
        self.assertEqual(installed["task_name"], schedule_mod.status(self.root)["task_name"])

    def test_two_units_get_different_task_names(self) -> None:
        with tempfile.TemporaryDirectory() as other_dir:
            other = Path(other_dir)
            init_workspace(other, name="other")
            self.assertNotEqual(
                schedule_mod.install(self.root)["task_name"],
                schedule_mod.install(other)["task_name"],
            )

    def test_period_must_be_positive(self) -> None:
        with self.assertRaises(LabError):
            schedule_mod.install(self.root, 0)

    def test_status_is_absent_before_install(self) -> None:
        self.assertEqual(schedule_mod.status(self.root)["status"], "absent")

    def test_remove_then_status_is_absent(self) -> None:
        schedule_mod.install(self.root)
        self.assertEqual(schedule_mod.status(self.root)["status"], "present")
        self.assertEqual(schedule_mod.remove(self.root)["status"], "removed")
        self.assertEqual(schedule_mod.status(self.root)["status"], "absent")

    def test_remove_without_install_is_not_found(self) -> None:
        self.assertEqual(schedule_mod.remove(self.root)["status"], "not_found")

    def test_task_xml_is_interactive_and_catches_up(self) -> None:
        xml = schedule_mod.task_xml(Path("C:/x y/scan.cmd"), 60)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
        self.assertIn("<Interval>PT60M</Interval>", xml)
        self.assertIn("scan.cmd", xml)

    def test_last_result_meaning_explains_a_lock_refusal(self) -> None:
        meaning = schedule_mod._explain_result({"Last Result": "-2147020576"})
        self.assertIsNotNone(meaning)
        self.assertIn("locked", meaning)
        self.assertEqual(schedule_mod._explain_result({"Last Result": "0"}), "ok")
        self.assertIsNone(schedule_mod._explain_result({}))


class DigestTests(unittest.TestCase):
    """Reading is allowed; storing content is not. The digest keeps shape only."""

    SENTINEL = "SECRET_SENTINEL_ALPHA"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="digest")
        self.cwd = self.root.as_posix()
        self.src = Path(self._tmp.name) / "src"
        self._write("projects/s1.jsonl", [
            '{"timestamp": "2026-01-01T00:00:00Z", "cwd": "%s", "type": "user", "text": "%s"}' % (self.cwd, self.SENTINEL),
            '{"timestamp": "2026-01-01T00:01:00Z", "cwd": "%s", "type": "assistant", "text": "%s"}' % (self.cwd, self.SENTINEL),
            '{"timestamp": "2026-01-01T00:02:00Z", "cwd": "%s", "type": "user"}' % self.cwd,
        ])
        self._write("projects/s2.jsonl", [
            '{"created_at": "2026-01-02T00:00:00Z", "cwd": "%s", "type": "system"}' % self.cwd,
            "not json at all",
        ])
        self._write("plugins/p.jsonl", ['{"timestamp": "2026-01-03T00:00:00Z", "cwd": "%s", "type": "tool"}' % self.cwd])
        (self.src / "projects" / "notes.md").write_text("not a session\n", encoding="utf-8")
        add_source(self.root, "src", str(self.src), harness="test")

    def _write(self, rel: str, lines: list[str]) -> None:
        path = self.src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def test_counts_lines_and_timestamps(self) -> None:
        digest = build_digest(self.root, include=["projects", "plugins"])
        self.assertEqual(digest["file_count"], 3)
        self.assertEqual(digest["event_count"], 6)
        s1 = next(item for item in digest["files"] if item["path"] == "projects/s1.jsonl")
        self.assertEqual(s1["events"], 3)
        self.assertEqual(s1["first_ts"], "2026-01-01T00:00:00Z")
        self.assertEqual(s1["last_ts"], "2026-01-01T00:02:00Z")
        self.assertEqual(s1["keys"], ["cwd", "text", "timestamp", "type"])

    def test_session_outside_the_workspace_is_unattributed(self) -> None:
        self._write("projects/other.jsonl", [
            '{"timestamp": "2026-01-04T00:00:00Z", "cwd": "C:/somewhere/else", "type": "user"}'
        ])
        digest = build_digest(self.root, include=["projects"])
        self.assertEqual(digest["file_count"], 2)
        self.assertEqual(digest["unattributed_outside"], 1)

    def test_session_without_cwd_is_unattributed(self) -> None:
        self._write("projects/nocwd.jsonl", ['{"timestamp": "2026-01-05T00:00:00Z", "type": "user"}'])
        digest = build_digest(self.root, include=["projects"])
        self.assertEqual(digest["unattributed_no_cwd"], 1)
        self.assertNotIn("projects/nocwd.jsonl", [item["path"] for item in digest["files"]])

    def test_subdirectory_cwd_belongs_to_the_workspace(self) -> None:
        self._write("projects/sub.jsonl", [
            '{"timestamp": "2026-01-06T00:00:00Z", "cwd": "%s/sub", "type": "user"}' % self.cwd
        ])
        digest = build_digest(self.root, include=["projects"])
        self.assertIn("projects/sub.jsonl", [item["path"] for item in digest["files"]])

    def test_never_stores_message_text(self) -> None:
        digest = build_digest(self.root, include=["projects"])
        self.assertFalse(digest["content_stored"])
        self.assertNotIn(self.SENTINEL, json.dumps(digest, ensure_ascii=False))

    def test_include_narrows_the_read_set(self) -> None:
        digest = build_digest(self.root, include=["projects"])
        paths = [item["path"] for item in digest["files"]]
        self.assertEqual(paths, ["projects/s1.jsonl", "projects/s2.jsonl"])

    def test_source_scoped_include(self) -> None:
        # Without a source prefix, "projects" would also hit every other source's projects/.
        self.assertEqual(build_digest(self.root, include=["nobody:projects"])["file_count"], 0)
        scoped = build_digest(self.root, include=["src:plugins"])
        self.assertEqual([item["path"] for item in scoped["files"]], ["plugins/p.jsonl"])

    def test_non_session_files_are_counted_not_parsed(self) -> None:
        digest = build_digest(self.root, include=["projects"])
        self.assertEqual(digest["skipped_non_session"], 1)

    def test_parse_errors_are_counted(self) -> None:
        digest = build_digest(self.root, include=["projects"])
        self.assertEqual(digest["parse_errors"], 1)

    def test_digest_cli_writes_a_file(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli_main(["--root", str(self.root), "digest", "--include", "projects"])
        self.assertEqual(code, 0)
        saved = sorted((self.root / ".groma" / "digests").glob("digest-*.json"))
        self.assertEqual(len(saved), 1)
        payload = json.loads(saved[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "GROMA_LOCAL_DIGEST")
        self.assertFalse(payload["content_stored"])
        self.assertNotIn(self.SENTINEL, saved[0].read_text(encoding="utf-8"))

    def test_digest_requires_a_declared_include_when_none_passed(self) -> None:
        with self.assertRaises(LabError):
            build_digest(self.root)

    def test_declared_include_is_used_when_none_passed(self) -> None:
        with tempfile.TemporaryDirectory() as other_dir:
            unit = Path(other_dir) / "unit"
            unit.mkdir()
            init_workspace(unit, name="declared")
            meta_path = unit / ".groma" / "unit.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["workspace_root"] = self.cwd
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            add_source(unit, "s", str(self.src), include=["projects"])
            digest = build_digest(unit)
            self.assertEqual(
                [item["path"] for item in digest["files"]],
                ["projects/s1.jsonl", "projects/s2.jsonl"],
            )


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="summary")
        src = Path(self._tmp.name) / "src"
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (src / "projects").mkdir(parents=True)
        cwd = self.root.as_posix()
        (src / "projects" / "recent.jsonl").write_text(
            "\n".join('{"timestamp": "%s", "cwd": "%s", "type": "user"}' % (recent, cwd) for _ in range(3)) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (src / "projects" / "old.jsonl").write_text(
            '{"timestamp": "%s", "cwd": "%s", "type": "user"}\n{"timestamp": "%s", "cwd": "%s", "type": "user"}\n'
            % (old, cwd, old, cwd),
            encoding="utf-8",
            newline="\n",
        )
        add_source(self.root, "s", str(src), include=["projects"])
        save_digest(self.root, build_digest(self.root))

    def test_window_excludes_old_sessions(self) -> None:
        summary = build_summary(self.root, days=7, tz=timezone.utc)  # W23b：显式传 tz，不靠开发机时区
        self.assertEqual(summary["session_count"], 1)
        self.assertEqual(summary["active_days"], 1)
        self.assertEqual(summary["event_count"], 3)

    def test_activity_summary_groups_by_local_day(self) -> None:
        """Y5：日期归属按本机日历；"谁更新"按绝对时刻比，不按裸串。

        三个**固定** instant——不用"一小时前"那种随钟表漂移的构造（时间口径契约在
        非跨午夜时刻跑会整天空转）：
        A `23:30Z` 与 B `15:30Z` 在 UTC 同属 11-09，但在 +08:00 下分属 11-10 / 11-09；
        C `2026-11-10T04:00+08:00` 的真实时刻（20:00Z）比 A 旧，**裸串却比 A 大**——
        裸串比较会把 newest_ts 挑成 C，这一条就是 W23b-2 的牙。
        """
        tz8 = timezone(timedelta(hours=8))
        root = Path(self._tmp.name) / "localday"
        root.mkdir()
        init_workspace(root, name="localday")
        src = Path(self._tmp.name) / "localday-src"
        (src / "projects").mkdir(parents=True)
        cwd = root.as_posix()
        for name, stamp in (("a", "2026-11-09T23:30:00Z"),
                            ("b", "2026-11-09T15:30:00Z"),
                            ("c", "2026-11-10T04:00:00+08:00")):
            (src / "projects" / ("%s.jsonl" % name)).write_text(
                '{"timestamp": "%s", "cwd": "%s", "type": "user"}\n' % (stamp, cwd),
                encoding="utf-8", newline="\n",
            )
        add_source(root, "s", str(src), include=["projects"])
        save_digest(root, build_digest(root))

        summary = build_summary(root, days=3650, tz=tz8)
        self.assertEqual(summary["session_count"], 3)  # 三条都必须在窗口内，否则下面的键集合是空集假绿
        self.assertEqual([day["date"] for day in summary["by_day"]], ["2026-11-09", "2026-11-10"])
        self.assertEqual([day["sessions"] for day in summary["by_day"]], [1, 2])
        self.assertEqual(summary["active_days"], 2)
        self.assertEqual(len(summary["by_source"]), 1)
        self.assertEqual(summary["by_source"][0]["newest_ts"], "2026-11-09T23:30:00Z")

    def test_wider_window_includes_old_sessions(self) -> None:
        summary = build_summary(self.root, days=30, tz=timezone.utc)
        self.assertEqual(summary["session_count"], 2)
        self.assertEqual(summary["active_days"], 2)

    def test_text_render_has_the_headline(self) -> None:
        text = render_text(build_summary(self.root, days=7, tz=timezone.utc))
        self.assertIn("sessions 1", text)
        self.assertIn("by source", text)

    def test_summary_cli_saves_and_prints(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(self.root), "summary", "--days", "7"])
        self.assertEqual(code, 0)
        self.assertIn("sessions 1", buffer.getvalue())
        saved = sorted((self.root / ".groma" / "summaries").glob("summary-*.json"))
        self.assertEqual(len(saved), 1)

    def test_summary_without_digest_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as other_dir:
            unit = Path(other_dir) / "unit"
            unit.mkdir()
            init_workspace(unit, name="empty")
            with self.assertRaises(LabError):
                build_summary(unit)


class CatchupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="catchup")
        self.src = Path(self._tmp.name) / "src"
        (self.src / "projects").mkdir(parents=True)
        (self.src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-01-01T00:00:00Z", "cwd": "%s", "type": "user"}\n' % self.root.as_posix(),
            encoding="utf-8",
            newline="\n",
        )

    def test_no_sources_is_a_no_op(self) -> None:
        result = catchup(self.root)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no sources declared")

    def test_catchup_writes_scan_and_digest(self) -> None:
        add_source(self.root, "s", str(self.src), include=["projects"])
        result = catchup(self.root)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["digest_status"], "written")
        self.assertTrue(any((self.root / ".groma" / "scans").glob("scan-*.json")))
        self.assertTrue(any((self.root / ".groma" / "digests").glob("digest-*.json")))

    def test_second_call_within_interval_is_skipped(self) -> None:
        add_source(self.root, "s", str(self.src), include=["projects"])
        catchup(self.root)
        again = catchup(self.root)
        self.assertTrue(again["skipped"])
        self.assertEqual(again["reason"], "within min_interval")

    def test_force_bypasses_the_interval(self) -> None:
        add_source(self.root, "s", str(self.src), include=["projects"])
        catchup(self.root)
        forced = catchup(self.root, force=True)
        self.assertFalse(forced["skipped"])


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        init_workspace(self.root, name="coverage")

    def test_coverage_counts_only_active_hours(self) -> None:
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        h0, h1, h2 = now - timedelta(hours=3), now - timedelta(hours=2), now - timedelta(hours=1)
        stamp = lambda t, minutes=10: (t + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_digest(self.root, {
            "created_at": stamp(now),
            "files": [{
                "source": "s",
                "path": "a.jsonl",
                "first_ts": stamp(h0),
                "last_ts": stamp(h1, 20),
                "hours": [h0.strftime("%Y-%m-%dT%H"), h1.strftime("%Y-%m-%dT%H")],
            }],
        })
        for t in (h0, h2):
            save_scan(self.root, {"created_at": stamp(t, 0), "file_count": 1})
        result = build_coverage(self.root, days=7)
        self.assertEqual(result["active_hours"], 2)
        self.assertEqual(result["covered_hours"], 1)
        self.assertEqual(result["coverage"], 0.5)
        self.assertEqual(len(result["missing_hours"]), 1)

    def test_long_idle_span_is_not_counted_as_active(self) -> None:
        # A session that sat idle for three days must not claim 72 active hours.
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        old = now - timedelta(days=3)
        stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        save_digest(self.root, {
            "created_at": stamp(now),
            "files": [{
                "source": "s",
                "path": "long.jsonl",
                "first_ts": stamp(old),
                "last_ts": stamp(now),
                "hours": [old.strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%dT%H")],
            }],
        })
        result = build_coverage(self.root, days=7)
        self.assertEqual(result["active_hours"], 2)

    def test_hours_before_the_first_scan_are_excluded(self) -> None:
        # History that predates collection cannot be covered; counting it would
        # make the metric permanently unfair.
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        old = now - timedelta(hours=5)
        stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        save_digest(self.root, {
            "created_at": stamp(now),
            "files": [{
                "source": "s",
                "path": "a.jsonl",
                "first_ts": stamp(old),
                "last_ts": stamp(now),
                "hours": [old.strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%dT%H")],
            }],
        })
        save_scan(self.root, {"created_at": stamp(now), "file_count": 1})
        result = build_coverage(self.root, days=7)
        self.assertEqual(result["active_hours"], 1)
        self.assertEqual(result["coverage"], 1.0)

    def test_coverage_without_digest_is_a_usage_error(self) -> None:
        with self.assertRaises(LabError):
            build_coverage(self.root)


class TriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="trigger")
        patcher = mock.patch.dict(os.environ, {"GROMA_STARTUP_DIR": str(Path(self._tmp.name) / "startup")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_install_status_remove(self) -> None:
        installed = trigger_mod.install(self.root)
        target = Path(installed["trigger"])
        self.assertTrue(target.is_file())
        self.assertTrue(trigger_mod.status(self.root)["installed"])
        body = target.read_text(encoding="utf-8")
        self.assertIn("catchup-all", body)
        self.assertTrue(trigger_mod.remove(self.root)["removed"])
        self.assertFalse(trigger_mod.status(self.root)["installed"])

    def test_remove_when_absent_is_idempotent(self) -> None:
        self.assertFalse(trigger_mod.remove(self.root)["removed"])


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        init_workspace(self.root, name="settings")

    def test_default_interval_is_15_minutes(self) -> None:
        self.assertEqual(collect_interval(self.root), 15)

    def test_save_and_reload(self) -> None:
        save_settings(self.root, {"collect_interval_minutes": 5})
        self.assertEqual(load_settings(self.root)["collect_interval_minutes"], 5)

    def test_rejects_out_of_range(self) -> None:
        for bad in (0, 1441, -3):
            with self.assertRaises(LabError):
                save_settings(self.root, {"collect_interval_minutes": bad})


class AgentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="agents")
        (self.root / ".qoder" / "agents").mkdir(parents=True)
        (self.root / ".qoder" / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        self.src = Path(self._tmp.name) / "src"
        (self.src / "projects").mkdir(parents=True)
        cwd = self.root.as_posix()
        (self.src / "projects" / "main.jsonl").write_text(
            '{"timestamp": "2026-10-28T00:00:00Z", "cwd": "%s", "type": "user"}\n' % cwd,
            encoding="utf-8",
            newline="\n",
        )
        (self.src / "projects" / "sub.jsonl").write_text(
            '{"timestamp": "2026-10-28T01:00:00Z", "cwd": "%s", "type": "assistant", "key_sub_agent_type": "reviewer"}\n' % cwd,
            encoding="utf-8",
            newline="\n",
        )
        add_source(self.root, "qoder", str(self.src), "qoder-cn", ["projects"])

    def test_sub_agent_is_found_and_linked_to_parent(self) -> None:
        agents = build_agents(self.root, ["qoder-cn"])
        subs = [item for item in agents if item["kind"] == "sub"]
        primaries = [item for item in agents if item["kind"] == "primary"]
        self.assertEqual([item["name"] for item in subs], ["reviewer"])
        self.assertTrue(subs[0]["workspace_ref"])
        self.assertEqual(subs[0]["parent_agent_id"], primaries[0]["agent_id"])

    def test_same_name_in_another_workspace_is_another_agent(self) -> None:
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        init_workspace(other, name="other")
        (other / ".qoder" / "agents").mkdir(parents=True)
        (other / ".qoder" / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        here = [item for item in build_agents(self.root, ["qoder-cn"]) if item["kind"] == "sub"][0]
        there = [item for item in build_agents(other, ["qoder-cn"]) if item["kind"] == "sub"][0]
        self.assertNotEqual(here["agent_id"], there["agent_id"])

    def test_sessions_are_attributed_to_the_right_agent(self) -> None:
        digest = build_digest(self.root)
        by_path = {item["path"]: item for item in digest["files"]}
        self.assertEqual(by_path["projects/sub.jsonl"]["agent_kind"], "sub")
        self.assertEqual(by_path["projects/sub.jsonl"]["agent_name"], "reviewer")
        self.assertEqual(by_path["projects/main.jsonl"]["agent_kind"], "primary")


class QoderDbTests(unittest.TestCase):
    """Dual source: local.db decides membership and sub-agent, transcripts add structure."""

    def setUp(self) -> None:
        import sqlite3

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="qoder-db")
        (self.root / ".qoder" / "agents").mkdir(parents=True)
        (self.root / ".qoder" / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")

        self.src = Path(self._tmp.name) / "src"
        (self.src / "projects").mkdir(parents=True)
        self.session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        (self.src / "projects" / (self.session + ".jsonl")).write_text(
            '{"timestamp": "2026-10-28T00:00:00Z", "cwd": "%s", "type": "user"}\n' % self.root.as_posix(),
            encoding="utf-8",
            newline="\n",
        )

        db = sqlite3.connect(self.src / "local.db")
        db.execute("CREATE TABLE chat_session (session_id TEXT, project_id TEXT, project_uri TEXT, gmt_create INTEGER, gmt_modified INTEGER)")
        db.execute("CREATE TABLE chat_record (session_id TEXT, extra TEXT)")
        db.execute("CREATE TABLE chat_message (session_id TEXT, gmt_create INTEGER)")
        stamp = 1761609600000  # 2026-10-28T00:00:00Z in ms
        db.execute("INSERT INTO chat_session VALUES (?,?,?,?,?)", (self.session, self.root.as_posix(), None, stamp, stamp))
        db.execute("INSERT INTO chat_session VALUES (?,?,?,?,?)", ("outside-session", "C:/elsewhere/project", None, stamp, stamp))
        db.execute("INSERT INTO chat_session VALUES (?,?,?,?,?)", ("no-workspace", None, None, stamp, stamp))
        db.execute("INSERT INTO chat_record VALUES (?,?)", (self.session, '{"key_sub_agent_type": "reviewer"}'))
        for i in range(3):
            db.execute("INSERT INTO chat_message VALUES (?,?)", (self.session, stamp + i * 60000))
        db.commit()
        db.close()

        add_source(self.root, "qoder", str(self.src), "qoder-cn", ["projects"])

    def test_db_session_is_attributed_to_the_sub_agent(self) -> None:
        digest = build_digest(self.root)
        entry = next(item for item in digest["files"] if item["path"].startswith("projects/"))
        self.assertEqual(entry["agent_name"], "reviewer")
        self.assertEqual(entry["agent_kind"], "sub")
        self.assertTrue(entry["transcript"])

    def test_duplicate_session_under_two_dirs_is_counted_once(self) -> None:
        other = self.src / "projects" / "other"
        other.mkdir(parents=True)
        line = json.dumps(
            {"timestamp": "2026-10-28T02:00:00Z", "cwd": self.root.as_posix(), "type": "user", "pad": "x" * 200}
        ) + "\n"
        (other / (self.session + ".jsonl")).write_text(line * 3, encoding="utf-8", newline="\n")
        digest = build_digest(self.root)
        matches = [item for item in digest["files"] if item["path"].endswith(self.session + ".jsonl")]
        self.assertEqual(len(matches), 1)
        self.assertIn("other", matches[0]["path"])

    def test_sessions_outside_the_workspace_are_excluded(self) -> None:
        digest = build_digest(self.root)
        self.assertEqual(digest["unattributed_outside"], 1)
        self.assertEqual(digest["unattributed_no_cwd"], 1)

    def test_db_only_session_still_appears(self) -> None:
        import sqlite3

        db = sqlite3.connect(self.src / "local.db")
        db.execute("INSERT INTO chat_session VALUES (?,?,?,?,?)", ("db-only", self.root.as_posix(), None, 1761609600000, 1761609600000))
        for i in range(2):
            db.execute("INSERT INTO chat_message VALUES (?,?)", ("db-only", 1761609600000 + i * 60000))
        db.commit()
        db.close()
        digest = build_digest(self.root)
        entry = next(item for item in digest["files"] if item["path"] == "db/db-only")
        self.assertFalse(entry["transcript"])
        self.assertEqual(entry["events"], 2)


class DiscoveryPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="preview")

    def test_file_breakdown_counts_inside_and_outside(self) -> None:
        from lab.discovery import _file_breakdown

        base = Path(self._tmp.name) / "sessions"
        base.mkdir()
        (base / "inside.jsonl").write_text(
            '{"cwd": "%s"}\n' % self.root.as_posix(), encoding="utf-8", newline="\n"
        )
        (base / "outside.jsonl").write_text(
            '{"cwd": "C:/elsewhere"}\n', encoding="utf-8", newline="\n"
        )
        (base / "nocwd.jsonl").write_text('{"type": "x"}\n', encoding="utf-8", newline="\n")
        result = _file_breakdown(self.root, base, self.root.as_posix())
        self.assertEqual(result["inside"], 1)
        self.assertEqual(result["outside"], 1)
        self.assertEqual(result["no_cwd"], 1)
        self.assertEqual(result["breakdown"], "cwd-preview")

    def test_qoder_breakdown_uses_database(self) -> None:
        import sqlite3

        from lab.discovery import _qoder_breakdown

        (self.root / ".qoder" / "agents").mkdir(parents=True)
        (self.root / ".qoder" / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        src = Path(self._tmp.name) / "src"
        src.mkdir()
        db = sqlite3.connect(src / "local.db")
        db.execute("CREATE TABLE chat_session (session_id TEXT, project_id TEXT, project_uri TEXT, gmt_create INTEGER, gmt_modified INTEGER)")
        db.execute("CREATE TABLE chat_record (session_id TEXT, extra TEXT)")
        db.execute("CREATE TABLE chat_message (session_id TEXT, gmt_create INTEGER)")
        db.execute("INSERT INTO chat_session VALUES ('s1', ?, NULL, 0, 0)", (self.root.as_posix(),))
        db.execute("INSERT INTO chat_session VALUES ('s2', 'C:/elsewhere', NULL, 0, 0)")
        db.execute("INSERT INTO chat_record VALUES ('s1', '{\"key_sub_agent_type\": \"reviewer\"}')")
        db.commit()
        db.close()

        result = _qoder_breakdown(self.root, src, self.root.as_posix())
        self.assertEqual(result["breakdown"], "database")
        self.assertEqual(result["inside"], 1)
        self.assertEqual(result["outside"], 1)
        names = {item["name"]: item["sessions"] for item in result["agents"]}
        self.assertEqual(names.get("reviewer"), 1)

    def test_file_breakdown_unions_multiple_workspaces(self) -> None:
        from lab.discovery import _file_breakdown

        other = Path(self._tmp.name) / "other"
        other.mkdir()
        base = Path(self._tmp.name) / "sessions"
        base.mkdir()
        (base / "a.jsonl").write_text('{"cwd": "%s"}\n' % self.root.as_posix(), encoding="utf-8", newline="\n")
        (base / "b.jsonl").write_text('{"cwd": "%s"}\n' % other.as_posix(), encoding="utf-8", newline="\n")
        result = _file_breakdown(self.root, base, [self.root.as_posix(), other.as_posix()])
        self.assertEqual(result["inside"], 2)  # both member workspaces counted together
        self.assertEqual(result["breakdown"], "cwd-preview")


class CatchupAllTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(Path(self._tmp.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.a = self._make("a")
        self.b = self._make("b")
        register_workspace(self.a)
        register_workspace(self.b)

    def _make(self, name: str) -> Path:
        root = Path(self._tmp.name) / name
        root.mkdir()
        init_workspace(root, name=name)
        src = Path(self._tmp.name) / (name + "-src")
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-10-28T00:00:00Z", "cwd": "%s"}\n' % root.as_posix(),
            encoding="utf-8",
            newline="\n",
        )
        add_source(root, "src", str(src), "test", ["projects"])
        return root

    def test_collects_every_unpaused_workspace(self) -> None:
        pause(self.b)
        result = catchup_all()
        self.assertEqual(result["workspaces"], 2)
        self.assertEqual(result["collected"], 1)
        self.assertTrue(list((self.a / ".groma" / "scans").glob("scan-*.json")))
        self.assertFalse(list((self.b / ".groma" / "scans").glob("scan-*.json")))

    def test_missing_directory_is_skipped(self) -> None:
        gone = Path(self._tmp.name) / "gone"
        register_workspace(gone)
        reasons = {item["workspace"]: item.get("reason") for item in catchup_all()["results"]}
        self.assertEqual(reasons.get(gone.as_posix()), "missing")


class WorkspaceRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(Path(self._tmp.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="removable")
        (self.root / "main.py").write_text("print('hi')\n", encoding="utf-8", newline="\n")
        register_workspace(self.root)

    def test_forget_keeps_the_evidence(self) -> None:
        result = remove_workspace(self.root)
        self.assertTrue(result["forgotten"])
        self.assertTrue((self.root / ".groma" / "unit.json").is_file())
        self.assertEqual(list_workspaces(), [])

    def test_delete_evidence_keeps_project_files(self) -> None:
        result = remove_workspace(self.root, delete_evidence=True)
        self.assertTrue(result["evidence_deleted"])
        self.assertFalse((self.root / ".groma").exists())
        self.assertTrue((self.root / "main.py").is_file())

    def test_bound_sub_agents_are_removed(self) -> None:
        (self.root / ".qoder" / "agents").mkdir(parents=True)
        (self.root / ".qoder" / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        build_agents(self.root, ["qoder-cn"], persist=True)
        ref = workspace_ref(self.root)
        remove_workspace(self.root)
        remaining = [item for item in load_global_agents() if item.get("workspace_ref") == ref]
        self.assertEqual(remaining, [])


class AssetTests(unittest.TestCase):
    SENTINEL = "SECRET_ASSET_MARKER"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="assets")
        self.src = Path(self._tmp.name) / "src"
        (self.src / "projects").mkdir(parents=True)
        cwd = self.root.as_posix()
        for index, agent_key in enumerate((None, "reviewer")):
            record = {
                "timestamp": "2026-10-28T0%d:00:00Z" % index,
                "cwd": cwd,
                "type": "assistant",
                "text": self.SENTINEL,
            }
            if agent_key:
                record["key_sub_agent_type"] = agent_key
            (self.src / "projects" / ("s%d.jsonl" % index)).write_text(
                json.dumps(record) + "\n", encoding="utf-8", newline="\n"
            )
        (self.root / ".qoder" / "agents").mkdir(parents=True)
        (self.root / ".qoder" / "agents" / "reviewer.md").write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        add_source(self.root, "src", str(self.src), "qoder-cn", ["projects"])
        catchup(self.root, force=True)

    def _build(self, title: str = "修复过程"):
        return build_asset(self.root, title, [("src", "projects/s0.jsonl"), ("src", "projects/s1.jsonl")])

    def test_build_writes_the_four_pieces(self) -> None:
        asset = self._build()
        folder = self.root / ".groma" / "assets" / "current"
        for name in ("asset.json", "README.md", "VALIDATION.md", "manifest.json"):
            self.assertTrue((folder / name).is_file(), name)

    def test_manifest_hashes_match_the_files(self) -> None:
        asset = self._build()
        folder = self.root / ".groma" / "assets" / "current"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        import hashlib

        for item in manifest["files"]:
            blob = (folder / item["path"]).read_bytes()
            self.assertEqual(item["bytes"], len(blob))
            self.assertEqual(item["sha256"], hashlib.sha256(blob).hexdigest())

    def test_no_absolute_path_and_no_content(self) -> None:
        asset = self._build()
        folder = self.root / ".groma" / "assets" / "current"
        text = "".join(path.read_text(encoding="utf-8") for path in folder.glob("*"))
        self.assertNotIn(self.root.as_posix(), text)
        self.assertNotIn(self.SENTINEL, text)
        payload = json.loads((folder / "asset.json").read_text(encoding="utf-8"))
        for item in payload["sessions"]:
            self.assertNotIn("cwd", item)

    def test_same_selection_same_id(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(first["asset_id"], second["asset_id"])

    def test_unknown_session_is_rejected(self) -> None:
        with self.assertRaises(LabError):
            build_asset(self.root, "x", [("src", "projects/missing.jsonl")])

    def test_export_writes_context_markdown(self) -> None:
        asset = self._build()
        result = export_context(self.root, asset["asset_id"])
        path = Path(result["context"])
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("groma:context asset=%s" % asset["asset_id"], text)
        self.assertIn(asset["title"], text)
        self.assertNotIn(self.root.as_posix(), text)
        self.assertNotIn(self.SENTINEL, text)

    def test_export_is_deterministic(self) -> None:
        asset = self._build()
        first = export_context(self.root, asset["asset_id"])
        second = export_context(self.root, asset["asset_id"])
        self.assertEqual(first["sha256"], second["sha256"])

    def test_export_to_a_custom_path(self) -> None:
        asset = self._build()
        target = Path(self._tmp.name) / "out" / "CONTEXT.md"
        export_context(self.root, asset["asset_id"], target)
        self.assertTrue(target.is_file())

    def test_export_unknown_asset_is_rejected(self) -> None:
        with self.assertRaises(LabError):
            export_context(self.root, "asset-nope")

    def test_content_asset_exports_its_readme(self) -> None:
        asset = self._build()
        folder = self.root / ".groma" / "assets" / "current"
        payload = json.loads((folder / "asset.json").read_text(encoding="utf-8"))
        payload["content_stored"] = True
        (folder / "asset.json").write_text(json.dumps(payload), encoding="utf-8")
        (folder / "README.md").write_text("# 内容资产\n\n真实摘要段落\n", encoding="utf-8", newline="\n")
        result = export_context(self.root, asset["asset_id"])
        text = Path(result["context"]).read_text(encoding="utf-8")
        self.assertIn("groma:context asset=%s" % asset["asset_id"], text)
        self.assertIn("真实摘要段落", text)
        self.assertNotIn(self.SENTINEL, text)

    def test_colocated_sessions_are_attributed(self) -> None:
        asset = self._build()
        kinds = {item["agent_kind"] for item in asset["sessions"]}
        self.assertIn("primary", kinds)
        self.assertIn("sub", kinds)


class EvidenceFixtureTests(unittest.TestCase):
    """The committed receipt must keep passing; rot here would invalidate the L1 evidence."""

    def test_examples_demo_unit_still_verifies(self) -> None:
        unit = REPO_ROOT / "examples" / "demo-unit"
        receipts = sorted((unit / ".groma" / "receipts").glob("*.json"))
        self.assertTrue(receipts, "no committed receipt found")
        result = verify_receipt(unit, receipts[-1])
        self.assertEqual(result["verdict"], "PASS", result)
        self.assertEqual(result["checked"], 2)


class LauncherTests(unittest.TestCase):
    def test_wb_py_runs_from_any_directory(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            with tempfile.TemporaryDirectory() as space:
                completed = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "groma.py"), "--root", space, "init", "--name", "launcher"],
                    cwd=work,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue((Path(space) / ".groma" / "unit.json").is_file())


def _run_walkthrough(case: unittest.TestCase, filename: str) -> None:
    bash = os.environ.get("GROMA_BASH") or shutil.which("bash")
    if not bash:
        case.skipTest("bash not available")
    script = REPO_ROOT / "examples" / filename
    probe = subprocess.run(
        [bash, "-c", 'test -f "$1"', "_", str(script)],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        case.skipTest("this bash cannot see the repo (WSL bash?)")
    env = dict(os.environ)
    env["PYTHON"] = sys.executable
    completed = subprocess.run(
        [bash, str(script)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    case.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
    case.assertIn("RESULT: PASS", completed.stdout)


class QuickstartScriptTests(unittest.TestCase):
    """README points at this script as the acceptance path, so it must actually run."""

    def test_quickstart_script_passes(self) -> None:
        _run_walkthrough(self, "quickstart.sh")


class L2aWalkthroughTests(unittest.TestCase):
    def test_l2a_walkthrough_passes(self) -> None:
        _run_walkthrough(self, "l2a-walkthrough.sh")


if __name__ == "__main__":
    unittest.main()
