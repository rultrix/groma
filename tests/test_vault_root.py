"""Vault location guardrails (docs/CONTEXT-VAULT.md §3 C1/C2/C7/C8, §5 naming).

State and vault config are redirected into a temp `GROMA_STATE_DIR` everywhere, so
nothing here reads or writes the real machine registry.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import vault
from lab.state import case_insensitive_fs, state_root
from lab.workspace import LabError


class VaultTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name).resolve()
        self.addCleanup(self._dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.tmp / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GROMA_VAULT_DIR", None)
        (self.tmp / "state").mkdir(parents=True, exist_ok=True)

    def codes(self, report: dict) -> list[str]:
        return [finding["code"] for finding in report["findings"]]

    def levels(self, report: dict, code: str) -> list[str]:
        return [f["level"] for f in report["findings"] if f["code"] == code]


class RootResolution(VaultTestCase):
    def test_platform_default_shares_the_state_dispatch(self) -> None:
        base = self.tmp / "base"
        with mock.patch("lab.vault.state_base", return_value=base):
            self.assertEqual(vault.default_root(), base / "groma" / "vault")

    def test_saved_root_is_used_when_no_environment_override(self) -> None:
        (state_root() / "vault.json").write_text(
            json.dumps({"schema": 1, "root": str(self.tmp / "somewhere")} ), encoding="utf-8"
        )
        self.assertEqual(vault.vault_root(), self.tmp / "somewhere")

    def test_environment_override_wins_over_the_saved_root(self) -> None:
        (state_root() / "vault.json").write_text(
            json.dumps({"schema": 1, "root": str(self.tmp / "somewhere")}), encoding="utf-8"
        )
        os.environ["GROMA_VAULT_DIR"] = str(self.tmp / "override")
        self.assertEqual(vault.vault_root(), self.tmp / "override")

    def test_unreadable_config_falls_back_instead_of_raising(self) -> None:
        (state_root() / "vault.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(vault.load_config(), {"schema": 1})


class WorkTreeAndWorkspace(VaultTestCase):
    def test_path_inside_a_git_work_tree_is_rejected(self) -> None:
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        target = self.tmp / "repo" / "deep" / "wb-vault"
        report = vault.check_root(target)
        self.assertEqual(report["verdict"], "reject")
        self.assertIn("C1", self.codes(report))

    def test_linked_worktree_dotgit_file_is_also_a_work_tree(self) -> None:
        (self.tmp / "wt").mkdir()
        (self.tmp / "wt" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        self.assertIn("C1", self.codes(vault.check_root(self.tmp / "wt" / "vault")))

    def test_the_work_tree_root_itself_counts_as_inside_it(self) -> None:
        (self.tmp / "repo2" / ".git").mkdir(parents=True)
        self.assertIn("C1", self.codes(vault.check_root(self.tmp / "repo2")))

    def test_outside_any_work_tree_there_is_no_c1_finding(self) -> None:
        self.assertNotIn("C1", self.codes(vault.check_root(self.tmp / "clean" / "vault")))

    def test_inside_a_registered_workspace_is_rejected(self) -> None:
        workspace = self.tmp / "joined"
        with mock.patch.object(vault, "list_workspaces", return_value=[{"path": str(workspace)}]):
            report = vault.check_root(workspace / "sub" / "vault")
        self.assertIn("C2", self.codes(report))

    @unittest.skipUnless(case_insensitive_fs(), "only a folding filesystem can match here")
    def test_a_case_flipped_workspace_still_counts_as_inside_it(self) -> None:
        # 防御钉，不是 bug 证明：Windows 的 resolve() 会把手写错的大小写规范化回磁盘上的形式，
        # 所以改坏 normalise_path 的折叠这条也照样绿——折叠本身由 tests/test_platform_paths.py 钉着。
        # 这里只钉"C2 用的是归一化后的前缀比较"这一步不会被绕过。
        workspace = self.tmp / "CaseProbe"
        workspace.mkdir()
        flipped = str(workspace).swapcase()
        self.assertNotEqual(flipped, str(workspace))
        with mock.patch.object(vault, "list_workspaces", return_value=[{"path": str(workspace)}]):
            report = vault.check_root(Path(flipped) / "vault")
        self.assertIn("C2", self.codes(report))
        detail = [f for f in report["findings"] if f["code"] == "C2"][0]["detail"]
        self.assertIn(str(workspace), detail)

    def test_a_sibling_folder_is_not_inside_a_registered_workspace(self) -> None:
        workspace = self.tmp / "joined"
        with mock.patch.object(vault, "list_workspaces", return_value=[{"path": str(workspace)}]):
            report = vault.check_root(self.tmp / "joined-other" / "vault")
        self.assertNotIn("C2", self.codes(report))


class LocationHygiene(VaultTestCase):
    def test_sync_folder_component_is_rejected_case_insensitively(self) -> None:
        report = vault.check_root(self.tmp / "ONEDRIVE" / "Groma" / "vault")
        self.assertIn("C7", self.codes(report))
        self.assertEqual(self.levels(report, "C7"), ["reject"])

    def test_icloud_uses_the_spelling_it_really_has(self) -> None:
        # `Mobile Documents` with a space, not `MobileDocuments`: exact-component
        # matching passed the second one and let the first one through.
        report = vault.check_root(self.tmp / "Library" / "Mobile Documents" / "Groma" / "vault")
        self.assertIn("C7", self.codes(report))
        self.assertEqual(self.levels(report, "C7"), ["reject"])

    def test_a_tenant_sufficed_onedrive_folder_is_still_a_sync_root(self) -> None:
        report = vault.check_root(self.tmp / "OneDrive - Contoso" / "wb-vault")
        self.assertIn("C7", self.codes(report))

    def test_an_ordinary_folder_is_not_called_a_sync_root(self) -> None:
        self.assertNotIn("C7", self.codes(vault.check_root(self.tmp / "work" / "wb-vault")))

    def test_mount_point_only_warns_and_says_why(self) -> None:
        if os.name == "nt":
            self.skipTest("/mnt is POSIX; resolve() on Windows re-roots it under the cwd drive")
        report = vault.check_root("/mnt/external/wb-vault")
        self.assertIn("C7", self.codes(report))
        self.assertEqual(self.levels(report, "C7"), ["warn"])
        self.assertEqual(report["verdict"], "warn")

    @unittest.skipUnless(os.name == "nt", "MAX_PATH is a Windows budget")
    def test_deep_root_is_rejected_on_the_path_budget(self) -> None:
        deep = self.tmp / ("d" * 60) / ("e" * 60) / ("f" * 60) / "vault"
        report = vault.check_root(deep)
        self.assertIn("LEN", self.codes(report))
        self.assertEqual(self.levels(report, "LEN"), ["reject"])

    @unittest.skipUnless(os.name == "nt", "the per-user ACL assertion is the Windows one")
    def test_a_vault_outside_the_user_profile_warns_about_acls(self) -> None:
        fake_profile = str(self.tmp / "profile")
        with mock.patch.dict(
            os.environ, {"LOCALAPPDATA": fake_profile, "APPDATA": fake_profile, "USERPROFILE": fake_profile}
        ):
            report = vault.check_root(self.tmp / "vault")
        self.assertIn("C8", self.codes(report))
        self.assertEqual(self.levels(report, "C8"), ["warn"])

    @unittest.skipUnless(os.name == "nt", "the per-user ACL assertion is the Windows one")
    def test_a_vault_under_localappdata_gets_no_acl_warning(self) -> None:
        report = vault.check_root(Path(os.environ["LOCALAPPDATA"]) / "Groma" / "vault")
        self.assertNotIn("C8", self.codes(report))


class Naming(VaultTestCase):
    def test_colon_never_reaches_the_filesystem(self) -> None:
        self.assertEqual(vault.safe_dir("qoder:11111111-2222-3333-4444-555555555555"),
                         "11111111-2222-3333-4444-555555555555")

    def test_separators_and_case_fold_the_same_way_on_every_os(self) -> None:
        with mock.patch.object(os, "name", "posix"):
            posix_shape = vault.safe_dir("codex:/Users/Foo/Session-ID")
        windows_shape = vault.safe_dir("codex:/Users/Foo/Session-ID")
        self.assertEqual(posix_shape, windows_shape)
        self.assertEqual(posix_shape, "users-foo-session-id")

    def test_a_key_without_a_colon_is_used_whole(self) -> None:
        self.assertEqual(vault.safe_dir("019FEB06-25EA"), "019feb06-25ea")

    def test_a_subagent_shaped_id_keeps_its_whole_stem(self) -> None:
        # 形状钉，不是 bug 证明：qoder 子 Agent 的 transcript 尾段不是 uuid（家仓实测 39 字，
        # `agent-ageneral-purpose-<16hex>`），这条钉的是 safe_dir 不会偷偷假设键定长或必须是 uuid。
        key = "qoder:agent-ageneral-purpose-2a43b54a5a95461d"
        self.assertEqual(vault.safe_dir(key), "agent-ageneral-purpose-2a43b54a5a95461d")
        self.assertEqual(len(vault.safe_dir(key)), 39)

    def test_an_id_that_is_only_punctuation_is_refused(self) -> None:
        # Not cosmetic: `qoder:` must not silently land every such session in one
        # `qoder` directory, which is what falling back to the whole key would do.
        for key in ("qoder:", "qoder:..", "codex::"):
            with self.subTest(key=key):
                with self.assertRaises(LabError):
                    vault.safe_dir(key)


class EnsureAndSave(VaultTestCase):
    def test_ensure_usable_raises_with_the_finding_code_in_the_message(self) -> None:
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        with self.assertRaises(LabError) as caught:
            vault.ensure_usable(self.tmp / "repo" / "vault")
        self.assertIn("C1", str(caught.exception))

    def test_a_warning_only_location_is_returned_not_raised(self) -> None:
        target = self.tmp / ("u" * 30) / "vault"
        report = vault.check_root(target)
        self.assertNotEqual(report["verdict"], "reject")
        self.assertEqual(vault.ensure_usable(target)["root"], report["root"])

    def test_save_refuses_and_creates_nothing(self) -> None:
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        target = self.tmp / "repo" / "vault"
        with self.assertRaises(LabError):
            vault.save_root(target)
        self.assertFalse(target.exists())
        self.assertFalse((state_root() / "vault.json").exists())

    def test_save_records_the_root_and_the_check_that_let_it_through(self) -> None:
        target = self.tmp / "vault-root"
        report = vault.save_root(target)
        self.assertTrue(target.is_dir())
        saved = json.loads((state_root() / "vault.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["root"], str(target))
        self.assertEqual(saved["last_check"]["verdict"], report["verdict"])
        self.assertEqual(saved["schema"], 1)

    def test_report_is_json_serialisable(self) -> None:
        report = vault.check_root(self.tmp / "vault-root")
        json.dumps(report)

    @unittest.skipIf(os.name == "nt", "chmod has no effect on a Windows volume here")
    def test_created_vault_is_not_readable_by_other_users(self) -> None:
        target = self.tmp / "vault-mode"
        vault.save_root(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
