from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import os
import tempfile
import unittest
from pathlib import Path

from lab import asset, classify, state, topics, unitmatch
from lab.workspace import init_workspace, load_unit


def write_digest(root: Path, rows: list[dict]) -> Path:
    folder = root / ".groma" / "digests"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "kind": "GROMA_LOCAL_DIGEST",
        "created_at": "2026-11-07T09:00:00Z",
        "unit_id": load_unit(root)["unit_id"],
        "workspace_root": root.as_posix(),
        "file_count": len(rows),
        "event_count": sum(row.get("events") or 0 for row in rows),
        "parse_errors": 0,
        "files": rows,
    }
    path = folder / "digest-20261107T090000Z.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


ROWS = [
    {"source": "qoder", "path": "projects/d--workspaces-unit-x/fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3.jsonl",
     "harness": "qoder-cn", "agent_kind": "primary", "events": 900,
     "first_ts": "2026-11-01T09:00:00Z", "last_ts": "2026-11-01T12:00:00Z"},
    {"source": "codex", "path": "sessions/2026/11/07/rollout-2026-11-07T09-00-00-019feb06-25ea-7d21-9986-8d57ff1da526.jsonl",
     "harness": "codex", "agent_kind": "primary", "events": 7722,
     "first_ts": "2026-08-10T09:34:37Z", "last_ts": "2026-08-12T05:00:00Z"},
]


class IsolatedState(unittest.TestCase):
    """Topics live in the machine state dir, so each test needs its own one."""

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


class TopicEvidenceTests(IsolatedState):
    def setUp(self) -> None:
        super().setUp()
        self.topic = topics.create_topic("Groma 搭建")

    def known(self) -> set[tuple[str, str]]:
        return {(row["source"], row["path"]) for row in ROWS}

    def test_anchor_missing_fields_is_refused_and_writes_nothing(self) -> None:
        result = topics.select(self.topic["topic_id"], [{"path": "x.jsonl"}], decided_by="human",
                              known_keys=self.known())
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("无锚点", result["rejected"][0]["reason"])
        self.assertEqual(topics.events(self.topic["topic_id"]), [])

    def test_anchor_not_in_any_digest_is_refused(self) -> None:
        ghost = [{"source": "qoder", "path": "projects/made-up-session.jsonl"}]
        result = topics.select(self.topic["topic_id"], ghost, decided_by="llm:qoder@auto",
                              known_keys=self.known())
        self.assertEqual(result["added"], 0)
        self.assertIn("不存在于任何 digest", result["rejected"][0]["reason"])

    def test_reapplying_the_same_pack_does_not_grow_membership(self) -> None:
        items = [{"source": row["source"], "path": row["path"]} for row in ROWS]
        first = topics.select(self.topic["topic_id"], items, decided_by="llm:qoder@auto",
                             known_keys=self.known())
        self.assertEqual(first["added"], 2)
        topics.select(self.topic["topic_id"], items, decided_by="llm:qoder@auto", known_keys=self.known())
        self.assertEqual(len(topics.active_evidence(self.topic["topic_id"])), 2)

    def test_undo_leaves_both_events_and_frees_the_session(self) -> None:
        items = [{"source": ROWS[0]["source"], "path": ROWS[0]["path"]}]
        topics.select(self.topic["topic_id"], items, decided_by="llm:qoder@auto", known_keys=self.known())
        topics.undo(self.topic["topic_id"], [(ROWS[0]["source"], ROWS[0]["path"])],
                   decided_by="human", reason="误召")
        self.assertEqual(topics.active_evidence(self.topic["topic_id"]), [])
        actions = [event["action"] for event in topics.events(self.topic["topic_id"])]
        self.assertEqual(actions, ["select", "undo"])

    def test_unclassified_is_the_only_proof_of_a_miss(self) -> None:
        counts = topics.classification_counts(ROWS)
        self.assertEqual(counts["classified"], 0)
        self.assertEqual(counts["unclassified"], 2)
        self.assertEqual(len(topics.unclassified(ROWS)), 2)
        topics.select(self.topic["topic_id"], [{"source": ROWS[1]["source"], "path": ROWS[1]["path"]}],
                     decided_by="llm:qoder@auto", known_keys=self.known())
        self.assertEqual(topics.classification_counts(ROWS),
                         {"collected_sessions": 2, "classified": 1, "unclassified": 1})


