"""Vault collection integration: catchup writes to vault.

Implemented by the vault session (2026-11-09), not the peer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lab.collect import collect_to_vault
from lab.workspace import init_workspace, write_json


class VaultCollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        init_workspace(self.root, "test-unit", "tester")
        self.vault_tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.vault_tmp, ignore_errors=True)

    @patch("lab.collect.vault_mod.vault_root")
    @patch("lab.collect.vault_mod.extract_transcript")
    def test_collect_to_vault_calls_extractor_for_transcript_files(
        self, mock_extract: MagicMock, mock_vault_root: MagicMock
    ) -> None:
        mock_vault_root.return_value = Path(self.vault_tmp)
        mock_extract.return_value = {"written": 5, "duplicates": 0}

        source_dir = self.root / "sources"
        source_dir.mkdir()
        transcript = source_dir / "session.jsonl"
        transcript.write_text("{}", encoding="utf-8")

        write_json(self.root / ".groma" / "sources.json", {"sources": [
            {"name": "test", "path": str(source_dir), "harness": "qoder-cn", "include": ["."]}
        ]})

        digest = {
            "files": [
                {
                    "source": "test",
                    "path": "session.jsonl",
                    "session_key": "qoder-cn:abc123",
                    "harness": "qoder-cn",
                    "agent_id": "agent-1",
                    "transcript": True,
                }
            ]
        }

        result = collect_to_vault(self.root, digest)

        self.assertEqual(result["sessions_attempted"], 1)
        self.assertEqual(result["sessions_written"], 1)
        mock_extract.assert_called_once()
        call_args = mock_extract.call_args
        self.assertEqual(call_args[0][1], "qoder-cn")
        self.assertEqual(call_args[0][2], "qoder-cn:abc123")

    @patch("lab.collect.vault_mod.vault_root")
    @patch("lab.qoder_db.locate_db")
    @patch("lab.qoder_db.message_rows")
    @patch("lab.collect.vault_mod.extract_db_session")
    def test_collect_to_vault_calls_db_extractor_for_db_only_sessions(
        self, mock_extract_db: MagicMock, mock_rows: MagicMock,
        mock_locate: MagicMock, mock_vault_root: MagicMock
    ) -> None:
        mock_vault_root.return_value = Path(self.vault_tmp)
        mock_locate.return_value = Path("/fake/local.db")
        mock_rows.return_value = [{"id": 1, "content": "test"}]
        mock_extract_db.return_value = {"written": 3, "duplicates": 0}

        source_dir = self.root / "sources"
        source_dir.mkdir()

        write_json(self.root / ".groma" / "sources.json", {"sources": [
            {"name": "test", "path": str(source_dir), "harness": "qoder-cn", "include": ["."]}
        ]})

        digest = {
            "files": [
                {
                    "source": "test",
                    "path": "db/session-xyz",
                    "session_key": "qoder-cn:xyz789",
                    "harness": "qoder-cn",
                    "agent_id": "agent-2",
                    "transcript": False,
                }
            ]
        }

        result = collect_to_vault(self.root, digest)

        self.assertEqual(result["sessions_attempted"], 1)
        self.assertEqual(result["sessions_written"], 1)
        mock_extract_db.assert_called_once()
        mock_rows.assert_called_once_with(Path("/fake/local.db"), "session-xyz")

    @patch("lab.collect.vault_mod.vault_root")
    def test_collect_to_vault_skips_files_without_session_key(self, mock_vault_root: MagicMock) -> None:
        mock_vault_root.return_value = Path(self.vault_tmp)

        digest = {
            "files": [
                {
                    "source": "test",
                    "path": "session.jsonl",
                    "harness": "qoder-cn",
                    "transcript": True,
                }
            ]
        }

        result = collect_to_vault(self.root, digest)

        self.assertEqual(result["sessions_attempted"], 0)
        self.assertEqual(result["sessions_written"], 0)

    @patch("lab.collect.vault_mod.vault_root")
    @patch("lab.collect.vault_mod.extract_transcript")
    def test_collect_to_vault_catches_errors_per_session(
        self, mock_extract: MagicMock, mock_vault_root: MagicMock
    ) -> None:
        mock_vault_root.return_value = Path(self.vault_tmp)
        from lab.workspace import LabError
        mock_extract.side_effect = LabError("test error")

        source_dir = self.root / "sources"
        source_dir.mkdir()
        transcript = source_dir / "session.jsonl"
        transcript.write_text("{}", encoding="utf-8")

        write_json(self.root / ".groma" / "sources.json", {"sources": [
            {"name": "test", "path": str(source_dir), "harness": "qoder-cn", "include": ["."]}
        ]})

        digest = {
            "files": [
                {
                    "source": "test",
                    "path": "session.jsonl",
                    "session_key": "qoder-cn:abc123",
                    "harness": "qoder-cn",
                    "agent_id": "agent-1",
                    "transcript": True,
                }
            ]
        }

        result = collect_to_vault(self.root, digest)

        self.assertEqual(result["sessions_attempted"], 1)
        self.assertEqual(result["sessions_written"], 0)
        self.assertIn("error", result["results"][0])
        self.assertEqual(result["results"][0]["error"], "test error")


if __name__ == "__main__":
    unittest.main()
