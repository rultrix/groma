"""S4: one classification pass, with every failure ending in nothing written."""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import os
import tempfile
import unittest
from pathlib import Path

from lab import classify, classify_run, provider, state, topics, unitmatch
from lab.workspace import init_workspace

ROWS = [
    {"source": "qoder", "path": "projects/d--workspaces-unit-x/fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3.jsonl",
     "harness": "qoder-cn", "agent_kind": "primary", "events": 90,
     "session_key": "qoder-cn:fdf7c2c9-06d6-454a-9b7c-f9a1837ee8f3"},
    {"source": "codex", "path": "sessions/2026/11/07/rollout-2026-11-07T09-00-00-019feb06-25ea-7d21-9986-8d57ff1da526.jsonl",
     "harness": "codex", "agent_kind": "primary", "events": 12,
     "session_key": "codex:019feb06-25ea-7d21-9986-8d57ff1da526"},
]


def write_digest(root: Path) -> None:
    folder = root / ".groma" / "digests"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1, "kind": "GROMA_LOCAL_DIGEST", "created_at": "2026-11-08T09:00:00Z",
        "unit_id": unitmatch.load_unit(root)["unit_id"], "workspace_root": root.as_posix(),
        "file_count": len(ROWS), "event_count": 102, "parse_errors": 0, "files": ROWS,
    }
    (folder / "digest-20261108T090000Z.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")


def make_provider(folder: Path, answer: str) -> Path:
    path = folder / "qoderclicn.cmd"
    head = (
        "@echo off\r\n"
        'if "%1"=="--help" (\r\n'
        "  echo Usage: qoderclicn [options]\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        'if "%1"=="status" (\r\n'
        "  echo Version: 1.0.0\r\n"
        "  echo Login Method: browser\r\n"
        "  echo Auth Source: local\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
    )
    path.write_text(head + answer + "\r\n", encoding="utf-8", newline="")
    return path


class RunFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._state_before = os.environ.get(state.STATE_ENV)
        os.environ[state.STATE_ENV] = str((Path(self.tmp.name) / "state").resolve())
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root, name="主题甲")
        write_digest(self.root)
        topics.create_topic("主题甲", intent="测试用：包含这批候选")
        self.folder = Path(self.tmp.name) / "bin"
        self.folder.mkdir()
        self.tokens = [classify.token_of(row["source"], row["path"]) for row in ROWS]

    def tearDown(self) -> None:
        if self._state_before is None:
            os.environ.pop(state.STATE_ENV, None)
        else:
            os.environ[state.STATE_ENV] = self._state_before
        self.tmp.cleanup()

    def stub(self, answer: str) -> Path:
        return make_provider(self.folder, answer)


class PromptTests(RunFixture):
    def test_the_prompt_carries_tokens_not_locations(self) -> None:
        pack = classify.candidates(self.root)
        prompt = classify_run.prompt_for("主题甲", pack)
        for token in self.tokens:
            self.assertIn(token, prompt)
        self.assertNotIn("d--workspaces-unit-x", prompt)
        self.assertNotIn("\\", prompt)
        self.assertNotIn("sessions/2026", prompt)

    def test_a_fenced_json_array_is_accepted_and_prose_is_not(self) -> None:
        self.assertEqual(classify_run.parse_tokens('```json\n["a", "b"]\n```'), ["a", "b"])
        with self.assertRaises(ValueError):
            classify_run.parse_tokens("我觉得这两个不错。")


