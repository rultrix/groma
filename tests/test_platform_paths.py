"""Platform behaviour of path folding and the state root.

These assertions exist because `ws_ref` is `sha256(normalise_path(...))`: if the
same folder folds differently on a second OS, the two machines silently stop
recognising it as one workspace.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import os
import sys
import unittest
from pathlib import PurePosixPath, PureWindowsPath
from unittest import mock

from lab.digest import in_workspace
from lab.state import case_insensitive_fs, normalise_path, state_base, state_root

# Built from code points, never typed as text: written as literals the two forms
# look identical and any editor normalises one, which makes the NFC assertion pass
# vacuously. It happened -- the first version of this file was exactly that.
NFD = "cafe" + chr(0x0301)  # e + combining acute: what macOS stores
NFC = "caf" + chr(0x00E9)  # precomposed: what Windows sends


class CaseFoldingTests(unittest.TestCase):
    def test_windows_and_macintosh_fold_case(self) -> None:
        with mock.patch.object(os, "name", "nt"), mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(case_insensitive_fs())
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sys, "platform", "darwin"):
            self.assertTrue(case_insensitive_fs())
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(case_insensitive_fs())

    def test_normalise_path_folds_case_only_when_the_fs_is_insensitive(self) -> None:
        mixed = "D:/Workspaces/CTX_Parser"
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(normalise_path(mixed), "d:/workspaces/ctx_parser")
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(normalise_path(mixed), "D:/Workspaces/CTX_Parser")

    def test_backslashes_fold_on_every_platform(self) -> None:
        """A cwd copied off a Windows machine must still fold to one shape."""
        windows_path = "C:\\Users\\DELL\\workspaces\\ctx_parser"
        cases = {
            ("nt", "win32"): "c:/users/dell/workspaces/ctx_parser",
            ("posix", "darwin"): "c:/users/dell/workspaces/ctx_parser",
            ("posix", "linux"): "C:/Users/DELL/workspaces/ctx_parser",
        }
        for names, expected in cases.items():
            with mock.patch.object(os, "name", names[0]), mock.patch.object(sys, "platform", names[1]):
                self.assertEqual(normalise_path(windows_path), expected, names)

    def test_mac_nfd_and_windows_nfc_forms_fold_together(self) -> None:
        self.assertNotEqual(NFD, NFC)  # guard: if these match, the test below proves nothing
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(normalise_path(NFD), normalise_path(NFC))

    def test_separators_and_trailing_slash_collapse(self) -> None:
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(normalise_path("D:/a//b/"), "D:/a/b")

    def test_blank_input_stays_blank(self) -> None:
        """`Path("").as_posix()` is '.', which would hash like a real folder."""
        self.assertEqual(normalise_path(""), "")
        self.assertEqual(normalise_path("   "), "")

    def test_attribution_survives_the_two_spellings_of_one_folder(self) -> None:
        workspace = "D:/workspaces/ctx_parser"
        with mock.patch.object(os, "name", "nt"), mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(in_workspace("D:\\Workspaces\\CTX_Parser\\sub", workspace))
        with mock.patch.object(os, "name", "posix"), mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(in_workspace("", workspace))
            self.assertFalse(in_workspace("D:/workspaces/other", workspace))


def _fake_path(flavour, home):
    """`lab.state.Path` swapped for a flavour we can build on any host.

    A Windows machine cannot instantiate PosixPath at all, so faking `os.name`
    alone would raise inside the code under test rather than test it.
    """

    class _Fake(flavour):
        @classmethod
        def home(cls):
            return cls(home)

    return _Fake


class StateRootTests(unittest.TestCase):
    def test_windows_uses_localappdata(self) -> None:
        with mock.patch.object(os, "name", "nt"), mock.patch.object(
            sys, "platform", "win32"
        ), mock.patch.dict(
            os.environ, {"LOCALAPPDATA": "C:/Users/DELL/AppData/Local"}
        ), mock.patch(
            "lab.state.Path", _fake_path(PureWindowsPath, "C:/Users/DELL")
        ):
            self.assertEqual(state_base(), PureWindowsPath("C:/Users/DELL/AppData/Local"))

    def test_windows_without_localappdata_stays_inside_profile(self) -> None:
        with mock.patch.object(os, "name", "nt"), mock.patch.object(
            sys, "platform", "win32"
        ), mock.patch.dict(os.environ, {"LOCALAPPDATA": ""}), mock.patch(
            "lab.state.Path", _fake_path(PureWindowsPath, "C:/Users/DELL")
        ):
            self.assertEqual(state_base(), PureWindowsPath("C:/Users/DELL/AppData/Local"))

    def test_macintosh_does_not_land_in_the_linux_directory(self) -> None:
        with mock.patch.object(os, "name", "posix"), mock.patch.object(
            sys, "platform", "darwin"
        ), mock.patch("lab.state.Path", _fake_path(PurePosixPath, "/Users/wangwei")):
            self.assertEqual(state_base(), PurePosixPath("/Users/wangwei/Library/Application Support"))

    def test_linux_honours_xdg_data_home(self) -> None:
        with mock.patch.object(os, "name", "posix"), mock.patch.object(
            sys, "platform", "linux"
        ), mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/xdg"}), mock.patch(
            "lab.state.Path", _fake_path(PurePosixPath, "/home/wangwei")
        ):
            self.assertEqual(state_base(), PurePosixPath("/tmp/xdg"))

    def test_linux_defaults_to_local_share(self) -> None:
        with mock.patch.object(os, "name", "posix"), mock.patch.object(
            sys, "platform", "linux"
        ), mock.patch.dict(os.environ, {"XDG_DATA_HOME": ""}), mock.patch(
            "lab.state.Path", _fake_path(PurePosixPath, "/home/wangwei")
        ):
            self.assertEqual(state_base(), PurePosixPath("/home/wangwei/.local/share"))

    def test_state_root_appends_groma_state(self) -> None:
        with mock.patch.object(os, "name", "posix"), mock.patch.object(
            sys, "platform", "darwin"
        ), mock.patch.dict(os.environ, {"GROMA_STATE_DIR": ""}), mock.patch(
            "lab.state.Path", _fake_path(PurePosixPath, "/Users/wangwei")
        ):
            self.assertEqual(
                state_root(),
                PurePosixPath("/Users/wangwei/Library/Application Support/groma/state"),
            )

    def test_state_dir_env_override_wins_over_the_platform(self) -> None:
        with mock.patch.dict(os.environ, {"GROMA_STATE_DIR": "/tmp/given/state"}), mock.patch(
            "lab.state.Path", _fake_path(PurePosixPath, "/Users/wangwei")
        ):
            self.assertEqual(state_root(), PurePosixPath("/tmp/given/state"))


if __name__ == "__main__":
    unittest.main()
