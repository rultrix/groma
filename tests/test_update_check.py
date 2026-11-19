"""刀 W25d · 更新检查判据（U1–U9，U9 = D1 兑现后的默认仓库钉）。

全部用注入的 `fetch`，**一条都不真出网**；仓库通过 `GROMA_RELEASE_REPO` 夹具注入。
跑法：`./.venv/Scripts/python.exe -m unittest discover -s tests -p test_update_check.py -v`
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must be first real import
import importlib
import json
import os
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from lab import update
from lab.timeformat import local_text
from lab.workspace import init_workspace


def payload(tag="0.54.0", prerelease=False, assets=("groma-0.54.0-windows-x64.zip", "SHA256SUMS.txt")):
    return json.dumps({
        "tag_name": tag,
        "html_url": "https://github.com/owner/repo/releases/tag/%s" % tag,
        "prerelease": prerelease,
        "assets": [{"name": name} for name in assets],
    })


class RepoConfigTests(unittest.TestCase):
    def test_check_update_requires_configured_repo(self) -> None:  # U1
        calls = []

        def fetch(request):
            calls.append(request)
            raise AssertionError("未配置仓库时不许出网")

        # D1 兑现后 repo=None 表示"用默认仓库"（默认值另有其钉，见 U9）；
        # 这里只钉显式空串与非法值仍不配置、不出网。
        for repo in ("", "just-a-name"):
            result = update.check_latest(repo=repo, fetch=fetch)
            self.assertEqual(result["state"], "not_configured", repo)
            self.assertIn("尚未配置", result["error"])
        self.assertEqual(calls, [], "not_configured 也发了请求")


class RequestShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests = []

    def fake_fetch(self, request):
        self.requests.append(request)
        return payload()

    def test_check_update_uses_get_and_sends_no_local_payload(self) -> None:  # U2
        result = update.check_latest(repo="owner/repo", current="0.53.0", fetch=self.fake_fetch)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data, "请求体必须为空")
        self.assertTrue(request.full_url.startswith("https://api.github.com/repos/owner/repo/"),
                        request.full_url)
        blob = request.full_url + json.dumps({k.lower(): v for k, v in request.header_items()})
        for leak in (str(Path.home()), os.getcwd(), "unit_", "session", "vault"):
            self.assertNotIn(leak.lower(), blob.lower(), "请求里带了本机信息：%s" % leak)
        self.assertEqual(result["state"], "update_available",
                         "0.53.0 对 0.54.0 该报有新版；比较本身由 U4 钉，这里只验取数通了")

    def test_release_tag_normalizes_v_prefix(self) -> None:  # U3
        self.assertEqual(update.parse_tag("v0.54.0"), (0, 54, 0))
        self.assertEqual(update.parse_tag("0.54.0"), (0, 54, 0))
        self.assertIsNone(update.parse_tag("0.54"))
        self.assertIsNone(update.parse_tag("nightly"))
        newer = update.check_latest(repo="o/r", current="0.53.0",
                                    fetch=lambda request: payload("v0.54.0"))
        self.assertEqual(newer["state"], "update_available")
        self.assertEqual(newer["latest"], "v0.54.0")


class CompareAndStateTests(unittest.TestCase):
    def test_version_compare_is_numeric_not_lexicographic(self) -> None:  # U4
        # "0.9.0" > "0.54.0" 在字符串序里成立；按数字序才是 0.54.0 更新
        result = update.check_latest(repo="o/r", current="0.9.0",
                                     fetch=lambda request: payload("0.54.0"))
        self.assertEqual(result["state"], "update_available")

    def test_bad_response_is_visible_error(self) -> None:  # U6
        for body in (json.dumps({"html_url": "x"}), json.dumps({"tag_name": "0.54"}),
                     json.dumps([1, 2]), "not json"):
            result = update.check_latest(repo="o/r", current="0.53.0",
                                         fetch=lambda request, body=body: body)
            self.assertEqual(result["state"], "bad_response", body)
            self.assertTrue(result["error"], "失败必须带原因")

    def test_network_failure_is_visible_error(self) -> None:  # U7
        def boom(request):
            raise urllib.error.URLError("getaddrinfo failed")

        result = update.check_latest(repo="o/r", current="0.53.0", fetch=boom)
        self.assertEqual(result["state"], "network_error")
        self.assertIn("getaddrinfo", result["error"])

    def test_prerelease_is_not_recommended(self) -> None:
        result = update.check_latest(repo="o/r", current="0.53.0",
                                     fetch=lambda request: payload("0.54.0", prerelease=True))
        self.assertEqual(result["state"], "prerelease_available")

    def test_up_to_date_persists_checked_at_utc(self) -> None:  # U5
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "unit"
            root.mkdir()
            init_workspace(root, name="upd-unit", owner="tester")
            env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(Path(tmp) / "state")})
            env.start()
            self.addCleanup(env.stop)

            result = update.check_latest(repo="o/r", current="0.53.0",
                                         fetch=lambda request: payload("0.54.0"))
            saved = update.save(root, result)
            self.assertEqual(saved.name, "update-check.json")
            stored = json.loads(saved.read_text(encoding="utf-8"))
            self.assertRegex(stored["checked_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                             "checked_at 存 UTC（W23 的存储口径不许在这里破）")
            shown = local_text(stored["checked_at"])
            self.assertRegex(shown, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+\u2212-]\d{2}:?\d{2}$",
                             "页面口径是本机时刻＋有效偏移")
            self.assertNotEqual(shown, stored["checked_at"])
            self.assertEqual(update.describe(stored), "有新版本 0.54.0（本机 0.53.0）")
            self.assertEqual(update.describe(None), update.NOT_CHECKED)


class NoSilentNetworkTests(unittest.TestCase):
    def test_startup_and_create_app_do_not_call_update_network(self) -> None:  # U8
        try:
            from web.app import create_app
        except ImportError:  # pragma: no cover
            self.skipTest("Flask is not installed in this interpreter")

        calls = []

        def trap(request):
            calls.append(request)
            return payload("0.54.0")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "unit"
            root.mkdir()
            init_workspace(root, name="quiet-unit", owner="tester")
            env = mock.patch.dict(os.environ, {
                "GROMA_STARTUP_DIR": str(Path(tmp) / "startup"),
                "GROMA_STATE_DIR": str(Path(tmp) / "state"),
            })
            env.start()
            self.addCleanup(env.stop)
            with mock.patch.object(update, "_read", side_effect=trap), \
                    mock.patch.object(update, "_open", side_effect=trap), \
                    mock.patch.object(update, "RELEASE_REPO", "owner/repo"):
                client = create_app(root).test_client()
                for page in ("/", "/discover", "/activity", "/assets", "/units", "/gaps",
                             "/observe", "/about", "/favicon.ico", "/healthz"):
                    self.assertEqual(client.get(page).status_code, 200, page)
                self.assertEqual(calls, [], "启动或建 app 时自动检查了更新")

                # 只有点下去才出网一次
                response = client.post("/about/check-update")
                self.assertEqual(response.status_code, 303)
                self.assertEqual(len(calls), 1, "一次点击一次取数，不许顺手多打")

                # 点了「打开发布页」才带上已查到的地址开浏览器
                update.save(root, update.record("owner/repo", state="update_available",
                                                 page="https://github.com/o/r/releases/latest"))
                with mock.patch("webbrowser.open") as opened:
                    second = client.post("/about/open-release")
                self.assertEqual(second.status_code, 303)
                opened.assert_called_once_with("https://github.com/o/r/releases/latest")


class DefaultRepoTests(unittest.TestCase):
    def test_default_repo_is_public_release_and_env_overrides(self) -> None:  # U9
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROMA_RELEASE_REPO", None)
            self.assertEqual(importlib.reload(update).RELEASE_REPO, "rultrix/groma")
            os.environ["GROMA_RELEASE_REPO"] = "someone/fork"
            self.assertEqual(importlib.reload(update).RELEASE_REPO, "someone/fork")
        os.environ.pop("GROMA_RELEASE_REPO", None)
        self.assertEqual(importlib.reload(update).RELEASE_REPO, "rultrix/groma")


if __name__ == "__main__":
    unittest.main()