class CandidatePackTests(IsolatedState):
    def setUp(self) -> None:
        super().setUp()
        self.pack = classify.candidates(self.root)

    def test_pack_carries_no_absolute_path_or_workspace_slug(self) -> None:
        exported = json.dumps({k: v for k, v in self.pack.items() if k != "_registry"}, ensure_ascii=False)
        self.assertNotIn("workspaces-unit-x", exported)
        self.assertNotIn(self.tmp.name.replace("\\", "/"), exported)
        self.assertEqual(classify.assert_no_leak(self.pack), [])

    def test_tokens_resolve_to_the_digest_anchor(self) -> None:
        tokens = [item["token"] for item in self.pack["items"]]
        known, unknown = classify.resolve(self.pack, tokens)
        self.assertEqual(unknown, [])
        self.assertEqual({(row["source"], row["path"]) for row in known},
                         {(row["source"], row["path"]) for row in ROWS})

    def test_a_token_from_a_stale_pack_is_refused_not_guessed(self) -> None:
        stale = self.pack["items"][0]["token"] + "0"
        known, unknown = classify.resolve(self.pack, [stale])
        self.assertEqual(known, [])
        self.assertEqual(len(unknown), 1)

    def test_recall_states_how_much_the_classifier_actually_sees(self) -> None:
        recall = self.pack["recall"]
        self.assertEqual(recall["candidates"], 2)
        self.assertIn("title_coverage", recall)
        self.assertTrue(recall["note"])

    def test_signals_are_metadata_only(self) -> None:
        for item in self.pack["items"]:
            keys = {signal.split("=")[0] for signal in item["signals"]}
            self.assertTrue(keys <= {"harness", "workspace", "agent_kind", "events",
                                     "session_type", "has_parent"}, keys)
            for signal in item["signals"]:
                value = signal.split("=", 1)[1]
                self.assertNotIn("\\", value)
                self.assertNotIn(".jsonl", value)
                self.assertNotIn("/", value)


class TitleJoinTests(IsolatedState):
    """The title join used to walk the workspace and hit a fixture database."""

    def make_db(self, path: Path, title: str) -> None:
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path))
        connection.execute(
            "CREATE TABLE chat_session (session_id TEXT, session_title TEXT,"
            " session_type TEXT, parent_session_id TEXT)"
        )
        connection.execute(
            "INSERT INTO chat_session VALUES (?, ?, ?, ?)",
            ("fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3", title, "assistant", None),
        )
        connection.commit()
        connection.close()

    def test_titles_come_from_the_declared_root_not_a_workspace_fixture(self) -> None:
        harness = Path(self.tmp.name) / "harness-qoder"
        self.make_db(harness / "local.db", "主题召回用的真标题")
        self.make_db(self.root / "work" / "_p0_stage" / "aliceX" / ".qoder" / "local.db", "夹具的别人的标题")
        from lab.sources import add_source

        add_source(self.root, "qoder", str(harness), harness="qoder-cn", include=["projects"])
        pack = classify.candidates(self.root)
        wanted = classify.token_of(ROWS[0]["source"], ROWS[0]["path"])
        item = next(entry for entry in pack["items"] if entry["token"] == wanted)
        self.assertEqual(item["title"], "主题召回用的真标题")
        self.assertEqual(pack["recall"]["with_title"], 1)

    def test_a_title_that_quotes_a_path_is_masked_and_flagged(self) -> None:
        harness = Path(self.tmp.name) / "harness-path"
        self.make_db(harness / "local.db", "把 D:/workspaces/secret-project 里的东西整理一下")
        from lab.sources import add_source

        add_source(self.root, "qoder", str(harness), harness="qoder-cn", include=["projects"])
        pack = classify.candidates(self.root)
        wanted = classify.token_of(ROWS[0]["source"], ROWS[0]["path"])
        item = next(entry for entry in pack["items"] if entry["token"] == wanted)
        exported = json.dumps({k: v for k, v in pack.items() if k != "_registry"}, ensure_ascii=False)
        self.assertIn(classify.MASK, item["title"])
        self.assertNotIn("secret-project", exported)
        self.assertIn("title_scrubbed=true", item["signals"])
        self.assertEqual(classify.assert_no_leak(pack), [])

    def test_a_workspace_without_a_declared_qoder_source_has_no_titles(self) -> None:
        self.make_db(self.root / "work" / "_p0_stage" / "aliceX" / ".qoder" / "local.db", "夹具的别人的标题")
        pack = classify.candidates(self.root)
        self.assertEqual(pack["recall"]["with_title"], 0)