class RunTests(RunFixture):
    def test_dry_run_reports_the_choice_and_writes_nothing(self) -> None:
        stub = self.stub('echo ["%s"]' % self.tokens[0])
        result = classify_run.run_once(self.root, "主题甲", provider_path=str(stub), dry_run=True)
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(result["chosen"], [self.tokens[0]])
        self.assertEqual(topics.events(), [])
        self.assertEqual(classify_run.last_runs(), [])

    def test_an_applied_pass_writes_events_with_the_model_on_them(self) -> None:
        stub = self.stub('echo ["%s","%s"]' % (self.tokens[0], self.tokens[1]))
        result = classify_run.run_once(self.root, "主题甲", provider_path=str(stub), model="Qwen3.8-Flash")
        self.assertEqual((result["state"], result["added"]), ("applied", 2))
        events = topics.active_evidence(result["topic_id"])
        self.assertEqual({event["decided_by"] for event in events}, {"llm:qoderclicn@Qwen3.8-Flash"})
        self.assertEqual(len(classify_run.last_runs()), 1)

    def test_a_token_the_model_invented_is_refused(self) -> None:
        stub = self.stub('echo ["%s","ev-made-up"]' % self.tokens[0])
        result = classify_run.run_once(self.root, "主题甲", provider_path=str(stub))
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(result["refused"]), 1)

    def test_prose_instead_of_an_array_writes_nothing(self) -> None:
        stub = self.stub("echo 这两个看起来像同一件事")
        result = classify_run.run_once(self.root, "主题甲", provider_path=str(stub))
        self.assertEqual(result["state"], "unparsable")
        self.assertEqual(topics.events(), [])

    def test_a_missing_provider_writes_nothing(self) -> None:
        result = classify_run.run_once(self.root, "主题甲",
                                       provider_path=str(self.folder / "nope.exe"))
        self.assertEqual(result["state"], "no_provider")
        self.assertEqual(topics.events(), [])

    def test_a_failing_harness_writes_nothing(self) -> None:
        stub = self.stub("exit /b 7")
        result = classify_run.run_once(self.root, "主题甲", provider_path=str(stub))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(topics.events(), [])

    def test_the_harness_is_called_without_persisting_a_session(self) -> None:
        marker = self.folder / "argv.txt"
        stub = self.folder / "echoargs.cmd"
        body = (
            "@echo off\r\n"
            'if "%1"=="--help" (\r\n'
            "  echo Usage: qoderclicn [options]\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            'if "%1"=="status" (\r\n'
            "  echo Login Method: browser\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            'echo %1 %2 %3 %4 %5 %6 %7 %8 > "' + str(marker) + '"\r\n'
            'echo ["' + self.tokens[0] + '"]\r\n'
        )
        stub.write_text(body, encoding="utf-8", newline="")
        classify_run.run_once(self.root, "主题甲", provider_path=str(stub))
        argv = marker.read_text(encoding="utf-8")
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--permission-mode default", argv)
        self.assertIn("-m", argv)
        self.assertNotIn("bypass", argv)
        self.assertNotIn("dangerously", argv)


class StoredIntentTests(RunFixture):
    def test_the_stored_intent_reaches_the_prompt_without_a_flag(self) -> None:
        topics.set_intent("主题甲", "包含采集与对账；不含服务端业务开发")
        stub = self.stub('echo []')
        captured = {}
        real = classify_run.prompt_for

        def spy(name, pack, limit=classify_run.PROMPT_LIMIT, intent=None):
            captured["intent"] = intent
            return real(name, pack, limit, intent)

        classify_run.prompt_for = spy
        try:
            result = classify_run.run_once(self.root, "主题甲", provider_path=str(stub), dry_run=True)
        finally:
            classify_run.prompt_for = real
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(captured["intent"], "包含采集与对账；不含服务端业务开发")


class NoIntentTests(RunFixture):
    def test_a_topic_without_a_definition_is_refused(self) -> None:
        folder = Path(self.tmp.name) / "unit-no-intent"
        folder.mkdir()
        unitmatch.create_unit("没有说明的主题", folder)
        topics.create_topic("没有说明的主题")
        stub = self.stub('echo ["%s"]' % self.tokens[0])
        result = classify_run.run_once(self.root, "没有说明的主题", provider_path=str(stub), dry_run=True)
        self.assertEqual(result["state"], "no_intent")
        self.assertEqual(topics.events(), [])


class SettingsDrivenTests(RunFixture):
    def test_the_model_comes_from_settings_when_no_flag_is_given(self) -> None:
        from lab import settings

        settings.save_settings(self.root, {"classify_model": "GLM-5.3",
                                           "classify_provider": str(self.stub('echo []'))})
        result = classify_run.run_once(self.root, "主题甲", dry_run=True)
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(result["model"], "GLM-5.3")


