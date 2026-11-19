"""Codex and Claude Code extraction (ported from acpe-agent-package's adapters).

Fixtures are the shapes measured on this machine's real files:
codex `response_item` payloads with `input_text`/`output_text`/`reasoning.summary`/
`custom_tool_call(_output)`; claude-family lines with `message.content` as a string or a
block list, plus the bookkeeping types (`queue-operation`, `ai-title`, `last-prompt`).
"""

import json
import tempfile
import unittest
from pathlib import Path

from lab.vault import extract_transcript, scan_transcript
from lab.workspace import LabError

CODEX_KEY = "codex:019fd07e-8557-7ad1-a2f5-9046b0d91f93"
CLAUDE_KEY = "claude-code:6fcdb175-1b2e-4205-933d-49e488014106"


def codex_line(payload, ts="2026-11-09T08:00:00.000Z"):
    return {"type": "response_item", "timestamp": ts, "payload": payload}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name).resolve()
        self.root = self.base / "vault"
        self.root.mkdir()
        self.source = self.base / "rollout.jsonl"
        self.addCleanup(self._dir.cleanup)

    def write(self, rows):
        with self.source.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def scan(self, harness, key, mode="normalized"):
        return scan_transcript(self.source, harness, key, rel="sessions/rollout.jsonl", mode=mode)


class CodexOuterFiltering(Fixture):
    ROWS = [
        {"type": "session_meta", "timestamp": "2026-11-09T08:00:00.000Z",
         "payload": {"id": "019fd07e-8557-7ad1-a2f5-9046b0d91f93", "cwd": "D:/x"}},
        {"type": "event_msg", "timestamp": "2026-11-09T08:00:01.000Z",
         "payload": {"type": "token_count", "info": {}}},
        {"type": "world_state", "timestamp": "2026-11-09T08:00:02.000Z", "payload": {"a": 1}},
        {"type": "compacted", "timestamp": "2026-11-09T08:00:03.000Z", "payload": {"type": "context_compacted"}},
        codex_line({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "问"}]}),
    ]

    def test_only_response_items_become_records(self) -> None:
        self.write(self.ROWS)
        records, stats = self.scan("codex", CODEX_KEY)
        self.assertEqual([r["text"] for r in records], ["问"])
        self.assertEqual(stats["lines"], 5)
        self.assertEqual(stats["skipped_outer"],
                         {"session_meta": 1, "event_msg": 1, "world_state": 1, "compacted": 1})

    def test_meta_lines_are_not_mistaken_for_a_second_session(self) -> None:
        # Real files carry a dozen session_meta lines; none of them may split the shard.
        rows = self.ROWS + [{"type": "session_meta", "timestamp": "2026-11-09T08:05:00.000Z",
                             "payload": {"id": "another"}}]
        self.write(rows)
        records, stats = self.scan("codex", CODEX_KEY)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["skipped_outer"].get("session_meta"), 2)


class CodexPayloads(Fixture):
    def test_developer_role_is_kept_verbatim(self) -> None:
        self.write([codex_line({"type": "message", "role": "developer",
                                "content": [{"type": "input_text", "text": "系统约束"}]})])
        records, _ = self.scan("codex", CODEX_KEY)
        self.assertEqual(records[0]["role"], "developer")
        self.assertEqual(records[0]["harness_type"], "message")

    def test_reasoning_with_a_summary_is_kept(self) -> None:
        self.write([codex_line({"type": "reasoning",
                                "summary": [{"type": "summary_text", "text": "先看 AGENTS.md"}],
                                "encrypted_content": "blob"})])
        records, stats = self.scan("codex", CODEX_KEY)
        self.assertEqual(records[0]["reasoning"], "先看 AGENTS.md")
        self.assertTrue(records[0]["reasoning_encrypted"])
        self.assertEqual(stats["reasoning_encrypted"], 0)

    def test_an_encrypted_reasoning_with_empty_summary_is_still_a_record(self) -> None:
        # Measured on a real rollout: 214 of 330 reasoning items have summary == [].
        # Dropping them would read as "the model did not think here".
        self.write([codex_line({"type": "reasoning", "summary": [], "encrypted_content": "blob"})])
        records, stats = self.scan("codex", CODEX_KEY)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["reasoning"])
        self.assertTrue(records[0]["reasoning_encrypted"])
        self.assertEqual(records[0]["type"], "reasoning")
        self.assertEqual(records[0]["text"], "")
        self.assertEqual(stats["reasoning_encrypted"], 1)
        self.assertEqual(stats["empty"], 0)

    def test_a_tool_call_and_its_output_become_two_records_with_the_same_call_id(self) -> None:
        self.write([
            codex_line({"type": "custom_tool_call", "call_id": "call_9", "name": "shell",
                        "arguments": "{\"cmd\":\"ls\"}"}, ts="2026-11-09T08:00:10.000Z"),
            codex_line({"type": "custom_tool_call_output", "call_id": "call_9",
                        "output": "file listing\n" + "x" * 5000}, ts="2026-11-09T08:00:11.000Z"),
        ])
        records, _ = self.scan("codex", CODEX_KEY)
        self.assertEqual([r["type"] for r in records], ["tool", "tool"])
        self.assertEqual(records[0]["tool"][0]["name"], "shell")
        self.assertEqual(json.loads(records[0]["tool"][0]["input"]), {"cmd": "ls"})
        self.assertEqual(records[1]["tool"][0]["call_id"], "call_9")
        self.assertEqual(len(records[1]["tool"][0]["result"]), 5000 + len("file listing\n"))

    def test_arguments_are_canonicalised_so_key_order_is_not_a_content_change(self) -> None:
        self.write([codex_line({"type": "function_call", "call_id": "c", "name": "n",
                                "arguments": '{"b":2,"a":1}'}),
                    codex_line({"type": "function_call", "call_id": "c2", "name": "n",
                                "arguments": '{"a":1,"b":2}'})])
        records, _ = self.scan("codex", CODEX_KEY)
        self.assertEqual(records[0]["tool"][0]["input"], records[1]["tool"][0]["input"])

    def test_an_unknown_payload_type_is_counted_not_silently_dropped(self) -> None:
        self.write([codex_line({"type": "something_new", "data": 1})])
        records, stats = self.scan("codex", CODEX_KEY)
        self.assertEqual(records, [])
        self.assertEqual(stats["skipped_payload"], {"something_new": 1})

    def test_verbatim_keeps_the_wrapper_that_normalized_strips(self) -> None:
        rows = [codex_line({"type": "message", "role": "user",
                            "content": [{"type": "input_text",
                                         "text": "<system-reminder>环境</system-reminder>真问题"}]})]
        self.write(rows)
        cleaned, _ = self.scan("codex", CODEX_KEY, mode="normalized")
        self.write(rows)
        raw, _ = self.scan("codex", CODEX_KEY, mode="verbatim")
        self.assertEqual(cleaned[0]["text"], "真问题")
        self.assertEqual(cleaned[0]["strip"], ["system-reminder"])
        self.assertIn("<system-reminder>", raw[0]["text"])
        self.assertEqual(raw[0]["strip"], [])


