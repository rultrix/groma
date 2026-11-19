"""S2: the pack -> apply -> reforge chain, with L1's refusal gates on the product path.

Test numbers in the names map to §六 of `docs/TASK-topic-model-v0.40.md`:
#2 unclassified arithmetic, #3 apply happy path, #4 idempotence, #5 anchorless
refusal, #6 undo reaching a re-forge, #8 stale token refusal.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from lab import asset, classify, cli, reconcile, state, topics, unitmatch
from lab.workspace import init_workspace

ROWS = [
    {"source": "qoder", "path": "projects/d--workspaces-unit-x/fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3.jsonl",
     "harness": "qoder-cn", "agent_kind": "primary", "events": 900,
     "fingerprint_kind": "transcript-sha256",
     "first_ts": "2026-11-01T09:00:00Z", "last_ts": "2026-11-01T12:00:00Z"},
    {"source": "qoder", "path": "projects/d--workspaces-unit-x/019feb06-25ea-7d21-9986-8d57ff1da526.jsonl",
     "harness": "qoder-cn", "agent_kind": "primary", "events": 120,
     "fingerprint_kind": "transcript-sha256",
     "first_ts": "2026-11-02T09:00:00Z", "last_ts": "2026-11-02T09:40:00Z"},
    {"source": "codex", "path": "sessions/2026/11/07/rollout-2026-11-07T09-00-00-019feb06-25ea-7d21-9986-8d57ff1da526.jsonl",
     "harness": "codex", "agent_kind": "primary", "events": 7722,
     "fingerprint_kind": "transcript-sha256",
     "first_ts": "2026-08-10T09:34:37Z", "last_ts": "2026-08-12T05:00:00Z"},
]

ANCHORLESS = {"source": "", "path": "", "harness": "qoder-cn", "events": 5}


def write_digest(root: Path, rows: list[dict], stamp: str = "20261108T090000Z") -> Path:
    folder = root / ".groma" / "digests"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "kind": "GROMA_LOCAL_DIGEST",
        "created_at": "2026-11-08T09:00:00Z",
        "unit_id": unitmatch.load_unit(root)["unit_id"],
        "workspace_root": root.as_posix(),
        "file_count": len(rows),
        "event_count": sum(row.get("events") or 0 for row in rows),
        "parse_errors": 0,
        "files": rows,
    }
    path = folder / ("digest-%s.json" % stamp)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


class ApplyFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state_before = os.environ.get(state.STATE_ENV)
        os.environ[state.STATE_ENV] = str((Path(self.tmp.name) / "state").resolve())
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root, name="Groma 搭建")
        write_digest(self.root, ROWS)

    def tearDown(self) -> None:
        if self._state_before is None:
            os.environ.pop(state.STATE_ENV, None)
        else:
            os.environ[state.STATE_ENV] = self._state_before
        self.tmp.cleanup()

    def tokens(self, rows: list[dict] | None = None) -> list[str]:
        return [classify.token_of(row["source"], row["path"]) for row in (rows or ROWS)]

    def keys(self, rows: list[dict] | None = None) -> set[tuple[str, str]]:
        return {(row["source"], row["path"]) for row in (rows or ROWS)}

    def topic(self, name: str = "Groma 搭建") -> dict:
        return topics.create_topic(name)


class ApplyGateTests(ApplyFixture):
    def test_t3_apply_records_the_decider_on_every_event(self) -> None:
        result = classify.apply(self.root, "Groma 搭建", self.tokens(), decided_by="llm:qoder@auto")
        self.assertTrue(result["topic_created"])
        self.assertEqual(result["added"], 3)
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["refused_tokens"], [])
        events = topics.active_evidence(result["topic_id"])
        self.assertEqual(len(events), 3)
        self.assertEqual({event["decided_by"] for event in events}, {"llm:qoder@auto"})

    def test_t4_applying_the_same_pack_again_does_not_grow_membership(self) -> None:
        first = classify.apply(self.root, "Groma 搭建", self.tokens(), decided_by="human")
        second = classify.apply(self.root, "Groma 搭建", self.tokens(), decided_by="human")
        self.assertEqual(second["topic_id"], first["topic_id"])
        self.assertEqual(len(topics.active_evidence(first["topic_id"])), 3)

    def test_t5_an_anchorless_digest_row_is_refused_and_writes_nothing(self) -> None:
        write_digest(self.root, ROWS + [ANCHORLESS])
        pack = classify.candidates(self.root)
        self.assertEqual(pack["recall"]["candidates"], 3)
        self.assertEqual(pack["recall"]["unanchored"], 1)
        result = classify.apply(self.root, "Groma 搭建", self.tokens(), decided_by="llm:qoder@auto")
        self.assertEqual(result["added"], 3)
        self.assertEqual(len(topics.events(result["topic_id"])), 3)
        self.assertNotIn(("", ""), {(event["source"], event["path"]) for event in topics.events()})

    def test_t8_a_token_from_a_stale_pack_is_refused_not_guessed(self) -> None:
        tokens = self.tokens()
        swept = ROWS[1]
        write_digest(self.root, [row for row in ROWS if row is not swept])
        result = classify.apply(self.root, "Groma 搭建", tokens, decided_by="human")
        self.assertEqual(result["added"], 2)
        self.assertEqual(len(result["refused_tokens"]), 1)
        self.assertIn("不在候选包", result["refused_tokens"][0]["reason"])
        self.assertEqual({(event["source"], event["path"]) for event in topics.events()},
                         self.keys([row for row in ROWS if row is not swept]))

    def test_apply_by_name_is_stable_and_reports_whether_it_created_the_topic(self) -> None:
        first = classify.apply(self.root, "Groma 搭建", self.tokens()[:1], decided_by="human")
        second = classify.apply(self.root, "Groma 搭建", self.tokens()[1:], decided_by="human")
        self.assertTrue(first["topic_created"])
        self.assertFalse(second["topic_created"])
        self.assertEqual(first["topic_id"], second["topic_id"])
        self.assertEqual(len(topics.active_evidence(second["topic_id"])), 3)

    def test_t2_unclassified_arithmetic_moves_only_with_evidence(self) -> None:
        rows = unitmatch.unit_files(unitmatch.digest_records_for(unitmatch.unit_workspaces(self.root)))
        counts = topics.classification_counts(rows)
        self.assertEqual(counts["unclassified"], counts["collected_sessions"])
        result = classify.apply(self.root, "Groma 搭建", self.tokens()[:1], decided_by="human")
        counts = topics.classification_counts(rows)
        self.assertEqual(counts["classified"], 1)
        self.assertEqual(counts["unclassified"], counts["collected_sessions"] - 1)
        topics.undo(result["topic_id"], list(self.keys(ROWS[:1])), decided_by="human")
        self.assertEqual(topics.classification_counts(rows)["classified"], 0)


class ReforgeTests(ApplyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.result = classify.apply(self.root, "Groma 搭建", self.tokens(), decided_by="human")
        self.topic_id = self.result["topic_id"]

    def sessions_of(self, asset_id: str) -> set[tuple[str, str]]:
        loaded = asset.load_asset(self.root, asset_id)
        return {(item["source"], item["path"]) for item in loaded["sessions"]}

    def test_t6_reforge_cites_exactly_the_active_evidence(self) -> None:
        built = topics.reforge(self.root, self.topic_id)
        self.assertEqual(built["sessions"], 3)
        self.assertEqual(self.sessions_of(built["asset_id"]), self.keys())

    def test_t6_an_undone_session_leaves_the_reforged_asset(self) -> None:
        first = topics.reforge(self.root, self.topic_id)
        dropped = (ROWS[1]["source"], ROWS[1]["path"])
        topics.undo(self.topic_id, [dropped], decided_by="human", reason="判断错了")
        stale = topics.stale_after_undo(self.topic_id)
        self.assertTrue(stale["reforged"])
        self.assertEqual(stale["asset_id"], first["asset_id"])
        self.assertEqual([list(item) for item in stale["dropped"]], [list(dropped)])

        second = topics.reforge(self.root, self.topic_id)
        self.assertEqual(second["sessions"], 2)
        self.assertNotEqual(second["asset_id"], first["asset_id"])
        self.assertNotIn(dropped, self.sessions_of(second["asset_id"]))
        self.assertEqual(self.sessions_of(second["asset_id"]), self.keys(ROWS[:1] + ROWS[2:]))
        self.assertEqual(topics.stale_after_undo(self.topic_id)["dropped"], [])

    def test_the_asset_already_served_is_not_silently_rewritten(self) -> None:
        first = topics.reforge(self.root, self.topic_id)
        topics.undo(self.topic_id, [(ROWS[0]["source"], ROWS[0]["path"])], decided_by="human")
        before = asset.load_asset(self.root, first["asset_id"])
        topics.reforge(self.root, self.topic_id)
        after = asset.load_asset(self.root, first["asset_id"])
        self.assertEqual(before, after)  # content-addressed and immutable: the old draft stays

    def test_reforge_refuses_a_topic_with_no_active_evidence(self) -> None:
        topics.undo(self.topic_id, sorted(self.keys()), decided_by="human")
        with self.assertRaises(ValueError):
            topics.reforge(self.root, self.topic_id)
        self.assertEqual(asset.list_assets(self.root), [])

    def test_reforge_names_an_unknown_topic_instead_of_creating_one(self) -> None:
        with self.assertRaises(ValueError):
            topics.reforge(self.root, "没有这个主题")

    def test_the_reforge_event_is_audit_trail_not_state(self) -> None:
        built = topics.reforge(self.root, self.topic_id)
        record = topics.last_reforge(self.topic_id)
        self.assertEqual(record["asset_id"], built["asset_id"])
        self.assertEqual(len(record["selection"]), 3)
        self.assertEqual(sorted(map(tuple, record["selection"])), sorted(self.keys()))

    def test_reforge_writes_no_absolute_path_into_the_evidence_log(self) -> None:
        topics.reforge(self.root, self.topic_id)
        text = (Path(os.environ[state.STATE_ENV]) / topics.REFORGES).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root).replace("\\", "/"), text)
        self.assertNotIn("\\", text)


class CliChainTests(ApplyFixture):
    """The commands themselves, not only the functions behind them."""

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--root", str(self.root)] + list(argv))
        text = buffer.getvalue()
        return code, (json.loads(text) if text.strip() else {})

    def test_pack_roundtrip_feeds_apply_and_reforge(self) -> None:
        pack_path = Path(self.tmp.name) / "pack.json"
        code, exported = self.run_cli("topic", "candidates", "--out", str(pack_path))
        self.assertEqual(code, 0)
        self.assertEqual(exported["leak_gate"], "pass")
        self.assertFalse(pack_path.read_text(encoding="utf-8").count("d--workspaces-unit-x"))
        tokens = [item["token"] for item in json.loads(pack_path.read_text(encoding="utf-8"))["items"]]
        code, result = self.run_cli("topic", "apply", "--topic", "Groma 搭建", "--from", str(pack_path),
                                    "--token", tokens[0], "--decided-by", "llm:qoder@auto")
        self.assertEqual((code, result["added"]), (0, 1))
        code, built = self.run_cli("topic", "reforge", "--topic", "Groma 搭建")
        self.assertEqual(code, 0)
        self.assertEqual(built["sessions"], 1)
        code, undone = self.run_cli("topic", "undo", "--topic", "Groma 搭建", "--from", str(pack_path),
                                    "--token", tokens[0], "--reason", "判断错了")
        self.assertEqual((code, undone["undone"], undone["dropped"]), (0, 1, 1))
        self.assertEqual(undone["still_served_by_asset"], built["asset_id"])
        code, _body = self.run_cli("topic", "reforge", "--topic", "Groma 搭建")
        self.assertEqual(code, 2)

    def test_apply_without_a_token_is_refused_rather_than_applying_the_whole_pack(self) -> None:
        pack_path = Path(self.tmp.name) / "pack.json"
        self.run_cli("topic", "candidates", "--out", str(pack_path))
        code, _body = self.run_cli("topic", "apply", "--topic", "Groma 搭建", "--from", str(pack_path))
        self.assertEqual(code, 2)
        self.assertEqual(topics.events(), [])

    def test_a_token_from_another_pack_is_refused(self) -> None:
        pack_path = Path(self.tmp.name) / "pack.json"
        self.run_cli("topic", "candidates", "--out", str(pack_path))
        code, _body = self.run_cli("topic", "apply", "--topic", "Groma 搭建", "--from", str(pack_path),
                                   "--token", "ev-notreal")
        self.assertEqual(code, 2)
        self.assertEqual(topics.list_topics(), [])

    def test_undo_refuses_to_run_half_a_request(self) -> None:
        pack_path = Path(self.tmp.name) / "pack.json"
        self.run_cli("topic", "candidates", "--out", str(pack_path))
        tokens = [item["token"] for item in json.loads(pack_path.read_text(encoding="utf-8"))["items"]]
        self.run_cli("topic", "apply", "--topic", "Groma 搭建", "--from", str(pack_path),
                     "--token", tokens[0], "--token", tokens[1])
        code, _body = self.run_cli("topic", "undo", "--topic", "Groma 搭建", "--from", str(pack_path),
                                   "--token", tokens[0], "--token", "ev-notreal")
        self.assertEqual(code, 2)
        self.assertEqual(len(topics.active_evidence(topics.resolve_topic("Groma 搭建")["topic_id"])), 2)

    def test_coverage_from_the_cli_names_its_denominator(self) -> None:
        code, report = self.run_cli("topic", "coverage")
        self.assertEqual(code, 0)
        self.assertEqual(report["kind"], reconcile.KIND)
        self.assertIn("NOT consulted", report["coverage"]["denominator_source"]["files"])

    def test_list_reports_the_classification_arithmetic(self) -> None:
        pack_path = Path(self.tmp.name) / "pack.json"
        self.run_cli("topic", "candidates", "--out", str(pack_path))
        tokens = [item["token"] for item in json.loads(pack_path.read_text(encoding="utf-8"))["items"]]
        self.run_cli("topic", "apply", "--topic", "Groma 搭建", "--from", str(pack_path), "--token", tokens[0])
        code, listed = self.run_cli("topic", "list")
        self.assertEqual(code, 0)
        self.assertEqual(listed["classification"], {"collected_sessions": 3, "classified": 1,
                                                     "unclassified": 2})
        self.assertEqual(listed["topics"][0]["active"], 1)


if __name__ == "__main__":
    unittest.main()
