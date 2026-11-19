"""First-run consent stamps, and the one thing that must never write them.

A fresh install used to accept a GB-scale vault on the system drive without ever
asking, because `create_app()` registers the boot folder and nothing in state
distinguished "Groma knows this folder" from "the operator agreed to this path".
These tests pin the two stamps and, above all, that registering a workspace is
not agreement.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import state
from lab.workspace import LabError

UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class FirstRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)

    @property
    def _file(self) -> Path:
        return self.base / "state" / state.FIRST_RUN_FILE

    def test_first_run_starts_empty(self) -> None:
        self.assertEqual(
            state.first_run(),
            {"vault_confirmed_at": None, "workspace_settled_at": None},
        )
        self.assertFalse(self._file.exists(), "reading first-run state must not create the file")

    def test_marking_vault_writes_a_utc_timestamp(self) -> None:
        result = state.mark_first_run("vault")
        self.assertRegex(result["vault_confirmed_at"], UTC_STAMP)
        self.assertIsNone(result["workspace_settled_at"])
        self.assertEqual(json.loads(self._file.read_text(encoding="utf-8"))["schema"], 1)

    def test_marking_is_idempotent(self) -> None:
        """The first agreement is the fact; a later call must not move it.

        The clock is patched to a far-future stamp between the two calls, so a
        rewrite cannot pass by landing in the same second.
        """
        first = state.mark_first_run("vault")["vault_confirmed_at"]
        with mock.patch.object(state, "_now", return_value="2099-01-01T00:00:00Z"):
            again = state.mark_first_run("vault")["vault_confirmed_at"]
        self.assertEqual(again, first)

    def test_unknown_step_is_refused(self) -> None:
        with self.assertRaises(LabError):
            state.mark_first_run("nope")

    def test_registering_a_workspace_does_not_settle_first_run(self) -> None:
        folder = self.base / "proj"
        folder.mkdir(parents=True)
        state.register_workspace(folder, "proj")
        self.assertIsNone(state.first_run()["workspace_settled_at"])


if __name__ == "__main__":
    unittest.main()
