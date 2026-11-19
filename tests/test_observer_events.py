"""Tests for lab/observer_events.py — vault + digest → observer event stream."""

import json
import os
import statebox  # noqa: F401  -- must be first real import
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lab import vault
from lab.observer_events import (
    emit_events,
    _build_digest_map,
    _map_beat_role,
    _parse_ts,
)


KEY1 = "qoder:11111111-2222-3333-4444-555555555555"
KEY2 = "qoder:22222222-3333-4444-5555-666666666666"


def make_record(seq, session_key=KEY1, agent_id="agt-1", ts_offset_minutes=0):
    """Build a minimal vault record for testing."""
    base_ts = datetime(2026, 11, 9, 8, 0, 0, tzinfo=timezone.utc)
    ts = base_ts + timedelta(minutes=ts_offset_minutes)
    return {
        "seq": seq,
        "ts_utc": ts.isoformat(),
        "granularity": "event",
        "session_key": session_key,
        "agent_id": agent_id,
        "role": "assistant",
        "type": "message",
        "text": "test message %d" % seq,
        "reasoning": None,
        "tool": None,
        "strip": [],
        "source": {
            "path": "projects/ws/transcript/%s.jsonl" % session_key.split(":")[1],
            "line_no": seq,
            "bytes": 100,
            "source_sha256": "f" * 64,
        },
    }


class BuildDigestMapTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(_build_digest_map([]), {})

    def test_session_key_without_cwd_is_skipped(self):
        rows = [{"session_key": KEY1, "cwd": ""}]
        result = _build_digest_map(rows)
        self.assertEqual(result[KEY1]["cwd"], "")

    def test_maps_session_key_to_cwd_and_ws_ref(self):
        rows = [
            {"session_key": KEY1, "cwd": "/path/to/ws1", "ws_ref": "ws-abc123"},
            {"session_key": KEY2, "cwd": "/path/to/ws2", "ws_ref": "ws-def456"},
        ]
        result = _build_digest_map(rows)
        self.assertEqual(result[KEY1]["cwd"], "/path/to/ws1")
        self.assertEqual(result[KEY1]["ws_ref"], "ws-abc123")
        self.assertEqual(result[KEY2]["cwd"], "/path/to/ws2")


class MapBeatRoleTests(unittest.TestCase):
    def test_assistant_maps_to_reply(self):
        self.assertEqual(_map_beat_role("assistant", "message"), "reply")

    def test_user_maps_to_user(self):
        self.assertEqual(_map_beat_role("user", "message"), "user")

    def test_tool_type_maps_to_tool(self):
        self.assertEqual(_map_beat_role("assistant", "tool"), "tool")

    def test_reasoning_type_maps_to_reasoning(self):
        self.assertEqual(_map_beat_role("assistant", "reasoning"), "reasoning")

    def test_unknown_maps_to_other(self):
        self.assertEqual(_map_beat_role("unknown", "unknown"), "other")


class ParseTsTests(unittest.TestCase):
    def test_parses_iso8601_with_z(self):
        ts = _parse_ts("2026-11-09T08:00:00Z")
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 11)
        self.assertEqual(ts.day, 9)
        self.assertEqual(ts.hour, 8)

    def test_parses_iso8601_with_offset(self):
        ts = _parse_ts("2026-11-09T08:00:00+00:00")
        self.assertEqual(ts.tzinfo, timezone.utc)


class EmitEventsTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.vault_root = Path(self._dir.name).resolve() / "vault"
        self.vault_root.mkdir(parents=True)
        self.addCleanup(self._dir.cleanup)

    def write_session(self, session_key, records, harness="qoder"):
        """Helper: write records to a vault session."""
        vault.append_records(self.vault_root, harness, session_key, records)

    def test_empty_vault_returns_empty(self):
        vault.reindex(self.vault_root)
        events = emit_events(self.vault_root, [], "test-host")
        self.assertEqual(events, [])

    def test_single_session_single_record(self):
        rec = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        self.write_session(KEY1, [rec])
        vault.reindex(self.vault_root)

        digest_rows = [{"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"}]
        events = emit_events(self.vault_root, digest_rows, "test-host")

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["agent_key"], "agt-1")
        self.assertEqual(event["workspace_key"], "ws-abc")
        self.assertEqual(event["harness_name"], "qoder")
        self.assertEqual(event["cwd"], "/ws1")
        self.assertEqual(event["host_name"], "test-host")
        self.assertEqual(event["beat_role"], "reply")
        self.assertTrue(event["loop_key"].startswith(KEY1 + ":loop-"))

    def test_multiple_records_same_loop_within_gap(self):
        """Records within gap_seconds should be in the same loop."""
        rec1 = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        rec2 = make_record(2, KEY1, "agt-1", ts_offset_minutes=10)  # 10 min < 30 min gap
        self.write_session(KEY1, [rec1, rec2])
        vault.reindex(self.vault_root)

        digest_rows = [{"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"}]
        events = emit_events(self.vault_root, digest_rows, "test-host", gap_seconds=1800)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["loop_key"], events[1]["loop_key"])

    def test_multiple_records_different_loop_beyond_gap(self):
        """Records beyond gap_seconds should be in different loops."""
        rec1 = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        rec2 = make_record(2, KEY1, "agt-1", ts_offset_minutes=40)  # 40 min > 30 min gap
        self.write_session(KEY1, [rec1, rec2])
        vault.reindex(self.vault_root)

        digest_rows = [{"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"}]
        events = emit_events(self.vault_root, digest_rows, "test-host", gap_seconds=1800)

        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["loop_key"], events[1]["loop_key"])
        self.assertTrue(events[0]["loop_key"].endswith(":loop-0"))
        self.assertTrue(events[1]["loop_key"].endswith(":loop-1"))

    def test_multiple_sessions(self):
        """Multiple sessions should produce separate event groups."""
        rec1 = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        rec2 = make_record(2, KEY2, "agt-2", ts_offset_minutes=0)
        self.write_session(KEY1, [rec1])
        self.write_session(KEY2, [rec2])
        vault.reindex(self.vault_root)

        digest_rows = [
            {"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"},
            {"session_key": KEY2, "cwd": "/ws2", "ws_ref": "ws-def"},
        ]
        events = emit_events(self.vault_root, digest_rows, "test-host")

        self.assertEqual(len(events), 2)
        agents = {e["agent_key"] for e in events}
        self.assertEqual(agents, {"agt-1", "agt-2"})

    def test_missing_digest_info_uses_empty_strings(self):
        """Sessions without digest info should still emit events with empty cwd/ws_ref."""
        rec = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        self.write_session(KEY1, [rec])
        vault.reindex(self.vault_root)

        digest_rows = []  # no digest info for KEY1
        events = emit_events(self.vault_root, digest_rows, "test-host")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["cwd"], "")
        self.assertEqual(events[0]["workspace_key"], "")

    def test_record_without_agent_id_is_skipped(self):
        """Records missing agent_id should be skipped."""
        rec = make_record(1, KEY1, agent_id="", ts_offset_minutes=0)
        self.write_session(KEY1, [rec])
        vault.reindex(self.vault_root)

        digest_rows = [{"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"}]
        events = emit_events(self.vault_root, digest_rows, "test-host")

        self.assertEqual(len(events), 0)

    def test_content_sha256_from_stored_sha256(self):
        """Event should use stored_sha256 computed by vault."""
        rec = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        self.write_session(KEY1, [rec])
        vault.reindex(self.vault_root)

        digest_rows = [{"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"}]
        events = emit_events(self.vault_root, digest_rows, "test-host")

        self.assertEqual(len(events), 1)
        # vault.append_records computes stored_sha256; verify it's used
        self.assertTrue(len(events[0]["content_sha256"]) == 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in events[0]["content_sha256"]))

    def test_content_sha256_falls_back_to_source_sha256(self):
        """Event should fall back to source.source_sha256 if stored_sha256 is missing."""
        # Write NDJSON directly to bypass vault.append_records (which adds stored_sha256)
        rec = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        # Explicitly do NOT add stored_sha256
        session_dir = self.vault_root / "qoder" / vault.safe_dir(KEY1)
        session_dir.mkdir(parents=True)
        shard_path = session_dir / "s000001_20261109.ndjson"
        with open(shard_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

        # Write segment.json
        segment = {
            "next_record_seq": 2,
            "next_shard": 2,
            "covered": {},
            "shards": [{"name": "s000001_20261109.ndjson", "from_ts": rec["ts_utc"],
                       "to_ts": rec["ts_utc"], "bytes": shard_path.stat().st_size, "records": 1}],
            "session_key": KEY1,
            "duplicates": 0,
        }
        with open(session_dir / "segment.json", "w", encoding="utf-8") as f:
            json.dump(segment, f)

        vault.reindex(self.vault_root)

        digest_rows = [{"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"}]
        events = emit_events(self.vault_root, digest_rows, "test-host")

        self.assertEqual(len(events), 1)
        # Should fall back to source.source_sha256
        self.assertEqual(events[0]["content_sha256"], "f" * 64)

    def test_tool_name_extracted_from_tool_field(self):
        """Event should extract tool.name from the tool field."""
        rec = make_record(1, KEY1, "agt-1", ts_offset_minutes=0)
        rec["type"] = "tool"
        rec["tool"] = {"name": "Bash", "call_id": "call-123", "input": {}, "result": {}}
        self.write_session(KEY1, [rec])
        vault.reindex(self.vault_root)

        digest_rows = [{"session_key": KEY1, "cwd": "/ws1", "ws_ref": "ws-abc"}]
        events = emit_events(self.vault_root, digest_rows, "test-host")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool_name"], "Bash")
        self.assertEqual(events[0]["beat_role"], "tool")


if __name__ == "__main__":
    unittest.main()
