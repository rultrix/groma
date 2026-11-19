"""刀 W25b · 关于页判据（A1–A5）。

跑法：`./.venv/Scripts/python.exe -m unittest discover -s tests -p test_about_page.py -v`
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must be first real import
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import __version__
from lab import update
from lab.workspace import init_workspace

try:
    from web.app import create_app

    HAVE_FLASK = True
except ImportError:  # pragma: no cover
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class AboutPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="about-unit", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(Path(self._tmp.name) / "startup"),
            "GROMA_STATE_DIR": str(Path(self._tmp.name) / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = create_app(self.root).test_client()

    def test_about_page_shows_version_paths_and_security_boundary(self) -> None:  # A1
        body = self.client.get("/about").get_data(as_text=True)
        self.assertIn("关于 Groma", body)
        self.assertIn(__version__, body, "版本没现读")
        self.assertIn(self.root.as_posix(), body, "工作区根没显示")
        from lab.state import state_root
        from lab.vault import vault_root

        self.assertIn(state_root().as_posix(), body, "状态根没显示")
        self.assertIn(vault_root().as_posix(), body, "Vault 根没显示")
        self.assertIn("不主动联网", body, "安全边界那句没进来")
        self.assertIn("明确点击", body)
        self.assertIn("不读取凭据", body)

    def test_about_does_not_add_a_seventh_primary_nav_entry(self) -> None:  # A2
        """关于的入口走页头品牌与页脚，主导航 `<nav>` 仍恰好六项（§0.3 的既有契约）。"""
        body = self.client.get("/about").get_data(as_text=True)
        first_nav = re.search(r"<nav\b[^>]*>([\s\S]*?)</nav>", body).group(1)
        self.assertEqual(first_nav.count("<a "), 6, "主导航被加成第七项了")
        self.assertNotIn("关于</a>", first_nav, "关于不许挤进主导航")
        self.assertIn('<span class="brand"><a href="/about">', body, "页头品牌没链到关于页")
        self.assertIn('<footer>本地运行 · 不上传 · 不写回 Harness ｜ <a href="/about">关于</a>', body)

    def test_update_status_empty_state_is_not_up_to_date(self) -> None:  # A3
        body = self.client.get("/about").get_data(as_text=True)
        self.assertIn("尚未检查更新", body)
        self.assertNotIn("已是最新", body, "没查过就说已最新，是 §6.3 明令不许的那种谎")

    def test_update_status_error_is_not_rendered_as_current(self) -> None:  # A4
        update.save(self.root, update.record("owner/repo", state="network_error",
                                             error="timed out"))
        body = self.client.get("/about").get_data(as_text=True)
        self.assertIn("最近一次检查失败", body)
        self.assertIn("timed out", body, "失败原因要可见，不能只说失败了")
        self.assertNotIn("已是最新", body)

    def test_about_page_is_zero_javascript(self) -> None:  # A5
        body = self.client.get("/about").get_data(as_text=True)
        self.assertNotIn("<script", body)
        self.assertNotIn("javascript:", body)
        for event in re.finditer(r"\son[a-z]+\s*=", body):
            self.fail("关于页出现内联事件：%s" % event.group(0))

    def test_open_release_refuses_without_a_checked_release_url(self) -> None:
        """没查成一次就不许开浏览器——按钮在，但路由这一关必须拦住。"""
        with mock.patch("webbrowser.open") as opened:
            response = self.client.post("/about/open-release")
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(opened.call_count, 0)
        self.assertIn("先点「检查更新」", self.client.get(response.headers["Location"])
                      .get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