class IntentTests(IsolatedState):
    """Name is how you refer to it; intent is the rule for what belongs."""

    def unit(self, name: str) -> dict:
        folder = Path(self.tmp.name) / ("unit-" + str(len(list((Path(self.tmp.name)).glob("unit-*")))))
        folder.mkdir()
        return unitmatch.create_unit(name, folder)

    def test_intent_is_optional_at_creation_and_stored_when_given(self) -> None:
        self.unit("只有名字")
        bare = topics.create_topic("只有名字")
        self.assertIsNone(bare["intent"])
        self.unit("带说明")
        named = topics.create_topic("带说明", intent="包含采集与对账；不含服务端业务开发")
        self.assertEqual(named["intent"], "包含采集与对账；不含服务端业务开发")
        self.assertEqual(named["intent_by"], "human")

    def test_setting_intent_rewrites_only_that_topic(self) -> None:
        self.unit("甲")
        first = topics.create_topic("甲")
        self.unit("乙")
        topics.create_topic("乙", intent="乙的说明")
        updated = topics.set_intent("甲", "甲的新说明")
        self.assertEqual(updated["intent"], "甲的新说明")
        rows = {row["name"]: row for row in topics.list_topics()}
        self.assertEqual(rows["乙"]["intent"], "乙的说明")
        self.assertEqual(topics.topic_record(first["topic_id"])["intent"], "甲的新说明")

    def test_an_empty_string_clears_the_intent(self) -> None:
        self.unit("丙")
        topics.create_topic("丙", intent="先写着")
        self.assertIsNone(topics.set_intent("丙", "   ")["intent"])

    def test_setting_intent_on_an_unknown_topic_refuses(self) -> None:
        with self.assertRaises(ValueError):
            topics.set_intent("没有这个主题", "随便")

    def test_a_topic_written_before_the_field_existed_reads_as_none(self) -> None:
        import json as _json
        from lab import state
        path = state.state_root() / topics.TOPICS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"schema": 1, "topics": [
            {"topic_id": "topic_legacy", "name": "老主题", "org_path": None,
             "origin": "local", "created_at": "2026-01-01T00:00:00Z"}]}, ensure_ascii=False),
            encoding="utf-8", newline="\n")
        self.assertIsNone(topics.list_topics()[0]["intent"])
        self.assertEqual(topics.topic_record("topic_legacy")["intent"], None)

    def test_changing_intent_does_not_touch_existing_evidence(self) -> None:
        self.unit("丁")
        topic = topics.create_topic("丁", intent="旧的说明")
        items = [{"source": ROWS[0]["source"], "path": ROWS[0]["path"]}]
        topics.select(topic["topic_id"], items, decided_by="human",
                      known_keys={(ROWS[0]["source"], ROWS[0]["path"])})
        topics.set_intent("丁", "新的说明")
        self.assertEqual(len(topics.active_evidence(topic["topic_id"])), 1)


if __name__ == "__main__":
    unittest.main()


class StaleEvidenceTests(IsolatedState):
    """刀 W26a0：键还在账上、但它已不在 digest 引用索引里时，重铸要跳过并报数。"""

    def setUp(self) -> None:
        super().setUp()
        self.topic = topics.create_topic("Groma 搭建")
        self.keys = [(row["source"], row["path"]) for row in ROWS]
        topics.select(self.topic["topic_id"],
                      [{"source": source, "path": path} for source, path in self.keys],
                      decided_by="llm:qoder@auto", known_keys=set(self.keys))

    def drop_from_digest(self, *indexes: int) -> None:
        keep = [row for index, row in enumerate(ROWS) if index not in indexes]
        write_digest(self.root, keep)

    def test_a_key_that_left_the_digest_is_skipped_not_fatal(self) -> None:  # A0-1
        self.drop_from_digest(1)
        built = topics.reforge(self.root, self.topic["topic_id"])
        self.assertEqual(built["skipped_stale"], 1)
        self.assertEqual(built["sessions"], 1)
        loaded = asset.load_asset(self.root, built["asset_id"])
        cited = {(item["source"], item["path"]) for item in loaded["sessions"]}
        self.assertEqual(cited, {self.keys[0]})

    def test_when_every_key_is_stale_reforge_says_so_instead_of_raising(self) -> None:  # A0-2
        self.drop_from_digest(0, 1)
        built = topics.reforge(self.root, self.topic["topic_id"])
        self.assertIsNone(built["asset_id"])
        self.assertEqual(built["skipped_stale"], 2)
        self.assertIn("没有可重铸的依据", built["reason"])

    def test_the_skip_count_lands_in_the_ledger_not_just_the_return_value(self) -> None:  # A0-4
        self.drop_from_digest(1)
        topics.reforge(self.root, self.topic["topic_id"])
        record = topics.last_reforge(self.topic["topic_id"])
        self.assertEqual(record["skipped_stale"], 1)
        self.assertEqual(sum(record["skipped"].values()), 1)

    def test_undo_still_changes_the_context(self) -> None:  # B0-1
        topics.undo(self.topic["topic_id"], [self.keys[1]], decided_by="human", reason="误召")
        self.assertEqual(topics.asset_selection(self.topic["topic_id"]), [self.keys[0]])
