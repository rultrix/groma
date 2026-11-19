"""S4: the provider is identified by its own first line, never by its path alone."""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import provider


def make_stub(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    path.write_text("@echo off\r\n" + body + "\r\n", encoding="utf-8", newline="")
    return path


class IdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_right_binary_is_proved_by_its_usage_line(self) -> None:
        stub = make_stub(self.folder, "good.cmd", "echo Usage: qoderclicn [options]")
        result = provider.check(str(stub))
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["path"], str(stub))
        self.assertIn("Usage: qoderclicn", result["first_line"])

    def test_a_binary_with_another_usage_line_is_refused(self) -> None:
        stub = make_stub(self.folder, "other.cmd", "echo Usage: gcloud compute")
        result = provider.check(str(stub))
        self.assertEqual(result["state"], "identity_mismatch")
        self.assertEqual(result["tried"][0]["first_line"], "Usage: gcloud compute")

    def test_a_missing_path_is_not_found_and_says_so(self) -> None:
        result = provider.check(str(self.folder / "nope.exe"))
        self.assertEqual(result["state"], "not_found")
        self.assertEqual(result["tried"][0]["returncode"], 127)

    def test_a_crashing_binary_is_not_mistaken_for_the_provider(self) -> None:
        stub = make_stub(self.folder, "crash.cmd", "echo boom & exit /b 3")
        result = provider.check(str(stub))
        self.assertEqual(result["state"], "identity_mismatch")
        self.assertEqual(result["tried"][0]["returncode"], 3)


class EnvironmentTests(unittest.TestCase):
    def test_inherited_qoder_variables_are_dropped_and_counted(self) -> None:
        clean, removed = provider.scrubbed_env({
            "PATH": "keep", "QODER_TOKEN": "x", "qoder_worker": "y", "USERNAME": "u",
        })
        self.assertEqual(removed, ["QODER_TOKEN", "qoder_worker"])
        self.assertEqual(set(clean), {"PATH", "USERNAME"})

    def test_a_provider_subprocess_cannot_see_them(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            stub = make_stub(Path(folder), "echo.cmd", "echo [%QODER_TOKEN%]")
            os.environ["QODER_TOKEN"] = "leak"
            try:
                done = provider.run([str(stub)])
            finally:
                os.environ.pop("QODER_TOKEN", None)
        self.assertEqual(done.stdout.strip(), "[]")

    def test_the_override_environment_variable_comes_first(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            stub = make_stub(Path(folder), "good.cmd", "echo Usage: qoderclicn")
            os.environ[provider.PROVIDER_ENV] = str(stub)
            try:
                self.assertEqual(provider.candidates()[0], str(stub))
                self.assertEqual(provider.check()["state"], "ok")
            finally:
                os.environ.pop(provider.PROVIDER_ENV, None)


class MachineTests(unittest.TestCase):
    def test_this_machine_finds_a_provider_by_its_identity(self) -> None:
        result = provider.check()
        if result["state"] == "not_found":
            self.skipTest("no qoderclicn on this machine")
        self.assertEqual(result["state"], "ok")
        self.assertIn("Usage: qoderclicn", result["first_line"])
        # `shutil.which` 的扩展名大小写跟着 PATHEXT 走（本机现取为 .EXE），断言不能钉字面量。
        self.assertTrue(result["path"].lower().endswith(("qoderclicn.exe", "qoderclicn")))

    def test_the_suffix_rule_tolerates_an_uppercase_extension(self) -> None:
        """上面那条判据换到大写扩展名上仍成立——不靠这台机器的 PATHEXT 恰好是什么。

        取法与真机那条同形（state=ok + 身份句 + 路径后缀），只是把 `which` 的结果换成
        大写 `.EXE`：大小写敏感的旧判据在这里就会红，而真机上一红就是整片 CI 皆红。
        """
        checked = provider.check()
        upper = checked.get("path") if checked["state"] == "ok" else None
        if not upper:
            self.skipTest("no qoderclicn on this machine")
        upper = str(Path(upper).with_suffix(".EXE"))
        with mock.patch.object(provider, "check", return_value={"state": "ok", "path": upper,
                                                                 "first_line": "Usage: qoderclicn"}):
            result = provider.check()
        self.assertEqual(result["state"], "ok")
        self.assertIn("Usage: qoderclicn", result["first_line"])
        self.assertTrue(result["path"].lower().endswith(("qoderclicn.exe", "qoderclicn")))


class AccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_models_drops_the_header_line(self) -> None:
        body = "echo MODEL\r\necho Fast\r\necho GLM-5.3"
        stub = make_stub(self.folder, "models.cmd", body)
        result = provider.models(str(stub))
        self.assertEqual(result["models"], ["Fast", "GLM-5.3"])

    def test_login_state_never_carries_the_account_name_or_avatar(self) -> None:
        body = ("echo Version: 9.9.9\r\necho Username: 张三\r\n"
                "echo Avatar: https://example/users/1/avatar\r\n"
                "echo Login Method: browser\r\necho Auth Source: local")
        stub = make_stub(self.folder, "status.cmd", body)
        result = provider.login_state(str(stub))
        self.assertTrue(result["logged_in"])
        self.assertEqual(result["version"], "9.9.9")
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("张三", text)
        self.assertNotIn("avatar", text.lower())

    def test_a_status_without_login_method_is_not_called_logged_in(self) -> None:
        stub = make_stub(self.folder, "out.cmd", "echo Version: 9.9.9")
        self.assertFalse(provider.login_state(str(stub))["logged_in"])


if __name__ == "__main__":
    unittest.main()
