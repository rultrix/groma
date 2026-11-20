"""S1: the vendor list, not our own digests, is what coverage is measured against.

Every test here is written so that a module which took its denominator from the
digests -- the old `coverage` shape -- would fail.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lab import reconcile, state, unitmatch
from lab.sources import add_source
from lab.workspace import init_workspace

UUID_A = "fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3"
UUID_B = "019feb06-25ea-7d21-9986-8d57ff1da526"
UUID_C = "9a3b7c21-4c1f-7a33-8b0e-2f6d9c1a4b77"
UUID_TWIN = "019feb06-7777-7d21-9986-8d57ff1da599"
UUID_GHOST = "5e5e5e5e-1111-2222-3333-444455556666"


def write_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user"}\n', encoding="utf-8", newline="\n")


def make_db(path: Path, session_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.execute(
        "CREATE TABLE chat_session (session_id TEXT, project_name TEXT, version TEXT,"
        " mode TEXT, session_type TEXT, parent_session_id TEXT)"
    )
    connection.executemany(
        "INSERT INTO chat_session VALUES (?, ?, ?, ?, ?, ?)",
        [(sid, "d--workspaces-ws", "8", "chat", "assistant", None) for sid in session_ids],
    )
    connection.commit()
    connection.close()


def write_digest(root: Path, rows: list[dict]) -> None:
    folder = root / ".groma" / "digests"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "kind": "GROMA_LOCAL_DIGEST",
        "created_at": "2026-11-08T09:00:00Z",
        "unit_id": unitmatch.load_unit(root)["unit_id"],
        "workspace_root": root.as_posix(),
        "file_count": len(rows),
        "event_count": 1,
        "parse_errors": 0,
        "files": rows,
    }
    (folder / "digest-20261108T090000Z.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n"
    )


def row_for(session_id: str, rel: str, harness: str = "qoder-cn") -> dict:
    return {"source": "qoder", "path": rel, "harness": harness,
            "session_key": "%s:%s" % (harness, session_id), "events": 1}


class ReconcileFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state_before = os.environ.get(state.STATE_ENV)
        os.environ[state.STATE_ENV] = str((Path(self.tmp.name) / "state").resolve())
        self.base = Path(self.tmp.name)
        self.root = self.base / "ws"
        self.root.mkdir()
        init_workspace(self.root)
        self.home = self.base / "qoder-home"

    def tearDown(self) -> None:
        if self._state_before is None:
            os.environ.pop(state.STATE_ENV, None)
        else:
            os.environ[state.STATE_ENV] = self._state_before
        self.tmp.cleanup()

    def declare_qoder(self, include: tuple[str, ...] = ("projects",)) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        add_source(self.root, "qoder", str(self.home), harness="qoder-cn", include=list(include))

    def projects(self, session_id: str) -> Path:
        return self.home / "projects" / "d--workspaces-ws" / ("%s.jsonl" % session_id)

    def cache_root(self, session_id: str) -> Path:
        prefix = session_id[:8]
        return (self.home / "cache" / "projects" / ("d--workspaces-ws-%08x" % len(prefix))
                / "conversation-history" / prefix / ("%s.jsonl" % prefix))

    def real_db(self, session_ids: list[str]) -> Path:
        path = self.home / "shared_client" / "cache" / "db" / "local.db"
        make_db(path, session_ids)
        return path


class DenominatorTests(ReconcileFixture):
    def setUp(self) -> None:
        super().setUp()
        write_jsonl(self.projects(UUID_A))
        write_jsonl(self.cache_root(UUID_B))
        self.real_db([UUID_A, UUID_B, UUID_C])
        self.declare_qoder()
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])
        self.report = reconcile.build_reconciliation(self.root)

    def test_the_vendor_list_sets_the_universe_not_the_digest(self) -> None:
        counts = self.report["counts"]
        self.assertEqual(counts["vendor_db_sessions"], 3)
        self.assertEqual(counts["sessions"], 3)
        self.assertEqual(counts["collected"], 1)
        self.assertEqual(counts["missing_from_collection"], 2)
        self.assertEqual(counts["vendor_only"], 1)
        self.assertEqual(counts["with_transcript"], 2)
        self.assertEqual(counts["transcript_only"], 0)

    def test_a_prefix8_file_lands_on_the_full_session_id_the_vendor_lists(self) -> None:
        examples = {item["id"] for item in self.report["examples"]["missing_from_collection"]}
        self.assertEqual(examples, {UUID_B, UUID_C})
        self.assertNotIn(UUID_B[:8], examples)
        self.assertEqual(self.report["key_shapes"], {"uuid": 1, "prefix8": 1})

    def test_the_report_says_where_its_numbers_come_from(self) -> None:
        self.assertIn("NOT consulted", self.report["denominator_source"]["files"])
        self.assertIn("never a denominator", self.report["denominator_source"]["digests"])

    def test_the_coverage_block_carries_the_gate_fields(self) -> None:
        coverage = self.report["coverage"]
        self.assertEqual(
            set(coverage),
            {"collected", "in_vendor_list", "missing_from_collection", "unclassified",
             "denominator_source"},
        )
        self.assertEqual(coverage["in_vendor_list"], 3)
        self.assertEqual(coverage["collected"], 1)
        self.assertEqual(coverage["missing_from_collection"], 2)
        self.assertEqual(coverage["unclassified"], 1)
        self.assertIs(coverage["denominator_source"], self.report["denominator_source"])

    def test_a_digest_row_backed_by_nothing_is_called_unverifiable(self) -> None:
        extra = row_for(UUID_GHOST, "projects/d--workspaces-ws/%s.jsonl" % UUID_GHOST)
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A), extra])
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["counts"]["collected_unverifiable"], 1)
        self.assertEqual(report["examples"]["collected_unverifiable"][0]["id"], UUID_GHOST)


class IncludeBlindnessTests(ReconcileFixture):
    def test_an_include_that_forgets_a_root_still_counts_that_root(self) -> None:
        write_jsonl(self.projects(UUID_A))
        write_jsonl(self.cache_root(UUID_B))
        self.real_db([UUID_A, UUID_B])
        self.declare_qoder()  # "projects" only -- the cache root is never scanned
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])
        report = reconcile.build_reconciliation(self.root)
        cache = next(entry for entry in report["roots"] if entry["subroot"] == "cache/projects")
        self.assertFalse(cache["declared_in_include"])
        self.assertEqual(cache["files"], 1)
        self.assertEqual(report["counts"]["undeclared_roots"], 1)
        self.assertEqual(report["counts"]["missing_from_collection"], 1)
        self.assertEqual(report["examples"]["missing_from_collection"][0]["id"], UUID_B)

    def test_a_subroot_the_vendor_moved_away_from_is_reported_as_missing(self) -> None:
        self.declare_qoder(include=("projects", "cache/projects"))
        self.projects(UUID_A).parent.mkdir(parents=True)
        self.real_db([UUID_A])
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["counts"]["missing_roots"], 1)
        missing = [entry for entry in report["roots"] if not entry["exists"]]
        self.assertEqual([entry["subroot"] for entry in missing], ["cache/projects"])

    def test_a_source_whose_directory_is_gone_is_named(self) -> None:
        gone = self.base / "codex-home"
        (gone / "sessions").mkdir(parents=True)
        add_source(self.root, "codex", str(gone), harness="codex", include=["sessions"])
        shutil.rmtree(gone)
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["counts"]["stale_sources"], 1)
        self.assertEqual(report["stale_sources"][0]["family"], "codex")


class PrefixCollisionTests(ReconcileFixture):
    def test_one_prefix_two_sessions_is_reported_instead_of_joined(self) -> None:
        write_jsonl(self.projects(UUID_A))
        write_jsonl(self.cache_root(UUID_B))
        self.real_db([UUID_A, UUID_B, UUID_TWIN])
        self.declare_qoder(include=("projects", "cache/projects"))
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["counts"]["ambiguous_prefix8"], 1)
        self.assertEqual(sorted(report["ambiguous_prefix"][UUID_B[:8]]), sorted([UUID_B, UUID_TWIN]))
        self.assertEqual(report["counts"]["with_transcript"], 1)
        self.assertEqual(report["counts"]["vendor_only"], 2)


class IdentityDatabaseChoiceTests(ReconcileFixture):
    def test_the_client_cache_database_wins_over_a_local_fixture(self) -> None:
        write_jsonl(self.projects(UUID_A))
        self.real_db([UUID_A, UUID_C])
        make_db(self.home / "local.db", [UUID_GHOST])
        self.declare_qoder()
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["counts"]["vendor_db_sessions"], 2)
        self.assertEqual(report["databases"][0]["rel"], "shared_client/cache/db/local.db")
        self.assertNotIn(UUID_GHOST, {item["id"] for item in report["examples"]["missing_from_collection"]})

    def test_a_harness_home_without_a_vendor_database_says_absent(self) -> None:
        write_jsonl(self.projects(UUID_A))
        self.declare_qoder()
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["databases"], [{"home": report["roots"][0]["home"], "status": "absent"}])
        self.assertEqual(report["counts"]["vendor_db_sessions"], 0)
        self.assertEqual(report["counts"]["transcript_only"], 1)


class LeakTests(ReconcileFixture):
    def test_the_report_carries_no_absolute_path_and_no_file_name(self) -> None:
        write_jsonl(self.projects(UUID_A))
        write_jsonl(self.cache_root(UUID_B))
        self.real_db([UUID_A, UUID_B, UUID_C])
        self.declare_qoder(include=("projects", "cache/projects"))
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])
        exported = json.dumps(reconcile.build_reconciliation(self.root), ensure_ascii=False)
        self.assertNotIn(str(self.base).replace("\\", "/"), exported)
        self.assertNotIn(self.home.as_posix(), exported)
        self.assertNotIn(".jsonl", exported)
        self.assertNotIn("d--workspaces-ws", exported)
        self.assertIn("ws-", exported)


class WorkspaceGapTests(ReconcileFixture):
    def setUp(self) -> None:
        super().setUp()
        write_jsonl(self.projects(UUID_A))
        self.real_db([UUID_A])
        self.declare_qoder()
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])

    def test_a_joined_folder_that_declares_no_qoder_source_is_listed_as_a_gap(self) -> None:
        member = self.base / "member"
        member.mkdir()
        init_workspace(member)
        unit_id = unitmatch.load_unit(self.root)["unit_id"]
        unitmatch.join_unit(unit_id, member, role=unitmatch.ROLE_EVIDENCE)
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["source_gaps"]["qoder"], [state.workspace_ref(member)])
        add_source(member, "qoder", str(self.home), harness="qoder-cn", include=["projects"])
        report = reconcile.build_reconciliation(self.root)
        self.assertNotIn("qoder", report["source_gaps"])

    def test_the_same_root_reached_from_two_workspaces_is_counted_once(self) -> None:
        member = self.base / "member"
        member.mkdir()
        init_workspace(member)
        unit_id = unitmatch.load_unit(self.root)["unit_id"]
        unitmatch.join_unit(unit_id, member, role=unitmatch.ROLE_EVIDENCE)
        add_source(member, "qoder", str(self.home), harness="qoder-cn", include=["projects"])
        report = reconcile.build_reconciliation(self.root)
        qoder_roots = [entry for entry in report["roots"] if entry["subroot"] == "projects"]
        self.assertEqual(len(qoder_roots), 1)
        self.assertEqual(qoder_roots[0]["workspaces"], 2)
        self.assertEqual(report["counts"]["transcript_files"], 1)
        self.assertEqual(report["counts"]["sessions"], 1)


class ShapeTests(ReconcileFixture):
    def test_subagent_files_are_their_own_shape_and_not_vendor_sessions(self) -> None:
        write_jsonl(self.home / "projects" / "d--workspaces-ws" / "agent-search-0123456789abcdef.jsonl")
        self.real_db([])
        self.declare_qoder()
        report = reconcile.build_reconciliation(self.root)
        self.assertEqual(report["key_shapes"], {"subagent": 1})
        self.assertEqual(report["counts"]["transcript_only"], 1)
        self.assertEqual(report["by_harness"]["qoder"]["vendor_listed"], 0)


if __name__ == "__main__":
    unittest.main()


class EvidenceLivenessTests(ReconcileFixture):
    """以证据键为输入的存活判定，三档都要有真实依据。"""

    def test_three_states_from_one_pass(self) -> None:
        write_jsonl(self.projects(UUID_A))
        write_jsonl(self.projects(UUID_B))
        self.real_db([UUID_A, UUID_B])
        self.declare_qoder()
        write_digest(self.root, [row_for(UUID_A, "projects/d--workspaces-ws/%s.jsonl" % UUID_A)])
        keys = [("qoder", "db/" + UUID_A), ("qoder", "db/" + UUID_B), ("qoder", "db/" + UUID_GHOST)]
        states = reconcile.evidence_liveness(self.root, keys)
        self.assertEqual(states[keys[0]], reconcile.LIVE)
        self.assertEqual(states[keys[1]], reconcile.VENDOR_LISTS)
        self.assertEqual(states[keys[2]], reconcile.GONE)
