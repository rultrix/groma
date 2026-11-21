"""脚本的沙箱不许打进真状态目录（W31 那格的运行时守卫）。

起因：`workspaces.json` 里躺着两枚 `Temp/w28box-*` 登记，仓内 grep 0 命中——一次性探针不落仓，
没有 diff 可审、只有注册表那一行痕迹，而那一行不区分"用户真把区放在 Temp 下"与"某个脚本刚拿 Temp 当沙箱"。
所以守卫只能长在**登记那一步**，并且必须留出显式出口，否则把正常的沙箱验证一起挡死。

`state_root` 是每次读写现算的，所以"没设环境变量"这条路可以这样演：清掉 `GROMA_STATE_DIR`
（守卫看的就是它）＋把 `state_root` 指到隔离目录（写入永远到不了真注册表）。
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import state
from lab.state import register_workspace
from lab.workspace import LabError

REAL_REGISTRY = Path.home() / "AppData" / "Local" / "groma" / "state" / "workspaces.json"


class EphemeralGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _isolated_state(self, name: str) -> Path:
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _temp_folder(self, parent: Path, prefix: str) -> Path:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))

    def test_the_default_state_dir_refuses_a_temp_folder(self) -> None:
        """A-1：没设状态目录环境变量时，登记 `Temp` 下的路径必须被拒，且一个字都不写。"""
        probe = self._temp_folder(self.base, "w31probe-")
        state_dir = self._isolated_state("default-like")
        before = REAL_REGISTRY.stat().st_mtime_ns if REAL_REGISTRY.is_file() else None
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(state, "state_root", return_value=state_dir):
            with self.assertRaises(LabError) as caught:
                register_workspace(probe, probe.name)
        self.assertIn("临时目录", str(caught.exception))
        self.assertFalse((state_dir / "workspaces.json").exists(), "拒登记的那条路还是把注册表写了")
        after = REAL_REGISTRY.stat().st_mtime_ns if REAL_REGISTRY.is_file() else None
        self.assertEqual(before, after, "拒登记的那条路把真注册表写了")

    def test_an_explicit_state_dir_still_registers_a_temp_folder(self) -> None:
        """A-1 的反向半边：挡住了泄漏，不许顺手把沙箱验证挡死。

        这条是这刀的成败点——本仓自己的测试天天在 `Temp` 下造工作区，靠的就是隔离的状态目录。
        缺了这半边，守卫会把自己这一族的每一轮验证都变成误伤。
        """
        probe = self._temp_folder(self.base, "w31sandbox-")
        with mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self._isolated_state("sandboxed"))}):
            record = register_workspace(probe, probe.name)
            self.assertEqual(1, len(state.list_workspaces()))
        self.assertEqual(probe.as_posix(), Path(record["path"]).as_posix())

    def test_the_flag_is_what_lets_an_ephemeral_path_through(self) -> None:
        """A-2：同一条路径，不带标记拒、带标记放行——两态各钉一次，防"参数没接上但测试也绿"。"""
        probe = self._temp_folder(self.base, "w31flag-")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(state, "state_root", return_value=self._isolated_state("flag-off")):
            with self.assertRaises(LabError):
                register_workspace(probe, probe.name)
        on_disk = self._isolated_state("flag-on")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(state, "state_root", return_value=on_disk):
            register_workspace(probe, probe.name, allow_ephemeral=True)
        rows = json.loads((on_disk / "workspaces.json").read_text(encoding="utf-8"))["workspaces"]
        self.assertEqual([probe.as_posix()], [row["path"] for row in rows])

    def _predicate(self):
        """判据得是个能单独调的东西：页面那枚 badge 要复用同一个函数，不能各写一遍。"""
        helper = getattr(state, "ephemeral_root", None)
        if helper is None:
            self.fail("state 上没有那个判断：登记那一步与界面 badge 只能各写一份范围")
        return helper

    def test_a_normal_folder_is_never_flagged_as_ephemeral(self) -> None:
        """判据本身：只认"在系统临时目录下"。

        反向半边必须用一个**真的不在 Temp 下**的路径——本文件的 `self.base` 本身就是
        `TemporaryDirectory()`，拿它当"普通目录"等于让判据自己给自己作证。
        """
        self.assertFalse(self._predicate()(Path.home() / "projects" / "an-ordinary-repo"),
                         "普通工作区被判成沙箱 ⇒ 登记这条路会被一起挡死")
        self.assertFalse(self._predicate()(Path(__file__).resolve().parents[1]),
                         "本仓自己被认成沙箱 ⇒ 用户日常那台机器上的区都登不进来")
        self.assertTrue(self._predicate()(Path(tempfile.gettempdir()) / "w31-anything"))
        self.assertTrue(self._predicate()(self.base), "测试夹具的根就在 Temp 下——它必须被认出来")


if __name__ == "__main__":
    unittest.main()
