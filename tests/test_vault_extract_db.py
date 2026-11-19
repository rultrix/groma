"""DB-only extraction (docs/CONTEXT-VAULT.md §5, C4). The bodies come from `local.db`.

`lab/qoder_db.py` has always read this database, but only COUNT / MIN / MAX / GROUP BY.
`message_rows` is the first place Groma reads `chat_message.content`, so these tests pin
what that new permission returns and where it is allowed to put it.
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lab.qoder_db import message_preview, message_rows
from lab.vault import (
    DB_STREAM,
    SEGMENT_FILE,
    extract_db_session,
    reindex,
    safe_dir,
    session_dir,
)
from lab.workspace import LabError

SID = "0611bcef-0000-4000-8000-000000000001"
KEY = "qoder-cn:" + SID


def make_db(path: Path, rows: list[tuple]) -> None:
    if path.exists():
        path.unlink()  # a fresh table each time: CREATE would collide with the last fixture
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE chat_message (id varchar(64), session_id varchar(64), role varchar(64),"
                " content TEXT, tool_result TEXT, extra TEXT, token_info TEXT, gmt_create INTEGER)")
    con.executemany("INSERT INTO chat_message (id, session_id, role, content, tool_result, extra, token_info,"
                    " gmt_create) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def row(mid="m1", role="user", content="问题正文", tool=None, ms=1762500000000, session=SID):
    return (mid, session, role, content, json.dumps(tool) if tool else None,
            json.dumps({"agent_version": "1"}), None, ms)


class DbReader(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name).resolve()
        self.db = self.base / "local.db"
        self.root = self.base / "vault"
        self.root.mkdir()
        self.addCleanup(self._dir.cleanup)

    def test_content_is_read_and_ordered_by_time(self) -> None:
        make_db(self.db, [row("m2", role="assistant", content="后", ms=1762500002000),
                          row("m1", content="先", ms=1762500001000)])
        rows = message_rows(self.db, SID)
        self.assertEqual([r["id"] for r in rows], ["m1", "m2"])
        self.assertEqual(rows[0]["content"], "先")

    def test_other_sessions_are_not_pulled_in(self) -> None:
        make_db(self.db, [row("m1"), row("m9", content="别人的", session="other-session")])
        self.assertEqual([r["id"] for r in message_rows(self.db, SID)], ["m1"])

    def test_preview_counts_bytes_without_reading_a_body(self) -> None:
        make_db(self.db, [row("m1", content="x" * 500), row("m2", role="tool", content="", tool={"results": "y" * 300})])
        preview = message_preview(self.db, [SID])
        self.assertEqual(preview["sessions"], 1)
        self.assertEqual(preview["rows"], 2)
        self.assertEqual(preview["content_bytes"], 500)
        self.assertEqual(preview["tool_result_bytes"], len(json.dumps({"results": "y" * 300})))

    def materialise(self, rows) -> list[dict]:
        """Tuples -> a real DB -> `message_rows`, so the extractor sees production dicts."""
        if self.db.exists():
            self.db.unlink()
        make_db(self.db, list(rows))
        return message_rows(self.db, SID)

    def test_preview_of_nothing_is_zero_not_missing(self) -> None:
        self.assertEqual(message_preview(self.db, [])["sessions"], 0)


class RecordMapping(DbReader):
    def extract(self, rows, mode="normalized"):
        return extract_db_session(self.root, KEY, self.materialise(rows), mode=mode)

    def test_epoch_ms_becomes_an_iso_utc_stamp(self) -> None:
        result = self.extract([row("m1", ms=1762500000000)])
        self.assertEqual(result["written"], 1)
        record = json.loads(next((self.root / "qoder-cn" / safe_dir(KEY)).glob("s*.ndjson")).read_bytes().split(b"\n")[0])
        self.assertTrue(record["ts_utc"].endswith("Z"))
        parsed = datetime.fromisoformat(record["ts_utc"].replace("Z", "+00:00"))
        self.assertEqual(int(parsed.timestamp() * 1000), 1762500000000)

    def test_the_locator_names_the_stream_and_the_row(self) -> None:
        self.extract([row("m1")])
        record = json.loads(next((self.root / "qoder-cn" / safe_dir(KEY)).glob("s*.ndjson")).read_bytes().split(b"\n")[0])
        self.assertEqual(record["source"]["path"], DB_STREAM)
        self.assertEqual(record["source"]["row_id"], "m1")
        self.assertEqual(record["source"]["line_no"], 1)
        self.assertNotIn("local.db", str(record["source"]["path"]).replace(DB_STREAM, ""))
        self.assertNotIn(str(self.base), json.dumps(record))

    def test_the_fingerprint_hashes_the_whole_row_as_stored(self) -> None:
        rows = [row("m1", content="内容", tool={"results": "输出"})]
        self.extract(rows)
        record = json.loads(next((self.root / "qoder-cn" / safe_dir(KEY)).glob("s*.ndjson")).read_bytes().split(b"\n")[0])
        # the reader hands back dicts keyed by column, so hash that exact mapping
        from lab.qoder_db import MESSAGE_BODY_COLUMNS
        as_dict = dict(zip(MESSAGE_BODY_COLUMNS, rows[0]))
        expected = hashlib.sha256(json.dumps(as_dict, ensure_ascii=False, sort_keys=True,
                                             default=str).encode("utf-8")).hexdigest()
        self.assertEqual(record["source"]["source_sha256"], expected)

    def test_a_tool_row_keeps_the_vendor_shape_and_lifts_the_three_fields(self) -> None:
        payload = {"toolCallId": "call_7", "name": "search", "parameters": {"q": "x"},
                   "results": [{"file": "a.py"}], "rawData": "…"}
        self.extract([row("m1", role="tool", content="", tool=payload)])
        record = json.loads(next((self.root / "qoder-cn" / safe_dir(KEY)).glob("s*.ndjson")).read_bytes().split(b"\n")[0])
        self.assertEqual(record["type"], "tool")
        self.assertEqual(record["tool"]["call_id"], "call_7")
        self.assertEqual(record["tool"]["input"], {"q": "x"})
        self.assertEqual(record["tool"]["result"], [{"file": "a.py"}])
        self.assertEqual(record["tool"]["raw"], payload)

    def test_an_empty_tool_result_column_is_not_a_tool(self) -> None:
        make_db(self.db, [("m1", SID, "assistant", "只有文字", "", None, None, 1762500000000)])
        extract_db_session(self.root, KEY, message_rows(self.db, SID))
        record = json.loads(next((self.root / "qoder-cn" / safe_dir(KEY)).glob("s*.ndjson")).read_bytes().split(b"\n")[0])
        self.assertIsNone(record["tool"])
        self.assertEqual(record["type"], "message")

    def test_a_row_with_no_text_and_no_tool_is_counted_not_written(self) -> None:
        result = self.extract([row("m1", content="   "), row("m2", content="有内容")])
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["skipped"]["empty"], 1)

    def test_missing_timestamp_is_refused_rather_than_filed_under_today(self) -> None:
        with self.assertRaises(LabError):
            self.extract([(  "m1", SID, "user", "x", None, None, None, None)])
        self.assertFalse(session_dir(self.root, "qoder-cn", KEY).exists())

    def test_normalized_strips_wrappers_and_verbatim_does_not(self) -> None:
        rows = [row("m1", content="<system-reminder>注入</system-reminder>真问题")]
        self.extract(rows, mode="normalized")
        where = session_dir(self.root, "qoder-cn", KEY)
        normalized = json.loads(next(where.glob("s*.ndjson")).read_bytes().split(b"\n")[0])
        self.assertEqual(normalized["text"], "真问题")
        self.assertEqual(normalized["strip"], ["system-reminder"])

        root2 = self.base / "vault2"
        root2.mkdir()
        extract_db_session(root2, KEY, self.materialise(rows), mode="verbatim")
        raw = json.loads(next(session_dir(root2, "qoder-cn", KEY).glob("s*.ndjson")).read_bytes().split(b"\n")[0])
        self.assertIn("<system-reminder>", raw["text"])
        self.assertEqual(raw["strip"], [])


class CursorBehaviour(DbReader):
    def test_replaying_the_same_rows_writes_nothing(self) -> None:
        rows = self.materialise([row("m1", ms=1762500001000),
                                 row("m2", role="assistant", content="答", ms=1762500002000)])
        first = extract_db_session(self.root, KEY, rows)
        where = session_dir(self.root, "qoder-cn", KEY)
        body = (where / first["shards"][0]["name"]).read_bytes()
        again = extract_db_session(self.root, KEY, rows)
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["duplicates"], 2)
        self.assertEqual((where / first["shards"][0]["name"]).read_bytes(), body)

    def test_a_new_message_continues_the_cursor_into_a_new_shard(self) -> None:
        rows = self.materialise([row("m1", ms=1762500001000)])
        first = extract_db_session(self.root, KEY, rows)
        where = session_dir(self.root, "qoder-cn", KEY)
        closed = (where / first["shards"][0]["name"]).read_bytes()
        make_db(self.db, [("m1", SID, "user", "问题正文", None, json.dumps({"agent_version": "1"}), None, 1762500001000),
                          ("m2", SID, "assistant", "回答", None, None, None, 1762500009000)])
        second = extract_db_session(self.root, KEY, message_rows(self.db, SID))
        self.assertEqual(second["written"], 1)
        self.assertEqual(second["shards"][0]["name"], "s000002_20251107.ndjson")
        self.assertEqual((where / "s000001_20251107.ndjson").read_bytes(), closed)
        segment = json.loads((where / SEGMENT_FILE).read_text(encoding="utf-8"))
        self.assertEqual(segment["covered"][DB_STREAM], 2)

    def test_a_foreign_key_still_cannot_share_the_directory(self) -> None:
        rows = self.materialise([row("m1")])
        extract_db_session(self.root, KEY, rows)
        # Same id, other case: `safe_dir` folds case on every OS, so this is the one
        # shape that would land in the directory another session already owns.
        with self.assertRaises(LabError):
            extract_db_session(self.root, "qoder-cn:" + SID.upper(), rows)


class IndexAndLeakage(DbReader):
    def test_the_index_sees_a_db_only_session_like_any_other(self) -> None:
        extract_db_session(self.root, KEY, self.materialise(
            [row("m1"), row("m2", role="assistant", content="答")]))
        index = reindex(self.root)
        entry = index["sessions"][0]
        self.assertEqual(entry["session_key"], KEY)
        self.assertEqual(entry["paths"], [DB_STREAM])
        self.assertEqual(index["drifted_shards"], [])
        self.assertEqual(index["orphan_shards"], [])

    def test_writing_a_vault_touches_nothing_outside_the_vault_root(self) -> None:
        extract_db_session(self.root, KEY, self.materialise([row("m1", content="哨兵文字ZZ")]))
        written = sorted(str(p.relative_to(self.base)).replace("\\", "/") for p in self.base.rglob("*") if p.is_file())
        self.assertTrue(all(part.startswith("vault/") or part.endswith("local.db") for part in written), written)
        self.assertTrue(any("s000001_" in part for part in written))


if __name__ == "__main__":
    unittest.main()
