"""CLI surface for the two CRUD axes: add/forget a workspace, delete a theme.

Two things are being pinned here:

- the commands work (A class: the commands do not exist before this cut), and
- `Groma` goes through **the same functions** as the web routes -- the orchestration
  (and with it the two guards) lives in `lab/inject.py`, so both surfaces inherit
  it instead of re-implementing it.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import inject, unitmatch
from lab.cli import main as cli_main
from lab.state import list_workspaces, normalise_path
from lab.workspace import init_workspace, load_unit


def run(argv: list[str]) -> tuple[int, object]:
    """Run the CLI and return (exit code, parsed JSON payload or raw text)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_main(argv)
    text = out.getvalue().strip()
    if not text:
        return code, None
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


class CliWorkspaceCrudTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="cli-unit", owner="tester")

    def _folder(self, name: str) -> Path:
        path = Path(self._tmp.name) / name
        path.mkdir()
        return path

    def _rows(self, path: Path) -> list[dict]:
        return [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(path)]

    # --- workspace add ------------------------------------------------------

    def test_workspace_add_registers_a_folder_without_a_theme(self) -> None:
        """A 类：`groma workspace add` 只登记（与 `POST /workspaces/add` 同一语义，不带主题）。"""
        other = self._folder("plain")
        code, payload = run(["--root", str(self.root), "workspace", "add", str(other)])
        self.assertEqual(code, 0)
        self.assertEqual(normalise_path(payload["path"]), normalise_path(other))
        rows = self._rows(other)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].get("unit_id"), "CLI 顺手给它配了一个主题")

    def test_workspace_add_refuses_a_missing_folder(self) -> None:
        """A 类（边界）：不存在的目录要被拒，不能往注册表里塞一行幽灵。"""
        ghost = Path(self._tmp.name) / "no-such-dir"
        code, _ = run(["--root", str(self.root), "workspace", "add", str(ghost)])
        self.assertNotEqual(code, 0)
        self.assertEqual(self._rows(ghost), [])

    # --- workspace remove ---------------------------------------------------

    def test_workspace_remove_needs_yes_and_says_what_would_go(self) -> None:
        """A 类（CLI 的两步确认）：不带 `--yes` 只报告，不动任何数据。"""
        other = self._folder("gone")
        init_workspace(other, name="gone-unit", owner="tester")
        run(["--root", str(self.root), "workspace", "add", str(other)])

        code, payload = run(["--root", str(self.root), "workspace", "remove", str(other)])
        self.assertEqual(code, 0)
        self.assertTrue(payload.get("would_remove"), "预览没说清要删什么")
        self.assertTrue((other / "groma-unit.json").is_file(), "没带 --yes 也把卡删了")
        self.assertEqual(len(self._rows(other)), 1, "没带 --yes 也把注册表行删了")

    def test_workspace_remove_goes_through_inject_forget_workspace(self) -> None:
        """同一函数钉：CLI 与 web 都调 `inject.forget_workspace`，不各写一套。"""
        other = self._folder("via-inject")
        run(["--root", str(self.root), "workspace", "add", str(other)])
        calls: list[tuple] = []

        def fake(root, boot_root=None):
            calls.append((Path(root), boot_root))
            return {"path": Path(root).as_posix(), "faces_removed": 0}

        with mock.patch.object(inject, "forget_workspace", side_effect=fake):
            code, _ = run(["--root", str(self.root), "workspace", "remove", str(other), "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, "CLI 没有走 inject.forget_workspace")
        self.assertEqual(normalise_path(calls[0][0]), normalise_path(other))

    # --- unit delete --------------------------------------------------------

    def test_unit_delete_previews_then_deletes(self) -> None:
        """A 类：不带 `--yes` 只预览；带了才删**主题**，工作区与 `.groma/` 全留。"""
        theme_home = self._folder("theme-home")
        code, created = run(["--root", str(self.root), "unit", "create",
                             "--name", "CLI 主题", "--home", str(theme_home)])
        self.assertEqual(code, 0)
        unit_id = created["unit_id"]

        code, preview = run(["--root", str(self.root), "unit", "delete", unit_id])
        self.assertEqual(code, 0)
        self.assertTrue(preview.get("would_delete"))
        self.assertTrue((theme_home / "groma-unit.json").is_file(), "没带 --yes 也删了卡")

        code, payload = run(["--root", str(self.root), "unit", "delete", unit_id, "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["unit_id"], unit_id)
        self.assertNotIn(unit_id, [item["unit_id"] for item in unitmatch.unit_list()])
        self.assertFalse((theme_home / "groma-unit.json").is_file(), "身份卡没删（F2 的复活路径）")
        self.assertTrue((theme_home / ".groma" / "unit.json").is_file(), ".groma/ 被删了（该留）")

    def test_unit_delete_goes_through_inject_drop_unit(self) -> None:
        """同一函数钉：CLI 与 `POST /units/delete` 都调 `inject.drop_unit`。"""
        theme_home = self._folder("theme-via-inject")
        code, created = run(["--root", str(self.root), "unit", "create",
                             "--name", "CLI 主题二", "--home", str(theme_home)])
        self.assertEqual(code, 0)
        unit_id = created["unit_id"]
        calls: list[tuple] = []

        def fake(unit, boot_root=None):
            calls.append((unit, boot_root))
            return {"unit_id": unit, "name": "CLI 主题二"}

        with mock.patch.object(inject, "drop_unit", side_effect=fake):
            code, _ = run(["--root", str(self.root), "unit", "delete", unit_id, "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, "CLI 没有走 inject.drop_unit")
        self.assertEqual(calls[0][0], unit_id)

    # --- the guards are inherited, not re-implemented ------------------------

    def test_the_boot_root_guard_is_inherited_by_the_cli(self) -> None:
        """护栏住在编排里 ⇒ CLI 自动继承（W13 的验收点）。

        先加一个工作区，让"最后一个"那道护栏不可能抢答——这条只考启动目录那一道。
        """
        decoy = self._folder("decoy")
        run(["--root", str(self.root), "workspace", "add", str(decoy)])
        code, _ = run(["--root", str(self.root), "workspace", "remove", str(self.root), "--yes"])
        self.assertNotEqual(code, 0, "CLI 把 Groma 自己的启动目录移除了")
        self.assertTrue((self.root / "groma-unit.json").is_file())
        self.assertEqual(len(self._rows(self.root)), 1)

    def test_the_last_theme_guard_is_inherited_by_the_cli(self) -> None:
        """CLI 也删不掉本机唯一的主题。

        ⚠ 与 W12 同一个遮蔽：本机唯一主题的家就是启动目录，所以 `drop_unit` 里 ① 先命中；
        "最后一个"那道 ② 在两个界面上都被 ① 挡在前面（W12 的用例走内核才单独考到它）。
        这条钉的是"CLI 没有绕过编排"，不是"② 单独可达"。
        """
        boot = load_unit(self.root)["unit_id"]
        code, _ = run(["--root", str(self.root), "unit", "delete", boot, "--yes"])
        self.assertNotEqual(code, 0)
        self.assertIn(boot, [item["unit_id"] for item in unitmatch.unit_list()])


if __name__ == "__main__":
    unittest.main()
