"""The two join keys the vault line depends on: session identity and a fingerprint.

`unitmatch` D8 has claimed since 2026-10 that dedup is "by session id + content
hash". The digest layer had neither — it folded duplicates by file-name stem.
These tests pin what landed, and stay clear of `org_path` on purpose: the identity
task book may change that field, and these keys must not move with it.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab.digest import (
    build_digest,
    file_sha256,
    identity_fields,
    normalise_session_id,
    session_key_for,
)
from lab.sources import add_source
from lab.workspace import init_workspace

CODEX_NAME = "rollout-2026-08-10T17-34-36-019feb06-25ea-7d21-9986-8d57ff1da526"
UUID = "019feb06-25ea-7d21-9986-8d57ff1da526"


class KeyShapeTests(unittest.TestCase):
    def test_codex_file_name_keeps_only_the_uuid_tail(self) -> None:
        self.assertEqual(normalise_session_id(CODEX_NAME + ".jsonl"), UUID)

    def test_a_plain_uuid_is_returned_lowercased(self) -> None:
        self.assertEqual(normalise_session_id("FDF7C2C9-06D6-454A-9B7C-F9A1837EE8F3.jsonl"),
                         "fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3")

    def test_a_name_without_uuid_is_kept_whole(self) -> None:
        self.assertEqual(normalise_session_id("session-42.ndjson"), "session-42")

    def test_the_same_session_under_two_directories_gets_one_key(self) -> None:
        left = session_key_for("qoder-cn", "projects/D-workspaces-ctx_parser/%s.jsonl" % UUID)
        right = session_key_for("qoder-cn", "projects/d--workspaces-ctx-parser/%s.jsonl" % UUID)
        self.assertEqual(left, right)
        self.assertEqual(left, "qoder-cn:" + UUID)

    def test_the_db_only_rows_id_still_folds_into_the_transcript_key(self) -> None:
        self.assertEqual(session_key_for("qoder-cn", "db/" + UUID), "qoder-cn:" + UUID)


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_hash_matches_an_independent_computation(self) -> None:
        path = self.root / "s.jsonl"
        path.write_text('{"a": 1}\n', encoding="utf-8", newline="\n")
        self.assertEqual(file_sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_a_missing_file_reports_empty_instead_of_inventing_a_hash(self) -> None:
        self.assertEqual(file_sha256(self.root / "nope.jsonl"), "")

    def test_db_only_rows_declare_no_transcript_hash(self) -> None:
        fields = identity_fields("qoder-cn", "db/" + UUID, None, UUID, transcript=False)
        self.assertEqual(fields["fingerprint_kind"], "db-rows-only")
        self.assertIsNone(fields["source_sha256"])


class DigestIdentityTests(unittest.TestCase):
    """Every row a digest produces must carry both keys, and the hash must be real."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="keys")
        self.src = base / "src"
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self._write("sessions/2026-08-10/%s.jsonl" % CODEX_NAME, '{"timestamp": "2026-08-10T09:34:37Z", "cwd": "%s", "type": "user"}' % self.root.as_posix())
        self._write("sessions/2026-08-11/other.jsonl", '{"timestamp": "2026-08-11T01:00:00Z", "cwd": "%s", "type": "user"}' % self.root.as_posix())
        add_source(self.root, "codex", str(self.src), harness="codex", include=["sessions"])
        self.digest = build_digest(self.root, include=["sessions"])

    def _write(self, rel: str, line: str) -> None:
        path = self.src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(line + "\n", encoding="utf-8", newline="\n")

    def test_every_row_carries_a_session_key_and_a_fingerprint(self) -> None:
        self.assertEqual(self.digest["file_count"], 2)
        for row in self.digest["files"]:
            self.assertTrue(row["session_key"].startswith("codex:"), row)
            self.assertEqual(row["fingerprint_kind"], "transcript-sha256")
            self.assertEqual(len(row["source_sha256"]), 64, "a real hash, not a placeholder")

    def test_the_fingerprint_is_the_bytes_on_disk(self) -> None:
        row = next(item for item in self.digest["files"] if item["session_key"] == "codex:" + UUID)
        on_disk = self.src / "sessions" / "2026-08-10" / (CODEX_NAME + ".jsonl")
        self.assertEqual(row["source_sha256"], hashlib.sha256(on_disk.read_bytes()).hexdigest())

    def test_the_fingerprint_moves_when_the_session_moves(self) -> None:
        before = next(item for item in self.digest["files"]
                      if item["session_key"] == "codex:" + UUID)["source_sha256"]
        self._write("sessions/2026-08-10/%s.jsonl" % CODEX_NAME,
                    '{"timestamp": "2026-08-10T09:34:37Z", "cwd": "%s", "type": "assistant"}' % self.root.as_posix())
        after = next(item for item in build_digest(self.root, include=["sessions"])["files"]
                     if item["session_key"] == "codex:" + UUID)["source_sha256"]
        self.assertNotEqual(before, after)

    def test_the_keys_never_bring_content_along(self) -> None:
        sentinel = "SECRET_SENTINEL_VAULT_KEY"
        self._write("sessions/2026-08-10/%s.jsonl" % CODEX_NAME,
                    '{"timestamp": "2026-08-10T09:34:37Z", "cwd": "%s", "type": "user", "text": "%s"}'
                    % (self.root.as_posix(), sentinel))
        rebuilt = build_digest(self.root, include=["sessions"])
        self.assertNotIn(sentinel, json.dumps(rebuilt, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
