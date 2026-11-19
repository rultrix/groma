"""The derived index (docs/CONTEXT-VAULT.md §5 key space, §6 index + reindex, C5).

`reindex` is the only place allowed to walk the vault tree; everything upstream
reads `index.json` or gets an error.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from lab import vault
from lab.workspace import LabError

KEY = "qoder:11111111-2222-3333-4444-555555555555"
PARENT = "fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3"


def record(line_no, session_key=KEY, day="2026-11-09", path=None):
    return {
        "ts_utc": "%sT08:%02d:00Z" % (day, line_no % 60),
        "granularity": "event",
        "session_key": session_key,
        "role": "user",
        "type": "message",
        "text": "x" * (line_no * 5),
        "reasoning": None,
        "tool": None,
        "strip": [],
        "agent_id": "agt-1",
        "source": {
            "path": path or "projects/ws/transcript/%s.jsonl" % session_key.split(":")[1],
            "line_no": line_no,
            "bytes": 100,
            "source_sha256": "f" * 64,
        },
    }


class IndexLayer(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name).resolve() / "vault"
        self.root.mkdir(parents=True)
        self.addCleanup(self._dir.cleanup)

    def write(self, *records, key=KEY, harness="qoder"):
        return vault.append_records(self.root, harness, key, list(records))

    def session_dir(self, harness="qoder", key=KEY):
        return vault.session_dir(self.root, harness, key)

    def strip_time(self, index):
        return {k: v for k, v in index.items() if k != "generated_at"}

    def test_two_batches_become_one_entry_with_two_shards(self) -> None:
        self.write(record(1), record(2))
        self.write(record(3))
        index = vault.reindex(self.root)
        self.assertEqual(index["session_count"], 1)
        self.assertEqual(index["shard_count"], 2)
        entry = index["sessions"][0]
        self.assertEqual([s["name"] for s in entry["shards"]],
                         ["s000001_20261109.ndjson", "s000002_20261109.ndjson"])
        self.assertEqual(entry["bytes_total"], sum(s["bytes"] for s in entry["shards"]))
        self.assertTrue(all(s["verified"] for s in entry["shards"]))

    def test_head_and_tail_hashes_are_recomputable_from_the_shard(self) -> None:
        self.write(record(1), record(2))
        entry = vault.reindex(self.root)["sessions"][0]
        shard = entry["shards"][0]
        self.assertIsNotNone(shard["stored_sha256_head"])
        self.assertEqual(shard["stored_sha256_head"], shard["stored_sha256_head"])
        lines = [json.loads(ln) for ln in
                 (self.session_dir() / shard["name"]).read_bytes().split(b"\n") if ln.strip()]
        self.assertEqual(shard["stored_sha256_head"], vault.stored_sha256(lines[0]))
        self.assertEqual(shard["stored_sha256_tail"], vault.stored_sha256(lines[-1]))

    def test_reindex_is_reproducible_on_the_fields_6_lists(self) -> None:
        self.write(record(1))
        self.write(record(2, day="2026-11-10"))
        first = self.strip_time(vault.reindex(self.root))
        (self.root / "index.json").unlink()
        rebuilt = self.strip_time(vault.reindex(self.root))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(rebuilt, sort_keys=True))
        # Repeated reindex over the same tree must not drift either.
        again = self.strip_time(vault.reindex(self.root))
        self.assertEqual(json.dumps(rebuilt, sort_keys=True), json.dumps(again, sort_keys=True))

    def test_a_missing_index_is_an_error_not_an_empty_vault(self) -> None:
        self.write(record(1))
        vault.reindex(self.root)
        (self.root / "index.json").unlink()
        with self.assertRaises(LabError) as caught:
            vault.read_index(self.root)
        self.assertIn("reindex", str(caught.exception))

    def test_read_index_returns_what_reindex_wrote(self) -> None:
        self.write(record(1))
        built = vault.reindex(self.root)
        self.assertEqual(vault.read_index(self.root), built)

    def test_a_tampered_shard_is_reported_not_trusted(self) -> None:
        self.write(record(1))
        vault.reindex(self.root)
        shard = self.session_dir() / "s000001_20261109.ndjson"
        shard.write_bytes(shard.read_bytes() + json.dumps(record(9), ensure_ascii=False).encode() + b"\n")
        index = vault.reindex(self.root)
        self.assertEqual(len(index["drifted_shards"]), 1)
        drift = index["drifted_shards"][0]
        self.assertNotEqual(drift["recorded"], drift["actual"])
        self.assertFalse(index["sessions"][0]["shards"][0]["verified"])

    def test_a_segment_naming_a_shard_that_is_gone_fails_loudly(self) -> None:
        self.write(record(1))
        (self.session_dir() / "s000001_20261109.ndjson").unlink()
        with self.assertRaises(LabError):
            vault.reindex(self.root)

    def test_a_shard_the_segment_does_not_know_is_reported_not_adopted(self) -> None:
        # The mirror case: crash after the shard was written, before segment.json landed.
        self.write(record(1))
        orphan = self.session_dir() / "s000099_20261109.ndjson"
        orphan.write_bytes(json.dumps(record(2), ensure_ascii=False).encode("utf-8") + b"\n")
        index = vault.reindex(self.root)
        self.assertEqual(len(index["orphan_shards"]), 1)
        found = index["orphan_shards"][0]
        self.assertEqual(found["shard"], "s000099_20261109.ndjson")
        self.assertEqual(found["file_sha256"], vault.file_sha256(orphan))
        entry = index["sessions"][0]
        self.assertEqual([s["name"] for s in entry["shards"]], ["s000001_20261109.ndjson"])
        self.assertEqual(entry["bytes_total"], entry["shards"][0]["bytes"])

    def test_two_session_keys_cannot_share_one_directory(self) -> None:
        upper, lower = "codex:ABCDEF", "codex:abcdef"
        self.write(record(1, session_key=upper), key=upper, harness="codex")
        # safe_dir folds case on every OS, so this second key lands in the same
        # directory name -- the accident the writer must refuse rather than merge.
        with self.assertRaises(LabError) as caught:
            self.write(record(2, session_key=lower), key=lower, harness="codex")
        self.assertIn("同一个目录", str(caught.exception))

    def test_a_directory_whose_key_no_longer_folds_to_its_name_is_refused(self) -> None:
        self.write(record(1))
        segment_path = self.session_dir() / "segment.json"
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        segment["session_key"] = "qoder:99999999-0000-0000-0000-000000000000"
        segment_path.write_text(json.dumps(segment), encoding="utf-8")
        with self.assertRaises(LabError):
            vault.reindex(self.root)

    def test_the_parent_is_resolved_once_and_stored(self) -> None:
        subagent = "agent-ageneral-purpose-2a43b54a5a95461d"
        key = "qoder:" + subagent
        nested = "projects/d--workspaces-ctx-parser/%s/subagents/%s.jsonl" % (PARENT, subagent)
        self.write(record(1, session_key=key, path=nested), key=key)
        entry = vault.reindex(self.root)["sessions"][0]
        self.assertEqual(entry["parent_session_key"], "qoder:" + PARENT)
        self.assertEqual(len(entry["paths"]), 1)

    def test_a_session_without_a_parent_carries_the_key_as_null(self) -> None:
        self.write(record(1))
        entry = vault.reindex(self.root)["sessions"][0]
        self.assertIn("parent_session_key", entry)
        self.assertIsNone(entry["parent_session_key"])

    def test_odd_key_shapes_survive_the_index(self) -> None:
        # Measured shapes: 36 uuid, 39 qoder subagent, 23 claude subagent, 8 prefix.
        for key in ("qoder:" + "a" * 39, "claude:agent-a25596ad9c5c815bb", "qoder:1a2b3c4d"):
            with self.subTest(key=key):
                harness = key.split(":")[0]
                self.write(record(1, session_key=key), key=key, harness=harness)
                entries = {item["session_key"]: item
                           for item in vault.reindex(self.root)["sessions"]}
                self.assertIn(key, entries)
                self.assertEqual(entries[key]["safe_dir"], vault.safe_dir(key))

    def test_unit_attribution_is_marked_when_nobody_supplied_it(self) -> None:
        self.write(record(1))
        self.assertEqual(vault.reindex(self.root)["unit_ids_source"], "not_supplied")
        index = vault.reindex(self.root, units={KEY: ["unit-b", "unit-a"]})
        self.assertEqual(index["unit_ids_source"], "supplied")
        self.assertEqual(index["sessions"][0]["unit_ids"], ["unit-a", "unit-b"])

    def test_index_is_pretty_printed_with_lf_endings(self) -> None:
        self.write(record(1))
        vault.reindex(self.root)
        raw = (self.root / "index.json").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertIn(b'\n  "schema"', raw)


if __name__ == "__main__":
    unittest.main()
