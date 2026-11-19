"""S3: every digest says who wrote it, and an unwritten writer is a signal.

The 15-minute rhythm used to be unattributable. These tests pin the shape of
the answer: `entry` names the command, `trigger` names the launcher, `pid`
names the process -- and a digest without the field means something wrote it
through a path we have not registered.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import os
import tempfile
import unittest
from pathlib import Path

from lab import state, trigger
from lab.collect import TRIGGER_ENV, catchup, catchup_all
from lab.digest import build_digest, save_digest
from lab.sources import add_source
from lab.workspace import init_workspace


def newest_digest(root: Path) -> dict:
    paths = sorted((root / ".groma" / "digests").glob("digest-*.json"))
    return json.loads(paths[-1].read_text(encoding="utf-8"))


class WrittenByTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state_before = os.environ.get(state.STATE_ENV)
        self._trigger_before = os.environ.get(TRIGGER_ENV)
        os.environ[state.STATE_ENV] = str((Path(self.tmp.name) / "state").resolve())
        os.environ.pop(TRIGGER_ENV, None)
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root)
        harness = Path(self.tmp.name) / "harness"
        (harness / "projects").mkdir(parents=True)
        (harness / "projects" / "one.jsonl").write_text('{"type":"user"}\n',
                                                       encoding="utf-8", newline="\n")
        add_source(self.root, "claude", str(harness), harness="claude-code", include=["projects"])

    def tearDown(self) -> None:
        if self._state_before is None:
            os.environ.pop(state.STATE_ENV, None)
        else:
            os.environ[state.STATE_ENV] = self._state_before
        if self._trigger_before is None:
            os.environ.pop(TRIGGER_ENV, None)
        else:
            os.environ[TRIGGER_ENV] = self._trigger_before
        self.tmp.cleanup()

    def test_the_entry_and_pid_name_the_writer(self) -> None:
        catchup(self.root, force=True, written_by="cli:catchup")
        written_by = newest_digest(self.root)["written_by"]
        self.assertEqual(written_by["entry"], "cli:catchup")
        self.assertEqual(written_by["pid"], os.getpid())
        self.assertIsNone(written_by["trigger"])
        self.assertTrue(written_by["at"].endswith("Z"))

    def test_the_launcher_names_itself_through_the_environment(self) -> None:
        os.environ[TRIGGER_ENV] = "startup-folder"
        catchup(self.root, force=True, written_by="cli:catchup-all")
        self.assertEqual(newest_digest(self.root)["written_by"]["trigger"], "startup-folder")

    def test_catchup_all_carries_the_entry_into_every_workspace(self) -> None:
        catchup_all(written_by="shell:timer")
        self.assertEqual(newest_digest(self.root)["written_by"]["entry"], "shell:timer")

    def test_a_digest_written_around_the_entry_points_has_no_name(self) -> None:
        save_digest(self.root, build_digest(self.root))
        self.assertNotIn("written_by", newest_digest(self.root))

    def test_written_by_carries_no_path_and_no_argument_string(self) -> None:
        catchup(self.root, force=True, written_by="web:refresh")
        written_by = newest_digest(self.root)["written_by"]
        self.assertEqual(set(written_by), {"schema", "pid", "entry", "trigger", "at"})
        text = json.dumps(written_by)
        self.assertNotIn("\\", text)
        self.assertNotIn("/", text)
        self.assertNotIn(self.tmp.name.replace("\\", ""), text)

    def test_the_startup_launcher_declares_itself(self) -> None:
        installed = trigger.install()
        body = Path(installed["trigger"]).read_text(encoding="utf-8")
        self.assertIn("set GROMA_TRIGGER=startup-folder", body)
        self.assertIn("catchup-all", body)


if __name__ == "__main__":
    unittest.main()