class ClaudeFamily(Fixture):
    def test_bookkeeping_lines_are_skipped_and_named(self) -> None:
        self.write([
            {"type": "queue-operation", "operation": "enqueue", "sessionId": "s",
             "timestamp": "2026-11-09T08:00:00.000Z", "content": "x"},
            {"type": "ai-title", "sessionId": "s", "timestamp": "2026-11-09T08:00:00.000Z"},
            {"type": "last-prompt", "sessionId": "s", "lastPrompt": "p"},
            {"type": "user", "sessionId": "s", "timestamp": "2026-11-09T08:00:01.000Z",
             "message": {"role": "user", "content": "纯字符串也算正文"}},
        ])
        records, stats = self.scan("claude-code", CLAUDE_KEY)
        self.assertEqual([r["text"] for r in records], ["纯字符串也算正文"])
        self.assertEqual(stats["skipped_outer"], {"queue-operation": 1, "ai-title": 1, "last-prompt": 1})

    def test_a_sidechain_line_is_not_stored_under_the_parents_key(self) -> None:
        self.write([
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-11-09T08:00:00.000Z",
             "isSidechain": True, "message": {"role": "assistant", "content": [{"type": "text", "text": "子对话"}]}},
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-11-09T08:00:01.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "主对话"}]}},
        ])
        records, stats = self.scan("claude-code", CLAUDE_KEY)
        self.assertEqual([r["text"] for r in records], ["主对话"])
        self.assertEqual(stats["skipped_sidechain"], 1)

    def test_thinking_and_tool_blocks_are_separated(self) -> None:
        self.write([
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-11-09T08:00:00.000Z",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "先读文件"},
                 {"type": "text", "text": "我看看"},
                 {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"path": "a"}}]}},
            {"type": "user", "sessionId": "s", "timestamp": "2026-11-09T08:00:01.000Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "call_1", "content": "contents"}]}},
        ])
        records, _ = self.scan("claude-code", CLAUDE_KEY)
        self.assertEqual(records[0]["reasoning"], "先读文件")
        self.assertEqual(records[0]["text"], "我看看")
        self.assertEqual(records[0]["tool"][0]["name"], "Read")
        self.assertEqual(records[1]["tool"][0]["result"], "contents")
        self.assertEqual(records[1]["role"], "user")

    def test_extract_writes_one_shard_per_day_for_codex_too(self) -> None:
        self.write([
            codex_line({"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "今天"}]}, ts="2026-11-09T23:59:00.000Z"),
            codex_line({"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "跨天了"}]}, ts="2026-11-10T00:01:00.000Z"),
        ])
        result = extract_transcript(self.root, "codex", CODEX_KEY, self.source,
                                    rel="sessions/2026/rollout.jsonl")
        self.assertEqual(result["written"], 2)
        self.assertEqual([s["name"] for s in result["shards"]],
                         ["s000001_20261109.ndjson", "s000002_20261110.ndjson"])
        self.assertEqual(result["lines_read"], 2)
        self.assertEqual(result["skipped"]["skipped_outer"], {})

    def test_a_record_without_a_timestamp_stops_before_writing(self) -> None:
        row = codex_line({"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "x"}]})
        del row["timestamp"]
        self.write([row])
        with self.assertRaises(LabError):
            extract_transcript(self.root, "codex", CODEX_KEY, self.source, rel="sessions/r.jsonl")
        self.assertFalse((self.root / "codex").exists())


if __name__ == "__main__":
    unittest.main()
