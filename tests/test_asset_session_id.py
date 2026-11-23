"""The session id an asset cites must be the id the digest measured (docs/CONTEXT-VAULT.md §5).

`asset.py` used to take `Path(path).stem`, which for codex hands back
`rollout-2026-08-10T17-34-36-<uuid>` — a capture timestamp wearing an identity. Measured on the
home workspace's newest digest: 49 of 124 rows disagreed with the digest's own `session_key`.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lab.asset import build_asset, session_id_of
from lab.digest import save_digest
from lab.state import register_workspace
from lab.workspace import init_workspace, load_unit

CODEX = "sessions/rollout-2026-08-10T17-34-36-019feb06-25ea-7d21-9986-8d57ff1da526.jsonl"
UUID = "019feb06-25ea-7d21-9986-8d57ff1da526"


class IdDerivation(unittest.TestCase):
    def test_a_codex_file_name_yields_the_session_not_its_timestamp(self) -> None:
        self.assertEqual(session_id_of({"path": CODEX}), UUID)

    def test_a_db_only_row_yields_the_uuid(self) -> None:
        self.assertEqual(session_id_of({"path": "db/" + UUID}), UUID)

    def test_qoder_is_unchanged_by_the_fix(self) -> None:
        self.assertEqual(session_id_of({"path": "projects/home/" + UUID + ".jsonl"}), UUID)

    def test_the_key_the_digest_measured_beats_the_file_name(self) -> None:
        # Two spellings of one folder, or a prefix-matched transcript: the digest
        # resolved the identity from the session record, so that answer wins.
        self.assertEqual(session_id_of({"path": CODEX, "session_key": "codex:" + "f" * 36}), "f" * 36)

    def test_a_key_without_a_harness_prefix_falls_back_to_the_path(self) -> None:
        self.assertEqual(session_id_of({"path": CODEX, "session_key": UUID}), UUID)

    def test_the_asset_id_and_the_id_join_key_stay_recomposable(self) -> None:
        item = {"path": CODEX, "session_key": "codex:" + UUID}
        self.assertEqual("codex:" + session_id_of(item), item["session_key"])


class AssetCitesIt(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="一把键")
        register_workspace(self.home, "home")
        self.stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def digest(self, rows: list[dict]) -> None:
        files = [
            {
                "source": "codex",
                "bytes": 120,
                "lines": 3,
                "events": 3,
                "transcript": True,
                "transcript_lines": 3,
                "first_ts": self.stamp,
                "last_ts": self.stamp,
                "hours": [self.stamp[:13]],
                "keys": ["cwd", "timestamp"],
                "parse_errors": 0,
                "cwd": self.home.as_posix(),
                "sub_type": None,
                "agent_id": "agt-codex",
                "agent_name": "Default Agent",
                "agent_kind": "primary",
                "parent_agent_id": None,
                "harness": "codex",
                **row,
            }
            for row in rows
        ]
        save_digest(
            self.home,
            {
                "schema": 1,
                "kind": "GROMA_LOCAL_DIGEST",
                "unit_id": load_unit(self.home)["unit_id"],
                "created_at": self.stamp,
                "content_stored": False,
                "workspace_root": self.home.as_posix(),
                "include": [],
                "file_count": len(files),
                "event_count": len(files),
                "parse_errors": 0,
                "skipped_non_session": 0,
                "unattributed_outside": 0,
                "unattributed_no_cwd": 0,
                "agents": [],
                "files": files,
            },
        )

    def test_the_written_asset_carries_the_short_id_not_the_rollout_name(self) -> None:
        self.digest([{"path": CODEX}])
        asset = build_asset(self.home, "取正文", [("codex", CODEX)])
        self.assertEqual(asset["sessions"][0]["session_id"], UUID)
        readme = (self.home / ".groma" / "assets" / "current" / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("rollout-2026-08-10T17-34-36", readme)

    def test_the_asset_id_does_not_move_when_the_id_spelling_changes(self) -> None:
        # Selection keys are (source, path), so re-spelling a session id must not
        # re-mint every asset id on the machine. If this ever fails, published
        # citations break for a cosmetic reason.
        self.digest([{"path": CODEX, "session_key": "codex:" + UUID}])
        with_key = build_asset(self.home, "取正文", [("codex", CODEX)])
        self.digest([{"path": CODEX}])
        without_key = build_asset(self.home, "取正文", [("codex", CODEX)])
        self.assertEqual(with_key["asset_id"], without_key["asset_id"])
        self.assertEqual(without_key["sessions"][0]["session_id"], UUID)

    def test_no_digest_row_needs_to_carry_a_key_for_this_to_work(self) -> None:
        self.digest([{"path": CODEX}])
        asset = build_asset(self.home, "旧 digest", [("codex", CODEX)])
        self.assertEqual(asset["sessions"][0]["session_id"], UUID)
        self.assertFalse(any("session_key" in json.dumps(row) for row in asset["sessions"]))


if __name__ == "__main__":
    unittest.main()
