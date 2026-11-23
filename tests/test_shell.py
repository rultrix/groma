"""Shell internals that do not need a display: server thread and tray actions."""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import json
import inspect
import os
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from lab.sources import add_source
from lab.workspace import init_workspace

try:
    from groma_shell import controller
    from groma_shell.server import LocalServer

    HAVE_SHELL = True
except ImportError:  # pragma: no cover - werkzeug/Flask absent
    HAVE_SHELL = False


@unittest.skipUnless(HAVE_SHELL, "Flask/werkzeug are not installed in this interpreter")
class ShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="shell-unit", owner="tester")
        self.src = Path(self._tmp.name) / "src"
        (self.src / "projects").mkdir(parents=True)
        (self.src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-10-28T00:00:00Z", "cwd": "%s", "type": "user"}\n' % self.root.as_posix(),
            encoding="utf-8",
            newline="\n",
        )
        env = mock.patch.dict(os.environ, {"GROMA_STARTUP_DIR": str(Path(self._tmp.name) / "startup")})
        env.start()
        self.addCleanup(env.stop)

    def test_server_serves_healthz_in_a_thread(self) -> None:
        server = LocalServer(self.root, port=0).start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % server.port, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
        finally:
            server.stop()

    def test_port_falls_back_when_requested_is_busy(self) -> None:
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        busy = blocker.getsockname()[1]
        try:
            server = LocalServer(self.root, host="127.0.0.1", port=busy).start()
            try:
                self.assertNotEqual(server.port, busy)
                with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % server.port, timeout=10) as response:
                    self.assertTrue(json.loads(response.read().decode("utf-8"))["ok"])
            finally:
                server.stop()
        finally:
            blocker.close()

    def test_toggle_pauses_then_resumes(self) -> None:
        self.assertEqual(controller.status(self.root)["label"], "采集中")
        self.assertEqual(controller.status(self.root)["menu_label"], "暂停采集")
        paused = controller.toggle(self.root)
        self.assertTrue(paused["paused"])
        self.assertEqual(paused["label"], "已暂停")
        self.assertEqual(paused["menu_label"], "恢复采集")
        resumed = controller.toggle(self.root)
        self.assertFalse(resumed["paused"])
        self.assertEqual(resumed["label"], "采集中")

    def test_pause_keeps_the_shared_trigger(self) -> None:
        # Pausing one workspace must not stop collection of the others.
        from lab import trigger as trigger_mod

        add_source(self.root, "src", str(self.src), include=["projects"])
        trigger_mod.install(self.root)
        self.assertTrue(trigger_mod.status(self.root)["installed"])
        controller.toggle(self.root)
        self.assertTrue(trigger_mod.status(self.root)["installed"])
        controller.toggle(self.root)
        self.assertTrue(trigger_mod.status(self.root)["installed"])


    def test_tray_menu_is_pure_data_with_about_and_update(self) -> None:
        """§7：托盘五项动作必须是**纯数据**，菜单形状不许写在 pystray 调用里。

        这条存在的原因是一条硬规定——测试不许把真实 GUI 拉起来。回归证伪：把
        `controller.menu_items()` 删回三项，本条立刻红；把「检查更新」的 action 接到启动路径，
        `test_startup_and_create_app_do_not_call_update_network` 会红（两处各管一半）。
        """
        specs = controller.menu_items(8787, controller.status(self.root))
        self.assertEqual([spec["label"] for spec in specs],
                         ["打开窗口", "关于 Groma", "检查更新", "暂停采集", "退出"])
        self.assertEqual([spec["action"] for spec in specs],
                         ["open_window", "open_about", "check_update", "toggle_capture", "quit"])
        self.assertEqual([spec["default"] for spec in specs], [True, False, False, False, False])
        self.assertEqual(specs[1]["url"], "http://127.0.0.1:8787/about")
        self.assertEqual(controller.about_url(8899), "http://127.0.0.1:8899/about")

        paused = controller.toggle(self.root)
        self.addCleanup(controller.toggle, self.root)  # 会话补的卫生修复：别把共享 state 留在「已暂停」，同进程后面的测试 catchup 会被 paused 跳过
        stopped = controller.menu_items(8787, paused)
        self.assertEqual(stopped[3]["label"], "恢复采集", "暂停语义被菜单改造带走了")

        from groma_shell import app as shell_app

        body = inspect.getsource(shell_app.run)
        for action in ("open_window", "open_about", "check_update", "toggle_capture", "quit"):
            self.assertIn('"%s"' % action, body, "菜单项 %s 在壳里没接到处理函数" % action)
        self.assertIn("controller.menu_items(port", body, "壳还在自己拼菜单，没走纯数据函数")


if __name__ == "__main__":
    unittest.main()
