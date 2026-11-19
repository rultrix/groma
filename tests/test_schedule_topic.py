"""S4: the classification job gets its own OS task, and only defined topics run."""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import os
import tempfile
import unittest
from pathlib import Path

from lab import classify, classify_run, schedule, topics, unitmatch
from lab.workspace import init_workspace

ROWS = [{"source": "qoder", "path": "projects/d--ws/019feb06-25ea-7d21-9986-8d57ff1da526.jsonl",
         "harness": "qoder-cn", "agent_kind": "primary", "events": 12}]


def make_provider(folder: Path, answer: str) -> Path:
    path = folder / "qoderclicn.cmd"
    head = (
        "@echo off\r\n"
        'if "%1"=="--help" (\r\n'
        "  echo Usage: qoderclicn [options]\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'if "%1"=="status" (\r\n'
        "  echo Login Method: browser\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
    )
    path.write_text(head + answer + "\r\n", encoding="utf-8", newline="")
    return path


class JobTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._env = {key: os.environ.get(key) for key in ("GROMA_STATE_DIR", schedule.TEST_MOCK_ENV)}
        os.environ["GROMA_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        os.environ[schedule.TEST_MOCK_ENV] = "1"
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root, name="主题甲")

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_the_classify_task_is_separate_from_the_scan_task(self) -> None:
        installed = schedule.install(self.root, 1440, kind="classify")
        self.assertTrue(installed["task_name"].endswith("-TOPIC"))
        self.assertEqual(schedule.status(self.root, kind="classify")["status"], "present")
        self.assertEqual(schedule.status(self.root)["status"], "absent")  # scan untouched
        # 登录态判据依赖本机已登录的 Harness（CI runner 没有，CI 红过一次实录）——没有就跳过
        status = schedule.status(self.root, kind="classify")
        if status["provider"]["logged_in"] is not True:
            self.skipTest("provider not logged in on this machine")
        self.assertEqual(status["provider"]["logged_in"], True)

    def test_the_classify_wrapper_runs_maintain_not_scan(self) -> None:
        wrapper = schedule.write_wrapper(self.root, kind="classify")
        body = Path(wrapper).read_text(encoding="utf-8")
        self.assertIn("topic maintain", body)
        self.assertNotIn(" digest", body)
        self.assertNotIn(" scan", body)

    def test_removing_one_kind_leaves_the_other_alone(self) -> None:
        schedule.install(self.root, 60, kind="scan")
        schedule.install(self.root, 1440, kind="classify")
        schedule.remove(self.root, kind="classify")
        self.assertEqual(schedule.status(self.root, kind="classify")["status"], "absent")
        self.assertEqual(schedule.status(self.root)["status"], "present")


class ClassifyAllTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state = os.environ.get("GROMA_STATE_DIR")
        os.environ["GROMA_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root, name="主题甲")
        folder = self.root / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "kind": "GROMA_LOCAL_DIGEST", "created_at": "2026-11-09T00:00:00Z",
                   "unit_id": unitmatch.load_unit(self.root)["unit_id"],
                   "workspace_root": self.root.as_posix(), "file_count": len(ROWS),
                   "event_count": 12, "parse_errors": 0, "files": ROWS}
        (folder / "digest-20261109T000000Z.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
        topics.create_topic("主题甲", intent="包含这批候选")
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        unitmatch.create_unit("主题乙", other)
        topics.create_topic("主题乙")
        self.folder = Path(self.tmp.name) / "bin"
        self.folder.mkdir()

    def tearDown(self) -> None:
        if self._state is None:
            os.environ.pop("GROMA_STATE_DIR", None)
        else:
            os.environ["GROMA_STATE_DIR"] = self._state
        self.tmp.cleanup()

    def test_only_topics_with_a_definition_are_sent_to_the_harness(self) -> None:
        token = classify.token_of(ROWS[0]["source"], ROWS[0]["path"])
        stub = make_provider(self.folder, 'echo ["%s"]' % token)
        result = classify_run.run_all(self.root, provider_path=str(stub), dry_run=True)
        by_topic = {row["topic"]: row for row in result["results"]}
        self.assertEqual(by_topic["主题甲"]["state"], "dry_run")
        self.assertEqual(by_topic["主题甲"]["chosen"], 1)
        self.assertEqual(by_topic["主题乙"]["state"], "skipped_no_intent")
        self.assertEqual(result["written"], 0)

    def test_a_dry_run_all_writes_no_events(self) -> None:
        token = classify.token_of(ROWS[0]["source"], ROWS[0]["path"])
        stub = make_provider(self.folder, 'echo ["%s"]' % token)
        classify_run.run_all(self.root, provider_path=str(stub), dry_run=True)
        self.assertEqual(topics.events(), [])


class FreshnessTests(unittest.TestCase):
    """A task that ran is not a task that wrote; the two must be separable."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state = os.environ.get("GROMA_STATE_DIR")
        os.environ["GROMA_STATE_DIR"] = str(Path(self.tmp.name) / "state")
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root, name="主题甲")
        self.folder = self.root / ".groma" / "digests"
        self.folder.mkdir(parents=True)

    def tearDown(self) -> None:
        if self._state is None:
            os.environ.pop("GROMA_STATE_DIR", None)
        else:
            os.environ["GROMA_STATE_DIR"] = self._state
        self.tmp.cleanup()

    def test_schtasks_times_are_parsed_or_ignored(self) -> None:
        self.assertIsNotNone(schedule._parse_task_time("11/10/2026 11:14:47 AM"))
        self.assertIsNone(schedule._parse_task_time("N/A"))

    def test_an_artifact_newer_than_the_run_counts_as_written(self) -> None:
        (self.folder / "digest-20261109T000000Z.json").write_text("{}", encoding="utf-8")
        stamp = Path(self.folder / "digest-20261109T000000Z.json").stat().st_mtime
        from datetime import datetime
        before = datetime.fromtimestamp(stamp - 3600).strftime("%m/%d/%Y %I:%M:%S %p")
        early = schedule._parse_task_time(before)
        result = schedule._freshness(self.root, "scan", {"Last Run Time": before})
        self.assertTrue(result["wrote_since_last_run"])
        self.assertIsNotNone(early)

    def test_an_artifact_older_than_the_run_is_reported_as_not_written(self) -> None:
        path = self.folder / "digest-20261109T000000Z.json"
        path.write_text("{}", encoding="utf-8")
        from datetime import datetime
        late = datetime.fromtimestamp(path.stat().st_mtime + 3600).strftime("%m/%d/%Y %I:%M:%S %p")
        result = schedule._freshness(self.root, "scan", {"Last Run Time": late})
        self.assertFalse(result["wrote_since_last_run"])

    def test_an_unreadable_time_is_unknown_not_false(self) -> None:
        self.assertIsNone(schedule._freshness(self.root, "scan", {"Last Run Time": "N/A"})["wrote_since_last_run"])

    def test_a_task_that_never_ran_reports_unknown_not_written(self) -> None:
        (self.folder / "digest-20261109T000000Z.json").write_text("{}", encoding="utf-8")
        result = schedule._freshness(self.root, "scan", {"Last Run Time": "11/10/2026 11:14:47 AM",
                                                         "Last Result": "267011"})
        self.assertIsNone(result["wrote_since_last_run"])


if __name__ == "__main__":
    unittest.main()
