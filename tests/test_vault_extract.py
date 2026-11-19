"""The producer: harness transcript -> vault records (docs/CONTEXT-VAULT.md §4, 裁决 3).

Fixtures are written here rather than read from the machine so the assertions stay
deterministic; a bounded real run is done separately and reported, not asserted.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lab.vault import (
    append_records,
    extract_transcript,
    session_dir,
    transcript_records,
)
from lab.workspace import LabError

KEY = "qoder-cn:11111111-2222-3333-4444-555555555555"


def line(role="user", blocks=None, ts="2026-11-09T08:00:00.000Z", kind=None):
    """One transcript line in the shape Qoder actually writes."""
    row = {"type": kind or role, "sessionId": KEY.split(":")[1], "timestamp": ts,
           "uuid": "u-" + ts[-8:-3], "cwd": "D:/workspaces/x", "isSidechain": False}
    # `blocks=[]` must mean an empty payload, not "use the default" -- the whole
    # point of the empty-line test.
    payload = blocks if blocks is not None else [{"type": "text", "text": "hello"}]
    if kind == "attachment":
        row["attachment"] = {"content": payload}
    else:
        row["message"] = {"role": role, "content": payload}
    return row


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name).resolve()
        self.root = self.base / "vault"
        self.root.mkdir()
        self.source = self.base / "transcript.jsonl"
        self.rel = "projects/d--workspaces-x/11111111-2222-3333-4444-555555555555.jsonl"
        self.addCleanup(self._dir.cleanup)

    def write(self, rows: list[dict]) -> None:
        with self.source.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read(self) -> list[dict]:
        with self.source.open("rb") as handle:
            return [json.loads(raw) for raw in handle if raw.strip()]


class WhichLines(Fixture):
    def test_only_content_lines_become_records(self) -> None:
        self.write([
            {"type": "workspace-directories", "sessionId": "x", "directories": []},
            {"type": "runtime-config", "sessionId": "x", "model": "m", "timestamp": 1793000876083},
            line(),
            {"type": "active-leaf", "sessionId": "x", "leafUuid": "l"},
            line(role="assistant", kind="assistant"),
        ])
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)
        self.assertEqual([r["role"] for r in records], ["user", "assistant"])

    def test_one_record_per_line_even_with_several_blocks(self) -> None:
        # The cursor is a per-file line number; two records sharing a line would
        # read as duplicates next round and silently disappear.
        self.write([line(blocks=[
            {"type": "thinking", "thinking": "planning"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}}])])
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "answer")
        self.assertEqual(records[0]["reasoning"], "planning")
        self.assertEqual(records[0]["tool"][0]["name"], "Bash")

    def test_a_line_with_no_payload_at_all_is_skipped(self) -> None:
        self.write([line(blocks=[{"type": "text", "text": "   "}]), line(blocks=[])])
        self.assertEqual(transcript_records(self.source, "qoder-cn", KEY, rel=self.rel), [])

    def test_attachment_lines_are_kept_and_labelled(self) -> None:
        self.write([line(kind="attachment")])
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)
        self.assertEqual(records[0]["role"], "attachment")


class NoiseAccounting(Fixture):
    def test_normalized_strips_wrappers_and_records_it(self) -> None:
        self.write([line(blocks=[{"type": "text", "text": "<system-reminder>be nice</system-reminder>\nreal question"}])])
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel, mode="normalized")
        self.assertEqual(records[0]["text"], "real question")
        self.assertEqual(records[0]["strip"], ["system-reminder"])

    def test_verbatim_keeps_every_byte_and_says_so(self) -> None:
        raw = "<system-reminder>be nice</system-reminder>\nreal question"
        self.write([line(blocks=[{"type": "text", "text": raw}])])
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel, mode="verbatim")
        self.assertEqual(records[0]["text"], raw)
        self.assertEqual(records[0]["strip"], [])

    def test_an_unterminated_wrapper_is_dropped_and_flagged(self) -> None:
        self.write([line(blocks=[{"type": "text", "text": "keep<system-reminder>dangling"}])])
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)
        self.assertEqual(records[0]["text"], "keep")
        self.assertEqual(records[0]["strip"], ["system-reminder-unterminated"])

    def test_tool_payloads_are_accounted_for_too(self) -> None:
        self.write([line(blocks=[{"type": "tool_result", "tool_use_id": "call_1",
                                  "content": "<system-reminder>noise</system-reminder>file listing"}])])
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)
        self.assertEqual(records[0]["tool"][0]["result"], "file listing")
        self.assertEqual(records[0]["strip"], ["system-reminder"])

    def test_an_unimplemented_mode_refuses_rather_than_quietly_doing_something_else(self) -> None:
        self.write([line()])
        with self.assertRaises(LabError) as caught:
            transcript_records(self.source, "qoder-cn", KEY, rel=self.rel, mode="redacted")
        self.assertIn("redacted", str(caught.exception))
        self.assertFalse(session_dir(self.root, "qoder-cn", KEY).exists())


class Fingerprints(Fixture):
    def test_source_fingerprint_is_the_raw_bytes_of_that_one_line(self) -> None:
        rows = [{"type": "runtime-config", "sessionId": "x"}, line(), line(role="assistant", kind="assistant")]
        self.write(rows)
        records = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)
        raw = self.source.read_bytes().split(b"\n")
        self.assertEqual(records[0]["source"]["line_no"], 2)
        self.assertEqual(records[0]["source"]["source_sha256"], hashlib.sha256(raw[1]).hexdigest())
        self.assertEqual(records[0]["source"]["bytes"], len(raw[1]))
        self.assertEqual(records[1]["source"]["line_no"], 3)

    def test_a_crlf_copy_of_the_same_transcript_anchors_the_same_way(self) -> None:
        self.write([line(), line(role="assistant", kind="assistant")])
        lf = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)
        crlf = self.base / "copy-crlf.jsonl"
        crlf.write_bytes(self.source.read_bytes().replace(b"\n", b"\r\n"))
        copied = transcript_records(crlf, "qoder-cn", KEY, rel=self.rel)
        self.assertEqual([r["source"]["source_sha256"] for r in lf],
                         [r["source"]["source_sha256"] for r in copied])
        self.assertEqual([r["source"]["bytes"] for r in lf], [r["source"]["bytes"] for r in copied])

    def test_the_relative_path_is_stored_not_the_absolute_one(self) -> None:
        self.write([line()])
        record = transcript_records(self.source, "qoder-cn", KEY, rel=self.rel)[0]
        self.assertEqual(record["source"]["path"], self.rel)
        self.assertNotIn(str(self.base), json.dumps(record, ensure_ascii=False))


class ExtractIntoVault(Fixture):
    def run_extract(self, mode="normalized"):
        return extract_transcript(self.root, "qoder-cn", KEY, self.source, rel=self.rel, mode=mode)

    def test_a_full_extract_lands_in_the_expected_shard(self) -> None:
        self.write([line(ts="2026-11-09T08:00:00.000Z"), line(role="assistant", kind="assistant",
                                                               ts="2026-11-09T08:00:05.000Z")])
        result = self.run_extract()
        self.assertEqual(result["written"], 2)
        self.assertEqual(result["shards"][0]["name"], "s000001_20261109.ndjson")
        where = session_dir(self.root, "qoder-cn", KEY)
        self.assertEqual(len([ln for ln in (where / "s000001_20261109.ndjson").read_bytes().split(b"\n") if ln.strip()]), 2)

    def test_re_extracting_the_same_file_writes_nothing(self) -> None:
        self.write([line(), line(role="assistant", kind="assistant")])
        first = self.run_extract()
        body = (session_dir(self.root, "qoder-cn", KEY) / first["shards"][0]["name"]).read_bytes()
        again = self.run_extract()
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["duplicates"], 2)
        self.assertEqual((session_dir(self.root, "qoder-cn", KEY) / first["shards"][0]["name"]).read_bytes(), body)

    def test_a_grown_transcript_appends_to_a_new_shard_only(self) -> None:
        self.write([line(ts="2026-11-09T08:00:00.000Z")])
        first = self.run_extract()
        where = session_dir(self.root, "qoder-cn", KEY)
        closed = (where / "s000001_20261109.ndjson").read_bytes()
        with self.source.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line(ts="2026-11-10T09:00:00.000Z"), ensure_ascii=False) + "\n")
        second = self.run_extract()
        self.assertEqual(second["written"], 1)
        self.assertEqual([s["name"] for s in second["shards"]], ["s000002_20261110.ndjson"])
        self.assertEqual((where / "s000001_20261109.ndjson").read_bytes(), closed)

    def test_a_line_without_a_timestamp_stops_the_extract(self) -> None:
        row = line()
        del row["timestamp"]
        self.write([row])
        with self.assertRaises(LabError):
            self.run_extract()
        self.assertFalse(session_dir(self.root, "qoder-cn", KEY).exists())

    def test_the_extracted_records_survive_the_index_layer(self) -> None:
        from lab.vault import reindex

        self.write([line(), line(kind="attachment")])
        self.run_extract()
        index = reindex(self.root)
        entry = index["sessions"][0]
        self.assertEqual(entry["session_key"], KEY)
        self.assertEqual(entry["paths"], [self.rel])
        self.assertEqual(index["drifted_shards"], [])
        self.assertEqual(index["orphan_shards"], [])

    def test_append_records_still_guards_against_a_foreign_key(self) -> None:
        self.write([line()])
        extract_transcript(self.root, "qoder-cn", KEY, self.source, rel=self.rel)
        with self.assertRaises(LabError):
            append_records(self.root, "qoder-cn", "qoder-cn:22222222-2222-2222-2222-222222222222",
                           transcript_records(self.source, "qoder-cn", KEY, rel=self.rel))


if __name__ == "__main__":
    unittest.main()
