"""Immutable shard writing (docs/CONTEXT-VAULT.md §4, §5, §6, §11).

Every test builds records by hand: nothing here reads a harness, so a red test
points at the writer, not at some parser.
"""

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from lab import vault
from lab.workspace import LabError


KEY = "qoder:11111111-2222-3333-4444-555555555555"


def record(line_no: int, day: str = "2026-11-05", text: str = "hello", session_key: str = KEY) -> dict:
    stamp = "%sT08:%02d:00Z" % (day, line_no % 60)
    return {
        "ts_utc": stamp,
        "granularity": "event",
        "session_key": session_key,
        "role": "user",
        "type": "message",
        "text": text,
        "reasoning": None,
        "tool": None,
        "strip": [],
        "agent_id": "agt-1",
        "source": {
            "path": "projects/x.jsonl",
            "line_no": line_no,
            "bytes": 128,
            "source_sha256": hashlib.sha256(b"%d" % line_no).hexdigest(),
        },
    }


class ShardWriter(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name).resolve() / "vault"
        self.root.mkdir(parents=True)
        self.addCleanup(self._dir.cleanup)
        self.key = KEY

    def write(self, *records, **kwargs) -> dict:
        return vault.append_records(self.root, "qoder", self.key, list(records), **kwargs)

    def session(self) -> Path:
        return vault.session_dir(self.root, "qoder", self.key)

    def lines(self, name: str) -> list[dict]:
        body = (self.session() / name).read_bytes()
        return [json.loads(line) for line in body.split(b"\n") if line]

    def test_records_land_in_one_shard_named_by_counter_then_day(self) -> None:
        result = self.write(record(1), record(2))
        self.assertEqual(result["written"], 2)
        self.assertEqual([s["name"] for s in result["shards"]], ["s000001_20261105.ndjson"])
        self.assertEqual(len(self.lines("s000001_20261105.ndjson")), 2)

    def test_the_colon_key_never_becomes_a_directory_name(self) -> None:
        self.write(record(1))
        self.assertEqual(self.session().name, "11111111-2222-3333-4444-555555555555")
        self.assertNotIn(":", self.session().name)

    def test_line_endings_are_lf_only_so_the_hash_survives_a_platform_move(self) -> None:
        self.write(record(1), record(2))
        body = (self.session() / "s000001_20261105.ndjson").read_bytes()
        self.assertNotIn(b"\r", body)
        self.assertTrue(body.endswith(b"\n"))

    def test_seq_is_assigned_by_the_writer_and_both_hashes_are_kept(self) -> None:
        self.write(record(7))
        stored = self.lines("s000001_20261105.ndjson")[0]
        self.assertEqual(stored["seq"], 1)
        self.assertEqual(stored["source"]["line_no"], 7)
        self.assertEqual(stored["source"]["source_sha256"], hashlib.sha256(b"7").hexdigest())
        self.assertEqual(stored["stored_sha256"], vault.stored_sha256(stored))

    def test_the_two_hashes_are_not_the_same_number(self) -> None:
        self.write(record(7))
        stored = self.lines("s000001_20261105.ndjson")[0]
        self.assertNotEqual(stored["stored_sha256"], stored["source"]["source_sha256"])

    def test_canonical_bytes_do_not_depend_on_insertion_order_or_indent(self) -> None:
        a = vault.canonical_bytes({"b": 1, "a": "值"})
        b = vault.canonical_bytes({"a": "值", "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a, b'{"a":"\xe5\x80\xbc","b":1}')

    def test_the_shard_hash_is_what_the_bytes_actually_hash_to(self) -> None:
        self.write(record(1), record(2))
        shard = self.session() / "s000001_20261105.ndjson"
        self.assertEqual(vault.file_sha256(shard), hashlib.sha256(shard.read_bytes()).hexdigest())

    def test_replaying_a_batch_writes_nothing_and_changes_no_bytes(self) -> None:
        first = self.write(record(1), record(2))
        before = (self.session() / first["shards"][0]["name"]).read_bytes()
        again = self.write(record(1), record(2))
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["duplicates"], 2)
        self.assertEqual(again["shards"], [])
        self.assertEqual((self.session() / first["shards"][0]["name"]).read_bytes(), before)

    def test_a_batch_with_nothing_new_creates_no_directory_at_all(self) -> None:
        other = "qoder:ffffffff-0000-0000-0000-000000000000"
        result = vault.append_records(self.root, "qoder", other, [])
        self.assertEqual(result["written"], 0)
        self.assertFalse(vault.session_dir(self.root, "qoder", other).exists())

    def test_a_later_capture_opens_a_new_shard_and_the_old_one_stays_closed(self) -> None:
        first = self.write(record(1), record(2))
        digest = first["shards"][0]["file_sha256"]
        body = (self.session() / "s000001_20261105.ndjson").read_bytes()
        second = self.write(record(3))
        self.assertEqual([s["name"] for s in second["shards"]], ["s000002_20261105.ndjson"])
        self.assertEqual((self.session() / "s000001_20261105.ndjson").read_bytes(), body)
        segment = json.loads((self.session() / "segment.json").read_text(encoding="utf-8"))
        self.assertEqual(segment["shards"][0]["file_sha256"], digest)

    def test_two_batches_on_the_same_day_do_not_collide(self) -> None:
        self.write(record(1))
        second = self.write(record(2))
        self.assertEqual(second["shards"][0]["name"], "s000002_20261105.ndjson")

    def test_a_batch_spanning_utc_days_splits_at_the_boundary(self) -> None:
        result = self.write(record(1, day="2026-11-05"), record(2, day="2026-11-06"))
        self.assertEqual([s["name"] for s in result["shards"]],
                         ["s000001_20261105.ndjson", "s000002_20261106.ndjson"])
        self.assertEqual(len(self.lines("s000001_20261105.ndjson")), 1)
        self.assertEqual(len(self.lines("s000002_20261106.ndjson")), 1)

    def test_size_rotation_splits_the_same_day_without_losing_records(self) -> None:
        result = self.write(record(1), record(2), record(3), shard_bytes_limit=200)
        self.assertGreater(len(result["shards"]), 1)
        self.assertEqual(sum(s["records"] for s in result["shards"]), 3)
        self.assertTrue(all(s["bytes"] <= 200 or s["records"] == 1 for s in result["shards"]))

    def test_the_cursor_is_per_source_file(self) -> None:
        a, b = record(1), record(2)
        b["source"] = dict(b["source"], path="projects/other.jsonl", line_no=1)
        self.write(a, b)
        again = self.write(a, b)
        self.assertEqual(again["written"], 0)

    def test_segment_records_the_cursor_and_whats_covered(self) -> None:
        self.write(record(1), record(5))
        segment = json.loads((self.session() / "segment.json").read_text(encoding="utf-8"))
        self.assertEqual(segment["covered"]["projects/x.jsonl"], 5)
        self.assertEqual(segment["extracted_through"]["seq"], 2)
        self.assertEqual(segment["next_shard"], 2)
        self.assertEqual(segment["session_key"], self.key)

    def test_a_refused_record_writes_nothing(self) -> None:
        bad = record(1)
        del bad["source"]["source_sha256"]
        with self.assertRaises(LabError):
            self.write(bad)
        self.assertFalse(self.session().exists())

    def test_missing_required_fields_are_named_not_guessed(self) -> None:
        for field in ("ts_utc", "granularity", "session_key", "source"):
            broken = record(1)
            broken.pop(field)
            with self.subTest(field=field):
                with self.assertRaises(LabError) as caught:
                    self.write(broken)
                self.assertIn(field, str(caught.exception))

    def test_an_unknown_granularity_is_refused(self) -> None:
        broken = record(1)
        broken["granularity"] = "paragraph"
        with self.assertRaises(LabError):
            self.write(broken)

    def test_a_record_from_another_session_cannot_enter_this_shard(self) -> None:
        stolen = record(1, session_key="qoder:99999999-0000-0000-0000-000000000000")
        with self.assertRaises(LabError):
            self.write(stolen)

    def test_an_unparseable_timestamp_is_refused_rather_than_rotated_to_day_zero(self) -> None:
        broken = record(1)
        broken["ts_utc"] = "yesterday"
        with self.assertRaises(LabError):
            self.write(broken)

    def test_a_shard_on_disk_the_cursor_does_not_know_about_is_reported_not_rewritten(self) -> None:
        self.session().mkdir(parents=True)
        (self.session() / "s000001_20261105.ndjson").write_bytes(b"already here\n")
        with self.assertRaises(LabError) as caught:
            self.write(record(1))
        self.assertIn("segment.json", str(caught.exception))
        self.assertEqual((self.session() / "s000001_20261105.ndjson").read_bytes(), b"already here\n")

    def test_a_corrupt_segment_is_refused_with_its_path(self) -> None:
        self.write(record(1))
        segment_path = self.session() / "segment.json"
        segment_path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(LabError) as caught:
            self.write(record(2))
        self.assertIn("segment.json", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "chmod has no effect on a Windows volume here")
    def test_shards_are_not_readable_by_other_users(self) -> None:
        self.write(record(1))
        shard = self.session() / "s000001_20261105.ndjson"
        self.assertEqual(stat.S_IMODE(shard.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