class MaintainTests(RunFixture):
    def test_the_chain_reforges_only_when_evidence_moved(self) -> None:
        stub = self.stub('echo ["%s"]' % self.tokens[0])
        first = classify_run.maintain(self.root, provider_path=str(stub))
        self.assertEqual(first["steps"][0]["state"], "reforged")
        self.assertEqual(first["steps"][0]["sessions"], 1)
        second = classify_run.maintain(self.root, provider_path=str(stub))
        self.assertEqual(second["steps"][0]["state"], "unchanged")

    def test_a_foreign_injection_source_is_never_taken_over(self) -> None:
        from lab import asset

        foreign = asset.build_asset(self.root, "别人的资产", [(ROWS[0]["source"], ROWS[0]["path"])])
        stub = self.stub('echo ["%s"]' % self.tokens[0])
        result = classify_run.maintain(self.root, provider_path=str(stub))
        self.assertEqual(result["steps"][0]["state"], "reforged")
        self.assertEqual(result["steps"][0].get("injected"), [])
        self.assertTrue(asset.find_asset(self.root, foreign["asset_id"]))

    def test_the_nightly_repoint_carries_the_theme_being_reforged(self) -> None:  # A1-6（刀 W27a）
        """夜间换指向时要把"正在重铸的这个主题"交给注入这一步，不能让它去问落点目录的卡。

        成员区自带另一张卡时，没带作用域的注入会撞 `asset not found`，而这里
        `except Exception: continue` 把它咽掉 ⇒ 断链在界面上不留痕。
        """
        from lab import inject as inject_mod
        from lab import surface as surface_mod

        first = classify_run.maintain(self.root, provider_path=self.stub('echo ["%s"]' % self.tokens[0]))
        served = first["steps"][0]["asset_id"]
        unit_id = unitmatch.load_unit(self.root)["unit_id"]
        book = surface_mod.read_manifest(self.root)
        book["source"] = {"asset_id": served, "unit_id": unit_id}
        surface_mod.write_manifest(self.root, book)

        calls = []
        real = inject_mod.inject_unit

        def spy(root, asset_id, faces=None, allow_project_files=False, unit_id=None):
            calls.append({"asset_id": asset_id, "unit_id": unit_id})
            return real(root, asset_id, faces=faces, allow_project_files=allow_project_files)

        inject_mod.inject_unit = spy
        try:
            second = classify_run.maintain(self.root, provider_path=self.stub('echo ["%s", "%s"]' % tuple(self.tokens)))
        finally:
            inject_mod.inject_unit = real
        self.assertEqual(second["steps"][0]["state"], "reforged")
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["unit_id"], unit_id, "扇出没带主题作用域：成员区会撞 asset not found")


if __name__ == "__main__":
    unittest.main()


class FailureLedgerTests(RunFixture):
    """刀 W26a1：失败也要入账，账本里只有成功就等于断链时没人看得见。"""

    def test_a_failure_is_recorded_not_silently_dropped(self) -> None:  # A5-1
        stub = self.stub("exit /b 7")
        classify_run.run_once(self.root, "主题甲", provider_path=str(stub))
        rows = classify_run.last_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "failed")
        self.assertEqual(rows[0]["returncode"], 7)
        self.assertEqual(rows[0]["written"], 0)

    def test_unparsable_output_is_recorded_too(self) -> None:  # A5-1 续
        stub = self.stub("echo 我觉得这两个不错。")
        classify_run.run_once(self.root, "主题甲", provider_path=str(stub))
        self.assertEqual(classify_run.last_runs()[-1]["state"], "unparsable")

    def test_a_dry_run_still_writes_nothing(self) -> None:  # B5-1 的边界
        stub = self.stub("exit /b 7")
        classify_run.run_once(self.root, "主题甲", provider_path=str(stub), dry_run=True)
        self.assertEqual(classify_run.last_runs(), [])

    def test_applied_record_keeps_its_shape(self) -> None:  # B5-1
        stub = self.stub('echo ["%s"]' % self.tokens[0])
        classify_run.run_once(self.root, "主题甲", provider_path=str(stub))
        row = classify_run.last_runs()[-1]
        for key in ("topic_id", "topic", "model", "candidates", "chosen", "state",
                    "added", "refused", "rejected", "written", "at"):
            self.assertIn(key, row)
        self.assertEqual(row["state"], "applied")

    def test_summary_separates_attempt_from_success(self) -> None:
        rows = [
            {"topic": "主题甲", "state": "applied", "at": "2026-11-01T00:00:00Z"},
            {"topic": "主题甲", "state": "failed", "at": "2026-11-02T00:00:00Z"},
        ]
        summary = classify_run.run_summary(rows, "主题甲")
        self.assertEqual(summary["attempt_at"], "2026-11-02T00:00:00Z")
        self.assertEqual(summary["success_at"], "2026-11-01T00:00:00Z")
        self.assertEqual(summary["attempt_state"], "failed")
