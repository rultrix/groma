"""Q1/Q2 of the S3 structure decision: one interval and one pause for the machine.

The per-workspace layers stay where they are -- `catchup-all` still skips on
them, and a test can pause one folder without silencing the rest -- but the
switch a person touches is the machine one.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import os
import tempfile
import unittest
from pathlib import Path

from lab import control, settings
from lab.collect import catchup_all
from lab.sources import add_source
from lab.workspace import init_workspace


class MachineScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state_before = os.environ.get("GROMA_STATE_DIR")
        os.environ["GROMA_STATE_DIR"] = str((Path(self.tmp.name) / "state").resolve())
        self.a = Path(self.tmp.name) / "a"
        self.b = Path(self.tmp.name) / "b"
        for root in (self.a, self.b):
            root.mkdir()
            init_workspace(root)
            harness = Path(self.tmp.name) / (root.name + "-harness")
            (harness / "projects").mkdir(parents=True)
            (harness / "projects" / "one.jsonl").write_text('{"type":"user"}\n',
                                                           encoding="utf-8", newline="\n")
            add_source(root, "claude", str(harness), harness="claude-code", include=["projects"])

    def tearDown(self) -> None:
        if self._state_before is None:
            os.environ.pop("GROMA_STATE_DIR", None)
        else:
            os.environ["GROMA_STATE_DIR"] = self._state_before
        self.tmp.cleanup()

    def test_the_interval_is_one_value_for_every_workspace(self) -> None:
        self.assertEqual(settings.collect_interval(self.a), 15)
        settings.save_settings(self.a, {"collect_interval_minutes": 7})
        self.assertEqual(settings.collect_interval(self.a), 7)
        self.assertEqual(settings.collect_interval(self.b), 7)

    def test_the_interval_lands_in_the_state_dir_not_in_the_workspace(self) -> None:
        settings.save_settings(self.a, {"collect_interval_minutes": 7})
        self.assertFalse((self.a / ".groma" / "settings.json").is_file())
        self.assertTrue((Path(os.environ["GROMA_STATE_DIR"]) / "settings.json").is_file())

    def test_a_legacy_workspace_file_is_reported_and_does_not_win(self) -> None:
        legacy = self.a / ".groma" / "settings.json"
        legacy.write_text('{"collect_interval_minutes": 3}', encoding="utf-8", newline="\n")
        settings.save_settings(self.a, {"collect_interval_minutes": 9})
        self.assertEqual(settings.collect_interval(self.a), 9)
        self.assertTrue(settings.load_settings(self.a)["legacy_workspace_file"])
        self.assertFalse(settings.load_settings(self.b)["legacy_workspace_file"])

    def test_the_master_switch_silences_every_workspace(self) -> None:
        control.pause_machine()
        self.assertTrue(control.is_paused(self.a))
        self.assertTrue(control.is_paused(self.b))
        control.resume_machine()
        self.assertFalse(control.is_paused(self.a))
        self.assertFalse(control.is_paused(self.b))

    def test_a_workspace_flag_still_pauses_only_that_workspace(self) -> None:
        control.pause(self.a)
        self.assertTrue(control.is_paused(self.a))
        self.assertFalse(control.is_paused(self.b))
        control.resume(self.a)
        self.assertFalse(control.is_paused(self.a))

    def test_the_master_switch_has_its_own_file(self) -> None:
        control.pause_machine()
        self.assertTrue((Path(os.environ["GROMA_STATE_DIR"]) / "pause.json").is_file())
        self.assertFalse((self.a / ".groma" / "state.json").is_file())
        self.assertEqual(control.machine_state()["scope"], "machine")


    def test_a_forced_refresh_reaches_every_workspace(self) -> None:
        self.assertEqual(catchup_all(written_by="test:first")["collected"], 2)
        self.assertEqual(catchup_all(written_by="test:second")["collected"], 0)  # the interval holds
        forced = catchup_all(force=True, written_by="web:refresh")
        self.assertEqual(forced["collected"], 2)
        self.assertEqual({item.get("skipped") for item in forced["results"]}, {False})


class ClassifyKnobTests(unittest.TestCase):
    """Model and harness are machine settings, so changing them needs no reinstall."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state = os.environ.get("GROMA_STATE_DIR")
        os.environ["GROMA_STATE_DIR"] = str((Path(self.tmp.name) / "state").resolve())
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root)

    def tearDown(self) -> None:
        if self._state is None:
            os.environ.pop("GROMA_STATE_DIR", None)
        else:
            os.environ["GROMA_STATE_DIR"] = self._state
        self.tmp.cleanup()

    def test_model_and_provider_round_trip_without_losing_the_interval(self) -> None:
        settings.save_settings(self.root, {"collect_interval_minutes": 20})
        settings.save_settings(self.root, {"classify_model": "GLM-5.3",
                                           "classify_provider": "C:/bin/qoderclicn.exe"})
        loaded = settings.load_settings()
        self.assertEqual(loaded["classify_model"], "GLM-5.3")
        self.assertEqual(loaded["classify_provider"], "C:/bin/qoderclicn.exe")
        self.assertEqual(loaded["collect_interval_minutes"], 20)

    def test_defaults_apply_when_nothing_is_saved(self) -> None:
        loaded = settings.load_settings()
        self.assertEqual(loaded["classify_model"], "Qwen3.8-Flash")
        self.assertIsNone(loaded["classify_provider"])
        self.assertEqual(loaded["classify_signature"], "Usage: qoderclicn")


if __name__ == "__main__":
    unittest.main()
