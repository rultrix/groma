"""S1 web surface. Needs Flask; skipped when absent so core tests stay stdlib-only."""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest import mock

from lab import trigger as trigger_mod
from lab import unitlink, unitmatch
from lab.asset import build_asset
from lab.collect import catchup
from lab.control import is_paused
from lab.settings import load_settings
from lab.sources import add_source, load_sources
from lab.state import list_workspaces, normalise_path, register_workspace
from lab.workspace import LabError, init_workspace, load_unit

try:
    from web.app import create_app

    HAVE_FLASK = True
except ImportError:  # pragma: no cover - depends on the venv
    HAVE_FLASK = False


TZ8 = timezone(timedelta(hours=8))  # 界面时刻一律在固定偏移下断言，不依赖开发机时区


def first_nav_block(body: str) -> str:
    """取文档里第一个 <nav> 块的内部——R17 的取法。

    旧取法 `split("<nav>")[1]` 按**字面标签**切，于是 `<nav aria-label=...>` 会切空；
    标签名（可带属性）与块内计数这两件事本来不该绑在一起，断言强度不变：
    仍是"第一个 nav 块"、仍数 `<a ` 的个数、仍逐个查页面名。
    """
    match = re.search(r"<nav[^>]*>(.*?)</nav>", body, re.S)
    if match is None:
        raise AssertionError("页面里没有 nav 块")
    return match.group(1)


def outside_details_blocks(body: str) -> str:
    """去掉所有 `<details>…</details>` 区间，留下"默认就看得见"的那一面。

    为什么需要这个取法：降级承载（`<details>`）把句子留在响应体里，`assertIn` 单查响应
    查不出"它被折起来了"——而**影响操作的句子折起来就等于删了**。

    模板里有**嵌套**（others 行里那个"详情"，套在外层管理详情里）。非贪婪正则
    从外层开始匹配到内层 `</details>` 就停，外层剩余内容（注入源 form、删除链接）会漏回
    "可见面"——这个假绿被 2026-11-16 验收的 M1 抓到（`ACCEPT.md`）。
    现在按"先剥最内层、迭代到不动点"做：每一轮只匹配内容里不含 `<details` 开标签的块，
    嵌套会随着层数被逐轮剥掉；非嵌套输入一轮即不动点。
    """
    innermost = re.compile(r"<details\b[^>]*>(?:(?!<details\b)[\s\S])*?</details>")
    while True:
        stripped = innermost.sub("", body)
        if stripped == body:
            return stripped
        body = stripped


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "unit"
        self.root.mkdir()
        init_workspace(self.root, name="web-unit", owner="tester")
        self.src = Path(self._tmp.name) / "src"
        (self.src / "projects").mkdir(parents=True)
        (self.src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-10-28T00:00:00Z", "cwd": "%s", "type": "user"}\n' % self.root.as_posix(),
            encoding="utf-8",
            newline="\n",
        )
        env = mock.patch.dict(
            os.environ,
            {
                "GROMA_STARTUP_DIR": str(Path(self._tmp.name) / "startup"),
                "GROMA_STATE_DIR": str(Path(self._tmp.name) / "state"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        # 取数改成只认「人建过的协作主题」后，这个夹具得先把自己声明成建过：走的就是界面那条路
        # （POST /unit/create → `create_unit`，它把这条记录标成 origin=human）。
        # 必须落在状态目录切过来之后、`create_app` 之前——早了会写进上一个状态目录，等于没声明。
        # 下面那批断言钉的对象没变（主题页与概览长什么样），变的是夹具要把账做真。
        unitmatch.create_unit("web-unit", self.root)
        self.client = create_app(self.root).test_client()

    def test_index_renders_unit_and_status(self) -> None:
        # 改写：原断言在"概览表遍历 units"的形状下成立；后来裁定表只列人建标签，
        # 那张表只列人建标签，所以测试先把这份工作区收成协作主题，再看同一行——断言对象一字未减。
        from lab import topics as topics_mod

        topics_mod.create_topic("web-unit")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("web-unit", body)
        self.assertIn("采集中", body)

    def test_units_page_separates_last_attempt_from_last_success(self) -> None:  # A5-2
        import json

        from lab import classify_run, topics
        from lab import state as state_mod

        topics.create_topic("web-unit")
        path = state_mod.state_root() / classify_run.RUNS
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"topic": "web-unit", "state": "applied", "at": "2026-11-01T00:00:00Z"},
            {"topic": "web-unit", "state": "failed", "at": "2026-11-02T00:00:00Z"},
        ]
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8", newline="\n")
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("上次归类成功", body)
        self.assertIn("上次尝试", body)
        self.assertIn("失败：命令行失败", body)
        self.assertIn("2026-11-01", body)
        self.assertIn("2026-11-02", body)

    def test_units_page_renders_without_org_path(self) -> None:
        response = self.client.get("/units")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        # 头部句正向化——说「是什么」，不说「不是什么」；
        # 实现判据仍钉在 ⓘ（「判据：units.json 里标了人建的…」那条 tip 原样保留）。
        self.assertIn("协作主题是<b>本机协作的记账单位</b>", body)
        self.assertIn("不需要 GitHub", body)  # local-only merge is first-class, no remote needed
        # 改写这一行：原句钉的是「并入工作区在」——发现页改版后**变成谎话**
        # （发现页不再并入主题，挂主题搬到主题页每个 block 下面）。强度不变：仍钉"这页把两轴的去处说清楚"，
        # 只是对象换成新句子。旧句子在改前必绿（@见 CHECK 的 W9 段）、未修版上被新钉子打红、实现后绿。
        self.assertIn("建好后在它的详情里可以把别的工作区并进来", body)  # joining lives on this page now; 发现 only registers folders
        self.assertIn("units.json", body)

    def test_units_rename_and_join_via_web(self) -> None:
        sibling = Path(self._tmp.name) / "sibling"
        sibling.mkdir()
        self.assertEqual(
            self.client.post("/units/rename", data={"name": "ACPE 采集改造"}).status_code, 303
        )
        self.assertIn("ACPE 采集改造", self.client.get("/units").get_data(as_text=True))
        self.assertEqual(self.client.post("/units/join", data={"path": str(sibling)}).status_code, 303)
        self.assertIn(sibling.name, self.client.get("/units").get_data(as_text=True))

    def test_joined_member_shows_up_in_unit_scoped_pages(self) -> None:
        sibling = Path(self._tmp.name) / "sibling"
        sibling.mkdir()
        self.assertEqual(self.client.post("/units/join", data={"path": str(sibling)}).status_code, 303)
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn(sibling.as_posix(), body)
        self.assertIn("未声明采集根", body)  # honest: joined, but nothing has been collected there yet
        self.assertFalse((sibling / ".groma").exists())  # joining still writes nothing into the folder
        self.assertIn("2 个工作区", self.client.get("/evidence").get_data(as_text=True))
        # 资产单例化改写（F10 的测试）：手工表单随草稿退役，
        # 成员集口径只剩页头「N 个工作区」。
        self.assertIn("2 个工作区", self.client.get("/assets").get_data(as_text=True))

    def test_assets_page_reforge_is_main_path_and_manual_form_is_folded(self) -> None:
        """资产单例化改写（账本化测试）：重铸=唯一铸造入口
        （块头实心按钮直连 topics.reforge）；手工表单随草稿一并退役——不再有
        「高级：从会话手工生成草稿」路径。"""
        body = self.client.get("/assets").get_data(as_text=True)
        self.assertIn('class="reforge"', body, "资产页块头没有重铸主按钮")
        self.assertNotIn("高级：从会话手工生成草稿", body, "手工表单没退役")
        self.assertNotIn("生成草稿", body, "手工生成动词还在")

    def test_assets_header_uses_the_topics_tag_with_the_key_in_a_tip(self) -> None:
        """命名统一（M3/M4/Q3）：资产页块头显示名唯一来源=topics 标签，工程键进 ⓘ。"""
        body = self.client.get("/assets").get_data(as_text=True)
        self.assertIn('data-tip="工程键 unit_', body, "工程键没有进 ⓘ")

    def test_units_page_injection_column_tells_the_truth(self) -> None:
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        self.assertIn("未注入", self.client.get("/units").get_data(as_text=True))

        asset = build_asset(self.root, "注入验收", [("src", "projects/s.jsonl")])
        # 改写，apply 不再读请求 asset_id（注入源 ≡ 服役资产）——
        # 先经 CLI 权力路径（显式 asset_id 照旧）把资产立成服役资产，再走 web 两步流重新投放。
        # apply 两步化，直写必须带 confirm=1（不带会落在确认页，不是错误）。
        from lab.inject import inject_theme

        inject_theme(load_unit(self.root)["unit_id"], asset["asset_id"])
        response = self.client.post(
            "/unit/inject/apply",
            data={"path": str(self.root), "confirm": "1"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=", response.headers["Location"])
        payload = self.root / ".groma" / "injections" / "payload.md"
        self.assertIn("项目全景", payload.read_text(encoding="utf-8"))

        after = self.client.get("/units").get_data(as_text=True)
        self.assertIn("载荷就绪 · 无装载面", after)  # 没有装载面在用，就不许显示"投放中"
        self.assertNotIn("投放中", after)
        self.assertIn("注册 Qoder hook", after)

        self.assertEqual(
            self.client.post("/unit/inject/hook", data={"path": str(self.root), "confirm": "1"}).status_code, 303
        )
        self.assertIn("投放中", self.client.get("/units").get_data(as_text=True))
        # 没跑过一次会话，就不该出现"上次送达"
        self.assertNotIn("上次送达", self.client.get("/units").get_data(as_text=True))

        self.client.post("/unit/inject/hook", data={"path": str(self.root), "off": "1"})
        self.assertIn("载荷就绪 · 无装载面", self.client.get("/units").get_data(as_text=True))
        self.assertEqual(self.client.post("/unit/inject/stop", data={"path": str(self.root)}).status_code, 303)
        # 改写（总书逐刀改写点名）：stop 语义从「整体停发」升为「按区暂停」——
        # 主题级源不变，这一区载荷摘掉、亮「已停发」并给「恢复投递」；不再回到「未注入」。
        # 原测试（断言停发后整页回到「未注入」）。
        self.assertFalse(payload.exists())
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("已停发", body)
        self.assertIn("恢复投递", body)
        self.assertNotIn("未注入", body)

    def test_injection_form_defaults_to_the_home_workspace(self) -> None:
        # 注入源选错目录，等于把整个项目的背景投给一个无关的工作区，所以默认必须是"家"。
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        sibling = Path(self._tmp.name) / "sibling"
        sibling.mkdir()
        self.assertEqual(self.client.post("/units/join", data={"path": str(sibling)}).status_code, 303)
        build_asset(self.root, "默认家", [("src", "projects/s.jsonl")])
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn('<option value="%s" selected>' % self.root.as_posix(), body)
        self.assertIn('<option value="%s">' % sibling.as_posix(), body)

    def test_hook_registration_needs_confirmation(self) -> None:
        response = self.client.post("/unit/inject/hook", data={"path": str(self.root)})
        self.assertIn("error=", response.headers["Location"])
        self.assertFalse((self.root / ".qoder" / "hooks" / "groma_inject.py").exists())

    def test_discover_lists_known_harnesses(self) -> None:
        """改写（三步留痕见 CHECK）：harness 名仍要出现，但「主题」这个词的载体换了。

        原来靠 candidates 行里的「本主题 X / 共 Y」满足 K10(b)，把那句改成「本区」后
        这个载体消失；改由添加卡那句「不挂主题。挂主题在主题页」承担同一件事——
        **断言强度不变**：仍钉"导航写主题，正文就用主题这个词，不写 Unit"。
        """
        body = self.client.get("/discover").get_data(as_text=True)
        for name in ("qoder", "codex", "claude"):
            self.assertIn(name, body)
        self.assertIn("不挂协作主题", body)  # K10 (b): 导航写「主题」，正文就不许再写 Unit
        self.assertIn("Agent", body)

    def test_add_source_from_discover(self) -> None:
        response = self.client.post(
            "/discover/add",
            data={"name": "src", "path": str(self.src), "include": "projects", "harness": "test"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual([item["name"] for item in load_sources(self.root)], ["src"])
        moved = self.client.get("/sources", follow_redirects=True)
        self.assertIn("src", moved.get_data(as_text=True))  # the page moved into 发现

    def test_refresh_writes_a_scan(self) -> None:
        add_source(self.root, "src", str(self.src), include=["projects"])
        before = len(list((self.root / ".groma" / "scans").glob("scan-*.json")))
        response = self.client.post("/refresh")
        self.assertEqual(response.status_code, 303)
        after = len(list((self.root / ".groma" / "scans").glob("scan-*.json")))
        self.assertEqual(after, before + 1)

    def test_pause_stops_catchup_but_keeps_the_trigger(self) -> None:
        add_source(self.root, "src", str(self.src), include=["projects"])
        trigger_mod.install(self.root)
        self.assertTrue(trigger_mod.status(self.root)["installed"])

        self.assertEqual(self.client.post("/pause").status_code, 303)
        self.assertTrue(is_paused(self.root))
        self.assertEqual(catchup(self.root, force=True)["reason"], "paused")
        self.assertTrue(trigger_mod.status(self.root)["installed"])
        self.assertIn("已暂停", self.client.get("/").get_data(as_text=True))

    def test_resume_clears_the_flag_and_keeps_the_trigger(self) -> None:
        add_source(self.root, "src", str(self.src), include=["projects"])
        trigger_mod.install(self.root)
        self.client.post("/pause")
        self.assertEqual(self.client.post("/resume").status_code, 303)
        self.assertFalse(is_paused(self.root))
        self.assertTrue(trigger_mod.status(self.root)["installed"])

    def test_activity_renders_after_a_catchup(self) -> None:
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        response = self.client.get("/activity")
        self.assertEqual(response.status_code, 200)
        self.assertIn("活跃天数", response.get_data(as_text=True))

    def test_evidence_page_counts_sessions(self) -> None:
        """呈现层下线，两条呈现断言按 docs/RULES.md 的登记删掉。

        Removed here: `assertIn("<svg", body)` and `assertIn('id="unit"', body)`.
        The count assertion below is byte-for-byte the one that used to point at
        /graph -- the contract "聚合计数进了页面" did not change, only the page.
        """
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        body = self.client.get("/evidence").get_data(as_text=True)
        self.assertIn("个会话", body)

    def test_session_detail_page(self) -> None:
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        response = self.client.get("/session", query_string={"source": "src", "path": "projects/s.jsonl"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("projects/s.jsonl", response.get_data(as_text=True))

    def test_workspaces_page_lists_current(self) -> None:
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn(self.root.as_posix(), body)
        self.assertIn("家", body)  # exactly one home, shown as a property of the Unit

    def test_units_nav_highlights_current_page(self) -> None:
        units_body = self.client.get("/units").get_data(as_text=True)
        self.assertIn('class="active" href="/units"', units_body)
        self.assertNotIn('class="active" href="/"', units_body)
        index_body = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="active" href="/"', index_body)
        self.assertNotIn('class="active" href="/units"', index_body)

    def test_workspaces_route_redirects_to_units(self) -> None:
        response = self.client.get("/workspaces")
        self.assertIn(response.status_code, (301, 302, 303))
        self.assertIn("/units", response.headers["Location"])

    def test_moving_the_home_initialises_that_folder_and_keeps_the_unit(self) -> None:
        other = Path(self._tmp.name) / "second"
        other.mkdir()
        unit_id = load_unit(self.root)["unit_id"]
        response = self.client.post("/unit/home", data={"path": str(other)})
        self.assertEqual(response.status_code, 303)
        self.assertTrue((other / ".groma" / "unit.json").is_file())
        self.assertEqual(load_unit(other)["unit_id"], unit_id)  # no second Unit around the same work
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], other.as_posix())
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn(unit_id, body)  # the page is still about the same Unit
        self.assertIn(self.root.as_posix(), body)  # the folder it left is still listed

    def test_moving_the_home_to_a_missing_path_is_rejected(self) -> None:
        response = self.client.post("/unit/home", data={"path": str(Path(self._tmp.name) / "nope")})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], self.root.as_posix())

    def test_moving_the_home_notifies_the_shell(self) -> None:
        seen = []
        client = create_app(self.root, on_switch=seen.append).test_client()
        other = Path(self._tmp.name) / "third"
        other.mkdir()
        self.assertEqual(client.post("/unit/home", data={"path": str(other)}).status_code, 303)
        self.assertEqual(seen, [other])

    def test_creating_a_unit_from_a_folder_switches_scope(self) -> None:
        other = Path(self._tmp.name) / "another-work"
        other.mkdir()
        response = self.client.post("/unit/create", data={"name": "另一件事", "home": str(other)})
        self.assertEqual(response.status_code, 303)
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("另一件事", body)
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], other.as_posix())
        # and the original Unit is still switchable
        first = load_unit(self.root)["unit_id"]
        self.assertEqual(self.client.get("/unit/select", query_string={"unit_id": first}).status_code, 303)
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], self.root.as_posix())

    def test_switching_unit_is_rejected_for_an_unknown_id(self) -> None:
        response = self.client.get("/unit/select", query_string={"unit_id": "unit_nope"})
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], self.root.as_posix())

    def test_creating_a_unit_refuses_a_folder_that_leaves_a_unit_homeless(self) -> None:
        sibling = Path(self._tmp.name) / "sibling"
        sibling.mkdir()
        self.client.post("/units/join", data={"path": str(sibling)})
        before = load_unit(self.root)["unit_id"]
        response = self.client.post("/unit/create", data={"name": "抢家", "home": str(self.root)})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(load_unit(self.root)["unit_id"], before)  # card untouched

    def test_remove_page_lists_the_evidence(self) -> None:
        body = self.client.get("/workspaces/remove", query_string={"path": str(self.root)}).get_data(as_text=True)
        self.assertIn("scans", body)
        self.assertIn("不可恢复", body)

    def test_removing_the_home_switches_the_unit_to_another_folder(self) -> None:
        other = Path(self._tmp.name) / "fourth"
        other.mkdir()
        self.client.post("/unit/home", data={"path": str(other)})
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], other.as_posix())
        # 破坏性动作要带 confirm=1（红线 2）；这条考的是"家换到别处"，不是确认门
        response = self.client.post("/workspaces/remove", data={"path": str(other), "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], self.root.as_posix())
        self.assertNotIn("fourth", self.client.get("/units").get_data(as_text=True))

    def test_cannot_remove_the_last_workspace(self) -> None:
        # **必须带 confirm=1**，否则这道拒绝来自确认门而不是"最后一个工作区"，归因就假了
        response = self.client.post("/workspaces/remove", data={"path": str(self.root), "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertTrue((self.root / ".groma" / "unit.json").is_file())

    def test_assets_page_and_build(self) -> None:
        from lab import topics as topics_mod

        topics_mod.create_topic("web-unit")  # 起块＝协作主题，没有主题就不成块
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        self.assertEqual(self.client.get("/assets").status_code, 200)
        # 资产单例化改写，/assets/build 路由退役，
        # 铸造唯一入口=重铸；资产仍进账面。
        response = self.client.post(
            "/assets/build",
            data={"title": "草稿一", "session": ["src:projects/s.jsonl"]},
        )
        self.assertEqual(response.status_code, 404, "/assets/build 没退役")
        from lab.asset import build_asset as _build

        built = _build(self.root, "草稿一", [("src", "projects/s.jsonl")])
        body = self.client.get("/assets").get_data(as_text=True)
        self.assertIn("草稿一", body)
        self.assertIn("未服役", body)

    def test_assets_page_has_no_free_target_selector(self) -> None:  # A1-1
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        body = self.client.get("/assets").get_data(as_text=True)
        self.assertNotIn('name="path"', body)
        self.assertIn("本协作主题的家", body)
        self.assertIn(self.root.as_posix(), body)

    def test_assets_build_lands_in_the_theme_home_only(self) -> None:  # A1-2
        """资产单例化改写（A1-2）：/assets/build 路由退役；
        落点保证由 build_asset 的单槽实现承载——恒 <家>/.groma/assets/current/。"""
        from lab.state import register_workspace

        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        other = Path(self._tmp.name) / "other-ws"
        other.mkdir()
        init_workspace(other, name="other-unit", owner="tester")
        register_workspace(other)
        response = self.client.post("/assets/build", data={
            "title": "落点归家", "session": ["src:projects/s.jsonl"], "path": str(other)})
        self.assertEqual(response.status_code, 404, "/assets/build 没退役")
        self.assertFalse((other / ".groma" / "assets").exists(), "资产被写进了别的区")
        from lab.asset import build_asset as _build

        _build(self.root, "落点归家", [("src", "projects/s.jsonl")])
        # 落点=主题家的单槽 assets/current/。
        self.assertTrue((self.root / ".groma" / "assets" / "current" / "asset.json").is_file())

    def test_a_session_outside_the_theme_is_named_as_such(self) -> None:  # A1-3
        from urllib.parse import unquote

        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        # 资产单例化改写，web 表单退役，越界判据归 build_asset 本体。
        from lab.asset import build_asset as _build

        with self.assertRaises(Exception) as caught:
            _build(self.root, "越界", [("src", "projects/missing.jsonl")])
        self.assertIn("not in this Unit", str(caught.exception))

    def test_the_fanout_reaches_a_member_that_carries_its_own_card(self) -> None:  # A1-4
        """勾「本协作主题全部工作区」时，自带身份卡的成员区也必须拿到同一份资产。

        真机形状：local-lab 属主题 26a7，但它自己的卡认另一个主题 ⇒ 0.55 撞
        `asset not found: asset-aad1a880d99f`（Owner @12:2x）。扇出入口在路由，
        作用域却在内核——只钉文案的那条判据看不见这件事。
        """
        from urllib.parse import unquote

        from lab import surface as surface_mod

        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        member = Path(self._tmp.name) / "member-ws"
        member.mkdir()
        init_workspace(member, name="member-unit", owner="tester")  # 关键：成员自带一张卡
        register_workspace(member)
        unitmatch.join_unit(load_unit(self.root)["unit_id"], member, role="evidence")
        asset = build_asset(self.root, "扇出资产", [("src", "projects/s.jsonl")])
        # 改写，apply 不再读请求 asset_id——CLI 权力路径先立服役资产。
        from lab.inject import inject_theme

        inject_theme(load_unit(self.root)["unit_id"], asset["asset_id"])

        # 同上，直写带 confirm=1。
        response = self.client.post(
            "/unit/inject/apply", data={"all": "1", "confirm": "1"}
        )
        location = unquote(response.headers["Location"])
        self.assertNotIn("error=", location, "扇出仍按落点自己的卡片解析资产：%s" % location)
        for folder in (self.root, member):
            self.assertTrue((folder / ".groma" / "injections" / "payload.md").is_file(), folder)
            source = surface_mod.read_manifest(folder)["source"]
            self.assertEqual(source["asset_id"], asset["asset_id"])
            self.assertEqual(source["unit_id"], load_unit(self.root)["unit_id"])

    def test_a_single_injection_names_the_theme_being_operated(self) -> None:  # A1-5
        """注入表单自己带上"这一块的主题 id"，不靠当前聚焦、也不靠落点目录的卡片。

        回归证伪：只查整页有没有 `name="unit"` 会被重铸表单蒙过去（它是同页的另一张 form），
        所以这里按表单取段。注入改两步提交：form 改 GET 去 confirm 页、带 confirm=1 才真写——
        判据跟着钉新形状（GET form + hidden unit），语义不变。
        """
        topic = self._theme_with_evidence()
        # 资产单例化改写：/assets/build 退役，资产改由 build_asset 造。
        from lab.asset import build_asset as _build_a1

        _build_a1(self.root, "带主题的注入", [("src", "projects/s.jsonl")])
        # 改写，注入源卡退役——hero 行末「重新投放」= 唯一入口，
        # 链接只带主题参数（确认页固定服役资产，不再有资产选择）。
        from lab.inject import inject_theme
        from lab.asset import list_assets

        home = unitmatch.home_path(topic["topic_id"])
        inject_theme(topic["topic_id"], list_assets(Path(home))[0]["asset_id"])
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn('href="/unit/inject/confirm?unit=%s"' % topic["topic_id"], body,
                      "hero 行末没有带主题参数的「重新投放」链接")
        self.assertNotIn("注入到这里", body)
        self.assertNotIn('name="asset_id"', body, "web 面还留着资产选择")
        assets_body = self.client.get("/assets").get_data(as_text=True)
        self.assertNotIn('action="/unit/inject/confirm"', assets_body, "资产页还留着注入入口")

    def _dead_registration(self, name: str = "ghost-ws") -> Path:
        """造一条"目录已经没了、登记行还在"的行——真机 web-unit 的形状。"""
        ghost = Path(self._tmp.name) / name
        ghost.mkdir()
        self.client.post("/workspaces/add", data={"path": str(ghost), "from": "discover_page"})
        shutil.rmtree(ghost)
        return ghost

    def test_a_dead_registration_is_not_offered_as_a_member(self) -> None:  # A8-1
        from urllib.parse import unquote

        # 回归证伪在同一条里：活的区仍进候选表，死的区只进「失效的登记」——
        # 否则"候选表一条都没有"会让这条判据空过。
        live = Path(self._tmp.name) / "live-ws"
        live.mkdir()
        self.client.post("/workspaces/add", data={"path": str(live), "from": "discover_page"})
        ghost = self._dead_registration()

        body = self.client.get("/units").get_data(as_text=True)
        forms = re.findall(r'<form method="post" action="/units/join".*?</form>', body, re.S)
        self.assertTrue([f for f in forms if live.as_posix() in f], "活的工作区被一起挡在候选表外了")
        self.assertFalse([f for f in forms if ghost.as_posix() in f],
                         "目录已不存在的登记行仍以「可并入这个协作主题的工作区」出现")
        self.assertIn("失效的登记", body)
        links = [unquote(h) for h in re.findall(r'href="([^"]+)"', body)]
        exits = [h for h in links if "/workspaces/forget/confirm" in h]
        self.assertTrue(exits, "死登记行在界面上没有任何出口")
        self.assertTrue(any(ghost.as_posix() in h for h in exits), "出口指的行不对")

    def test_a_dead_registration_is_forgotten_in_two_steps_without_touching_others(self) -> None:
        from urllib.parse import unquote

        ghost = self._dead_registration()
        keeper = Path(self._tmp.name) / "keeper-ws"
        keeper.mkdir()
        self.client.post("/workspaces/add", data={"path": str(keeper), "from": "discover_page"})

        unconfirmed = unquote(self.client.post(
            "/workspaces/forget", data={"path": str(ghost)}).headers["Location"])
        self.assertIn("error=", unconfirmed, "没经过确认页就动了注册表")
        self.assertTrue(any(item["path"] == ghost.as_posix() for item in list_workspaces()))

        done = unquote(self.client.post(
            "/workspaces/forget", data={"path": str(ghost), "confirm": "1"}).headers["Location"])
        self.assertNotIn("error=", done)
        after = {item["path"] for item in list_workspaces()}
        self.assertNotIn(ghost.as_posix(), after)
        self.assertIn(keeper.as_posix(), after)
        self.assertTrue(keeper.is_dir())
        self.assertFalse((keeper / ".groma").exists(), "只清登记，不该在别的区写下什么")

    def test_a_live_workspace_is_not_removable_by_this_step(self) -> None:  # A8-2
        """这一步只管死登记：目录还在 ⇒ 退回「移除工作区」那条有清单的确认页。

        启动目录那道护栏不在这里重述（`test_cli_workspace_crud.py` 与 1329 行那条已钉）。
        """
        from urllib.parse import unquote

        location = unquote(self.client.post(
            "/workspaces/forget", data={"path": str(self.root), "confirm": "1"}).headers["Location"])
        self.assertIn("移除工作区", location)
        self.assertTrue((self.root / ".groma").is_dir(), "越界的一步居然动了启动目录")

    def test_assets_h1_drops_the_draft_word_and_history_folds_away(self) -> None:
        """资产单例化改写：h1「上下文资产」；
        单例化后一主题恒一份资产，历史折叠区随之退役——账面只列当前槽一份，
        追溯归 reforges 账本（母本 §13）；「注入到这里」仍退场。"""
        from lab.asset import build_asset

        from lab import topics as topics_mod

        topics_mod.create_topic("web-unit")
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        build_asset(self.root, "服役资产", [("src", "projects/s.jsonl")])
        from lab.inject import inject_theme

        inject_theme(self._root_unit_id(), build_asset(
            self.root, "服役资产", [("src", "projects/s.jsonl")])["asset_id"])
        body = self.client.get("/assets").get_data(as_text=True)
        self.assertIn("<h1>上下文资产</h1>", body)
        self.assertNotIn("<h1>上下文资产草稿</h1>", body)
        self.assertNotIn("另有", body)
        self.assertNotIn("注入到这里", body, "资产页还留着注入入口")

    def test_session_page_lists_assistant_blocks_with_anchor_links(self) -> None:
        """屏4（新增）：session 详情 assistant 块有「落锚」入口，预填参数进资产页落锚表单。"""
        from urllib.parse import parse_qs, urlparse

        from lab import vault
        from lab.collect import catchup as _catchup
        from lab.sources import add_source as _add

        src = self.src
        _add(self.root, "src", str(src), "qoder-cn", ["projects"])
        _catchup(self.root, force=True)
        # 落一个真分片进 vault（fixture 同 test_asset_claims 的合成形状）
        from lab.asset import build_asset

        build_asset(self.root, "落锚底稿", [("src", "projects/s.jsonl")])
        # vault 分片只读列出 assistant 块：先建索引（catchup 只写 digest，不写 vault 索引）
        from lab.vault import reindex as vault_reindex, vault_root as vault_root_of

        vault_reindex(vault_root_of())
        # vault 里落一个真分片（合成形状，session_key 与 digest 的口径一致），
        # 否则 session 页的 assistant 块区是空态——落锚入口无处可挂。
        from lab import vault as vault_mod

        vault_mod.append_records(vault_mod.vault_root(), "qoder-cn", "qoder-cn:s", [
            {"ts_utc": "2026-11-10T09:01:00Z", "granularity": "event", "session_key": "qoder-cn:s",
             "role": "assistant", "type": "message", "text": "这是 assistant 块正文，可整块落锚。",
             "reasoning": None, "tool": None, "strip": [], "agent_id": "agt-1",
             "source": {"path": "projects/s.jsonl", "line_no": 1, "bytes": 32,
                        "source_sha256": "0" * 64}},
        ])
        from lab.vault import reindex as vault_reindex, vault_root as vault_root_of

        vault_reindex(vault_root_of())
        page = self.client.get("/session", query_string={"source": "src", "path": "projects/s.jsonl"})
        self.assertEqual(page.status_code, 200)
        session_body = page.get_data(as_text=True)
        self.assertIn("assistant 块", session_body)
        self.assertIn("assistant 块", session_body, "session 页没有 assistant 块区")
        anchor_link = re.search(r'/assets\?[^"]*anchor_session=[^"]+', session_body)
        self.assertIsNotNone(anchor_link, "assistant 块没有落锚入口")
        query = parse_qs(urlparse(anchor_link.group(0).replace("&amp;", "&")).query)
        self.assertIn("anchor_session", query)
        self.assertIn("anchor_from", query)
        self.assertIn("anchor_quote", query)
        # 预填参数真的落进资产页表单
        assets_body = self.client.get(anchor_link.group(0).replace("&amp;", "&")).get_data(as_text=True)
        self.assertIn("自会话详情预填", assets_body)
        self.assertIn('name="from_ts" value="%s"' % query["anchor_from"][0], assets_body)

    def test_assets_blocks_are_themes_and_each_asset_lists_once(self) -> None:  # A2-1
        from lab import topics as topics_mod

        topics_mod.create_topic("web-unit")
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        asset = build_asset(self.root, "唯一草稿", [("src", "projects/s.jsonl")])
        body = self.client.get("/assets").get_data(as_text=True)
        self.assertEqual(body.count('data-topic="'), len(topics_mod.list_topics()))
        # 资产单例化改写：删除 href 与落锚资产下拉退役，
        # 同一份资产在页上恰好运 2 处（行内 code、导出 hidden）。
        self.assertEqual(body.count(asset["asset_id"]), 2, "同一份资产在页上不是恰好 2 处")

    def test_block_header_names_the_theme_not_a_workspace_list(self) -> None:  # A2-2
        from lab import topics as topics_mod

        topics_mod.create_topic("web-unit")
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        build_asset(self.root, "块头验收", [("src", "projects/s.jsonl")])
        body = self.client.get("/assets").get_data(as_text=True)
        fragment = body.split('data-topic="', 1)[1]
        self.assertIn("web-unit", fragment)
        self.assertIn(self.root.as_posix(), fragment)  # 家路径写在块头上（可点的字符串）
        self.assertNotIn("本机每个工作区各自保存自己的草稿", body)
        self.assertNotIn("整机口径", body)

    def test_row_actions_point_at_the_theme_home(self) -> None:  # A2-3
        import json

        from lab import topics as topics_mod

        topic = topics_mod.create_topic("web-unit")
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        build_asset(self.root, "家里的草稿", [("src", "projects/s.jsonl")])
        # 旧行为的病灶样本：另一份草稿落在成员区（不是家）
        member = Path(self._tmp.name) / "member-ws"
        member.mkdir()
        init_workspace(member, name="member-unit", owner="tester")
        (member / ".groma" / "digests").mkdir(parents=True)
        digest = {"schema": 1, "kind": "GROMA_LOCAL_DIGEST", "created_at": "2026-11-08T09:00:00Z",
                  "unit_id": load_unit(member)["unit_id"], "workspace_root": member.as_posix(),
                  "file_count": 1, "event_count": 1, "parse_errors": 0,
                  "files": [{"source": "src", "path": "projects/s.jsonl", "harness": "src",
                             "agent_kind": "primary", "events": 1}]}
        (member / ".groma" / "digests" / "digest-20261108T090000Z.json").write_text(
            json.dumps(digest, ensure_ascii=False), encoding="utf-8", newline="\n")
        build_asset(member, "成员区里的旧草稿", [("src", "projects/s.jsonl")])
        self.client.post("/units/join", data={"unit": topic["topic_id"], "path": str(member)})
        body = self.client.get("/assets").get_data(as_text=True)
        # 资产单例化改写：单槽后账面至多一份，
        # 家的槽里的导出 hidden 指向家；成员区槽里的草稿不被拿它的目录当落点。
        self.assertEqual(body.count('name="path" value="%s"' % self.root.as_posix()), 1,
                         "导出 hidden 不是指向家")
        self.assertEqual(body.count('name="path" value="%s"' % member.as_posix()), 0,
                         "成员区的草稿被拿它的目录当成了落点")

    def _theme_with_evidence(self):
        from lab import topics as topics_mod

        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        topic = topics_mod.create_topic("web-unit")
        topics_mod.select(topic["topic_id"], [{"source": "src", "path": "projects/s.jsonl"}],
                          decided_by="human", known_keys={("src", "projects/s.jsonl")})
        return topic

    def test_reforge_route_and_buttons_exist(self) -> None:  # A3-1
        self._theme_with_evidence()
        units = self.client.get("/units").get_data(as_text=True)
        assets = self.client.get("/assets").get_data(as_text=True)
        self.assertIn('action="/topic/reforge"', units)
        self.assertIn('action="/topic/reforge"', assets)

    def test_the_reforge_control_looks_like_a_button_not_the_sentence_it_sits_in(self) -> None:  # A7-1
        """Owner 0.55 实测①：「重铸的按钮混在一堆文字，不仔细看根本不知道是个按钮」。

        钉三件：动作离开文字流（`block-actions` 容器）、它不再是白底 ghost、
        下发的 CSS 给它一个实心底 —— 只挪位置不给视觉差，人眼仍然找不出它是按钮。
        """
        self._theme_with_evidence()
        # 改写，units 页重铸按钮挪 hero 下方、文字改「立即重铸」，
        # block-actions 容器随之只在 /assets 保留（屏 8 形状）；实心底断言两页共用。
        assets = self.client.get("/assets").get_data(as_text=True)
        self.assertIn('<span class="block-actions">', assets)
        self.assertIn('<button type="submit" class="reforge" '
                      'title="按主题说明重铸——没变不重铸；别人的资产绝不顶掉">重铸</button>', assets)
        units = self.client.get("/units").get_data(as_text=True)
        self.assertIn('<button type="submit" class="reforge" '
                      'title="按主题说明重铸——没变不重铸；别人的资产绝不顶掉">立即重铸</button>', units)
        self.assertNotIn('class="ghost" style="padding:2px 8px;font-size:12px">重铸', assets)
        self.assertRegex(assets, r"button\.reforge\s*\{[^}]*background:\s*(?:var\(--teal\)|#(?!fff))")

    def test_reforge_from_the_page_writes_ledger_and_asset(self) -> None:  # A3-2
        from lab import topics as topics_mod
        from lab.asset import load_asset

        topic = self._theme_with_evidence()
        response = self.client.post("/topic/reforge", data={"unit": topic["topic_id"], "from": "units"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("error=", response.headers["Location"])
        record = topics_mod.last_reforge(topic["topic_id"])
        self.assertIsNotNone(record)
        self.assertEqual(record["sessions"], 1)
        self.assertEqual(load_asset(self.root, record["asset_id"])["sessions"][0]["source"], "src")

    def test_reforge_reports_how_many_keys_it_had_to_skip(self) -> None:  # A3-3
        from urllib.parse import unquote

        from lab import topics as topics_mod

        add_source(self.root, "src", str(self.src), include=["projects"])
        second = self.src / "projects" / "s2.jsonl"
        second.write_text('{"timestamp": "2026-10-29T00:00:00Z", "cwd": "%s", "type": "user"}\n'
                          % self.root.as_posix(), encoding="utf-8", newline="\n")
        catchup(self.root, force=True)
        topic = topics_mod.create_topic("web-unit")
        keys = {("src", "projects/s.jsonl"), ("src", "projects/s2.jsonl")}
        topics_mod.select(topic["topic_id"],
                          [{"source": source, "path": path} for source, path in sorted(keys)],
                          decided_by="human", known_keys=keys)
        second.unlink()
        catchup(self.root, force=True)
        response = self.client.post("/topic/reforge", data={"unit": topic["topic_id"], "from": "units"})
        location = unquote(response.headers["Location"])
        self.assertIn("跳过 1 条", location)

    def test_the_button_calls_the_one_reforge_implementation(self) -> None:  # B3-1
        from lab import topics as topics_mod

        topic = self._theme_with_evidence()
        with mock.patch.object(topics_mod, "reforge", wraps=topics_mod.reforge) as spy:
            self.client.post("/topic/reforge", data={"unit": topic["topic_id"], "from": "units"})
        spy.assert_called_once()

    def test_assets_and_units_stay_zero_js(self) -> None:  # B3-2
        self._theme_with_evidence()
        for page in ("/assets", "/units"):
            body = self.client.get(page).get_data(as_text=True).lower()
            self.assertNotIn("<script", body, page)
            self.assertNotIn("javascript:", body, page)
            self.assertNotIn(" onclick=", body, page)

    def test_export_asset_from_the_page(self) -> None:
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        asset = build_asset(self.root, "草稿", [("src", "projects/s.jsonl")])
        response = self.client.post("/assets/export", data={"asset_id": asset["asset_id"]})
        self.assertEqual(response.status_code, 303)
        exported = self.root / ".groma" / "exports" / ("%s-CONTEXT.md" % asset["asset_id"])
        self.assertTrue(exported.is_file())
        self.assertIn("groma:context", self.client.get("/assets").get_data(as_text=True) + exported.read_text(encoding="utf-8"))

    def test_settings_interval_roundtrip(self) -> None:
        response = self.client.post("/settings", data={"interval": "5"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(load_settings(self.root)["collect_interval_minutes"], 5)
        self.assertIn("5", self.client.get("/").get_data(as_text=True))

    def test_settings_rejects_a_bad_value(self) -> None:
        before = load_settings(self.root)["collect_interval_minutes"]
        response = self.client.post("/settings", data={"interval": "abc"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertEqual(load_settings(self.root)["collect_interval_minutes"], before)

    def test_unknown_session_redirects_with_error(self) -> None:
        response = self.client.get("/session", query_string={"source": "nope", "path": "x.jsonl"})
        self.assertEqual(response.status_code, 303)

    def test_healthz_reports_paused(self) -> None:
        self.assertTrue(self.client.get("/healthz").get_json()["ok"])
        self.client.post("/pause")
        self.assertTrue(self.client.get("/healthz").get_json()["paused"])


    def test_intent_can_be_edited_on_the_theme_page(self) -> None:
        from lab import topics

        response = self.client.post("/units/intent", data={"unit": "web-unit", "intent": "包含 A；不含 B"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(topics.resolve_topic("web-unit")["intent"], "包含 A；不含 B")
        self.assertIn("包含 A；不含 B", self.client.get("/units").get_data(as_text=True))

        self.client.post("/units/intent", data={"unit": "web-unit", "intent": ""})
        self.assertIsNone(topics.resolve_topic("web-unit")["intent"])

    def test_an_empty_intent_is_announced_as_a_degradation(self) -> None:
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("没有说明时自动归类会退化", body)


    def test_a_topic_named_after_the_unit_shares_its_id(self) -> None:
        from lab import topics, unitmatch

        unit = unitmatch.resolve_unit(unitmatch.load_unit(self.root)["unit_id"])
        topic = topics.create_topic(unit["name"])
        self.assertEqual(topic["topic_id"], unit["unit_id"])

    # --- 数字说真话（K3 / K4 / K1 / K23）--------------------------------------

    def _fake_vault(self, index: dict) -> Path:
        """Point GROMA_VAULT_DIR at a temp root holding only an index.json.

        The page reads the index, never the shard bytes, so a hand-written
        index is the honest fixture: it pins which top-level key the UI reads.
        """
        import json
        import os
        from pathlib import Path
        from unittest import mock

        vault_dir = Path(self._tmp.name) / "vault"
        vault_dir.mkdir()
        (vault_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"GROMA_VAULT_DIR": str(vault_dir)})
        patch.start()
        self.addCleanup(patch.stop)
        return vault_dir

    def test_vault_card_shard_count_matches_the_index(self) -> None:
        from lab import vault as vault_mod

        self._fake_vault({
            "schema": 1,
            "kind": "GROMA_VAULT_INDEX",
            "root": "x",
            "generated_at": "2026-11-10T00:00:00Z",
            "session_count": 1,
            "shard_count": 3,
            "bytes_total": 1234,
            "sessions": [{"harness": "qoder-cn", "session_key": "q:a",
                          "shards": [{"name": "s1"}, {"name": "s2"}, {"name": "s3"}]}],
        })
        index = vault_mod.read_index(vault_mod.vault_root())
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("3 个分片", body)
        self.assertEqual(index["shard_count"], 3)

    def test_observe_bake_passes_a_real_host(self) -> None:
        import socket
        from unittest import mock

        from web import app as web_app

        self._fake_vault({
            "schema": 1,
            "kind": "GROMA_VAULT_INDEX",
            "generated_at": "2026-11-10T00:00:00Z",
            "session_count": 1,
            "shard_count": 1,
            "sessions": [{"harness": "qoder-cn", "session_key": "q:a",
                          "shards": [{"name": "s1", "records": 2}]}],
        })
        seen: list = []

        def bake(vault_root, digest_rows, host_name, output_path=None):
            seen.append(host_name)
            return "<!DOCTYPE html><html>%s</html>" % host_name

        with mock.patch.object(web_app, "generate_observation_page", bake):
            html, error = web_app._bake_observe_html()
        self.assertIsNone(error)
        self.assertEqual(seen, [socket.gethostname()])
        self.assertNotIn(str(html).lower(), ("unknown", ""))

    def test_saving_classify_fields_without_interval(self) -> None:
        from lab.settings import load_settings

        before = load_settings(self.root)
        response = self.client.post(
            "/settings",
            data={"classify_model": "Model-X", "classify_provider": "C:/bin/wb.exe",
                  "classify_signature": "Usage: other"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=", response.headers["Location"])
        after = load_settings(self.root)
        self.assertEqual(after["classify_model"], "Model-X")
        self.assertEqual(after["classify_provider"], "C:/bin/wb.exe")
        self.assertEqual(after["classify_signature"], "Usage: other")
        self.assertEqual(after["collect_interval_minutes"], before["collect_interval_minutes"])

    def test_clearing_the_provider_falls_back_to_autodetect(self) -> None:
        from lab.settings import load_settings

        self.client.post("/settings", data={"classify_provider": "C:/bin/wb.exe"})
        self.assertEqual(load_settings(self.root)["classify_provider"], "C:/bin/wb.exe")
        response = self.client.post("/settings", data={"classify_provider": ""})
        self.assertIn("notice=", response.headers["Location"])
        self.assertIsNone(load_settings(self.root)["classify_provider"])

    def test_overview_write_line_uses_one_record(self) -> None:
        import json

        from lab.workspace import init_workspace

        newer = Path(self._tmp.name) / "newer"
        newer.mkdir()
        init_workspace(newer, name="newer-unit", owner="tester")
        for root, stamp, entry in ((self.root, "2026-11-01T00:00:00Z", "cli:old"),
                                   (newer, "2026-11-09T00:00:00Z", "cli:new")):
            digests = root / ".groma" / "digests"
            digests.mkdir(parents=True, exist_ok=True)
            (digests / ("digest-%s.json" % stamp.replace("-", "").replace(":", "").replace("Z", ""))).write_text(
                json.dumps({"schema": 1, "created_at": stamp, "file_count": 1,
                            "written_by": {"entry": entry, "trigger": "manual", "pid": 42}}),
                encoding="utf-8",
            )
        state = self.root / ".groma" / "last-catchup.json"
        state.write_text(json.dumps({"at": "2026-11-01T00:00:00Z", "written_by": {"entry": "cli:old"}}),
                         encoding="utf-8")
        # 改写：原断言是 `上次写入 2026-11-09T00:00:00Z`（裸 UTC），
        # 改成本机 +08:00 下的同一时刻，并加一条"裸 UTC 串不再出现在这行"——同一条记录、同一行文案，强度只增不减。
        with mock.patch("lab.timeformat.local_tz", return_value=TZ8):
            body = self.client.get("/").get_data(as_text=True)
        self.assertIn("上次写入 2026-11-09 08:00:00 +08:00", body)
        self.assertNotIn("上次写入 2026-11-09T00:00:00Z", body)
        self.assertIn("<code>cli:new</code>", body)

    def test_overview_instant_is_rendered_in_local_timezone(self) -> None:
        """Y3：概览那个时刻按本机时区显示，且带有效偏移。"""
        import json

        from lab import topics as topics_mod

        # 同扩：这处 2 依赖"表格行来自 units"；表改走人建标签后，
        # 让这份工作区同时成为一个协作主题的家，同一时刻仍出现在两处，强度不变。
        topics_mod.create_topic("web-unit")
        digests = self.root / ".groma" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        stamp = "2026-11-09T00:00:00Z"
        (digests / ("digest-%s.json" % stamp.replace("-", "").replace(":", "").replace("Z", ""))).write_text(
            json.dumps({"schema": 1, "created_at": stamp, "file_count": 1,
                        "written_by": {"entry": "cli:w23", "trigger": "manual", "pid": 7}}),
            encoding="utf-8",
        )
        with mock.patch("lab.timeformat.local_tz", return_value=TZ8):
            body = self.client.get("/").get_data(as_text=True)
        at = body.index("上次写入")
        line = body[at:at + 90]
        self.assertIn("2026-11-09 08:00:00 +08:00", line)
        self.assertNotIn("2026-11-09T00:00:00Z", line)
        # 实测：这同一个 instant 在概览出现在**两处**（「上次写入」与那一行主题的「采集」列），
        # 按"每个实例都要查"的规矩逐处钉——两处都必须是本机串，且裸 UTC 在整个响应里 0 命中。
        # 链路健康句也渲染同一条「上次写入」时刻，2→3 处。
        self.assertEqual(body.count("2026-11-09 08:00:00 +08:00"), 3)
        self.assertEqual(body.count("2026-11-09T00:00:00Z"), 0)

    def _two_units_one_theme(self) -> dict:
        """造现机那个形状：两份 Unit 记录（一份是人建主题的家、一份是工作区自带的自动卡），一份人建标签。"""
        from lab import topics as topics_mod
        from lab.workspace import init_workspace

        other = Path(self._tmp.name) / "auto-card"
        other.mkdir()
        init_workspace(other, name="auto-card", owner="tester")
        return topics_mod.create_topic("web-unit")

    def test_the_chip_counts_themes_not_unit_cards(self) -> None:  # A-1
        self._two_units_one_theme()
        body = self.client.get("/").get_data(as_text=True)
        line = body[body.index("整机口径"):][:160]
        self.assertIn("1 个协作主题", line)
        self.assertNotIn("2 个协作主题", line, "chip 还在数 units.json 的自动卡")
        # 回归证伪：同一行里工作区那一格仍数 2——收词没把机器维度一起挪走。
        self.assertIn("2 个工作区", line)

    def test_the_overview_table_lists_themes_not_auto_cards(self) -> None:  # A-2
        self._two_units_one_theme()
        body = self.client.get("/").get_data(as_text=True)
        head = '<tr><th style="width:26%">协作主题</th>'
        table = body[body.index(head):]
        table = table[:table.index("</table>")]
        self.assertEqual(table.count("<tr>") - 1, 1, "概览主题表把没起过标签的自动卡也列成了协作主题")
        self.assertIn("web-unit", table)
        self.assertNotIn("auto-card", table)

    def test_the_overview_explains_where_a_theme_gets_its_source(self) -> None:  # A-2′（Owner 拍词 #2）
        self._two_units_one_theme()
        body = self.client.get("/").get_data(as_text=True)
        line = body[body.index("整机口径"):][:400]
        self.assertIn("已经采到会话", line)
        self.assertNotIn("起过名", line, "文案还在说『给工作区起名』，Owner 已判这个定义不成立")

    def test_the_overview_empty_state_points_at_creating_a_theme(self) -> None:  # A-2″（Owner 拍 §2-②）
        # 表改成只列人建标签后，"没有行"的意思变了：本机可能有工作区，但一个主题都建不起来。
        # 取数改成谓词后，夹具默认就带着一个主题 ⇒ "空"这一格要显式把谓词置空来造。
        # 断言面一字未减：仍是"空态那句在表里、被否掉的旧句子不在"。
        with mock.patch("lab.unitmatch.themes", return_value=[]):
            body = self.client.get("/").get_data(as_text=True)
        head = '<tr><th style="width:26%">协作主题</th>'
        table = body[body.index(head):]
        table = table[:table.index("</table>")]
        self.assertIn("本机还没有协作主题。", table)
        self.assertIn("已经采到会话的工作区", table)
        self.assertNotIn("把一个工作区收进来", table, "空态还在把用户指向『收工作区进本机』那一步")

    def test_the_candidate_column_names_the_theme_a_folder_claims(self) -> None:  # A-3
        outsider = Path(self._tmp.name) / "outsider-ws"
        outsider.mkdir()
        self.client.post("/workspaces/add", data={"path": str(outsider), "from": "discover_page"})
        body = self.client.get("/units").get_data(as_text=True)
        cells = re.findall(r"<th[^>]*>([^<]*)</th>", body)
        self.assertIn("它自称属于哪个协作主题", cells)
        self.assertNotIn("它自称的协作主题", cells)
        # 按实例数钉：这一格出现几次，取决于有几张候选表；候选表本身为 0 时这条不许空过。
        self.assertEqual(cells.count("它自称属于哪个协作主题"),
                         body.count("可并入这个协作主题的工作区"),
                         "列名与候选表张数不同源")

    def test_the_theme_page_lists_only_themes_a_human_made(self) -> None:  # A-1
        tree = self._two_units_one_theme()
        body = self.client.get("/units").get_data(as_text=True)
        cards = re.findall(r'href="[^"]*unit=([^&"]+)"[^>]*>删除这个协作主题</a>', body)
        self.assertEqual([tree["topic_id"]], cards,
                         "主题页把没人建过的自动身份记录也列成了协作主题（每张卡都该有一个删除口）")
        # 回归证伪：被摘掉的那张卡仍是一个工作区，只是不再冒充协作主题——不许把它从页面上整个抹掉。
        self.assertIn("auto-card", body)
        # 它现在只出现在候选表那一格里，而那一格得说「没有协作主题」，不能再报一个名字。
        # 取段从候选表那张表的标题往后找——页首「新建一个协作主题」的下拉里也有同一个目录名，
        # 从页首取段会钉到 option 文本上，那是另一格。
        pool = body[body.index("可并入这个协作主题的工作区"):]
        cell = pool[pool.index("auto-card"):pool.index("auto-card") + 600]
        self.assertIn("没有协作主题", cell, "候选表还在给自动折出来的身份记录报主题名")

    def test_the_auto_card_carries_no_theme_actions(self) -> None:  # A-2
        self._two_units_one_theme()
        body = self.client.get("/units").get_data(as_text=True)
        # 按实例数钉，三个动作各一个：留下的那个主题必须全有（空过＝把动作一起删了），
        # 被摘的那张卡必须一个都没有（今天它有两套 ⇒ 计数 2）。
        for anchor, label in (('class="reforge"', "重铸"),
                              (">加入这个协作主题<", "加入"),
                              (">删除这个协作主题<", "删除")):
            self.assertEqual(1, body.count(anchor),
                             "「%s」的实例数不等于主题块数——自动卡那块上还挂着动作" % label)
        self.assertEqual(1, body.count("可并入这个协作主题的工作区"))

    def test_the_theme_block_offers_retiring_it_to_a_plain_workspace(self) -> None:
        """「退成纯工作区」只挂在人建的那块上，并与删除口一样折在详情里。

        按实例取数：链接少一个＝出口没给；多一个＝自动卡那块又冒充了主题。
        """
        tree = self._two_units_one_theme()
        body = self.client.get("/units").get_data(as_text=True)
        links = re.findall(
            r'href="[^"]*unit=([^&"]+)"[^>]*>退成纯工作区（身份卡与说明都留）</a>', body)
        self.assertEqual([tree["topic_id"]], links,
                         "退口的实例数不等于人建主题的块数")
        self.assertNotIn("退成纯工作区", outside_details_blocks(body),
                         "两个不可逆动作并排常驻可见面")

    def test_the_retire_confirmation_shows_the_intent_it_keeps(self) -> None:
        """确认页要把"留下什么"摊开：那句说明、哪些工作区、以及它现在还是主题。

        这一页只许读账——把"看一眼确认页"变成"已经退了"，用户就找不到撤销的地方了。
        """
        from lab import state
        from lab import topics as topics_mod

        topic_id = topics_mod.create_topic("web-unit")["topic_id"]
        topics_mod.set_intent("web-unit", "只看这个仓的重构讨论")
        page = self.client.get("/units/retire/confirm?unit=%s" % topic_id).get_data(as_text=True)
        self.assertIn("只看这个仓的重构讨论", page, "确认页没把要留下的那句话亮出来")
        self.assertIn("工作区 1 个", page)
        self.assertIn(self.root.as_posix(), page, "留下哪些工作区没报名字")
        row = next(item for item in state.list_units() if item["unit_id"] == topic_id)
        self.assertEqual("human", row.get("origin"), "确认页自己动了账")
        self.assertIn(topic_id, [item["unit_id"] for item in unitmatch.themes()])

    def test_retiring_a_theme_from_the_page_keeps_the_card_rows_and_the_intent(self) -> None:
        """界面上退一条主题：来源改成 retired，主题从四页消失，数据一样不少。

        工作区那一格单独钉——改词改账不许把机器维度一起挪走。
        """
        from urllib.parse import unquote

        from lab import state
        from lab import topics as topics_mod

        topic_id = topics_mod.create_topic("web-unit")["topic_id"]
        topics_mod.set_intent("web-unit", "只看这个仓的重构讨论")

        def workspaces_half() -> str:
            body = self.client.get("/").get_data(as_text=True)
            line = body[body.index("整机口径"):]
            return line[:line.index("、")]

        kept_workspaces = workspaces_half()

        denied = self.client.post("/units/retire", data={"unit": topic_id},
                                  follow_redirects=False)
        self.assertIn("要经过确认页", unquote(denied.headers["Location"]))
        self.assertEqual("human", next(item for item in state.list_units()
                                       if item["unit_id"] == topic_id).get("origin"),
                         "不带确认标记那一步就该原样退出，不许先改了再报错")

        done = self.client.post("/units/retire", data={"unit": topic_id, "confirm": "1"},
                                follow_redirects=False)
        self.assertIn("已退成纯工作区", unquote(done.headers["Location"]))

        self.assertEqual("retired", next(item for item in state.list_units()
                                         if item["unit_id"] == topic_id).get("origin"),
                         "写成 card 会被谓词的存量那一支重新认成主题")
        self.assertNotIn(topic_id, [item["unit_id"] for item in unitmatch.themes()])
        self.assertTrue((self.root / "groma-unit.json").is_file(), "身份卡被动了")
        self.assertEqual("只看这个仓的重构讨论", topics_mod.topic_record(topic_id)["intent"],
                         "用户写的那句判据被退了就没回来了")

        units_body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("本机还没有协作主题", units_body)
        self.assertNotIn("退成纯工作区", units_body, "退过的东西还在主题页上挂着出口")
        self.assertEqual(kept_workspaces, workspaces_half(),
                         "工作区那一格跟着主题数一起掉了")

    def test_a_new_theme_shows_up_before_any_intent_is_written(self) -> None:  # A-3
        from lab import topics as topics_mod
        fresh = Path(self._tmp.name) / "fresh-ws"
        fresh.mkdir()
        self.client.post("/workspaces/add", data={"path": str(fresh), "from": "discover_page"})
        before = len(topics_mod.list_topics())
        self.client.post("/unit/create", data={"name": "刚建的主题", "home": str(fresh)})
        self.assertEqual(before, len(topics_mod.list_topics()),
                         "这条要钉的是「没写说明」那一格：建主题不该顺手补 topics 行")
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("刚建的主题", body, "新建完当场看不见自己的主题——闸门只认了 topics.json 那份账")
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn("刚建的主题", index[index.index('<tr><th style="width:26%">协作主题</th>'):],
                      "概览那张表漏了刚建好的主题")

    def test_creating_a_theme_records_that_a_human_made_it(self) -> None:  # A-5（人为这一半）
        from lab import state
        fresh = Path(self._tmp.name) / "marked-ws"
        fresh.mkdir()
        self.client.post("/workspaces/add", data={"path": str(fresh), "from": "discover_page"})
        self.client.post("/unit/create", data={"name": "标了人的主题", "home": str(fresh)})
        record = next(item for item in state.list_units() if item["name"] == "标了人的主题")
        self.assertEqual("human", record.get("origin"),
                         "人建动作没记进 units.json ⇒ 这条主题要到写说明才看得见")

    def test_the_shell_chip_counts_workspaces_and_themes_apart(self) -> None:  # B-1
        self._two_units_one_theme()
        body = self.client.get("/units").get_data(as_text=True)
        chip = re.search(r'工作区 <b>(\d+)</b> ｜ 协作主题 <b>(\d+)</b>', body)
        self.assertIsNotNone(chip, "外壳那条 chip 取不到数了")
        self.assertEqual("2", chip.group(1), "收词把机器维度一起挪走了：工作区仍按登记数计")
        self.assertEqual("1", chip.group(2), "外壳 chip 还在数 units.json 的自动卡")

    def test_a_registration_under_temp_is_badged_and_removable(self) -> None:
        """A-3（W31b）：脚本沙箱混进清单的那种行，要在页面上认得出来、并且有去处。

        按实例取段：这枚 badge 的次数必须等于"路径在临时目录下"的行数——整页 grep 会被
        同一行里那几枚别的 badge（当前查看／启动目录）蒙过，而空集合会假绿。
        回归证伪在同一条里：普通工作区那行不许亮这枚 badge。
        """
        stray = Path(self._tmp.name) / "w31-stray"
        stray.mkdir()
        self.client.post("/workspaces/add", data={"path": str(stray), "from": "discover_page"})
        # 反向半边要用一个**真的不在 Temp 下**的目录：本夹具自己的根就在 Temp 里，
        # 拿它当"普通工作区"等于让判据自己给自己作证（那条会一直绿，因为它认不出反例）。
        plain = Path(__file__).resolve().parents[1]  # 本仓根：在盘上，不在临时目录下
        self.client.post("/workspaces/add", data={"path": str(plain), "from": "discover_page"})
        body = self.client.get("/discover").get_data(as_text=True)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        stray_rows = [row for row in rows if "w31-stray" in row]
        plain_rows = [row for row in rows if plain.as_posix() in row]
        self.assertEqual(1, len(stray_rows), "清单里找不到那行沙箱登记，判据就没法按实例数")
        self.assertEqual(1, len(plain_rows), "对照那行也没渲染出来，回归证伪就是空集合")
        self.assertIn("临时目录", stray_rows[0])
        self.assertIn("移除", stray_rows[0])  # 去处复用既有那个出口，不另写第二份"能不能删"
        self.assertNotIn("临时目录", plain_rows[0], "普通工作区被一起打上 badge ⇒ 判据认错了范围")

    def test_membership_date_is_converted_before_truncation(self) -> None:
        """Y4：日期先换算再截断——UTC 23:30 在本机已是次日。"""
        from lab import state, unitmatch

        member = Path(self._tmp.name) / "member-w23"
        member.mkdir()
        unit_id = load_unit(self.root)["unit_id"]
        with mock.patch.object(state, "_now", return_value="2026-11-08T23:30:00Z"):
            unitmatch.join_unit(unit_id, member, role=unitmatch.ROLE_EVIDENCE,
                                authorized_by="w23-tester")
        with mock.patch("lab.timeformat.local_tz", return_value=TZ8):
            body = self.client.get("/units").get_data(as_text=True)
        marker = "w23-tester · "
        at = body.index(marker)
        self.assertEqual(body[at + len(marker):at + len(marker) + 10], "2026-11-09")
        self.assertNotIn(marker + "2026-11-08", body)

    # --- 成功要有反馈（K6）--------------------------------------------------

    def test_each_action_announces_its_result(self) -> None:
        """Every success path says what it did; only errors used to speak up.

        The shape is already in the codebase -- assets_export passes notice= --
        so these seven are callers that dropped an existing parameter, not a
        new feedback mechanism.
        """
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        other = Path(self._tmp.name) / "h2"
        (other / "projects").mkdir(parents=True)
        (other / "projects" / "t.jsonl").write_text('{"type":"user"}\n', encoding="utf-8", newline="\n")
        third = Path(self._tmp.name) / "h3"
        (third / "projects").mkdir(parents=True)
        setup = self.client.post("/discover/add", data={"name": "rm-me", "path": str(other),
                                                        "include": "projects", "harness": "test"})
        self.assertNotIn("error=", setup.headers["Location"])  # precondition, not the finding

        # 资产单例化改写，/assets/build 退役，从动作清单摘除。
        actions = [
            ("/settings", {"interval": "7"}),
            ("/refresh", {}),
            ("/pause", {}),
            ("/resume", {}),
            ("/discover/add", {"name": "src2", "path": str(third), "include": "projects",
                               "harness": "test"}),
            ("/sources/remove", {"name": "rm-me"}),
        ]
        for path, data in actions:
            with self.subTest(path=path):
                response = self.client.post(path, data=data, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertNotIn("error=", response.headers["Location"])
                self.assertIn("notice=", response.headers["Location"])

    # --- 发现页读写同源（K9′）------------------------------------------------

    def _second_workspace(self) -> Path:
        from lab.workspace import init_workspace

        other = Path(self._tmp.name) / "b"
        other.mkdir()
        init_workspace(other, name="b-unit", owner="tester")
        return other

    def test_discover_write_follows_the_displayed_workspace(self) -> None:
        """The page renders `chosen`, so the writes must land on `chosen` too.

        Before this, add/remove called `current()` while the table showed a
        different folder: staring at an empty workspace and clicking 申报 wrote
        somewhere else, and the interface never said where.
        """
        other = self._second_workspace()
        b_src = Path(self._tmp.name) / "bsrc"
        (b_src / "projects").mkdir(parents=True)
        add_source(self.root, "keep-me", str(self.src), include=["projects"])
        untouched = (self.root / ".groma" / "sources.json").read_bytes()

        page = self.client.get("/discover", query_string={"ws": other.as_posix()})
        self.assertIn(other.as_posix(), page.get_data(as_text=True))

        response = self.client.post("/discover/add", data={
            "name": "in-b", "path": str(b_src), "include": "projects",
            "harness": "test", "ws": other.as_posix(),
        })
        self.assertNotIn("error=", response.headers["Location"])
        self.assertIn("in-b", [item["name"] for item in load_sources(other)])
        self.assertEqual([item["name"] for item in load_sources(self.root)], ["keep-me"])
        self.assertEqual((self.root / ".groma" / "sources.json").read_bytes(), untouched)

        removed = self.client.post("/sources/remove", data={"name": "in-b", "ws": other.as_posix()})
        self.assertNotIn("error=", removed.headers["Location"])
        self.assertEqual(load_sources(other), [])
        self.assertEqual([item["name"] for item in load_sources(self.root)], ["keep-me"])

    def test_discover_form_says_which_workspace_it_writes_to(self) -> None:
        """The hidden `ws` and the 申报到 line are the same fact, shown and sent.

        A behaviour test alone would not catch "the form forgot to carry ws":
        that variant still writes correctly for the fallback, and quietly
        re-aim itself the moment a second workspace is on the page.

        candidates 的「加入」按钮撤掉后，页面上唯一的申报表单来自
        root_rows，所以必须让 b 有一个"存在但没申报"的根，否则这条会被空表空转成绿。
        """
        other = self._second_workspace()
        b_home = Path(self._tmp.name) / "bhome"
        (b_home / "cache" / "projects").mkdir(parents=True)
        add_source(other, "bsrc", str(b_home), "qoder-cn", ["projects"])
        body = self.client.get("/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True)
        self.assertIn('name="ws" value="%s"' % other.as_posix(), body)
        self.assertIn("申报到：<b>%s</b>" % other.name, body)

    # --- 默认值与术语（K7 / K8 / K20 / K10(b)）-------------------------------

    def test_discover_defaults_to_the_workspace_in_use_not_the_first_row(self) -> None:
        """`registered_paths()[0]` is whichever folder the registry happens to lead with.

        A scratch directory landing there puts an outsider's first look at 发现
        on a page of zero rows. The scope the app is already working in
        (`current()`, which is always some Unit's home) is the honest default.
        """
        from lab.state import list_workspaces

        self._second_workspace()
        add_source(self.root, "junk", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        order = [Path(item["path"]) for item in list_workspaces()]
        self.assertEqual(len(order), 2, order)
        lead, in_use = order[0], order[1]   # the registry leads with `lead`
        self.assertNotEqual(lead, in_use)   # otherwise this test proves nothing

        self.client.get("/unit/select", query_string={"unit_id": load_unit(in_use)["unit_id"]})
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], in_use.as_posix())
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn("申报到：<b>%s</b>" % in_use.name, body)
        self.assertNotIn("申报到：<b>%s</b>" % lead.name, body)

    # 发现页的「并进一个主题」表单已撤（添加工作区与挂主题是两个动作、两个页面），
    # 因此这里原本有两条钉那块表形状的用例，一并作废：
    #   · `test_discover_join_form_marks_the_selected_theme`（钉 <select name="unit"> 的 selected 落点，K8 那笔账）
    #   · `test_discover_join_form_prefills_the_viewed_workspace_path`（钉 path 预填当前查看的区）
    # 作废理由与 §2.4 #2 同形：**它们守的是已被 Owner 推翻的形状**，表单本身没了，selected/prefill 两个问题不再存在。
    # 三条留痕：改前在未改代码上绿 @10:37:16（与 651/1618 同一跑，4 tests OK）→ 代码跟随后这两条红 @10:41:19
    # （红在 `<option ... selected>` / `<input name="path" value=...>` 两个字面上，正是被撤走的形状）→ 删掉测试后整套绿。
    # "主题下拉要标默认项"这条判据的强度不在这两处丢失——主题页的【加入这个主题】由
    # "每个 form 的 hidden unit 必须等于本 block 的 unit_id"接管，那是更强的逐实例判据。

    def test_the_theme_page_says_theme_not_unit(self) -> None:
        """K10 (b): the navigation says 主题, so the body text must not say Unit.

        这条其实打在发现页上（名字是历史）。把发现页唯一的「本主题」换成
        「本区」后，载体改由添加卡那句「不挂主题。挂主题在主题页」承担——**强度不变**：
        仍是"正文用主题这个词、不写 Unit"。主题页那半边由主题页自己的测试覆盖。
        """
        other = self._second_workspace()
        body = self.client.get("/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True)
        self.assertNotIn("本 Unit", body)
        self.assertIn("不挂协作主题", body)
        # 发现页那块「并进一个主题」（含 others 表）已撤，
        # 所以这里原本断言 others 块形状的三行**不是删掉就算**——它随块搬到主题页，
        # 由 `test_the_theme_page_lists_the_outsiders_it_can_absorb` 按"每个实例"接手。
        removed = self.client.get("/workspaces/remove", query_string={"path": str(self.src)}).get_data(as_text=True)
        self.assertIn("← 返回", removed)  # 退路文案收短："协作主题"二字这页已经说清
        self.assertNotIn("← 返回协作主题", removed)  # 子串判据要有牙：W21b 改名后，防的是新词写回去
        assets = self.client.get("/assets").get_data(as_text=True)
        self.assertNotIn("本 Unit", assets)

    # --- 破坏性确认两步化（K11）----------------------------------------------

    def test_destructive_actions_need_two_submissions(self) -> None:
        """What this knife adds is the confirm page, not the server gate.

        `vault_purge` already refused a submission without `confirm=1`; the
        hole was that the only thing stopping a one-click 1.9 GB deletion with
        JavaScript off was `onsubmit`, because the form hard-coded the token.
        So the falsifiable half here is the page, and the gate half is a pin.
        """
        from urllib.parse import unquote

        vault_dir = self._fake_vault({
            "schema": 1,
            "kind": "GROMA_VAULT_INDEX",
            "generated_at": "2026-11-10T00:00:00Z",
            "session_count": 2,
            "shard_count": 3,
            "bytes_total": 1234567,
            "sessions": [{"harness": "qoder-cn", "session_key": "q:a", "shards": [{"name": "s1"}]},
                         {"harness": "codex", "session_key": "c:b", "shards": [{"name": "s2"}]}],
        })

        purge_page = self.client.get("/vault/purge/confirm")
        self.assertEqual(purge_page.status_code, 200)
        purge_body = purge_page.get_data(as_text=True)
        self.assertIn("2 个会话", purge_body)
        self.assertIn("3 个分片", purge_body)
        self.assertIn("不可恢复", purge_body)
        self.assertIn('name="confirm" value="1"', purge_body)

        denied = self.client.post("/vault/purge", follow_redirects=False)
        self.assertEqual(denied.status_code, 303)
        self.assertIn("确认", unquote(denied.headers["Location"]))
        self.assertTrue((vault_dir / "index.json").is_file())

        move_page = self.client.get("/vault/move/confirm", query_string={"to": str(Path(self._tmp.name) / "newvault")})
        self.assertEqual(move_page.status_code, 200)
        self.assertIn("newvault", move_page.get_data(as_text=True))
        refused = self.client.post("/vault/move", data={"to": str(Path(self._tmp.name) / "newvault")})
        self.assertIn("确认", unquote(refused.headers["Location"]))
        self.assertTrue((vault_dir / "index.json").is_file())

        remove_page = self.client.get("/sources/remove/confirm",
                                      query_string={"name": "nope", "ws": self.root.as_posix()})
        self.assertEqual(remove_page.status_code, 200)
        remove_body = remove_page.get_data(as_text=True)
        self.assertIn("不会删除任何会话文件", remove_body)
        self.assertIn("nope", remove_body)

    # --- 观测页不再嵌套整份文档（K5）------------------------------------------

    def test_observe_artifact_is_served_as_a_standalone_document(self) -> None:
        """A whole document inside a card leaked the artifact's CSS into the shell.

        The page is generated as a standalone file (it has to be, to keep its
        own styles), so the shell links to it instead of wearing it.
        """
        from web.app import _observe_html_path

        artifact = ('<!DOCTYPE html>\n<html lang="zh"><head><meta charset="utf-8">'
                    '<style>h1{color:#123456}</style></head><body><h1>单机观测</h1></body></html>')
        _observe_html_path().write_text(artifact, encoding="utf-8", newline="\n")

        response = self.client.get("/observe/artifact")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertTrue(response.get_data(as_text=True).lstrip().lower().startswith("<!doctype html"))
        self.assertIn("单机观测", response.get_data(as_text=True))

        page = self.client.get("/observe").get_data(as_text=True)
        self.assertEqual(page.lower().count("<!doctype"), 1)
        self.assertNotIn("#123456", page)
        self.assertIn("打开观测页", page)

    # --- 拆关系图页（K17）-----------------------------------------------------

    def test_evidence_page_lists_active_evidence_and_links_to_sessions(self) -> None:
        """主题页那一行「N 条证据」点进来，条数必须等于 active 证据数。"""
        from lab import topics

        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        unit_id = load_unit(self.root)["unit_id"]
        topics.create_topic("web-unit")
        topics.select(unit_id, [{"source": "src", "path": "projects/s.jsonl"}],
                      decided_by="human", known_keys={("src", "projects/s.jsonl")})

        body = self.client.get("/evidence").get_data(as_text=True)
        self.assertEqual(len(topics.active_evidence(unit_id)), 1)
        self.assertIn("1 条证据", body)
        self.assertEqual(body.count('class="evidence-row"'), 1)
        self.assertIn("projects/s.jsonl", body)
        self.assertIn("/session?", body)

    def test_gap_page_rows_equal_the_overview_number(self) -> None:
        """概览上那个「漏采 N」点进去要有 N 行，且每行给出为什么。"""
        import re

        add_source(self.root, "src", str(self.src), harness="claude-code", include=["projects"])
        overview = self.client.get("/").get_data(as_text=True)
        declared = re.search(r"漏采 (\d+)", overview)
        self.assertIsNotNone(declared, "概览页没有漏采数字")
        expected = int(declared.group(1))
        self.assertGreaterEqual(expected, 1, "夹具没有缺口，这条测试将一事无成")

        body = self.client.get("/gaps").get_data(as_text=True)
        self.assertIn("漏采 <b>%d</b>" % expected, body)
        self.assertEqual(body.count('class="gap-row"'), expected)
        self.assertIn("厂商清单", body)
        self.assertIn("分母不来自我们自己的产出", body)

    def test_the_navigation_has_six_entries(self) -> None:
        """7 → 6：关系图这一页下线，证据与缺口挂回原来的入口。

        导航加第 7 项「设置」（设置收纳新页），计数守卫 6→7。
        """
        nav = first_nav_block(self.client.get("/").get_data(as_text=True))
        self.assertNotIn("/graph", nav)
        self.assertEqual(nav.count("<a "), 7)

    def test_rail_is_the_first_nav_container(self) -> None:
        """外壳搬进左栏后，页面里**第一个** `<nav>` 仍必须是那 6 项导航，工作区块住在 `<aside>`。

        这条是缺陷回归钉（未修版上红在 `assertIn("<aside", body)`——今天根本没有 aside）。
        为什么盯"第一个 nav + aside 存在"这一对：既有契约 `test_the_navigation_has_six_entries`
        是按第一个 nav 块数 `<a `=6 的（取法见 `first_nav_block`）。左栏要新增工作区列表，
        **一旦把它也塞进 `<nav>`，那条红线就变成 6+N**——红的是排版选择，不是它守的东西。
        所以这里把"家在哪"钉死：nav 归 nav，工作区归 `<aside>`。
        """
        body = self.client.get("/").get_data(as_text=True)
        nav = first_nav_block(body)
        # nav 7 项（+「设置」）。
        self.assertEqual(nav.count("<a "), 7)
        self.assertIn("<aside", body)

    def test_shell_keeps_six_entry_labels(self) -> None:
        """6 个页面名必须逐个仍是链接的完整文字——搬家不许把谁换成只剩图标的东西。

        **不变量**：它今天就是绿的（六个名字就在 `base.html` 的 `<nav>` 里），
        它守的是外壳改造过程中不许退化。指定的证伪手法：把「观测」改名成"单机观测"→
        本条必须红 → 改回必须绿；**不许用"删一项"来证**——删项会同时撞 nav 计数守卫，
        两条测试就分不清谁在守什么，等于少守一条。

        因此断言打在**锚点全文**上（`>观测</a>`）而不是裸子串上：裸 `assertIn("观测")` 对
        "单机观测"这种改名是**证空**的（子串还在，测试照绿），回归证伪就白付了。
        顺序不作断言——DOM 顺序不是契约。
        """
        nav = first_nav_block(self.client.get("/").get_data(as_text=True))
        for label in ("概览", "发现", "活动", "资产", "协作主题", "观测"):
            self.assertIn(">%s</a>" % label, nav)
        self.assertNotIn(">主题</a>", nav)  # 防半改：旧标签不许和「协作主题」同时在场

    def test_the_first_nav_block_declares_its_name(self) -> None:
        """R17：nav 块必须自带 `aria-label="主导航"`。

        取法放宽（允许标签带属性）本身不可测——要测的是放宽换回来的那件东西，
        否则"改了取法"和"什么都没改"在测试上长得一样。
        """
        body = self.client.get("/").get_data(as_text=True)
        opening = re.search(r"<nav[^>]*>", body)
        self.assertIsNotNone(opening, "页面里没有 nav 标签")
        self.assertIn('aria-label="主导航"', opening.group(0))

    def test_observe_template_has_no_dead_blocks(self) -> None:
        """`observe.html` 里那两个从不渲染的块要删掉（拍板 6：删，不修）。

        `base.html` 只定义 `{% block body %}`，所以子模板里的 `{% block title %}` 与
        `{% block nav %}` **永远不会出现在响应里**——它是上一轮删关系图页时留下的残骸。
        残骸比缺代码更坏：那块死 nav 只列 5 项（缺「资产」），还写成 `href=… class="active"`，
        **与红线要求的属性顺序相反**。谁哪天给 base 补一个 `{% block nav %}`，它就会活过来，
        同时撞掉「六项计数」与「第一个 nav」两条红线——那才是查半天的现场。

        这条只能查**模板源码**：Jinja 块不渲染，响应里根本看不到它。
        """
        from pathlib import Path

        import web

        source = (Path(web.__file__).resolve().parent / "templates" / "observe.html").read_text(encoding="utf-8")
        self.assertNotIn("{% block nav %}", source)
        self.assertNotIn("{% block title %}", source)

    def test_shell_css_declares_sticky_rail_and_header(self) -> None:
        """左栏与抬头要吸顶：本页 CSS 里 `position:sticky` 必须**至少出现两次**。

        **这条是弱断言，自标清楚**：它只钉"CSS 规则声明了两次 sticky"，
        **钉不到渲染是否真生效**——sticky 会被祖先的 `overflow` 吃掉（`.shell` 少了
        `align-items:flex-start` 时 flex 的默认 stretch 同样会让它失效）。
        真生效与否归真机几何复量，**不许因为这条绿了就写"已验证吸顶"**。
        """
        body = self.client.get("/").get_data(as_text=True)
        self.assertGreaterEqual(body.count("position:sticky"), 2)

    def test_the_scope_selector_is_a_single_get_form(self) -> None:
        """切换器恰好一个 GET 表单，pills 已删。

        零 JS 的做法是原生 select + submit（不用 onchange）；「一个动词、一处入口」
        在切换上是真的只有一处——清单行链接是"跳到该区查看"，但**切换控件**只有这张卡。
        """
        import re

        body = self.client.get("/discover").get_data(as_text=True)
        forms = re.findall(r'<form[^>]*action="/discover"[^>]*>.*?</form>', body, re.S)
        self.assertEqual(len(forms), 1, "发现页上的 GET 切换表单不是恰好一处：%d" % len(forms))
        self.assertIn('method="get"', forms[0])
        self.assertIn('name="ws"', forms[0])
        self.assertNotIn('class="scope-switch"', body, "pill 容器还在——W20 要求删掉")

    def test_every_workspace_appears_as_an_option_once(self) -> None:
        """W20（A 类）：每个已登记工作区在 select 里恰好一个 option（按实例数）。"""
        import re

        other = self._second_workspace()
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn('<select name="ws"', body)
        select = body.split('<select name="ws"')[1].split("</select>")[0]
        options = re.findall(r'<option value="([^"]*)"', select)
        self.assertEqual(len(options), 2, "option 数 ≠ 已登记工作区数：%r" % options)
        self.assertEqual(set(options), {self.root.as_posix(), other.as_posix()})

    def test_the_scope_selector_renders_without_an_explicit_ws_parameter(self) -> None:
        """W20（A 类，改写自 W16 的同名判据）：不带 `ws` 时 select 仍渲染、选中回落项。

        `discover_target` 有回落（无 `ws` 时选 current()）；注册表为空时整卡（切换 + 采集面）
        都不渲染——没有可切对象时渲染空控件没有动作价值。
        """
        import re

        other = self._second_workspace()
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn('<select name="ws"', body)
        self.assertLess(body.index('class="scope-bar"'), body.index("我的工作区"),
                        "切换卡跑到清单后面去了——它必须在清单之前")
        selected = re.findall(r'<option value="([^"]*)" selected>', body)
        self.assertEqual(len(selected), 1, "selected 不是恰好一项：%r" % selected)

        with mock.patch("web.app.list_workspaces", return_value=[]):
            empty = self.client.get("/discover")
        self.assertEqual(empty.status_code, 200, "注册表为空时发现页崩了")
        empty_body = empty.get_data(as_text=True)
        self.assertNotIn('<select name="ws"', empty_body)
        self.assertNotIn('class="scope-bar"', empty_body)
        self.assertNotIn("这个区的采集面", empty_body)

    def test_switching_via_the_selector_targets_the_same_workspace_for_writing(self) -> None:
        """W20（改写自 W16 同名判据）：选到 B 之后，页面处处说"这次读写的是 B"。

        旧形状是 pill 链接；现在切换控件是 `GET /discover?ws=` 表单。保留的三条一致性：
        切换卡认得出 B（页头「当前查看」+ 路径）、B 的 option 带 `selected`、
        「申报到：<b>B</b>」原样在。
        """
        import re

        other = self._second_workspace()
        body = self.client.get("/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True)
        form = re.search(r'<form[^>]*action="/discover"[^>]*>.*?</form>', body, re.S)
        self.assertIsNotNone(form, "没有 GET 切换表单")
        form = form.group(0)
        self.assertIn('method="get"', form)
        self.assertIn('<option value="%s" selected>' % other.as_posix(), form)
        self.assertNotIn('<option value="%s" selected>' % self.root.as_posix(), form)
        card = body.split('class="scope-bar"')[1].split("</form>")[0]
        self.assertIn(other.as_posix(), card)
        self.assertIn("申报到：<b>%s</b>" % other.name, body)

        # 第①步面板的去向：换成表单后，写入仍跟着显示的那个区（POST 行为本身由
        # test_discover_write_follows_the_displayed_workspace 覆盖）。
        bsrc = Path(self._tmp.name) / "bsrc"
        bsrc.mkdir()
        response = self.client.post("/discover/add", data={
            "name": "via-selector", "path": str(bsrc),
            "include": "projects", "harness": "test", "ws": other.as_posix(),
        })
        self.assertNotIn("error=", response.headers["Location"])

    def test_selector_options_disambiguate_twins(self) -> None:
        """W20（改写自 W16）：同名目录的 option 文本各多带一级父目录，非重名的不加前缀。"""
        import re
        from pathlib import Path

        from lab.state import register_workspace
        from lab.workspace import init_workspace

        twins = []
        for parent in ("left", "right"):
            folder = Path(self._tmp.name) / parent / "ws"
            folder.mkdir(parents=True)
            init_workspace(folder, name="twin-%s" % parent)
            register_workspace(folder, folder.name)
            twins.append(folder)

        body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn('<select name="ws"', body)
        select = body.split('<select name="ws"')[1].split("</select>")[0]
        labels = {value: text.strip() for value, text in
                  re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select)}
        left_label, right_label = labels[twins[0].as_posix()], labels[twins[1].as_posix()]
        self.assertNotEqual(left_label, right_label, "两个同名 option 的可见文本不许一样")
        self.assertIn("left", left_label)
        self.assertIn("right", right_label)
        # 本夹具的 self.root 同时是 boot root，所以它带后缀、不带父目录前缀。
        self.assertEqual(labels[Path(self.root).as_posix()], Path(self.root).name + "（Groma 启动目录）")

    def test_rail_shows_no_selected_state_on_machine_wide_pages(self) -> None:
        """非发现页不许显示"当前工作区"选中态——把 §2.2 硬规定 1 从文档主张升成会红的测试。

        **B 类不变量钉**（未修版即为绿）：三手都要留痕——①搬家前记绿 ②搬家后记绿
        ③回归证伪。指定的手法：把选中条件里「是发现页」这半段去掉 ⇒ `/` 与
        `/observe` 上冒出「当前查看」⇒ 本条必须红。这正是搬家时最容易顺手写出来的错。

        正向半边（发现页必须含「当前查看」）是**防空转的锚**：没有它，`assertNotIn`
        可能因为整页压根没有这个元素而白绿。
        """
        other = self._second_workspace()
        for page in ("/", "/observe"):
            self.assertNotIn("当前查看", self.client.get(page).get_data(as_text=True))
        self.assertIn("当前查看", self.client.get(
            "/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True))

    def test_no_page_outside_discover_offers_a_workspace_switcher(self) -> None:
        """切换工作区的入口只许存在于发现页——整机口径的页面上不许出现它（A 类）。

        断言的是**反面**：`/`、`/observe`、`/units` 里连 `/discover?ws=` 这个串都不该有。
        发现页的 `?ws=` 只剩清单行链接（切换控件走 GET 表单的 `name=ws`），
        这条判据的字面与强度都没变。
        """
        for page in ("/", "/observe", "/units"):
            body = self.client.get(page).get_data(as_text=True)
            self.assertNotIn("/discover?ws=", body)
        self.assertIn("/discover?ws=", self.client.get("/discover").get_data(as_text=True))

    def test_the_selector_keeps_the_current_workspace_marked(self) -> None:
        """W20（改写自 W16）：当前区由 option 的 `selected` 标出，且页头可见「当前查看」。"""
        other = self._second_workspace()
        body = self.client.get("/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True)
        card = body.split('class="scope-bar"')[1].split("</form>")[0]
        self.assertIn("当前查看", card)
        self.assertIn('<option value="%s" selected>' % other.as_posix(), card)

    def test_every_declare_form_carries_the_displayed_workspace(self) -> None:
        """页面上**每一个** POST 到 /discover/add 的表单都要自带 hidden `ws`。

        这条不是额外收紧，是补 K9′ 的漏，取证时刻见提交信息：既有红线
        `test_discover_form_says_which_workspace_it_writes_to` 是整页
        `assertIn('name="ws" value="B"')`，而页面上有**两处**申报表单——
        第②步表格里的「申报这个根」没有 hidden `ws`、底部 candidates 那个有 ⇒
        **一个对的表单把另一个缺字的掩盖掉了**（那条测试自己的 docstring 就写着
        "the form forgot to carry ws" 是它要防的东西，防法却不足以防住）。
        后果：界面写「申报到：b」，点下去 `discover_target(request.form)` 拿不到 `ws`，
        回落 `current()`/注册表首行 ⇒ 写进另一个工作区，界面一个字都没说。
        """
        import re

        other = self._second_workspace()
        b_home = Path(self._tmp.name) / "bhome"
        (b_home / "cache" / "projects").mkdir(parents=True)
        # 只申报 projects，让 cache/projects 成为"存在但没申报"的那一行——
        # 它会渲染出第②步那个缺字表单。
        add_source(other, "bsrc", str(b_home), "qoder-cn", ["projects"])

        body = self.client.get("/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True)
        forms = re.findall(r'<form[^>]*action="/discover/add"[^>]*>.*?</form>', body, re.S)
        self.assertTrue(forms, "第②步没渲染出申报表单——夹具没布好，不许把这条当通过")
        for form in forms:
            self.assertIn('name="ws" value="%s"' % other.as_posix(), form)

    def test_observe_page_links_to_the_artifact_without_wearing_it(self) -> None:
        """观测页既不嵌 iframe、也不内联产物——产物只在 `/observe/artifact` 出现。

        **把两条 iframe 测试合并成这一条**（原名
        `test_observe_page_embeds_the_artifact_in_an_iframe` 与
        `test_observe_iframe_does_not_inline_the_artifact`）：三条 doctype 红线**原样搬进来**，
        只把"有框"换成"没框"。合并前的三步留痕第①步：两条在未改代码上 OK @ 16:11:42。

        为什么去掉框：iframe 是**第二条滚动条**，而且产物本身没有返回入口（生成器里
        `<a`/`href` 各 0 命中）——嵌进来照样回不去。项目所有者 的原始反馈是
        "打开观测页没有返回也没有关闭"，框并没有解决它，只是把孤岛搬进了本页。
        保留的部分：`打开观测页` 这个字面子串（K5 的护栏）与"不内联"这件事本身。
        """
        from web.app import _observe_html_path

        _observe_html_path().write_text(
            '<!DOCTYPE html>\n<html lang="zh"><head><meta charset="utf-8">'
            '<style>h1{color:#123456}</style></head><body><h1 id="probe">单机观测</h1></body></html>',
            encoding="utf-8", newline="\n")

        body = self.client.get("/observe").get_data(as_text=True)
        self.assertNotIn("<iframe", body)                     # 没框（本轮换掉的那半）
        self.assertIn("打开观测页", body)                      # 主入口的字面子串仍在
        self.assertEqual(body.lower().count("<!doctype"), 1)   # K5 三条原样搬入
        self.assertNotIn("#123456", body)
        self.assertNotIn('id="probe"', body)

    def test_observe_artifact_response_carries_a_self_labelled_return_bar(self) -> None:
        """产物经 Groma 供给时顶回一条**自标身份**的返回条，且磁盘那份一个字节不少（A 类）。

        自标身份是设计侧复核：注入条必须可被机器判定（`<meta name="groma-observe-return-bar">`），
        否则下一个读产物的人会拿"响应 == 磁盘文件"当不变量——而它现在不成立了。
        "磁盘文件不含标记"这半边同样重要：注入只许发生在供给层，产物仍是可独立存在的快照。
        """
        from web.app import _observe_html_path

        artifact = _observe_html_path()
        artifact.write_text(
            '<!DOCTYPE html>\n<html lang="zh"><head><meta charset="utf-8"></head>'
            '<body><h1>单机观测</h1></body></html>\n', encoding="utf-8", newline="\n")

        body = self.client.get("/observe/artifact").get_data(as_text=True)
        self.assertIn('name="groma-observe-return-bar"', body)
        self.assertIn('href="/observe"', body)
        # 注入必须落在 <body 之后：doctype 前缀是 K5 的红线，不能被顶掉
        self.assertTrue(body.lstrip().lower().startswith("<!doctype html"), body[:60])
        self.assertLess(body.lower().index("<body"), body.index('name="groma-observe-return-bar"'))
        self.assertNotIn("groma-observe-return-bar", artifact.read_text(encoding="utf-8"))

    def test_observe_artifact_injection_is_idempotent(self) -> None:
        """返回条只许出现一次：没标记的补上，已标记的不再叠（A 类，两段式）。

        **卡面把这条写成"未修版必红（今天计数 0）"——那个预测是错的**：
        如果只喂一份已带标记的产物，今天的计数就是 1（标记在我喂的文件里），测试**今天绿**，
        于是它其实是条空钉。所以这里分成两段：
        第一段喂**无标记**产物 ⇒ 今天计数 0 ⇒ A 类真红；
        第二段喂**已标记**产物 ⇒ 守的是"实现之后别每次都插一遍"（删掉幂等守卫就该红）。
        """
        from web.app import _observe_html_path

        artifact = _observe_html_path()

        artifact.write_text('<!DOCTYPE html>\n<html lang="zh"><head><meta charset="utf-8"></head>'
                            '<body><h1>单机观测</h1></body></html>\n', encoding="utf-8", newline="\n")
        first = self.client.get("/observe/artifact").get_data(as_text=True)
        self.assertEqual(first.count('name="groma-observe-return-bar"'), 1, "无标记的产物应被补上恰好一条返回条")

        artifact.write_text('<!DOCTYPE html>\n<html lang="zh"><head><meta charset="utf-8">'
                            '<meta name="groma-observe-return-bar" content="1"></head>'
                            '<body><h1>单机观测</h1></body></html>\n', encoding="utf-8", newline="\n")
        again = self.client.get("/observe/artifact").get_data(as_text=True)
        self.assertEqual(again.count('name="groma-observe-return-bar"'), 1, "已带标记的产物不许再叠一条")

    def test_artifact_without_a_body_tag_is_served_untouched(self) -> None:
        """找不到 `<body` 就原样供给：宁可不救，不可救坏（**B 类不变量钉**）。

        今天它是绿的——因为今天**根本没有注入**，响应天然等于文件。
        它守的是供给层注入函数写坏以后的样子：无条件插、或者把标记插到 doctype 前面，
        都会让一份畸形产物被改得无法渲染。

        证伪手法：
        删掉降级分支、让它无条件注入 ⇒ 本条必须红 ⇒ 改回必须绿。
        """
        from web.app import _observe_html_path

        odd = "<!DOCTYPE html>\n<html lang=\"zh\"><head><meta charset=\"utf-8\">" \
              "<title>畸形产物</title></head>没有 body 标签的一段正文\n"
        artifact = _observe_html_path()
        artifact.write_text(odd, encoding="utf-8", newline="\n")

        served = self.client.get("/observe/artifact").get_data(as_text=True)
        self.assertEqual(served, artifact.read_text(encoding="utf-8"))

    def test_build_graph_model_still_describes_the_unit(self) -> None:
        """防御钉：删的是 render_svg 的调用点，不是内核数据模型。

        这条钉不出任何缺陷——它现在就是绿的，存在的意义是下一次有人
        「页面都删了顺手把模型也清了」时能红。
        """
        from lab.graph import build_graph

        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        graph = build_graph(self.root)
        for key in ("nodes", "edges", "unit", "agents", "sessions",
                    "session_count", "event_count", "workspaces", "digest"):
            self.assertIn(key, graph)
        self.assertEqual(graph["session_count"], 1)

    # --- 花额度的自动化上界面（K2）--------------------------------------------

    def test_settings_stores_boolean_switches_without_a_code_change(self) -> None:
        """设计要求的先证：`lab/settings.py` 本轮真的不用动。

        这条在未改任何代码时就该绿（CLASSIFY_DEFAULTS 已含两个 bool，
        save_settings 对 bool 走 _as_bool）。它绿了，才只剩界面半边。
        """
        from lab.settings import save_settings

        saved = save_settings(self.root, {"auto_reforge": False})
        self.assertIs(saved["auto_reforge"], False)
        self.assertIs(load_settings(self.root)["auto_reforge"], False)
        self.assertIs(load_settings(self.root)["auto_inject"], True)

    def test_overview_offers_the_two_spend_switches(self) -> None:
        """花额度的自动化：默认开，但必须能关、且看得见（项目所有者 定的底线）。

        开关表单随设置收纳整体搬去 /settings；
        本条断言对象不变，改取设置页；概览不再含任何设置输入框（新测试另钉）。
        """
        body = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('name="auto_form" value="1"', body)
        self.assertIn('name="auto_reforge" value="on" checked', body)
        self.assertIn('name="auto_inject" value="on" checked', body)
        self.assertIn("一次归类会顺带花额度", body)

    def _serving_fixture(self) -> None:
        """夹具：让主题真有一份服役资产（hero/投放状态列的取数源）。"""
        from lab.asset import build_asset
        from lab.collect import catchup
        from lab.inject import inject_theme
        from lab.sources import add_source

        add_source(self.root, "src", str(self.src), "qoder-cn", ["projects"])
        catchup(self.root, force=True)
        asset = build_asset(self.root, "英雄条试跑", [("src", "projects/s.jsonl")])
        inject_theme(self._root_unit_id(), asset["asset_id"])
        # 成员行（移出协作主题的出处）+ 注册过 hook 的区（摘除 hook 的出处）
        member = Path(self._tmp.name) / "u2-member"
        member.mkdir()
        init_workspace(member, name="u2-member", owner="tester")
        self.client.post("/units/join", data={"unit": self._root_unit_id(), "path": str(member)})
        self.client.post("/unit/inject/hook", data={"path": str(self.root), "confirm": "1"})
        inject_theme(self._root_unit_id(), asset["asset_id"])

    def test_theme_hero_bar_names_the_serving_asset_and_delivery(self) -> None:
        """M3（新增）：主题卡 hero 条——当前生效资产名/asset_id/会话数/重铸时刻/注入比/送达对账/载荷原文链接。"""
        self._serving_fixture()
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("当前生效：", body)
        self.assertIn("英雄条试跑", body)
        self.assertIn("重铸）", body, "hero 条没有重铸时刻")
        self.assertIn("送达对账", body)
        self.assertIn("查看载荷原文", body)

    def test_workspace_table_is_flat_with_delivery_badges_and_actions(self) -> None:
        """预览对齐改写：工作区表 4 列（屏 8 形状）；
        「暂停此区/恢复投递」升一级，「更多 ▾」收 摘除 hook/设为家/移出协作主题/从本机移除，
        行内无直接移出/摘除。POST 路由零新增零改名。"""
        self._serving_fixture()
        body = self.client.get("/units").get_data(as_text=True)
        plane = outside_details_blocks(body)
        # 平铺：4 列表头 + 一级动作在可见面
        for head in ("成员工作区", "装载面", "投递状态", "投放"):
            self.assertIn(head, plane)
        self.assertIn("刷新载荷", plane)
        self.assertIn("暂停此区", plane)
        # 「更多 ▾」是 details 的 summary——helper 把整个 details 算折叠面，但它视觉上是可见的入口；
        # 钉在响应体上（动作本体「摘除 hook」等在 details 里，由下面断言钉）。
        self.assertIn("更多 ▾", body)
        # 更多内含低频/不可逆动作（确认页路由不变）
        more = body.split("更多 ▾", 1)[1]
        self.assertIn("摘除 hook", more)
        self.assertIn("设为家", more)
        self.assertIn("移出协作主题", more)
        self.assertIn("从本机移除", more)
        # 行内不再直接出现移出/摘除（只在「更多」里）
        before_more = body.split("更多 ▾", 1)[0]
        self.assertNotIn("摘除 hook", before_more)
        self.assertNotIn("移出协作主题", before_more)

    def test_overview_chain_health_sentence_names_the_serving_asset_and_delivery(self) -> None:
        """M5（新增）：概览通栏一句话叙述闭环——采集/证据/服役资产名/注入比/送达字样。

        无服役资产时显示「未设注入源」，不装作健康也不报错。
        """
        from lab.asset import build_asset
        from lab.collect import catchup
        from lab.inject import inject_theme
        from lab.sources import add_source

        add_source(self.root, "src", str(self.src), "qoder-cn", ["projects"])
        catchup(self.root, force=True)
        from lab import topics as topics_mod

        asset = build_asset(self.root, "链路健康试跑", [("src", "projects/s.jsonl")])
        inject_theme(self._root_unit_id(), asset["asset_id"])
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("链路健康：", body)
        self.assertIn("链路健康试跑", body, "链路健康句没有点名服役资产")
        self.assertIn("送达", body, "送达对账字样没有上链路健康句")
        self.assertIn("注入 1/1 个工作区", body)

        # 无服役资产的空态：另建一个没有注入源的主题不可行（fixture 单主题），
        # 直接撤掉源（uninject）后句子退到「未设注入源」。
        from lab.inject import uninject
        uninject(self.root)
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("未设注入源", body)

    def test_settings_page_hosts_the_moved_forms_and_index_has_none(self) -> None:
        """设置收纳（新增）：GET /settings 200 且含间隔表单；概览不含任何设置输入框。"""
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('name="interval"', body)
        self.assertIn("归类用的模型", body)
        index = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('name="interval"', index, "间隔输入框还留在概览")
        self.assertNotIn('name="classify_model"', index)
        self.assertIn("设置页", index, "概览状态卡没有指去设置页")

    def test_overview_theme_rows_drop_the_unit_key_to_a_tip(self) -> None:
        """新增：概览主题表行撤 unit_ 码（ⓘ 里保留）。"""
        from lab import topics as topics_mod

        topics_mod.create_topic("web-unit")
        body = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("<code>unit_", body, "unit_ 码还在可见面")
        self.assertIn("工程键 unit_", body, "工程键不在 ⓘ 里了——撤出可见面不等于删数据")

    def test_nav_carries_the_settings_entry(self) -> None:
        """新增：nav 含「设置」项（观测之后）。"""
        nav = first_nav_block(self.client.get("/").get_data(as_text=True))
        self.assertIn(">设置</a>", nav)
        self.assertLess(nav.index(">观测</a>"), nav.index(">设置</a>"), "设置应在观测之后")

    def _root_unit_id(self) -> str:
        # 夹具辅助：主题级注入按注册表 id 寻家（同 test_t1_quick_wins 的修正）。
        from lab import unitmatch as um

        if not hasattr(self, "_cached_root_unit_id"):
            self._cached_root_unit_id = um.create_unit("web-unit", self.root)["unit_id"]
        return self._cached_root_unit_id

    def test_unchecked_box_turns_the_switch_off_for_real(self) -> None:
        """复选框不勾选时表单根本不发这个键；哨兵规则是唯一能把"关"说清楚的写法。"""
        # 表单在 /settings 页（端点不变）。
        body = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('name="auto_form" value="1"', body)

        off = self.client.post("/settings", data={"auto_form": "1", "auto_inject": "on"})
        self.assertIn("notice=", off.headers["Location"])
        self.assertIs(load_settings(self.root)["auto_reforge"], False)
        self.assertIs(load_settings(self.root)["auto_inject"], True)

        # 保存别的区（间隔）不许把这两个键顶回去
        self.client.post("/settings", data={"interval": "9"})
        self.assertIs(load_settings(self.root)["auto_reforge"], False)

        back_on = self.client.post("/settings", data={"auto_form": "1", "auto_reforge": "on",
                                                      "auto_inject": "on"})
        self.assertIn("notice=", back_on.headers["Location"])
        self.assertIs(load_settings(self.root)["auto_reforge"], True)

    def test_theme_page_shows_the_chain_stopped(self) -> None:
        """关掉之后，"没传导"要在界面上看得见，不能只停在数据库里。"""
        from lab import topics

        topics.create_topic("web-unit")
        self.client.post("/settings", data={"auto_form": "1"})
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("未传导（自动重铸已关）", body)

    # --- 主题页列表化 ------------------------------

    def test_the_theme_summary_is_visible_without_expanding(self) -> None:
        """摘要行留在可见面、管理动作收进 `<details>`（A 类）。

        判据必须是 outside_details（本轮 §3 新规矩）：折叠后 assertIn 全绿，
        测不出"用户要展开才看得见"。所以这里同时用正反两面——摘要在外面，改名/表格在里面。
        """
        body = self.client.get("/units").get_data(as_text=True)
        plane = outside_details_blocks(body)
        self.assertIn("web-unit", plane)
        self.assertIn("1 个工作区", plane)
        self.assertIn("0 份资产", plane)
        self.assertNotIn('action="/units/rename"', plane, "改名 form 还留在摘要面上")
        self.assertNotIn("移出协作主题", plane)

    def test_the_chain_status_stays_outside_details(self) -> None:
        """两句状态/警告不许折（B 类加强；红线 5）。

        今天在 /units 上 `<details>` 只有 others 的 id 详情，所以这条改前改后都应绿；
        它的牙齿靠回归证伪：把状态行整段包进 `<details>` ⇒ 必红。
        """
        from lab import topics

        topics.create_topic("web-unit")
        self.client.post("/settings", data={"auto_form": "1"})   # 两个自动都关
        plane = outside_details_blocks(self.client.get("/units").get_data(as_text=True))
        self.assertIn("未传导（自动重铸已关）", plane)
        self.assertIn("未投放（自动投放已关）", plane)
        self.assertIn("没有说明时自动归类会退化。", plane)

    def test_the_delete_theme_link_is_inside_details(self) -> None:
        """「删除这个主题」收进详情（A 类——与机会点 3 的红色分级无关）。"""
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("删除这个协作主题", body)
        self.assertNotIn("删除这个协作主题", outside_details_blocks(body))

    def test_theme_details_are_collapsed_by_default(self) -> None:
        """每个 block 的详情默认不展开（A 类）：有 `<details>`、没有一个带 `open`。

        同时钉 summary 自述：折叠的代价是"看不见里面有什么"，summary 是唯一补偿位。
        """
        import re

        body = self.client.get("/units").get_data(as_text=True)
        tags = re.findall(r"<details[^>]*>", body)
        self.assertGreaterEqual(len(tags), 1, "主题页没有任何 <details>——折叠没做")
        for tag in tags:
            self.assertNotIn(" open", tag, "有 details 默认展开：%s" % tag)
        # 工作区表平铺上浮，管理区瘦身为「家 · 改名 · 说明 · 角色表 · 注入源 · 可并入」；
        # 再瘦一级——角色表卡整体上移进「接力」卡，摘要随之去掉「角色表」；
        # ② 注入源卡退役，摘要再去「注入源」。
        self.assertIn("<summary class=\"muted\" style=\"cursor:pointer\">展开管理（家 · 改名 · 说明 · 可并入）</summary>", body)

    def test_outside_details_blocks_strips_nested_blocks(self) -> None:
        """M1（2026-11-16 验收，A 类）：嵌套 `<details>` 下"可见面"不许漏出折叠内容。

        造一个带身份卡的 outsider ⇒ others 行渲染内层 `<details><summary>详情</summary>`。
        旧的非贪婪正则会从外层一直吃到内层 `</details>`，把外层剩余（注入源 form、删除链接）
        漏回"可见面"——验收就是用这个状态把 `test_the_delete_theme_link_is_inside_details`
        打红的。正反两面都查：折叠的东西不在可见面，摘要行的东西在。
        """
        other = self._second_workspace()          # init_workspace 写了 groma-unit.json
        self.client.post("/workspaces/add", data={"path": str(other)})
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("<details><summary>详情</summary>", body, "夹具没造出内层 details")
        plane = outside_details_blocks(body)
        self.assertIn("web-unit", plane)          # 防空转：可见面确实取到了摘要
        self.assertNotIn("删除这个协作主题", plane)
        self.assertNotIn("加入这个协作主题", plane)

    def test_a_theme_without_workspaces_still_paints_a_summary(self) -> None:
        """空主题也有摘要行，空表提示留在详情里（A 类；防"折叠后啥也没有"）。

        用 `detect` 打桩造"这个主题 0 个工作区"；不打桩时每个主题至少有自己的家。
        """
        from web import app as web_app

        with mock.patch.object(web_app.unitmatch, "detect", return_value={"rows": []}):
            body = self.client.get("/units").get_data(as_text=True)
        plane = outside_details_blocks(body)
        self.assertIn("web-unit", plane)
        self.assertIn("0 个工作区", plane)
        # 工作区表平铺上浮后，空表提示随表进了可见面。
        self.assertIn("这个协作主题还没有本机工作区。", body)
        self.assertIn("这个协作主题还没有本机工作区。", plane)

    def test_the_first_manual_injection_is_announced_where_it_is_needed(self) -> None:
        """两条护栏写在注入表单旁边，不是写在文档里。

        注入唯一入口收口到主题页；
        再改写（注入源 ≡ 服役资产）：hero 行末「重新投放」= 唯一手工动作，
        「注入到这里」select 卡退役；两条护栏句仍留在资产页可见面。
        """
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        asset = build_asset(self.root, "护栏验收", [("src", "projects/s.jsonl")])
        from lab.inject import inject_theme

        inject_theme(load_unit(self.root)["unit_id"], asset["asset_id"])
        body = self.client.get("/assets").get_data(as_text=True)
        # 资产单例化改写，首句改新话，两条护栏原句不动。
        self.assertIn("注入跟随机器设置自动走", body)
        self.assertIn("唯一手工动作=重新投放", body)
        self.assertIn("没变不重铸", body)
        self.assertIn("绝不顶掉", body)
        units_body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("重新投放", units_body)
        self.assertNotIn("注入到这里", units_body)

    # --- K12 重建按钮的进行中反馈 ------------------------------------------------------

    # 两条都先 self._fake_vault(...)：tests/statebox.py 不重定向 GROMA_VAULT_DIR，
    # 而 lab/vault.py 的 default_root() 不住在 state_root() 下面——不带假 vault
    # 就直接读写开发者机器上那份 1.8 GB 真 vault（验收侧实测 49.8 秒并真改过）。

    def _unfinished_reindex_marker(self, started_at: str | None = None) -> None:
        import json

        from lab.workspace import utc_stamp
        from web.app import _observe_reindex_path
        _observe_reindex_path().write_text(
            json.dumps({"started_at": started_at or utc_stamp(), "finished_at": None,
                        "outcome": None, "seconds": None}),
            encoding="utf-8", newline="\n")

    def test_reindex_refuses_a_second_run_while_one_is_in_progress(self) -> None:
        """重入守的是"这一轮真的没再跑"，不是"响应带了 error="。

        审查侧第一版只断言 error=，在未修版上是绿的：夹具里假 vault 没有会话，
        旧代码跑一遍也返回 error。所以这里钉三件事——文案、被拒方留下的标记
        仍未收尾、以及**副作用没发生**（index.json 的 generated_at 前后相等）。
        """
        import json
        from urllib.parse import unquote

        from web.app import _observe_reindex_path

        vault_dir = self._fake_vault({
            "schema": 1, "kind": "GROMA_VAULT_INDEX",
            "generated_at": "2026-11-10T11:00:00Z",
            "session_count": 0, "shard_count": 0, "bytes_total": 0, "sessions": [],
        })
        index_file = vault_dir / "index.json"
        before = json.loads(index_file.read_text(encoding="utf-8"))["generated_at"]
        self._unfinished_reindex_marker()

        response = self.client.post("/observe/reindex", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        location = unquote(response.headers["Location"])
        self.assertIn("仍在进行", location)
        self.assertIn("别重复点", location)
        # 真机抓到的措辞缺陷：进行中的标记会覆盖上一次完成的回执，此时说「首次」
        # 就是在撒谎——这台机器往往已经跑过很多次。回落语必须中性。
        self.assertNotIn("首次", location)
        self.assertEqual(json.loads(index_file.read_text(encoding="utf-8"))["generated_at"], before)
        marker = json.loads(_observe_reindex_path().read_text(encoding="utf-8"))
        self.assertIsNone(marker["finished_at"])

        # 反面：进程崩了留下永久未收尾的标记，不能把按钮永久锁死。
        # 超过中断窗口（5 分钟）的未收尾标记只当凭据，不当在跑。
        self._unfinished_reindex_marker(started_at="2026-11-10T00:00:00Z")
        stale = self.client.post("/observe/reindex", follow_redirects=False)
        self.assertNotIn("仍在进行", unquote(stale.headers["Location"]))
        self.assertNotEqual(json.loads(index_file.read_text(encoding="utf-8"))["generated_at"], before)

    def test_observe_page_and_reindex_share_one_running_verdict(self) -> None:
        """「在不在跑」只许有一处判断：页面说的必须就是路由做的。

        R11 的现场：`observe.html` 自己按「有 started_at 且没 finished_at」重算了一遍，
        而路由用的是带 300 秒窗的 `_reindex_running()`。造一个 31 分钟前、未收尾的标记，
        实测 `GET /observe` 写「上一次重建仍在进行……再点一次不会并行跑」，而同一时刻
        `POST /observe/reindex` **没有被拒、真跑了一遍**。界面在承诺一条服务端
        已经不再执行的护栏——比缺失更坏，因为它是照着界面做决定的。

        本测试在未修版上红在前两条断言（页面文案），后两条两侧本来就一致。
        """
        from datetime import datetime, timedelta, timezone
        from urllib.parse import unquote

        import json

        stale_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
        vault_dir = self._fake_vault({
            "schema": 1, "kind": "GROMA_VAULT_INDEX",
            "generated_at": "2026-11-10T11:00:00Z",
            "session_count": 0, "shard_count": 0, "bytes_total": 0, "sessions": [],
        })
        index_file = vault_dir / "index.json"

        # ① 中断侧：路由已判定为中断（POST 不拦），页面就必须说中断，不许说「仍在进行」。
        self._unfinished_reindex_marker(started_at=stale_at)
        body = self.client.get("/observe").get_data(as_text=True)
        self.assertNotIn("仍在进行", body)
        self.assertIn("视为中断", body)
        self.assertNotIn("仍在进行", unquote(
            self.client.post("/observe/reindex", follow_redirects=False).headers["Location"]))

        # ② 在跑侧：页面说「仍在进行」时，POST 必须真的被拒（同一条判断，两个出口）。
        # 基准取①那次放行之后的值——那一次真跑了重建，拿最初的 11:00:00 比会假红。
        self._unfinished_reindex_marker()
        self.assertIn("仍在进行", self.client.get("/observe").get_data(as_text=True))
        before_refused = json.loads(index_file.read_text(encoding="utf-8"))["generated_at"]
        refused = unquote(self.client.post("/observe/reindex", follow_redirects=False).headers["Location"])
        self.assertIn("仍在进行", refused)
        self.assertEqual(json.loads(index_file.read_text(encoding="utf-8"))["generated_at"],
                         before_refused, "被拒的那一次不该动 vault 索引")

    def test_reindex_leaves_a_receipt_the_page_shows(self) -> None:
        """跑完要留下带时刻的凭据：开始、结束、用时、成败——零 JS 也能证明它跑过。"""
        import json

        from web.app import _observe_reindex_path

        self._fake_vault({
            "schema": 1, "kind": "GROMA_VAULT_INDEX",
            "generated_at": "2026-11-10T11:00:00Z",
            "session_count": 1, "shard_count": 1, "bytes_total": 10,
            "sessions": [{"harness": "qoder-cn", "session_key": "q:a",
                          "shards": [{"name": "s1", "records": 1}]}],
        })
        response = self.client.post("/observe/reindex", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(_observe_reindex_path().is_file(), "重建没有留下回执")
        marker = json.loads(_observe_reindex_path().read_text(encoding="utf-8"))
        self.assertIsNotNone(marker["started_at"])
        self.assertIsNotNone(marker["finished_at"])
        self.assertIn(marker["outcome"], ("ok", "error"))
        self.assertIsNotNone(marker["seconds"])

        body = self.client.get("/observe").get_data(as_text=True)
        self.assertIn("上次重建", body)
        self.assertIn("别重复点", body)

        # 耗时预告不再背一个常量：有成功回执时，页面引用这台机器自己量出来的数。
        # 旧文档写的「7–20 秒」在 1.8 GB 真 vault 上实测 52.4 秒——照抄就是第二条谎。
        _observe_reindex_path().write_text(json.dumps({
            "started_at": "2026-11-10T12:00:00Z", "finished_at": "2026-11-10T12:00:52Z",
            "outcome": "ok", "seconds": 52.4, "detail": None,
        }), encoding="utf-8", newline="\n")
        self.assertIn("上次重建实测 52.4 秒",
                      self.client.get("/observe").get_data(as_text=True))

    # --- 提示文字瘦身 --------------------------------------------------

    def test_tooltip_is_hidden_by_display_not_by_opacity(self) -> None:
        """ⓘ 浮窗的隐藏态必须是 `display:none`，不许退回 `opacity:0`（真机复量查出来的回归**B 类**）。

        为什么源码级断言而不是渲染断言：`git archive` 树上跑不了布局，而这条钉的恰好是布局事实——
        绝对定位的 `::after` 即使完全透明**仍参与可滚动区域**。曾用 `opacity:0` 藏着它，
        900 宽下概览页 scrollWidth 1050、把 .hint 摘掉是 885（+165 是一条看不见也摸不着的死滚动）。
        改成 display:none 后同一处量到 885；这条守卫的作用是让"顺手改回 opacity"必须过一遍这里。

        回归证伪：把隐藏态改回 `opacity:0` → 必须红。
        """
        css = self.client.get("/").get_data(as_text=True)
        block = re.search(r"\.hint::after \{(.*?)\}", css, re.S)
        self.assertIsNotNone(block, "base.html 里找不到 .hint::after 规则")
        rule = block.group(1)
        self.assertIn("display:none", rule, "浮窗隐藏态不是 display:none——透明不等于不占位（实测到 +165 的死滚动）")
        self.assertNotIn("opacity", rule, "隐藏态里出现 opacity 说明有人把 display:none 换回了透明方案")

        shown = re.search(r"\.hint:hover::after[^{]*\{([^}]*)\}", css)
        self.assertIsNotNone(shown, "找不到 ⓘ 的显示规则")
        self.assertIn("display:block", shown.group(1))

    def test_removed_design_notes_are_gone_from_the_rendered_pages(self) -> None:
        """裁定为「删」的设计期原话必须从**响应字节**里消失（A 类）。

        只查响应、不查源码，是因为承载三允许的去处就包括 Jinja `{# #}`（编译期剥掉、不进页面）
        与代码注释；**不合规的去处是 CSS `/* */`**——它会进每一页的响应，上一轮就是这么把一条
        assertNotIn 打红的。降级（`<details>` / ⓘ）的句子仍在响应里，那不算"还留着"，
        本条列的每一句都是"页面上再也读不到"的句子。

        清单每一行都对着 PROPOSAL §4 的编号，出处写在提交信息里。
        """
        # I6 的徽章只在"工作区里还留着旧设置文件"时渲染；不造这个状态，这句就是空钉。
        (self.root / ".groma" / "settings.json").write_text("{}\n", encoding="utf-8", newline="\n")

        # A3 那句住在活动页"有摘要"的分支、AS2 那句住在"有资产块"的分支里，同样要给夹具，
        # 否则查的是一条渲染不到的路径（I6 踩过，之后每一句都先问"这句今天渲染得到吗"）。
        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        build_asset(self.root, "护栏验收", [("src", "projects/s.jsonl")])

        gone = {
            "/": [
                "（0.39 产物，或绕过了登记的写入口）",   # I4：版本考古 → Jinja 注释
                "本区还留着旧的",                        # I6：徽章改短，文件名进 ⓘ
                "——不用猜",                              # I8：设计师口吻
                "协作主题页会写明",                          # I8：跨页指路，协作主题页自己会说
            ],
            "/discover": [
                "0 条会话在这里要能说出是哪一种 0",       # D2：设计目标自述 → 承载是表格「0 条的原因」列
                "属于本协作主题的一个工作区",                 # D5：与切换条重复，且"本主题"在这页是错口径
                "能采的、以及各自认的 Agent",             # D8：抬头只留名字
            ],
            "/activity": [
                "活跃时段覆盖率不再在这里算",             # A3：设计期注释 → 去处是 ARCHITECTURE §4.2.2
            ],
            "/assets": [
                "都在既有设计里，不是新规矩",             # AS2：对我们的设计过程说话，不对用户说话
            ],
            "/units": [
                "只是导出件",                            # 实现细节 → 去处是 ARCHITECTURE §2.1 存储表
                "实测同一批候选",                        # 设计期论据 → Jinja 注释
            ],
            "/evidence": [
                "谁和谁有关系",                          # E1：对着已下线的关系图辩护 → 去处 ARCHITECTURE §4.2.2
                "每行都能点进",                          # E4：出处那一格本来就是链接
            ],
        }
        for path, phrases in gone.items():
            body = self.client.get(path).get_data(as_text=True)
            for phrase in phrases:
                self.assertNotIn(phrase, body, "%s 上这条已裁定删除，却还在响应里：%s" % (path, phrase))

        # O6 那句住在"有产物、但读不到 vault 索引"的分支里，得单独造：
        # 产物写进沙箱 state 根，vault 指到一个**空目录**（绝不许让这条去读真 vault）。
        from web.app import _observe_html_path
        _observe_html_path().write_text('<!DOCTYPE html><html lang="zh"><body>观测</body></html>',
                                        encoding="utf-8", newline="\n")
        empty_vault = Path(self._tmp.name) / "vault-empty"
        empty_vault.mkdir()
        with mock.patch.dict(os.environ, {"GROMA_VAULT_DIR": str(empty_vault)}):
            body = self.client.get("/observe").get_data(as_text=True)
        self.assertNotIn("免得用旧的顶上", body, "/observe 上 O6 那句已裁定删除，却还在响应里")

        # S1 / C1+C2 两句各自只在自己那条路上渲染；先要 200，再查句子——
        # 状态码不查就只能证明"这条页面没这句话"，那是空钉的一种藏法。
        session_page = self.client.get("/session", query_string={"source": "src", "path": "projects/s.jsonl"})
        self.assertEqual(session_page.status_code, 200, "会话详情页没打开，S1 那句就查不到")
        self.assertNotIn("所以详情页也显示不出来", session_page.get_data(as_text=True))

        confirm_page = self.client.get("/sources/remove/confirm",
                                       query_string={"name": "src", "ws": self.root.as_posix()})
        self.assertEqual(confirm_page.status_code, 200, "确认页没打开，C1/C2 就查不到")
        confirm_body = confirm_page.get_data(as_text=True)
        self.assertIn("本页不改任何东西", confirm_body)          # C1 改写后的句子必须在，否则上面那条是空钉
        self.assertNotIn("两步操作的第二步之前", confirm_body)   # C1：模式自述
        self.assertNotIn("确认页本身就是安全门", confirm_body)   # C2：对自己的设计辩护

    def test_operator_notes_that_must_stay_visible_are_not_folded(self) -> None:
        """影响操作的句子必须留在**可见面**——折起来等于删（**B 类不变量钉**）。

        判据是"在响应里，且不在任何 `<details>…</details>` 区间内"。
        单查响应查不出折叠：降级承载就是把文本留在 `details` 里，`assertIn` 照样绿。

        资产页那三句是 项目所有者 拍板 3 点名的：护栏段可以补 `<details>`，
        但「第一次要手动点一次」「没变不重铸」「绝不顶掉」必须在默认看得见的那一面。
        夹具照 `test_the_first_manual_injection_is_announced_where_it_is_needed`——
        护栏住在注入表单旁边，没有资产时那块整个不渲染，不造状态这条就是空钉。

        回归证伪 = 把资产页护栏整段包进 `<details>` → 必须红。
        """
        visible = {
            # 「开着时…花额度…」那句随设置表单搬去了 /settings（在 settings 的可见面判据里钉）。
            "/": ["会涨到 GB 级", "建议改到别的盘"],                            # 首启卡第 1 步那句（红线 6）
            "/settings": ["开着时，一次归类会顺带花额度重铸资产并投放到工作区"],  # I8 保留的警告句（随表单搬家）
            # 主题页的退化警告也进这张表（只增不减）。"未传导（自动重铸已关）"
            # 在默认夹具下不渲染（自动是开的），它由 test_the_chain_status_stays_outside_details 单独验。
            "/units": ["没有说明时自动归类会退化。"],
        }
        for path, phrases in visible.items():
            plane = outside_details_blocks(self.client.get(path).get_data(as_text=True))
            for phrase in phrases:
                self.assertIn(phrase, plane, "%s 上这句被折进 details 了：%s" % (path, phrase))

        add_source(self.root, "src", str(self.src), include=["projects"])
        catchup(self.root, force=True)
        build_asset(self.root, "护栏验收", [("src", "projects/s.jsonl")])
        plane = outside_details_blocks(self.client.get("/assets").get_data(as_text=True))
        for phrase in ("唯一手工动作=重新投放", "没变不重铸", "绝不顶掉"):  # 首句改新话，
            self.assertIn(phrase, plane, "资产页护栏被折进 details 了：%s" % phrase)

    # --- 首启两步卡 --------------------------------------------------------------

    FIRSTRUN_CARD = 'class="card firstrun"'
    # 门禁的判据必须能匹配真实形状：`<fieldset disabled style="…">` 里 `disabled` 后面还有属性，
    # 写成 "<fieldset disabled>" 会**永远匹配不到**——那三条断言于是全变成假绿（W2 第一版就踩在这上面）。
    FIELDSET_LOCKED = r"<fieldset[^>]*\bdisabled\b"

    def test_first_run_card_appears_on_a_fresh_install(self) -> None:
        """新装机：概览上有卡，第 1 步预填当前 vault 根，第 2 步整块禁用（A 类）。

        默认夹具就是首启态——`statebox` 把 GROMA_STATE_DIR/GROMA_VAULT_DIR 指到 temp，
        所以既没有 `first_run.json` 也没有 `vault.json` 的 `set_at`，注册表里只有启动目录自己。
        """
        from lab import vault as vault_mod

        body = self.client.get("/").get_data(as_text=True)
        self.assertIn(self.FIRSTRUN_CARD, body)
        self.assertIn(vault_mod.vault_root().as_posix(), body)  # 预填的是**当前根**，不是示意值
        self.assertIn('action="/firstrun/vault"', body)
        self.assertRegex(body, self.FIELDSET_LOCKED)      # 拍板 ②：设置好才到下一步
        self.assertIn("先定第 1 步", body)

    def test_first_run_card_is_only_on_the_overview(self) -> None:
        """提示式 = 只在概览挂一张：其余四页连卡片容器都不许出现（A 类）。"""
        for page in ("/discover", "/units", "/observe", "/activity"):
            body = self.client.get(page).get_data(as_text=True)
            self.assertNotIn(self.FIRSTRUN_CARD, body, "%s 上多了首启卡" % page)

    def test_confirming_the_default_vault_path_does_not_move_anything(self) -> None:
        """同一个按钮的"相等"走法：只写标记，不碰 vault 配置、不改根（A 类）。"""
        from lab import vault as vault_mod
        from lab.state import first_run

        root = vault_mod.vault_root().as_posix()
        response = self.client.post("/firstrun/vault", data={"path": root}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["Location"].startswith("/?"), response.headers["Location"])
        self.assertEqual(vault_mod.vault_root().as_posix(), root, "确认默认路径却换了根")
        self.assertFalse(vault_mod.config_path().exists(), "非破坏性那一支不该写 vault.json")
        self.assertIsNotNone(first_run()["vault_confirmed_at"])
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn(self.FIRSTRUN_CARD, body)        # 第 2 步没定，整张卡不退
        self.assertNotRegex(body, self.FIELDSET_LOCKED)  # 但门禁开了

    def test_asking_for_a_different_vault_path_goes_through_the_confirm_page(self) -> None:
        """"不等"走法：只做转交（303 到既有确认页）**搬不搬由那一步说了算**（红线 4，A 类）。"""
        from urllib.parse import unquote

        from lab import vault as vault_mod
        from lab.state import first_run

        target = Path(self._tmp.name) / "elsewhere"
        response = self.client.post("/firstrun/vault", data={"path": str(target)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        location = unquote(response.headers["Location"])
        self.assertTrue(location.startswith("/vault/move/confirm?to="), location)
        self.assertIn("elsewhere", location)
        self.assertFalse(vault_mod.config_path().exists(), "第一步不许自己搬数据、也不许写配置")
        self.assertIsNone(first_run()["vault_confirmed_at"], "只是转交，还没人同意过")

    def test_moving_the_vault_marks_the_first_run_confirmed(self) -> None:
        """走完既有两步链 ⇒ `/vault/move` 成功那一支顺带写标记（后半，A 类）。

        `move_vault` 本身由 vault 那轮的测试管；这里钉的是**回执与标记的接线**，
        所以打桩给出 `moved: True` 的回执，判据是"路由有没有照回执落戳"。
        """
        from lab.state import first_run

        receipt = {"moved": True, "old_root": "old", "new_root": "new", "bytes_moved": 0}
        with mock.patch("lab.vault.move_vault", return_value=receipt) as move:
            response = self.client.post("/vault/move", data={"to": "new", "confirm": "1"},
                                        follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(move.call_count, 1)
        self.assertIsNotNone(first_run()["vault_confirmed_at"])

    def test_both_steps_settled_hides_the_card(self) -> None:
        """两步都定完 ⇒ 卡整张消失；**只定第 2 步不算完**（的与关系，A 类）。"""
        from lab import vault as vault_mod
        from lab.state import first_run

        settled = self.client.post("/firstrun/workspace", data={}, follow_redirects=False)
        self.assertEqual(settled.status_code, 303)
        self.assertIsNotNone(first_run()["workspace_settled_at"])
        self.assertIn(self.FIRSTRUN_CARD, self.client.get("/").get_data(as_text=True),
                      "第 1 步还没确认，卡不该退")

        self.client.post("/firstrun/vault", data={"path": vault_mod.vault_root().as_posix()})
        self.assertNotIn(self.FIRSTRUN_CARD, self.client.get("/").get_data(as_text=True))

    def test_an_already_set_vault_root_never_shows_step_one(self) -> None:
        """老用户不被打扰：`set_at` 存在就是"有人显式设过路径"⇒ 第 1 步不再出现（**B 类**）。

        回归证伪 = 把 `vault_ok` 里 `or ... get("set_at")` 那半段删掉
        ⇒ 这台已经设过根的机器重新看见第 1 步 ⇒ 必须红。
        """
        from lab import vault as vault_mod

        vault_mod.save_root(Path(self._tmp.name) / "vault-set")
        self.assertIsNotNone(vault_mod.load_config().get("set_at"))  # 夹具自证：set_at 真写进去了
        body = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('action="/firstrun/vault"', body)   # 第 1 步整块不渲染
        self.assertNotRegex(body, self.FIELDSET_LOCKED)  # 第 2 步的门禁是开的
        self.assertIn(self.FIRSTRUN_CARD, body)              # 但工作区还没落定，卡仍在

    def _settle_first_run(self) -> None:
        """走产品路径把两步都判过（不走 `save_root` 侧门，见回归证伪 A 那一手）。"""
        from lab import vault as vault_mod

        self.client.post("/firstrun/vault", data={"path": vault_mod.vault_root().as_posix()})
        self.client.post("/firstrun/workspace", data={})

    # --- 发现页两级化 -----------------------------
    # --- 发现页 IA：切换器 → 采集面 → 清单 -------------------------------------------

    def test_the_discover_page_has_exactly_four_levels(self) -> None:
        """四段固定顺序：添加工作区 < 当前查看（切换卡）< 采集面 < 我的工作区（A 类，W20 ）。

        前身是 W16 的 `test_the_discover_page_has_exactly_three_levels`；W20 把「切换」从
        采集面卡里抽成独立一级、并把清单置底，所以级数 +1。「添加在最前」这个关系不变。
        """
        body = self.client.get("/discover").get_data(as_text=True)
        for anchor in ("添加工作区", 'class="scope-bar"', "这个区的采集面", "我的工作区"):
            self.assertIn(anchor, body)
        self.assertLess(body.index("添加工作区"), body.index('class="scope-bar"'))
        self.assertLess(body.index('class="scope-bar"'), body.index("这个区的采集面"))
        self.assertLess(body.index("这个区的采集面"), body.index("我的工作区"))

    def test_the_workspace_list_sits_after_the_detail(self) -> None:
        """清单置底，且 5 列/移除在第一格/「0 条的原因」在最后一列都不变（A 类，W20 /6）。"""
        self._second_workspace()
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertLess(body.index("这个区的采集面"), body.index("我的工作区"),
                        "清单又挡在采集面前面了——W20 的重点就是把诊断清单放到最后")
        table = body.split('<th>0 条的原因</th>')[1].split("</table>")[0]
        rows = [row for row in table.split("<tr")[1:] if "<td" in row]
        self.assertEqual(len(rows), 2, "清单行数不对：%d" % len(rows))
        for row in rows:
            cells = row.split("<td")[1:]
            self.assertEqual(len(cells), 5, "清单不是 5 列了")
            self.assertIn("/workspaces/remove?path=", cells[0], "移除入口不在第一格")
            self.assertNotIn("/workspaces/remove?path=", cells[-1], "移除入口跑到最后一列了")

    def test_the_switch_card_sits_between_add_and_detail(self) -> None:
        """切换卡在添加与采集面之间（A 类，W20 ）。

        与四段顺序那条同源，但单独留一条：回归证伪"把切换卡搬回采集面之后"要能精确点名。
        """
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertLess(body.index("添加工作区"), body.index('class="scope-bar"'))
        self.assertLess(body.index('class="scope-bar"'), body.index("这个区的采集面"))

    def test_the_harness_card_is_not_a_separate_card(self) -> None:
        """「本机装着的 Harness」降级为采集面卡里的**信息块**，不再有第二个申报入口（A 类）。

        今天是两张卡、两个按钮做同一件事（root_rows 的「申报这个根」与 candidates 的「加入」）；
        合并后只留 root_rows 那一个入口，candidates 只说"装了什么、这区采了多少"。
        """
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn("这个区的采集面", body)
        self.assertIn("本机装着的 Harness", body)
        self.assertLess(body.index("这个区的采集面"), body.index("本机装着的 Harness"))
        self.assertNotIn(">加入<", body, "candidates 还在渲染第二个申报按钮")

    def test_a_zero_source_workspace_still_offers_a_declare_entry(self) -> None:
        """B1（2026-11-16 验收 Blocker，A 类）：零源工作区也要有申报入口。

        `harness_roots_for` 只遍历**已申报源** ⇒ 零源时采集根表为空；W16 删掉 candidates 的
        「加入」按钮后，那页从"两个入口做同一件事"变成"零个入口"——页面写着"没申报"，
        却把用户指到一张空表。修法（验收给的 A）：把本机装着但没申报的家族补成采集根行，
        B 表仍是每个家族唯一的申报口。打桩 `discover` 是为了不依赖这台机器装了什么。
        """
        import re

        other = self._second_workspace()
        stub = [{"name": "codex", "harness": "codex", "root": "C:/stub/codex", "include": "sessions",
                 "exists": True, "declared": False}]
        with mock.patch("web.app.discover", return_value=stub):
            body = self.client.get("/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True)
        forms = re.findall(r'<form[^>]*action="/discover/add"[^>]*>.*?</form>', body, re.S)
        self.assertGreaterEqual(len(forms), 1, "零源区一个申报入口都没有（B1 复发）")
        for form in forms:
            self.assertIn('name="ws" value="%s"' % other.as_posix(), form)
        self.assertIn("申报这个根", body)

    def test_the_discover_page_says_this_scope_not_this_theme(self) -> None:
        """计数口径写「本区 X / 共 Y」不写「本主题」（A 类，/ audit-1）。

        这一页按工作区不按主题（页面自己那句「作用域只在这一页生效」就这么写），
        「本主题」是唯一自相矛盾的一处。打桩 candidates 是为了不依赖这台机器装了什么。
        """
        stub = [{"name": "codex", "harness": "codex", "root": "C:/stub/codex",
                 "include": "sessions", "exists": True, "declared": False,
                 "inside": 2, "total": 5, "outside": 3, "no_cwd": 0,
                 "agents": [], "breakdown": "cwd-preview"}]
        with mock.patch("web.app.discover", return_value=stub):
            body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn("本区 <strong>2</strong> / 共 5", body)
        self.assertNotIn("本协作主题", body)

    def test_the_workspace_name_is_a_visible_link(self) -> None:
        """工作区名要有肉眼可见的可点提示（A 类）：下划线 + 「查看 ›」后缀。

        audit-4 的抱怨是"选中的唯一入口是列表里一个裸链接"——名字看起来像纯文本。
        """
        import re

        body = self.client.get("/discover").get_data(as_text=True)
        table = body.split('<th>0 条的原因</th>')[1].split("</table>")[0]
        rows = [row for row in table.split("<tr")[1:] if self.root.as_posix() in row]
        self.assertEqual(len(rows), 1, "工作区行数不对，夹具没布好：%d" % len(rows))
        row = rows[0]
        self.assertIn("查看 ›", row)
        self.assertRegex(row, r'<a[^>]*text-decoration:\s*underline')

    # --- 首启态：并入表单置顶 + boot root 标注 --------------------------------------

    # --- 发现页【添加工作区】（两轴拆开：登记 ≠ 挂主题） ------------------------------

    def test_the_add_workspace_card_comes_before_the_workspace_list(self) -> None:
        """【添加工作区】的偏移必须小于工作区列表（A 类；§2.4 #1 的**换对象不换强度**）。

        前身是 `test_in_first_run_state_the_join_form_comes_before_step_one`（钉"并进一个主题"在首启态置顶），
        再前身是 `test_the_add_workspace_card_comes_before_step_one`；W16 把「第 ① 步」改名「我的工作区」，
        **关系不动**：添加在列表之前。
        """
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn("添加工作区", body)
        self.assertLess(body.index("添加工作区"), body.index("我的工作区"))

    def test_the_add_card_is_rendered_exactly_once(self) -> None:
        """两种状态都只渲染**一处**（§2.4 #3 的改写：字面从「并进一个主题」换成「添加工作区」，仍按实例数）。"""
        unsettled = self.client.get("/discover").get_data(as_text=True)
        self.assertEqual(unsettled.count("添加工作区"), 1, "首启态下出现两处添加口")

        self._settle_first_run()
        settled = self.client.get("/discover").get_data(as_text=True)
        self.assertEqual(settled.count("添加工作区"), 1, "常态下出现两处添加口")

    def test_discover_offers_adding_a_workspace_without_a_theme(self) -> None:
        """POST /workspaces/add 只登记：注册表多一条、**该条没有主题**、主题总数不变（A 类）。"""
        from lab import unitmatch

        fresh = Path(self._tmp.name) / "themeless"
        fresh.mkdir()
        before = len(unitmatch.unit_list())
        response = self.client.post("/workspaces/add", data={"path": str(fresh)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(fresh)]
        self.assertEqual(len(rows), 1, "添加后注册表里不是恰好一条")
        self.assertFalse(rows[0].get("unit_id"), "只登记的动作却挂上了主题：%r" % rows[0].get("unit_id"))
        self.assertEqual(len(unitmatch.unit_list()), before, "添加工作区不该顺手造出主题")

    def test_a_themeless_workspace_survives_a_restart(self) -> None:
        """重新 `create_app`（内含 `sync_units`）之后：仍在注册表、仍无主题（A 类，钉 F1 的反面）。

        **删除/登记类动作必须验一次重启**，只验"做完当下查得到"不够——
        `create_app` 每次启动都会跑 `ensure_unit(boot_root)` + `register_workspace` + `sync_units()`，
        而 `sync_units()` 的取值是 `link.get("unit_id") or record.get("unit_id")`。
        """
        from lab import unitmatch
        from web.app import create_app

        fresh = Path(self._tmp.name) / "restart-case"
        fresh.mkdir()
        self.client.post("/workspaces/add", data={"path": str(fresh)})
        before = len(unitmatch.unit_list())

        again = create_app(self.root).test_client()
        again.get("/discover")
        rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(fresh)]
        self.assertEqual(len(rows), 1, "重启把刚添加的工作区弄丢了")
        self.assertFalse(rows[0].get("unit_id"), "重启给它凭空配了一个主题")
        self.assertEqual(len(unitmatch.unit_list()), before, "重启顺手造了主题")

    def test_discover_no_longer_offers_joining_a_theme(self) -> None:
        """发现页不再有挂主题的口子（A 类，钉的"整块撤走"）：路由字面与标题各 0 命中。"""
        body = self.client.get("/discover").get_data(as_text=True)
        self.assertNotIn("并进一个协作主题", body)  # W21b 改名后，防的是新词写回来
        self.assertNotIn("/units/join", body)
        self.assertNotIn("可并入这个协作主题的工作区", body)

    def test_adding_an_existing_workspace_is_idempotent(self) -> None:
        """连 POST 两次只留一条（**B 类**）。

        **回归证伪**：去掉路由里的幂等分支（改调 `register_workspace` 之外再无条件 append）⇒ 必红。
        内核 `state.register_workspace` 本身按 `ref` 幂等，所以这一手钉的是**路由 + 内核合起来**的形状。
        """
        fresh = Path(self._tmp.name) / "twice"
        fresh.mkdir()
        for _ in range(2):
            self.client.post("/workspaces/add", data={"path": str(fresh)})
        rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(fresh)]
        self.assertEqual(len(rows), 1, "同一个目录被登记了 %d 次" % len(rows))
        self.assertIn("已经登记过了", self.client.post("/workspaces/add", data={"path": str(fresh)},
                                                     follow_redirects=True).get_data(as_text=True))

    def test_every_registered_workspace_row_offers_removal(self) -> None:
        """第①步**每一行**都要有移除入口，按行数数（A 类；入口以前只在主题页）。

        钉的是的后果面：只登记未挂主题的工作区在概览/主题页都隐形，
        所以发现页必须能把它移除——否则它会变成"加得进来、去不掉"的死账。
        """
        other = self._second_workspace()
        body = self.client.get("/discover").get_data(as_text=True)
        table = body.split('<th>0 条的原因</th>')[1].split("</table>")[0]
        rows = table.split("<tr")[1:]
        mine = [row for row in rows if self.root.as_posix() in row]
        theirs = [row for row in rows if other.as_posix() in row]
        self.assertEqual((len(mine), len(theirs)), (1, 1), "第①步里这两个区各该恰好一行")
        self.assertEqual(table.count("/workspaces/remove?path="), 2, "移除入口不是按行数出来的")
        for row in mine + theirs:
            self.assertIn("/workspaces/remove?path=", row)

    def test_the_boot_root_is_labelled_as_such(self) -> None:
        """Groma 自己的启动目录要标注，且**只**标它：新装用户看到的第一个"工作区"是 Groma 的家（A 类）。

        **回归证伪**：给所有行都加 badge ⇒ 第①步表格里那句会出现两次 ⇒ 本条红；
        一枚都不加 ⇒ `assertIn` 红。两个方向都有判据，不是只查"有"。
        """
        other = self._second_workspace()
        body = self.client.get("/discover", query_string={"ws": other.as_posix()}).get_data(as_text=True)

        self.assertIn('<th>0 条的原因</th>', body)      # 先锚住表格，红才不会长成 IndexError
        table = body.split('<th>0 条的原因</th>')[1].split("</table>")[0]
        # 切点落在表头行**内部**，所以从这里数的第一个 `<tr` 就是第一条数据行（不是表头）。
        # 前一版按 `[2:]` 取，把表格首行丢了 ⇒ 判据对，取法错（10:17:01 那一手就是它报的）。
        rows = table.split("<tr")[1:]
        boot_rows = [row for row in rows if self.root.as_posix() in row]
        other_rows = [row for row in rows if other.as_posix() in row]
        # 逐行取法先自证（各恰好一行），否则"没标注"会红成 StopIteration 而不是一句人话
        self.assertEqual(len(boot_rows), 1, "第①步表格里启动目录的行数不对：%d" % len(boot_rows))
        self.assertEqual(len(other_rows), 1, "第①步表格里第二工作区的行数不对：%d" % len(other_rows))
        self.assertIn("Groma 启动目录", boot_rows[0])
        self.assertNotIn("Groma 启动目录", other_rows[0])

        # 切换条的 badge 换成 option 文本后缀；仍要"只标它"。
        import re

        select = body.split('<select name="ws"')[1].split("</select>")[0]
        labels = {value: text for value, text in
                  re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select)}
        self.assertEqual(labels[self.root.as_posix()], self.root.name + "（Groma 启动目录）")
        self.assertEqual(labels[other.as_posix()], other.name)
        self.assertEqual(sum("Groma 启动目录" in label for label in labels.values()), 1,
                         "option 里要么没标、要么多标了")


    def test_a_second_registered_workspace_settles_step_two(self) -> None:
        """注册表里有一条 ≠ 启动目录的记录 ⇒ 第 2 步判过 ⇒ 整张卡退（**B 类**）。

        回归证伪 = 把 `workspace_ok` 里 `any(... != boot_root)` 那半段删掉 ⇒ 必红。
        """
        from lab import vault as vault_mod

        # 第 1 步用**产品自己的路径**判过（不是 save_root 那条侧门）：
        # 否则回归证伪 A（删掉 `set_at` 那半段）会连这条一起打红，两条的归因就分不开。
        self.client.post("/firstrun/vault", data={"path": vault_mod.vault_root().as_posix()})
        self.assertIn(self.FIRSTRUN_CARD, self.client.get("/").get_data(as_text=True))
        self._second_workspace()
        self.assertNotIn(self.FIRSTRUN_CARD, self.client.get("/").get_data(as_text=True))


    # --- 主题页【加入这个主题】（挂主题的口子从发现页搬进来） --------------------

    def _theme_cards(self, body: str) -> dict[str, str]:
        """主题页每个主题一张 card，按**block 头那行**的短码把每张 card 单独切出来。

        切点用 `<div class="muted"><code>unit_xxx</code>`（card 头的短码）**不是**裸 `<code>`：
        W9 之后别的主题的 others 表里也会渲染同一个短码（在 `<details>` 内），
        按裸短码 `find` 会切进上一张 card。逐实例切是这条刀的核心判据（R15 的反面）。
        （Q3）把工程键收进 ⓘ 之后，抬头不再有短码行——切点跟着搬到 ⓘ 的
        data-tip（"工程键 unit_xxx"），逐实例切法不变，短码仍在响应体里。
        """
        starts = [match.start() for match in re.finditer(r'<div class="card">', body)]
        cards = {}
        for unit_id in [item["unit_id"] for item in unitmatch.unit_list()]:
            at = body.find('data-tip="工程键 %s' % unit_id)
            self.assertNotEqual(at, -1, "主题页没有这个主题的 card：%s" % unit_id)
            begin = max(start for start in starts if start < at)
            after = [start for start in starts if start > at]
            cards[unit_id] = body[begin:after[0]] if after else body[begin:]
        return cards

    @staticmethod
    def _join_forms(card: str) -> list[dict]:
        """这张 card 里每一个指向 /units/join 的 form，取它 hidden 的 (unit, path)。

        逐个 form 查，不是整页查一次：整页查会让"两个主题的按钮串了线"（unit 是别人的）照样绿。
        """
        forms = []
        for match in re.finditer(r'<form[^>]*action="[^"]*/units/join"[^>]*>(.*?)</form>', card, re.S):
            inner = match.group(1)
            hidden = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', inner))
            forms.append(hidden)
        return forms

    def test_the_theme_page_offers_joining_each_registered_outsider(self) -> None:
        """每个 block 只列**自己**的 outsider，按钮数按 block 数。

        这接管两笔旧账：
        · K8（主题下拉要标默认项）——表单里已经没有 `<select>`，判据换成逐实例的 (unit, path) 配对；
        · 主题名进主视图、短码只进 `<details>`——被摘掉的那三行强度在这里接回来。
        """
        out1 = Path(self._tmp.name) / "out1"
        out2 = Path(self._tmp.name) / "out2"
        for path in (out1, out2):
            path.mkdir()
            self.client.post("/workspaces/add", data={"path": str(path), "from": "discover_page"})
        boot_unit = load_unit(self.root)["unit_id"]
        # 主题二的家 = out2；再把 out1 并进启动主题 ⇒ 两个 block 的 outsider 数**不一样**（1 对 2），
        # 整页 counts 或串线的 unit 都会在这里红，只有"各数各的"才绿。
        self.client.post("/unit/create", data={"name": "主题二", "home": str(out2)}, follow_redirects=True)
        second = next(item["unit_id"] for item in unitmatch.unit_list()
                      if item.get("name") == "主题二" and item["unit_id"] != boot_unit)
        self.client.post("/units/join", data={"unit": boot_unit, "path": str(out1), "from": "units_page"},
                         follow_redirects=True)

        cards = self._theme_cards(self.client.get("/units").get_data(as_text=True))
        first_forms = self._join_forms(cards[boot_unit])
        second_forms = self._join_forms(cards[second])
        self.assertEqual((len(first_forms), len(second_forms)), (1, 2),
                         "主题页的【加入这个主题】不是按每个主题自己的 outsider 数出来的")
        for forms, unit_id in ((first_forms, boot_unit), (second_forms, second)):
            for form in forms:
                self.assertEqual(form.get("unit"), unit_id, "有 form 的 hidden unit 不是本 block 的主题")
                self.assertEqual(form.get("from"), "units_page")
        self.assertEqual([f["path"] for f in first_forms], [out2.as_posix()])
        self.assertEqual({f["path"] for f in second_forms},
                         {self.root.as_posix(), out1.as_posix()})

        others = cards[second].split("可并入这个协作主题的工作区")[1].split("</table>")[0]
        self.assertIn("web-unit", others)                       # 主题名进主视图（K10(b)）
        self.assertIn("<details>", others)                     # 短码只在详情里
        self.assertNotIn('<td class="muted">unit_', others)    # 旧形状：主视图直接铺 id
        self.assertNotIn("<select", others)                    # block 就是主题，不要下拉

    def test_joining_from_the_theme_page_does_not_inject(self) -> None:
        """从主题页加入只记关系**不自动投放资产**。

        两件事都要钉：只断"没注入"会因为"根本没加入"而假绿，所以同时验关系确实记上了。
        """
        from lab import surface

        outsider = Path(self._tmp.name) / "no-inject"
        outsider.mkdir()
        self.client.post("/workspaces/add", data={"path": str(outsider), "from": "discover_page"})
        boot_unit = load_unit(self.root)["unit_id"]
        self.client.post("/units/join", data={"unit": boot_unit, "path": str(outsider), "from": "units_page"})

        self.assertIn(outsider.as_posix(), unitmatch.workspaces_for_unit(boot_unit),
                      "关系没记上——那「没注入」是假绿")
        self.assertEqual(surface.read_manifest(outsider)["surfaces"], [],
                         "加入主题顺手把资产投到了装载面（说不）")

    def test_a_theme_without_outsiders_renders_no_join_block(self) -> None:
        """本机只有一个区且它就是主题的家 ⇒ 整块不渲染。

        **回归证伪**：去掉模板里 `{% if block.others %}` 的门 ⇒ 空表头也出来 ⇒ 本条红。
        """
        body = self.client.get("/units").get_data(as_text=True)
        self.assertEqual(body.count("可并入这个协作主题的工作区"), 0, "没有 outsider 也渲染了整块")
        self.assertEqual(body.count("加入这个协作主题"), 0)

    def test_the_theme_page_points_at_the_discover_page_without_a_scope(self) -> None:
        """指路句要说清"登记在发现页、挂主题在本页"，且链接是裸 /discover（红线 3**B 类**）。

        改掉的是"并入工作区在发现页"那句——发现页不做这件事了，
        留着就是一句会说谎的导航。回归证伪 = 链接带上 `?ws=` ⇒ 本条红。
        """
        body = self.client.get("/units").get_data(as_text=True)
        self.assertNotIn("并入工作区在", body)
        self.assertIn("建好后在它的详情里可以把别的工作区并进来", body)
        self.assertNotIn("discover?ws=", body)


    # --- 移除工作区＝一次删干净 Groma 的关联 -------------------------

    def _inject_a_block(self, root: Path, unit_id: str | None = None) -> str:
        """给这个区写一个 Groma 标记块 + 载荷 + manifest（F6 三件套的真实形状）。

        不走 `inject_unit()`：那要先把资产建出来，与这条判据无关。
        这里写的就是 `uninject()` 要删的那三样东西——块、载荷、manifest 条目。
        修正 fixture：真实链路（inject_theme）写行盖的是**主题** unit_id，
        成员区自带卡时卡的 id 不是主题 id——所以允许显式指定盖哪个 id。
        """
        from lab import surface

        unit_id = unit_id or load_unit(root)["unit_id"]
        surface.write_payload(root, "Groma 注入的载荷：本主题的会话摘要")
        record = surface.apply_block(root, "AGENTS.md", unit_id, "asset-test", "后台摘要正文")
        surface.write_manifest(root, {"schema": 1, "surfaces": [{
            "face": "codex-agents", "path": record["path"], "unit_id": unit_id,
            "asset_id": "asset-test", "block_sha256": record["block_sha256"],
            "written_at": "2026-11-15T00:00:00Z"}]})
        return "AGENTS.md"

    def test_removing_a_workspace_takes_the_injected_blocks_out(self) -> None:
        """A 类（F6）：关系一断，载荷就走——块面、载荷、manifest 三件都要没。

        今天 `remove_workspace()` 只动注册表/成员/主题**一个注入痕迹都不碰**，
        所以这条在未修版上必红。
        """
        from lab import surface

        other = self._second_workspace()
        self.client.post("/workspaces/add", data={"path": str(other)})
        injected = self._inject_a_block(other)
        self.assertTrue((other / injected).is_file())

        response = self.client.post("/workspaces/remove", data={"path": str(other), "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        # `remove_block()` 会把"这个文件当初是我们建的、原内容是空"的那种文件整个删掉，
        # 所以判据是"块内容不在了"，不是"文件还在"——文件留着也不许留 Groma 的块。
        marker_file = other / injected
        if marker_file.exists():
            self.assertNotIn("groma:inject", marker_file.read_text(encoding="utf-8"),
                             "注入块还在装载面上")
        self.assertEqual(surface.read_manifest(other)["surfaces"], [], "manifest 还记着这个区")
        self.assertFalse(surface.payload_path(other).is_file(), "载荷文件还在")

    def test_removing_a_workspace_deletes_its_identity_card(self) -> None:
        """A 类（F2）：身份卡在工作区**根目录**，不在 `.groma/` —— 不删它，重新添加会被带回来。"""
        other = self._second_workspace()
        card = other / "groma-unit.json"
        self.assertTrue(card.is_file(), "前置：这个区该带卡")
        self.client.post("/workspaces/add", data={"path": str(other)})
        self.client.post("/workspaces/remove", data={"path": str(other), "confirm": "1"})
        self.assertFalse(card.is_file(), "身份卡还在——重新添加就会复活旧主题")
        self.assertTrue(other.is_dir(), "把工作区目录本身删了（只该删 Groma 的东西）")

    def test_re_adding_a_removed_workspace_does_not_restore_the_old_theme(self) -> None:
        """A 类（F1+F2 合流）：移除 → 重新添加 → **重启** ⇒ 旧主题不回来。

        必须重新 `create_app`：`sync_units()` 按 `link.get("unit_id") or record.get("unit_id")`
        重建主题，只看"当下查不到"会漏掉复活路径。
        """
        from lab import unitmatch
        from web.app import create_app

        other = self._second_workspace()
        old_unit = load_unit(other)["unit_id"]
        self.client.post("/workspaces/add", data={"path": str(other)})
        self.client.post("/workspaces/remove", data={"path": str(other), "confirm": "1"})
        self.client.post("/workspaces/add", data={"path": str(other)})

        again = create_app(self.root).test_client()
        again.get("/discover")
        self.assertNotIn(old_unit, [item["unit_id"] for item in unitmatch.unit_list()],
                         "旧主题被复活路径带回来了")
        rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(other)]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].get("unit_id"), "重新添加后注册表行还指着旧主题")

    def test_removing_a_workspace_retires_its_orphan_theme_and_says_so(self) -> None:
        """A 类：只剩它一个工作区的主题会退役**notice 要说出主题名**，`retired-units.jsonl` 多一行。"""
        from lab import state as state_mod

        gone = Path(self._tmp.name) / "theme-home"
        gone.mkdir()
        self.client.post("/unit/create", data={"name": "要被退役的主题", "home": str(gone)},
                         follow_redirects=True)
        before = 0
        retired = state_mod.state_root() / "retired-units.jsonl"
        if retired.is_file():
            before = len(retired.read_text(encoding="utf-8").splitlines())

        response = self.client.post("/workspaces/remove", data={"path": str(gone), "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=", response.headers["Location"])
        from urllib.parse import unquote

        self.assertIn("要被退役的主题", unquote(response.headers["Location"]),
                      "notice 没说退役了哪个主题——用户看不见这一步")
        after = len(retired.read_text(encoding="utf-8").splitlines()) if retired.is_file() else 0
        self.assertEqual(after, before + 1, "retired-units.jsonl 没记这一笔")

    def test_the_boot_root_cannot_be_removed(self) -> None:
        """A 类（①）：Groma 自己的启动目录不能被这一页移除（它是 Groma 的家，不是普通工作区）。"""
        self._second_workspace()  # 让"最后一个工作区"那道护栏不抢答
        card = self.root / "groma-unit.json"
        response = self.client.post("/workspaces/remove", data={"path": str(self.root), "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertTrue((self.root / ".groma" / "unit.json").is_file())
        self.assertTrue(card.is_file())

    def test_the_confirm_page_lists_every_wb_owned_thing_it_will_delete(self) -> None:
        """A 类：确认页要把**会删的每一件**列出来（块面路径 / 身份卡 / 退役主题），并保留两个字面。

        「不可恢复」与 `scans` 是红线 5 的字面；块面路径来自 `surface.read_manifest()` 现读，
        退役主题来自与内核同一套判据——**确认页不许自己另算一套**（两套算法迟早对不上）。

        **这一条的假绿正是改名暴露出来的**：旧断言 `assertIn("b-unit", body)` 一直
        被文件名 `wb-unit.json` 里的子串满足，主题名那一格其实**从来没被渲染过**。所以现在
        ① 登记 b 之后重开一次 app——`sync_units()` 只在启动跑（`web/app.py:290`），不重开就不会把
        b 的卡折进 `units.json`，退役预览永远是空态（本文件 `:1886`/`:2160` 同一手法）；
        ② 断言对象从"整页裸串"换成**「会被退役的协作主题」那一格**，并钉成 `<strong>…</strong>`
        这种只有列表分支才有的形状 ⇒ 路径文本冒充不了它。

        退役复活语义，原断言钉卡名 `<strong>b-unit</strong>`——
        起 sync 折行不再抄卡上的名字（卡只证身份），折出的行用注册表行名 `b`，
        所以这里钉 `<strong>b</strong>`。
        """
        other = self._second_workspace()
        self.client.post("/workspaces/add", data={"path": str(other)})
        client = create_app(self.root).test_client()
        self._inject_a_block(other)
        body = client.get("/workspaces/remove", query_string={"path": str(other)}).get_data(as_text=True)
        self.assertIn("AGENTS.md", body, "没列注入块所在的文件")
        self.assertIn("groma-unit.json", body, "没列身份卡（它可能被版本库跟踪）")
        orphan_cell = body.split("会被退役的协作主题", 1)[1].split("</tr>", 1)[0]
        self.assertIn("<strong>b</strong>", orphan_cell,
                      "主题名那一格没列出 b（拿整页裸串冒充是假绿，见 docstring；折行不抄卡名）")
        self.assertNotIn("没有——这些协作主题还有别的工作区", orphan_cell,
                         "退役那一格走了空态，说明预览根本没认出这个主题")
        self.assertNotIn("groma-unit.json", orphan_cell,
                         "身份卡路径串进了主题格——这一格只许列主题名")
        self.assertIn("不可恢复", body)
        self.assertIn("scans", body)
        self.assertIn('class="modal-veil"', body, "弹层壳没了")
        self.assertIn("<dialog open", body, "弹层不是 <dialog open>")

    def test_remove_needs_the_confirm_flag(self) -> None:
        """**A 类**（红线 2）：不带 `confirm=1` 的 POST 不动任何数据。

        **实测当时没有这道门**——
        不带 confirm 的 POST 照样把注册表和卡都删了 ⇒ 它是行为变更，按 A 类记（未修版必红）。
        **回归证伪**：去掉路由开头那道 gate ⇒ 本条红。
        """
        other = self._second_workspace()
        self.client.post("/workspaces/add", data={"path": str(other)})
        card = other / "groma-unit.json"
        response = self.client.post("/workspaces/remove", data={"path": str(other)})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertTrue(card.is_file(), "没带 confirm 也把卡删了")
        rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(other)]
        self.assertEqual(len(rows), 1, "没带 confirm 也把注册表行删了")


    # --- 移出主题 ⇒ 不再属于任何主题时摘注入 ---------------------------------------

    def _leaver(self, name: str = "leaver") -> tuple[Path, str]:
        """一个已并入启动主题、并且带注入痕迹的工作区。"""
        other = Path(self._tmp.name) / name
        other.mkdir()
        init_workspace(other, name=name + "-unit", owner="tester")
        unit_id = load_unit(self.root)["unit_id"]
        self.client.post("/units/join", data={"unit": unit_id, "path": str(other)})
        self._inject_a_block(other)
        return other, unit_id

    def test_leaving_the_last_theme_takes_the_payload_out(self) -> None:
        """A 类（的另一半）：最后一个主题也移出了 ⇒ 载荷没有来源，必须摘掉。

        今天的 `/units/leave` 只删一行成员记录**一个注入痕迹都不碰** ⇒ 未修版必红。
        """
        from lab import surface

        other, unit_id = self._leaver()
        response = self.client.post("/units/leave", data={"unit": unit_id, "path": str(other)})
        self.assertEqual(response.status_code, 303)
        from urllib.parse import unquote

        self.assertIn("不再属于任何协作主题", unquote(response.headers["Location"]))
        self.assertEqual(surface.read_manifest(other)["surfaces"], [], "manifest 还记着这个区")
        self.assertFalse(surface.payload_path(other).is_file(), "载荷文件还在")

    def test_leaving_one_of_two_themes_keeps_the_payload(self) -> None:
        """A 类（的"不误伤"半边）：还在另一个主题里 ⇒ **不许摘**，notice 要说清。

        回归证伪 = 把摘注入提到"还剩几个"判断之外无条件调 ⇒ 本条红。
        """
        from lab import surface

        other, unit_id = self._leaver()
        second_home = Path(self._tmp.name) / "theme-b-home"
        second_home.mkdir()
        self.client.post("/unit/create", data={"name": "第二个主题", "home": str(second_home)},
                         follow_redirects=True)
        second = next(item["unit_id"] for item in unitmatch.unit_list()
                      if item.get("name") == "第二个主题" and item["unit_id"] != unit_id)
        self.client.post("/units/join", data={"unit": second, "path": str(other)})

        response = self.client.post("/units/leave", data={"unit": unit_id, "path": str(other)})
        self.assertEqual(response.status_code, 303)
        from urllib.parse import unquote

        notice = unquote(response.headers["Location"])
        self.assertIn("还在", notice)
        self.assertIn("第二个主题", notice)
        self.assertTrue(surface.read_manifest(other)["surfaces"], "还在另一个主题里，块却被摘了")
        self.assertTrue(surface.payload_path(other).is_file())

    def test_leaving_without_membership_still_changes_nothing(self) -> None:
        """**B 类**：本就未并入 ⇒ 只回一句"本就未并入"**注入痕迹一动不动**。

        回归证伪：把 `uninject` 提到 `removed` 判断之外无条件调 ⇒ 本条红。
        """
        from lab import surface

        loose = Path(self._tmp.name) / "loose"
        loose.mkdir()
        init_workspace(loose, name="loose-unit", owner="tester")
        self._inject_a_block(loose)
        response = self.client.post("/units/leave",
                                    data={"unit": load_unit(self.root)["unit_id"], "path": str(loose)})
        self.assertEqual(response.status_code, 303)
        from urllib.parse import unquote

        self.assertIn("本就未并入", unquote(response.headers["Location"]))
        self.assertTrue(surface.read_manifest(loose)["surfaces"], "没并入也把块摘了")
        self.assertTrue(surface.payload_path(loose).is_file())


    # --- 删除主题（保留工作区与项目文件；删完重启不许复活） --------------------------

    def _theme_with_home(self, name: str = "第二主题", folder: str = "theme-home") -> tuple[Path, str]:
        home = Path(self._tmp.name) / folder
        home.mkdir()
        self.client.post("/unit/create", data={"name": name, "home": str(home)}, follow_redirects=True)
        unit_id = next(item["unit_id"] for item in unitmatch.unit_list() if item.get("name") == name)
        return home, unit_id

    def test_deleting_a_theme_keeps_its_workspaces_and_files(self) -> None:
        """A 类：只忘主题——注册表行、`.groma/`、项目文件全留下；`units.json` 里没了。"""
        from lab import state as state_mod

        home, unit_id = self._theme_with_home()
        (home / "README.md").write_text("项目自己的文件\n", encoding="utf-8")
        marker = home / ".groma" / "unit.json"

        # 先不给 confirm：必须一个字都不动（红线 2）
        bare = self.client.post("/units/delete", data={"unit": unit_id})
        self.assertIn("error=", bare.headers["Location"])
        self.assertIn(unit_id, [item["unit_id"] for item in state_mod.list_units()])

        response = self.client.post("/units/delete", data={"unit": unit_id, "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("error=", response.headers["Location"])
        rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(home)]
        self.assertEqual(len(rows), 1, "注册表行被删了（该留）")
        self.assertTrue(marker.is_file(), ".groma/ 被删了（该留）")
        self.assertTrue((home / "README.md").is_file(), "项目文件被删了")
        self.assertNotIn(unit_id, [item["unit_id"] for item in state_mod.list_units()])

    def test_deleting_a_theme_takes_the_payload_out_of_every_workspace(self) -> None:
        """A 类（F6 在主题轴上的那一半）：主题下每个工作区的块都要摘干净。"""
        from lab import surface

        home, unit_id = self._theme_with_home()
        second = Path(self._tmp.name) / "theme-member"
        second.mkdir()
        init_workspace(second, name="member-unit", owner="tester")
        self.client.post("/units/join", data={"unit": unit_id, "path": str(second)})
        self._inject_a_block(home, unit_id)
        self._inject_a_block(second, unit_id)

        self.client.post("/units/delete", data={"unit": unit_id, "confirm": "1"})
        for path in (home, second):
            self.assertEqual(surface.read_manifest(path)["surfaces"], [], "%s 的 manifest 还记着" % path.name)
            self.assertFalse(surface.payload_path(path).is_file(), "%s 的载荷还在" % path.name)

    def test_a_deleted_theme_does_not_come_back_after_a_restart(self) -> None:
        """A 类（钉 F1，本次核心）：删完**重新 `create_app`** ⇒ 旧主题不回来。

        两件事都要钉：**卡删了** 且 **注册表行不带 unit_id 了**——
        `sync_units()` 的取值正是 `link.get("unit_id") or record.get("unit_id")`，只断一件会留复活路径。
        """
        from web.app import create_app

        home, unit_id = self._theme_with_home()
        self.assertTrue((home / "groma-unit.json").is_file(), "前置：这个区该带卡")
        self.client.post("/units/delete", data={"unit": unit_id, "confirm": "1"})

        again = create_app(self.root).test_client()
        again.get("/units")
        self.assertNotIn(unit_id, [item["unit_id"] for item in unitmatch.unit_list()], "旧主题被复活路径带回来了")
        self.assertNotIn("第二主题", [item.get("name") for item in unitmatch.unit_list()], "同名主题被重建了")
        self.assertFalse((home / "groma-unit.json").is_file(), "卡还在——重启就会照它重建主题")
        rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(home)]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].get("unit_id"), "注册表行还指着已删的主题")

    def test_registry_rows_of_a_deleted_theme_carry_no_unit_id(self) -> None:
        """A 类：主题下每个工作区的注册表行都清掉 `unit_id`/`org_path`（两行都查，不只查一行）。"""
        home, unit_id = self._theme_with_home()
        second = Path(self._tmp.name) / "theme-member-2"
        second.mkdir()
        init_workspace(second, name="member2-unit", owner="tester")
        self.client.post("/units/join", data={"unit": unit_id, "path": str(second)})
        self.client.post("/units/delete", data={"unit": unit_id, "confirm": "1"})

        for path in (home, second):
            rows = [item for item in list_workspaces() if normalise_path(item["path"]) == normalise_path(path)]
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0].get("unit_id"), "%s 的行还指着已删主题" % path.name)
            self.assertFalse(rows[0].get("org_path"), "%s 的行还留着旧组织路径" % path.name)

    def test_the_boot_root_theme_cannot_be_deleted(self) -> None:
        """A 类（①）：启动目录的主题删了下次启动会以新身份回来 ⇒ 拒绝。"""
        from lab import state as state_mod

        self._theme_with_home()  # 让"最后一个主题"那道护栏不抢答
        boot = load_unit(self.root)["unit_id"]
        response = self.client.post("/units/delete", data={"unit": boot, "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertIn(boot, [item["unit_id"] for item in state_mod.list_units()])
        self.assertTrue((self.root / "groma-unit.json").is_file())

    def test_a_ghost_theme_can_be_deleted_and_stays_gone(self) -> None:  # A6-1
        import shutil

        home, unit_id = self._theme_with_home(name="幽灵主题", folder="ghost-home")
        shutil.rmtree(home)
        response = self.client.post("/units/delete", data={"unit": unit_id, "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("error=", response.headers["Location"])
        rows = [item for item in list_workspaces() if item.get("unit_id") == unit_id]
        self.assertEqual(rows, [], "登记行还指着已删主题")
        from web.app import create_app

        again = create_app(self.root).test_client()
        again.get("/units")
        self.assertNotIn(unit_id, [item["unit_id"] for item in unitmatch.unit_list()],
                         "重启后幽灵主题以目录名复活了")

    def test_a_ghost_workspace_is_named_with_a_cleanup_entry(self) -> None:  # A6-2
        import shutil

        home, unit_id = self._theme_with_home(name="幽灵主题", folder="ghost-home")
        shutil.rmtree(home)
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("失联工作区", body)
        self.assertIn(home.as_posix(), body)
        self.assertIn("/units/delete/confirm?unit=%s" % unit_id, body)

    def test_the_last_theme_cannot_be_deleted(self) -> None:
        """A 类：最后一个主题不能删。

        ⚠ **走内核、不走路由**：web 路径上启动目录那道护栏总是先命中（本机只有主题时，
        那一个主题就是启动目录的主题），所以"最后一个"这道护栏在路由上被遮蔽——
        它是给 CLI 的（W13 的 `groma unit delete` 不带 web 的 boot root 概念）。这里直接调 `drop_unit`。
        """
        from lab import state as state_mod
        from lab.inject import drop_unit

        boot = load_unit(self.root)["unit_id"]
        with self.assertRaises(LabError):
            drop_unit(boot)
        self.assertIn(boot, [item["unit_id"] for item in state_mod.list_units()])
        self.assertTrue((self.root / ".groma" / "unit.json").is_file())

    def test_deleting_the_focused_theme_moves_focus(self) -> None:
        """**B 类**：删掉当前聚焦的主题 ⇒ `active["unit"]` 落到还活着的主题上。

        回归证伪：删掉路由里 `focus()` 那一步 ⇒ `/healthz` 还指着已删主题的家（或直接 500）。
        """
        home, unit_id = self._theme_with_home()  # `/unit/create` 会 focus 到它
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], home.as_posix())
        self.client.post("/units/delete", data={"unit": unit_id, "confirm": "1"})
        self.assertEqual(self.client.get("/healthz").get_json()["workspace"], self.root.as_posix())
        # 光看 /healthz 钉不住 focus 那一步：`current()` 在主题没了时会落到 boot_root，
        # 与"focus 到启动主题"结果相同。再加一手**默认落在当前主题**的动作：
        # 不带 unit 的改名若还指着已删主题，会报"没有这个主题"，这才是 focus 的牙。
        renamed = self.client.post("/units/rename", data={"name": "删完之后的主题"})
        self.assertNotIn("error=", renamed.headers["Location"], "active 还指着已删的主题")

    def test_the_delete_confirm_page_lists_what_stays(self) -> None:
        """A 类（的界面面）：确认页要说清"工作区与项目文件都留下"，并列出会被摘的东西。"""
        home, unit_id = self._theme_with_home()
        self._inject_a_block(home)
        page = self.client.get("/units/delete/confirm", query_string={"unit": unit_id})
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('class="modal-veil"', body)
        self.assertIn("<dialog open", body)
        self.assertIn("工作区与项目文件都留下", body)
        self.assertIn("没有协作主题的工作区", body)
        self.assertIn("AGENTS.md", body)
        self.assertIn(home.as_posix(), body)
        self.assertIn("取消", body)
        # 入口挂在每个 block 自己的抬头，且 href 带着**本 block** 的 unit_id（不混选择）
        page_units = self.client.get("/units").get_data(as_text=True)
        self.assertIn("/units/delete/confirm?unit=%s" % unit_id, page_units)
        self.assertIn("删除这个协作主题", page_units)


    # --- 「0 条的原因」：补第四支 + 收敛无证据的归因 --------------------------------

    @staticmethod
    def _stub_candidates(name: str = "codex") -> list[dict]:
        """一台装着 codex、但这个区没申报它的根。

        键名照 `lab/discovery.py` 的 `discover()` 实际返回挑（`name` / `harness` / `root` /
        `include` / `exists` / `declared`）**不打桩就会去扫真盘的 `~/.codex`**——
        测试跟着开发者机器变，正是本仓最忌的那类"今天绿明天红"。
        """
        return [{"name": name, "harness": name, "root": "C:/stub/%s" % name,
                 "include": "sessions", "exists": True, "declared": False}]

    def _digest(self, folder: Path, *, file_count: int, outside: int) -> None:
        """给这个工作区写一份摘要：`collected` 与 `outside` 两个判据的唯一来源。"""
        import json

        digests = folder / ".groma" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "digest-20261113T000000Z.json").write_text(
            json.dumps({"schema": 1, "created_at": "2026-11-13T00:00:00Z",
                        "file_count": file_count, "unattributed_outside": outside}),
            encoding="utf-8",
        )

    def _reason_cell(self, body: str, path_text: str) -> str:
        """第①步里某个区的那一格「0 条的原因」——**逐行取，不按整页判**。

        R15 的教训：整页级 `assertIn` 会被别的行满足，看起来验了，其实什么都没钉住。
        """
        self.assertIn('<th>0 条的原因</th>', body)
        table = body.split('<th>0 条的原因</th>')[1].split("</table>")[0]
        rows = [row for row in table.split("<tr")[1:] if path_text in row]
        self.assertEqual(len(rows), 1, "%s 在第①步表格里不是恰好一行" % path_text)
        row = rows[0]
        return row[row.rindex("<td"):]

    def test_the_missing_harness_reason_names_the_harness(self) -> None:
        """R18 第四支：申报的根里缺本机装着的那家 ⇒ 说得出**缺哪一家**（A 类）。"""
        add_source(self.root, "keep-me", str(self.src), include=["projects"])
        self._digest(self.root, file_count=0, outside=0)
        with mock.patch("web.app.discover", return_value=self._stub_candidates("codex")):
            body = self.client.get("/discover").get_data(as_text=True)
        cell = self._reason_cell(body, self.root.as_posix())
        self.assertIn("申报的根里没有", cell)
        self.assertIn("codex", cell)

    def test_the_attribution_reason_only_appears_with_evidence(self) -> None:
        """R22：`outside` 为 0 时那句「归别的工作区」是**谎话**，必须不出现；为 3 时必须带条数（A 类）。"""
        add_source(self.root, "keep-me", str(self.src), include=["projects"])
        self._digest(self.root, file_count=0, outside=0)
        with mock.patch("web.app.discover", return_value=[]):   # 无未申报的根 ⇒ 排除第四支抢位
            zero = self.client.get("/discover").get_data(as_text=True)
        # 查**这一格**里最宽的「别的工作区」，不查整页、也不查新句的字面：
        # W4 之前的病灶句写的是"这区的会话都归别的工作区"，只钉"归**在**别的工作区"会放过它。
        cell_zero = self._reason_cell(zero, self.root.as_posix())
        self.assertNotIn("别的工作区", cell_zero, "outside=0 还在说会话归别处（R22 的病灶）")
        # 指向的块改名了（「第 ② 步」→「这个区的采集面」），判据仍是"这格把人指到证据所在处"。
        self.assertIn("这个区的采集面", cell_zero)

        self._digest(self.root, file_count=0, outside=3)
        with mock.patch("web.app.discover", return_value=[]):
            three = self.client.get("/discover").get_data(as_text=True)
        cell = self._reason_cell(three, self.root.as_posix())
        self.assertIn("归在别的工作区", cell)
        self.assertIn("3", cell)

    def test_the_fourth_reason_is_not_computed_for_unselected_rows(self) -> None:
        """第四支**只对选中行给**：非选中行不许为它多扫一次盘。

        回归证伪：把模板里 `if row.selected` 那半段去掉 ⇒ 非选中行也会拿到打桩的未申报根 ⇒ 本条必须红。
        为了让这一手真的能红，非选中行也写成 `collected == 0`（否则它压根进不了那一支的判据）。
        """
        other = self._second_workspace()
        add_source(self.root, "keep-me", str(self.src), include=["projects"])
        self._digest(self.root, file_count=0, outside=0)
        self._digest(other, file_count=0, outside=0)
        with mock.patch("web.app.discover", return_value=self._stub_candidates("codex")):
            body = self.client.get("/discover").get_data(as_text=True)
        self.assertIn("申报的根里没有", self._reason_cell(body, self.root.as_posix()))
        self.assertNotIn("申报的根里没有", self._reason_cell(body, other.as_posix()))

    # --- 整页横滚：成因（长路径不断行）与处置（overflow-wrap:anywhere） --------------

    def test_long_paths_are_allowed_to_wrap(self) -> None:
        """壳 CSS 的 `code` 规则必须让长路径**参与 min-content 计算**地断行（A 类）。

        成因是量出来的，不是推的（browser-use 打在**跑着的 0.44.0** 上，窗口 747、clientWidth 732）：
        第①步那张表 5 列的 min-content 之和 525 里有两列是 `<code>` 里的 Windows 绝对路径
        （单条 code 实测 min-content = 整条宽度，默认不断行：`D:/workspaces/ctx_parser` **154**），
        传导链是 表格 525 → 卡片 563 → `main` **611**（`main` 是 column flex item，`min-width:auto`
        把 min-content 顶成自己的宽度）→ `.content` 604 溢出 → `document.scrollWidth` **739 > 732** ⇒ 整页横滚。
        ⇒ 703–753 这一段窗口整页横向滚动的**根因是路径不断行**，不是断点、不是 rail。

        处置取裁定的 (a) 分支：一处 `overflow-wrap:anywhere`。**关键词这一手实测过**：
        同一处换成 `break-word` 页面**一点没变**（仍 739 / main 611）——`break-word` 不参与
        intrinsic min-content 计算，只有 `anywhere`（和 `word-break:break-all`）参与；
        加对之后 `main` 的 min-content 从 **611 降到 434**、表格 525→348、页面 scrollWidth 回到 732（= clientWidth，无横滚）。
        """
        css = self.client.get("/discover").get_data(as_text=True)
        rule = re.search(r"\bcode\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "壳 CSS 里找不到 code 规则——这条测试的取法就失效了")
        self.assertRegex(
            rule.group(1),
            r"overflow-wrap\s*:\s*anywhere|word-break\s*:\s*break-all",
            "长路径不参与 min-content 断行 ⇒ 703–753 整页横滚（break-word 不算，实测无效）",
        )

    def test_the_content_cap_allows_wide_windows(self) -> None:
        """`main` 上限 1600，且不是靠改 padding 冒充。

        只钉 CSS 字面——1440/1936 的观感是人眼项，归 Owner，单测里不假装量过。
        回归证伪：把 1600 改回 1020 ⇒ 必红。
        """
        import re

        body = self.client.get("/").get_data(as_text=True)
        css = "\n".join(re.findall(r"<style>(.*?)</style>", body, re.S))
        self.assertIn("max-width:1600px", css)
        self.assertNotIn("max-width:1020px", css)
        rule = re.search(r"\bmain\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "壳 CSS 里找不到 main 规则——这条测试的取法就失效了")
        self.assertIn("padding:0 24px", rule.group(1), "padding 被改了：那不是这次要做的事")

    def test_the_rail_breakpoint_is_still_702(self) -> None:
        """断点仍 702、rail 仍 128/36——**不动断点**，所以这条是不变量（B 类）。

        为什么特意钉它：设计只在"量到成因 (c)（rail 自己）"那一支才允许动断点，
        而实测成因是 (a) ⇒ 断点一个数都不该改。这条钉的是"没人顺手把 702 挪了"。
        **回归证伪**：把 `@media (max-width:702px)` 改成 `703px` ⇒ 本条必须红（128 那条会跟着红在"36 不在 media 里"上）。
        """
        css = self.client.get("/").get_data(as_text=True)
        self.assertEqual(
            len(re.findall(r"@media\s*\(\s*max-width\s*:\s*702px\s*\)", css)),
            1,
            "外壳断点 702 不止一处或已消失（没授权动断点）",
        )
        self.assertRegex(css, r"\.rail\s*\{[^}]*flex:\s*0 0 128px", "rail 满宽不再是 128")
        media = css.split("max-width:702px", 1)[1]
        self.assertRegex(media, r"\.rail\s*\{[^}]*flex:\s*0 0 36px", "36px 图标条不在 702 断点块里")


    def test_the_old_theme_term_is_gone_from_rendered_pages(self) -> None:
        """A 类（W21b）：旧口径里**会变成新词子串**的短语，在可见面上必须 0。

        不钉裸「主题」「主题页」「主题名称」——它们是「协作主题…」的子串，钉了必假红。
        """
        unit_id = unitmatch.unit_list()[0]["unit_id"]
        outsider = Path(self._tmp.name) / "outsider"
        outsider.mkdir()
        self.client.post("/workspaces/add", data={"path": str(outsider), "from": "discover_page"})
        pages = {
            "/": self.client.get("/"),
            "/discover": self.client.get("/discover"),
            "/activity": self.client.get("/activity"),
            "/assets": self.client.get("/assets"),
            "/units": self.client.get("/units"),
            "/observe": self.client.get("/observe"),
            "/units/delete/confirm": self.client.get(
                "/units/delete/confirm", query_string={"unit": unit_id}
            ),
            "/workspaces/remove": self.client.get(
                "/workspaces/remove", query_string={"path": self.root.as_posix()}
            ),
        }
        old = (
            "本主题", "本主题资产", "新建一个主题", "加入这个主题", "删除这个主题", "移出主题",
            "它自称的主题", "本机还没有主题", "没有主题的工作区", "本主题全部工作区",
            "已新建主题", "没有这个主题", "旧主题", "挂主题", "每个主题", "这个主题",
            "本主题的", "没有主题的区", "本机已登记、但不在这个主题里的工作区",
        )
        for path, response in pages.items():
            self.assertEqual(response.status_code, 200, path)
            body = response.get_data(as_text=True)
            for phrase in old:
                self.assertNotIn(phrase, body, "%s 上还留有旧口径：%s" % (path, phrase))

    def test_the_shell_and_pages_use_the_collaboration_theme_term(self) -> None:
        """A 类（W21b）：新词在每处该在的地方都出现——防「只改一半」。"""
        unit_id = unitmatch.unit_list()[0]["unit_id"]
        index = self.client.get("/").get_data(as_text=True)
        self.assertIn(">协作主题</a>", index)
        self.assertIn("协作主题是行，不是开关", index)
        self.assertIn("不挂协作主题", self.client.get("/discover").get_data(as_text=True))
        self.assertIn("每个协作主题一段", self.client.get("/activity").get_data(as_text=True))
        self.assertIn("本协作主题的", self.client.get("/assets").get_data(as_text=True))
        units = self.client.get("/units").get_data(as_text=True)
        self.assertIn("<h1>协作主题</h1>", units)
        self.assertIn("新建一个协作主题", units)
        self.assertIn("删除这个协作主题", units)
        confirm = self.client.get(
            "/units/delete/confirm", query_string={"unit": unit_id}
        ).get_data(as_text=True)
        self.assertIn("删除协作主题", confirm)
        remove = self.client.get(
            "/workspaces/remove", query_string={"path": self.root.as_posix()}
        ).get_data(as_text=True)
        self.assertIn("会被退役的协作主题", remove)


    # --- 协作主题页 B 方案 ------------------------------

    @staticmethod
    def _create_form(body: str) -> str:
        match = re.search(r'<form[^>]*action="[^"]*/unit/create"[^>]*>(.*?)</form>', body, re.S)
        return match.group(0) if match else ""

    def test_the_create_card_comes_before_the_theme_list(self) -> None:
        """A 类（W21a-1）：新建卡在主题卡之前——先建，再在列表里管。"""
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("新建一个协作主题", body)
        self.assertIn("展开管理（家", body)
        self.assertLess(body.index("新建一个协作主题"), body.index("展开管理（家"))

    def test_the_create_form_uses_a_registered_workspace_select(self) -> None:
        """A 类（W21a-2）：家从自由文本换成「已登记且存在」的下拉，option 按实例数。"""
        extra = Path(self._tmp.name) / "extra"
        extra.mkdir()
        self.client.post("/workspaces/add", data={"path": str(extra), "from": "discover_page"})
        body = self.client.get("/units").get_data(as_text=True)
        form = self._create_form(body)
        self.assertTrue(form, "协作主题页没有新建表单")
        self.assertIn('name="home"', form)
        self.assertIn('id="new-theme-home"', form)
        self.assertIn("required", form)
        self.assertNotIn('<input name="home"', form)
        self.assertIn("依附工作区", form)
        options = re.findall(r"<option[^>]*>", form)
        attachable = [item for item in list_workspaces() if Path(item["path"]).is_dir()]
        self.assertEqual(len(options), len(attachable) + 1, "option 数 != 可依附区数 + 1 占位项")
        for item in attachable:
            self.assertEqual(form.count('value="%s"' % item["path"]), 1,
                             "工作区在选项里出现次数不是 1：%s" % item["path"])

    def test_the_create_form_defaults_to_the_current_home(self) -> None:
        """A 类（W21a-3）：默认项 = 当前协作主题的家，占位项不默认。"""
        form = self._create_form(self.client.get("/units").get_data(as_text=True))
        select = re.search(r'<select[^>]*name="home"[^>]*>(.*?)</select>', form, re.S)
        self.assertIsNotNone(select, "新建表单里没有 home 下拉")
        options = re.findall(r"<option[^>]*>", select.group(1))
        selected = [tag for tag in options if "selected" in tag]
        self.assertEqual(len(selected), 1, "默认选中项不是恰好一个：%r" % selected)
        self.assertIn(self.root.as_posix(), selected[0], "默认项不是当前家")

    def test_the_create_form_does_not_default_to_an_unrelated_workspace(self) -> None:
        """A 类（W21a-4）：当前家不在选项里 ⇒ 占位项选中，不许默认第一个区。"""
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        with mock.patch("web.app.list_workspaces",
                        return_value=[{"path": other.as_posix(), "name": "other"}]), \
                mock.patch("web.app.unitmatch.home_path", return_value=None):
            response = self.client.get("/units")
        self.assertEqual(response.status_code, 200)
        form = self._create_form(response.get_data(as_text=True))
        select = re.search(r'<select[^>]*name="home"[^>]*>(.*?)</select>', form, re.S)
        self.assertIsNotNone(select)
        options = re.findall(r"<option[^>]*>", select.group(1))
        selected = [tag for tag in options if "selected" in tag]
        self.assertEqual(len(selected), 1, "占位项没有成为唯一默认：%r" % selected)
        self.assertIn('value=""', selected[0], "默认落到了一个无关工作区")
        self.assertNotIn(other.as_posix(), selected[0])

    def test_a_machine_without_attachable_workspaces_gets_a_pointer_not_a_form(self) -> None:
        """A 类（W21a-5）：一个可依附区都没有 ⇒ 不渲染表单，只给去发现页的裸链接。"""
        with mock.patch("web.app.list_workspaces", return_value=[]):
            response = self.client.get("/units")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("本机还没有可依附的工作区", body)
        self.assertIn('href="/discover"', body)
        self.assertNotIn('action="/unit/create"', body)
        self.assertNotIn('name="home"', body)

    def test_a_machine_without_themes_paints_an_empty_list(self) -> None:
        """A 类（W21a-6）：0 个协作主题也要有一张空态卡，不许一片空白。"""
        with mock.patch("web.app.unitmatch.unit_list", return_value=[]):
            response = self.client.get("/units")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("本机还没有协作主题。", body)
        self.assertIn("协作主题列表", body)

    def test_the_vault_card_is_labeled_as_machine_settings(self) -> None:
        """A 类（W21a-7）：Vault 前有一行「本机设置」，位置在主题卡之后。"""
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("本机设置（不属于任何协作主题）", body)
        label = body.index("本机设置（不属于任何协作主题）")
        self.assertLess(body.rindex("展开管理（"), label)
        self.assertLess(label, body.index("Vault 上下文保险库"))

    def test_the_page_does_not_add_a_workspace_pool_outside_themes(self) -> None:
        """B 类（W21a-8）：页级不另起「没有协作主题的工作区」池——加入口只在详情里。"""
        outsider = Path(self._tmp.name) / "pool-outsider"
        outsider.mkdir()
        self.client.post("/workspaces/add", data={"path": str(outsider), "from": "discover_page"})
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("加入这个协作主题", body)
        visible = outside_details_blocks(body)
        self.assertNotIn("加入这个协作主题", visible)
        self.assertNotIn("没有协作主题的工作区", visible)
        self.assertNotIn("待分配", visible)
        self.assertNotIn("未归主题", visible)

    def test_the_page_has_no_theme_selector(self) -> None:
        """B 类（W21a-9）：协作主题是并列标签，不造「当前协作主题」下拉。"""
        body = self.client.get("/units").get_data(as_text=True)
        self.assertNotIn('<select name="unit"', body)
        self.assertNotIn("切换协作主题", body)


    def test_the_main_rule_declares_an_explicit_cross_size(self) -> None:
        """A 类（W22a）：`main` 必须显式占满交叉轴，而不是只声明上限。

        W19 只钉了 `max-width`，所以"1600"是字面、不是几何：column flex 里横向 auto margin
        会压过 stretch，实际宽度退成按内容收缩。本条钉 `width:100%` + `min-width:0`，
        与 W19 旧测试分工：旧的守上限值，本条守"规则能产生几何保证"。
        """
        css = self.client.get("/").get_data(as_text=True)
        rule = re.search(r"\bmain\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "壳 CSS 里找不到 main 规则")
        decls = {}
        for part in rule.group(1).split(";"):
            if ":" in part:
                name, value = part.split(":", 1)
                decls[name.strip()] = value.strip()
        self.assertEqual(decls.get("width"), "100%", "main 没有显式 width:100%")
        self.assertEqual(decls.get("min-width"), "0", "main 没有 min-width:0")
        self.assertEqual(decls.get("max-width"), "1600px")
        self.assertEqual(decls.get("margin"), "24px auto 60px")
        self.assertEqual(decls.get("padding"), "0 24px")


    def test_body_links_use_the_teal_token(self) -> None:
        """A 类（W22b）：正文链接统一走 teal token，visited/hover/focus-visible 都有定义。

        只钉 `main` 作用域——rail/header 的链接态不在此范围；`a.primary` / `a.danger` 排除在外。
        """
        css = self.client.get("/").get_data(as_text=True)
        base = re.search(r"main a:not\(\.primary\):not\(\.danger\)\s*\{([^}]*)\}", css)
        self.assertIsNotNone(base, "没有正文链接基础规则")
        self.assertIn("color:var(--teal)", base.group(1))
        self.assertIn("text-decoration:underline", base.group(1))
        visited = re.search(r"main a:not\(\.primary\):not\(\.danger\):visited\s*\{([^}]*)\}", css)
        self.assertIsNotNone(visited, "没有 :visited 规则（会落到 UA 紫）")
        self.assertIn("color:var(--teal)", visited.group(1))
        hover = re.search(r"main a:not\(\.primary\):not\(\.danger\):hover\s*\{([^}]*)\}", css)
        self.assertIsNotNone(hover)
        self.assertIn("filter:brightness(.9)", hover.group(1).replace(" ", ""))
        focus = re.search(r"main a:not\(\.primary\):not\(\.danger\):focus-visible\s*\{([^}]*)\}", css)
        self.assertIsNotNone(focus)
        self.assertIn("outline", focus.group(1))
        self.assertNotIn("nav a:visited", css)

    def test_templates_have_one_red_token(self) -> None:
        """A 类（W22b）：模板里只剩一个红真值（`:root` 的 `--red`）。"""
        root = Path(__file__).resolve().parent.parent / "web" / "templates"
        texts = {p.name: p.read_text(encoding="utf-8") for p in root.glob("*.html")}
        dc = sum(text.count("#dc2626") for text in texts.values())
        b9 = sum(text.count("#b91c1c") for text in texts.values())
        self.assertEqual(dc, 0, "#dc2626 仍有 %d 处" % dc)
        self.assertEqual(b9, 1, "#b91c1c 应只剩 :root 一处，实有 %d" % b9)
        self.assertIn("--red:#b91c1c", texts["base.html"])


    def test_the_attach_workspace_hint_says_it_becomes_the_home(self) -> None:
        """A 类（W22c）：「依附工作区」旁边要说清——选中的区就是新家，可能被改名/换家。"""
        hint = ("选中的区就是新协作主题的「家」；若它已独自属于另一个协作主题，"
                "这次是改名/换家，不会再多出一个。")
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("依附工作区", body, "没有可依附的工作区，这条测试的夹具不成立")
        self.assertEqual(body.count(hint), 1, "这句说明不是恰好一处")
        self.assertIn(hint, outside_details_blocks(body), "说明被折进详情，新建前看不见")
        self.assertLess(body.index("新建一个协作主题"), body.index(hint))
        self.assertLess(body.index("依附工作区"), body.index(hint))


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class RelayCardTests(unittest.TestCase):
    """接力卡（屏 5+6）：棒位状态条三态、时刻区三态、预览折面（现算零写入）、
    历史「历史棒」标注；角色表卡升一级出管理区（POST /units/pipeline 路由不动）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-relay-card-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="接力卡工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(Path(self._tmp.name) / "startup"),
            "GROMA_STATE_DIR": str(Path(self._tmp.name) / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        unitmatch.create_unit("接力卡主题", self.ws)
        from lab import topics as topics_mod
        topics_mod.create_topic("接力卡主题")

    def _write_digest(self, entries: list[dict]) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _entry(self, harness: str, ts: str) -> dict:
        return {"source": harness, "path": "sessions/%s-%s.jsonl" % (harness, ts),
                "events": 3, "created_at": ts, "last_ts": ts}

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def _units(self, client):
        return client.get("/units").get_data(as_text=True)

    def test_pending_moment_state_shows_decide_buttons(self) -> None:
        """预览对齐，时刻卡=「检测到换 harness」句式+触发判据行；
        主按钮「按最近 5 会话铸简报」，直投「现在投」从时刻卡退场；忽略 ghost 保留。"""
        self._write_digest([self._entry("codex", "2026-11-22T01:00:00Z"),
                            self._entry("qoder", "2026-11-22T02:00:00Z")])
        body = self._units(self._client())
        self.assertIn("检测到换 harness", body)
        self.assertIn("触发判据：digest 中同工作区出现新 harness 的首个会话", body)
        self.assertIn("按最近 5 会话铸简报", body)
        self.assertIn("忽略这一次", body)
        self.assertIn("/units/handoff/ignore", body)
        self.assertNotIn("现在投", body, "直投还挂在时刻卡上——投递只留预览页")
        self.assertIn("2026-11-22T02:00:00Z", body, "时刻卡的 at 不该缺席")

    def test_observed_state_labels_history_baton(self) -> None:
        """时刻区第二态：无 pending 有 observed → 最近历史棒，标注「历史棒 · 自动入账不投」。"""
        self._write_digest([self._entry("codex", "2026-11-22T01:00:00Z"),
                            self._entry("qoder", "2026-11-22T02:00:00Z"),
                            self._entry("codex", "2026-11-22T03:00:00Z")])
        from lab import handoff

        handoff.replay_handoffs(self.ws)
        body = self._units(self._client())
        self.assertNotIn("接力时刻：", body, "observed 不该被当成待办")
        self.assertIn("历史棒 · 自动入账不投", body)
        self.assertIn("codex → qoder", body)

    def test_empty_state_says_no_baton_change_yet(self) -> None:
        """时刻区第三态：无换棒史 → 「还没发生过换棒——最近 N 场都是同一 harness」。"""
        self._write_digest([self._entry("codex", "2026-11-22T01:00:00Z"),
                            self._entry("codex", "2026-11-22T02:00:00Z")])
        body = self._units(self._client())
        self.assertIn("还没发生过换棒", body)
        self.assertIn("最近 2 场都是同一 harness", body)

    def test_baton_bar_three_states(self) -> None:
        """棒位状态条三态：未配表引导 / mapped 下一棒 / 推不出诚实账。"""
        client = self._client()
        body = self._units(client)
        self.assertIn("接力：未启用——三站流水线（任务书→施工→验收）", body)
        self._write_digest([self._entry("codex", "2026-11-22T01:00:00Z"),
                            self._entry("qoder", "2026-11-22T02:00:00Z")])
        body = self._units(client)  # 未配表仍引导（配表前不启用）
        self.assertIn("接力：未启用", body)
        client.post("/units/pipeline", data={"unit": self._unit_id(), "role_codex": "任务书",
                                             "role_qoder": "施工"})
        body = self._units(client)
        self.assertIn("下一棒", body)
        self.assertIn("验收", body, "codex=任务书、qoder=施工 ⇒ 下一棒应是验收")
        # mapped=False 诚实账：最近 harness 换成没配的
        self._write_digest([self._entry("qoder", "2026-11-22T02:00:00Z"),
                            self._entry("claude-code", "2026-11-22T03:00:00Z")])
        body = self._units(client)
        self.assertIn("棒位推不出", body)
        self.assertIn("不在表里（诚实账，不猜）", body)

    def _unit_id(self) -> str:
        from lab import unitmatch

        return unitmatch.folder_unit(self.ws)

    def test_preview_fold_is_computed_now_and_zero_write(self) -> None:
        """预览折面=「按当前窗口现算」；GET 零写入。
        预览对齐，折面正文由 render_brief 纯文本改为结构化 payload 卡
        （四段 <b> 标签形状，与 handoff.html 同一宏）。"""
        from lab import surface

        self._write_digest([self._entry("codex", "2026-11-22T01:00:00Z"),
                            self._entry("qoder", "2026-11-22T02:00:00Z")])
        surface.write_payload(self.ws, "资产载荷正文")
        before = surface.payload_path(self.ws).read_bytes()
        body = self._units(self._client())
        self.assertIn("预览简报", body)
        self.assertIn("按当前窗口现算", body)
        self.assertIn("<b>进行到哪：</b>", body)
        self.assertIn("<b>交接材料：</b>", body)
        self.assertEqual(surface.payload_path(self.ws).read_bytes(), before, "GET 预览写了载荷")
        self.assertFalse((self.ws / "AGENTS.md").exists(), "GET 预览写了块面")

    def test_history_fold_labels_observed_rows(self) -> None:
        """简报历史折面上移到接力卡；observed 条目标注「历史棒」。"""
        self._write_digest([self._entry("codex", "2026-11-22T01:00:00Z"),
                            self._entry("qoder", "2026-11-22T02:00:00Z")])
        from lab import handoff, topics

        handoff.replay_handoffs(self.ws)
        body = self._units(self._client())
        self.assertIn("简报历史（1 笔）", body)
        self.assertIn("历史棒 · 自动入账不投", body)

    def test_role_table_is_promoted_out_of_management(self) -> None:
        """改写：角色表卡在接力卡一级，不再住在管理区 details 里。
        标题行补副句，接力卡锚改到副句后。"""
        self._write_digest([self._entry("codex", "2026-11-22T01:00:00Z")])
        body = self._units(self._client())
        relay = body.split("同一套铸造与投放设施", 1)[1]
        self.assertIn("流水线角色表", relay, "角色表卡没升进接力卡")
        self.assertIn('action="/units/pipeline"', relay, "POST /units/pipeline 路由被动了")
        management = body.split("展开管理（家 · 改名 · 说明 · 可并入）", 1)[1]
        self.assertNotIn("流水线角色表", management, "角色表还留在管理区里")


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class CopyTests(unittest.TestCase):
    """投影句两态、assets「N 份资产」。口径测试另在 test_inject_ratio.py。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u5-copy-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="口径工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(Path(self._tmp.name) / "startup"),
            "GROMA_STATE_DIR": str(Path(self._tmp.name) / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("口径主题", self.ws)["unit_id"]
        topics_mod.create_topic("口径主题")

    def _write_digest(self) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/a.jsonl", "events": 3,
                    "created_at": "2026-11-22T01:00:00Z", "last_ts": "2026-11-22T01:00:00Z"}]
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": 1,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def test_evidence_projection_line_without_table(self) -> None:
        """未配表态：诚实说「未配角色表——载荷全文同投」。"""
        from web.app import create_app

        client = create_app(self.ws).test_client()
        body = client.get("/evidence", query_string={"unit": self.unit_id}).get_data(as_text=True)
        self.assertIn("未配角色表——载荷全文同投", body)

    def test_evidence_projection_line_with_table(self) -> None:
        """配表态：一行说清按受众投影 + 未列出的 harness 收全文。"""
        self._write_digest()
        from web.app import create_app

        client = create_app(self.ws).test_client()
        client.post("/units/pipeline", data={"unit": self.unit_id,
                                             "role_codex": "任务书", "role_qoder": "施工"})
        body = client.get("/evidence", query_string={"unit": self.unit_id}).get_data(as_text=True)
        self.assertIn("本载荷按受众投影：", body)
        self.assertIn("codex=任务书视角", body)
        self.assertIn("未列出的 harness 收全文", body)

    def test_assets_block_header_counts_assets_not_drafts(self) -> None:
        """资产单例化改写：块头「N 份资产」，
        「草稿」从计数退场；单例化后两次构建同指纹=没变不重铸，恒 1 份资产
        （「删除草稿」动词已退役，本项仍保留）。"""
        self._write_digest()
        first = build_asset(self.ws, "资产甲", [("codex", "sessions/a.jsonl")])
        again = build_asset(self.ws, "资产甲", [("codex", "sessions/a.jsonl")])
        self.assertTrue(again.get("unchanged"), "同指纹没提示没变不重铸")
        self.assertEqual(first["asset_id"], again["asset_id"])
        from web.app import create_app

        client = create_app(self.ws).test_client()
        body = client.get("/assets").get_data(as_text=True)
        self.assertIn("1 份资产", body)
        self.assertNotIn("1 份草稿", body)
        self.assertNotIn("删除草稿", body, "「删除草稿」动词没随草稿退役")


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class SessionScopeTests(unittest.TestCase):
    """会话解析域修复：证据页列主题全域，session 页旧解析域=current() 的家 Unit 并集——
    列表域 ⊋ 解析域，证据落在别的成员区即「找不到」（0.62.0 真机实锤屏4 落锚入口全灭）。
    修复=链接带 unit 参数 + session 页按主题成员并集解析；无参数向后兼容。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u6-scope-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws_a = base / "unit-a"
        self.ws_a.mkdir()
        init_workspace(self.ws_a, name="跨区甲", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        # 主题家=成员 B：这样 current()（甲区身份 Unit，并集只有本区）解析不到乙区的会话，
        # 「不带 unit 参数 → 现状找不到」的对照才成立。
        self.ws_b = base / "unit-b"
        self.ws_b.mkdir()
        init_workspace(self.ws_b, name="跨区乙", owner="tester")
        self.unit_id = unitmatch.create_unit("跨区主题", self.ws_b)["unit_id"]
        unitmatch.join_unit(self.unit_id, self.ws_a, role="evidence")

    def _seed_member_b_session(self) -> None:
        """证据会话落在成员 B：digest 行 + vault 分片（session_key 口径一致）。"""
        folder = self.ws_b / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        key = "codex:sessions/b.jsonl"
        entries = [{"source": "codex", "path": "sessions/b.jsonl", "events": 3,
                    "session_key": key,
                    "cwd": str(self.ws_b), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-22T00:59:00Z",
                    "created_at": "2026-11-22T01:00:00Z", "last_ts": "2026-11-22T01:00:00Z"}]
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": 1,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")
        from lab import vault as vault_mod
        from lab.vault import reindex as vault_reindex, vault_root as vault_root_of

        vault_mod.append_records(vault_mod.vault_root(), "codex", key, [
            {"ts_utc": "2026-11-22T01:00:00Z", "granularity": "event", "session_key": key,
             "role": "assistant", "type": "message", "text": "跨成员区会话的 assistant 块正文。",
             "reasoning": None, "tool": None, "strip": [], "agent_id": "agt-1",
                        "source": {"path": "sessions/b.jsonl", "line_no": 1, "bytes": 32,
                        "source_sha256": "0" * 64}},
        ])
        vault_reindex(vault_root_of())
        # 证据页列出的是主题全域：给这条跨区会话落一条 active 证据，链接才会出现
        from lab import topics as topics_mod

        topics_mod.create_topic("跨区主题")
        records = unitmatch.digest_records_for([self.ws_b.as_posix()])
        known = {(f.get("source"), f.get("path"))
                 for r in records for f in r.get("files") or []}
        topics_mod.select(topics_mod.resolve_topic("跨区主题")["topic_id"],
                          [{"source": "codex", "path": "sessions/b.jsonl"}],
                          decided_by="llm:test", known_keys=known)

    def _client(self):
        from web.app import create_app

        return create_app(self.ws_a).test_client()

    def test_cross_member_session_resolves_with_unit_param(self) -> None:
        """新卷：带 unit 参数 → 成员 B 的会话可解析，assistant 块与落锚入口渲染。"""
        self._seed_member_b_session()
        page = self._client().get("/session", query_string={
            "source": "codex", "path": "sessions/b.jsonl", "unit": self.unit_id})
        self.assertEqual(page.status_code, 200, "带 unit 参数仍解析不到成员区会话")
        body = page.get_data(as_text=True)
        self.assertIn("assistant 块", body)
        self.assertIn("落锚到资产", body, "屏4 落锚入口没有渲染")
        self.assertIn("跨成员区会话的 assistant 块正文", body)
        # 证据页链接本身带上了主题参数
        evidence = self._client().get("/evidence", query_string={"unit": self.unit_id}).get_data(as_text=True)
        self.assertIn("unit=" + self.unit_id, evidence, "证据页会话链接没带 unit 参数")

    def test_without_unit_param_keeps_current_scope_and_honest_miss(self) -> None:
        """不带 unit 参数维持 current() 现状——成员 B 会话仍「找不到」（诚实账）。"""
        self._seed_member_b_session()
        client = self._client()
        response = client.get("/session", query_string={"source": "codex", "path": "sessions/b.jsonl"})
        self.assertEqual(response.status_code, 303)
        body = client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("找不到这个会话：codex / sessions/b.jsonl", body)


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class InjectionSourceTests(unittest.TestCase):
    """注入源 ≡ 服役资产：web 面退役资产选择——注入源卡退场、
    hero 承载状态并追加唯一手工动作「重新投放」；确认页固定服役资产；
    无服役资产 → hero 空态句且无重新投放。CLI 显式 asset_id 照旧。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u7-src-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="口径工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("口径主题", self.ws)["unit_id"]
        topics_mod.create_topic("口径主题")

    def _seed_digest(self) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/a.jsonl", "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-22T00:59:00Z",
                    "created_at": "2026-11-22T01:00:00Z", "last_ts": "2026-11-22T01:00:00Z"}]
        (folder / "digest-20261122T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": 1,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _serve_asset(self) -> dict:
        """CLI 权力路径：显式 asset_id 注入，立成服役资产。"""
        from lab.asset import build_asset, list_assets
        from lab.inject import inject_theme

        self._seed_digest()
        asset = build_asset(self.ws, "服役资产", [("codex", "sessions/a.jsonl")])
        inject_theme(self.unit_id, asset["asset_id"])
        return asset

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def test_hero_carries_redispatch_and_select_is_retired(self) -> None:
        """hero 有「重新投放」；select 与「注入到这里」整体退场。"""
        self._serve_asset()
        body = self._client().get("/units").get_data(as_text=True)
        self.assertIn("重新投放", body)
        self.assertIn("/unit/inject/confirm?unit=", body)
        self.assertNotIn("注入到这里", body, "注入源卡还留在页面上")
        self.assertNotIn('name="asset_id"', body, "web 面还留着资产选择")

    def test_confirm_page_fixes_the_serving_asset(self) -> None:
        """确认页不再有资产选择——固定显示服役资产 + 将写入哪些区/面原形状。"""
        asset = self._serve_asset()
        client = self._client()
        page = client.get("/unit/inject/confirm", query_string={"unit": self.unit_id})
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("确认注入", body)
        self.assertIn(asset["asset_id"], body, "确认页没有固定显示服役资产")
        self.assertIn("服役资产", body)
        self.assertNotIn('name="asset_id"', body, "确认页还带着资产选择字段")
        self.assertIn('name="unit"', body)
        # POST 不再读 asset_id：confirm=1 直写也成立（按服役资产重新投放）
        response = client.post("/unit/inject/apply",
                               data={"unit": self.unit_id, "confirm": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=", response.headers["Location"])

    def test_no_serving_asset_shows_empty_state_and_no_redispatch(self) -> None:
        """无服役资产 → hero 空态句；「重新投放」不渲染；确认页诚实挡回。"""
        self._seed_digest()
        client = self._client()
        body = client.get("/units").get_data(as_text=True)
        self.assertIn("未设注入源——重铸产出服役资产后自动投放", body)
        # 改写，表尾 footnote 会出现「重新投放」一词（解释性），
        # 改钉**重投放入口**（hero 行末链接）不渲染。
        self.assertNotIn("重新投放</a>", body)
        response = client.get("/unit/inject/confirm", query_string={"unit": self.unit_id})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertIn("还没有服役资产",
                      client.get(response.headers["Location"]).get_data(as_text=True))


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class RenameDisplayTests(unittest.TestCase):
    """改名写显示名源：topics.rename_topic（显示名唯一来源，M4）+
    unitmatch.rename_unit 双写，失败任一不半写；表单预填=显示名；空名拒。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u8-rename-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="登记名单机版采集", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("登记名单机版采集", self.ws)["unit_id"]
        topics_mod.create_topic("登记名单机版采集")
        # M4 场景：登记名与显示名分叉——显示名读 topics 标签（卡片标题），不再是死界面
        topics_mod.rename_topic(self.unit_id, "ctx-parser")
        from web.app import create_app

        self.client = create_app(self.ws).test_client()

    def test_rename_follows_the_display_name(self) -> None:
        """① 改名后卡片 display_name 跟随——topics 标签变更生效（死界面修复）。"""
        response = self.client.post("/units/rename", data={"name": "ACPE 采集改造"})
        self.assertEqual(response.status_code, 303)
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("ACPE 采集改造", body, "卡片标题没跟随改名——显示名源没写")
        from lab import topics as topics_mod

        self.assertEqual(topics_mod.topic_record(self.unit_id)["name"], "ACPE 采集改造")

    def test_rename_syncs_the_registry_name_too(self) -> None:
        """①续 · 双写：登记名跟随，回退=显示=一致（M4 不破——显示仍以 topics 为源）。"""
        self.client.post("/units/rename", data={"name": "ACPE 采集改造"})
        from lab import state

        record = next(item for item in state.list_units()
                      if item.get("unit_id") == self.unit_id)
        self.assertEqual(record.get("name"), "ACPE 采集改造", "登记名没有跟随双写")

    def test_rename_form_prefills_the_display_name(self) -> None:
        """② 预填=显示名——「用户看到的名字」就是输入框里的名字。"""
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn('value="ctx-parser"', body, "预填还是登记名（单机版采集）")
        self.assertNotIn('value="登记名单机版采集"', body)

    def test_empty_name_is_refused(self) -> None:
        """③ 空名拒（沿用仓库口径）。"""
        response = self.client.post("/units/rename", data={"name": "   "})
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["Location"])
        self.assertIn("名称不能为空",
                      self.client.get(response.headers["Location"]).get_data(as_text=True))

    def test_m4_regression_rename_targets_what_is_displayed(self) -> None:
        """④ M4 回归：topics 标签存在时，改名改的就是显示的那个——
        卡片标题（display_name）与登记名同时变更且一致。"""
        self.client.post("/units/rename", data={"name": "改名后的主题"})
        body = self.client.get("/units").get_data(as_text=True)
        self.assertIn("改名后的主题", body)
        from lab import topics as topics_mod, state

        self.assertEqual(topics_mod.topic_record(self.unit_id)["name"], "改名后的主题")
        record = next(item for item in state.list_units()
                      if item.get("unit_id") == self.unit_id)
        self.assertEqual(record.get("name"), "改名后的主题")


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class RelayBriefTests(unittest.TestCase):
    """屏 5：时刻卡句式与按钮分级、结构化简报 payload 卡（预览页与 units 折面同形状）、
    接力历史四列表格、assets 页顶部接力时机位。GET 零写入、POST 路由零新增。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u9k5-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="接力卡工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("接力卡主题", self.ws)["unit_id"]
        topics_mod.create_topic("接力卡主题")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "note.md").write_text("材料文件正文\n", encoding="utf-8", newline="\n")

    def _seed(self) -> dict:
        """pending 换棒（codex→qoder）+ 服役资产带材料活引用 claim。"""
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": s, "path": "sessions/%s.jsonl" % s, "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": ts, "created_at": ts, "last_ts": ts}
                   for s, ts in (("codex", "2026-11-23T01:00:00Z"),
                                 ("qoder", "2026-11-23T02:00:00Z"))]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": 2,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")
        from lab.asset import attach_claims, build_asset

        asset = build_asset(self.ws, "屏5资产", [("codex", "sessions/codex.jsonl")])
        attach_claims(self.ws, asset["asset_id"], [
            {"title": "材料 claim", "material": {"locator": {"ref": "docs/note.md"},
                                                 "role": "task-spec"}},
        ])
        return asset

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def test_structured_brief_card_same_shape_on_preview_and_units_fold(self) -> None:
        """payload 卡=标题行+新鲜徽标+四段 <b> 标签+材料块（role+快照态）；两处同形状。"""
        self._seed()
        client = self._client()
        # units 折面先取（预览页 GET 会把时刻入账，一次性检出就消失——诚实账）
        units_body = client.get("/units").get_data(as_text=True)
        preview = client.get("/units/handoff/preview",
                             query_string={"path": self.ws.as_posix()}).get_data(as_text=True)
        self.assertIn("接力简报：codex → qoder · unit", preview, "标题行没落形")
        self.assertIn('<span class="badge on">新鲜</span>', preview)
        for section in ("<b>进行到哪：</b>", "<b>最近决策：</b>", "<b>开放问题：</b>", "<b>交接材料：</b>"):
            self.assertIn(section, preview)
        self.assertIn("（role: task-spec · ws-rel · 快照 ", preview, "材料块缺 role/快照")
        self.assertIn("√ 未变）", preview, "材料快照态没现算")
        self.assertIn("接力简报：codex → qoder · unit", units_body, "units 折面不是同形状")
        self.assertIn("<b>交接材料：</b>", units_body)

    def test_history_fold_is_a_four_column_table(self) -> None:
        """历史升四列表格；简报列无窗口快照=「—」；送达徽标。"""
        self._seed()
        from lab import handoff

        handoff.replay_handoffs(self.ws)
        body = self._client().get("/units").get_data(as_text=True)
        self.assertIn("<th>时刻</th><th>交接</th><th>简报</th><th>送达</th>", body)
        self.assertIn('<span class="badge off">历史棒</span>', body)

    def test_assets_top_moment_slot_two_states(self) -> None:
        """assets 页顶部时机位有/无两态——同一张时刻卡（同宏同句式）。"""
        self._seed()
        client = self._client()
        body = client.get("/assets").get_data(as_text=True)
        self.assertIn("检测到换 harness", body, "assets 页顶部没有时机位")
        self.assertIn("按最近 5 会话铸简报", body)
        response = client.post("/units/handoff/ignore", data={"path": self.ws.as_posix()})
        self.assertEqual(response.status_code, 303)
        body = client.get("/assets").get_data(as_text=True)
        self.assertNotIn("检测到换 harness", body, "忽略后时机位还挂着")


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class PipelineStationTests(unittest.TestCase):
    """屏 6：三站 .pipe 条、下一棒 health 行（[现在就铸] 两态）、投影预告句、
    角色表展示态（badges+details 内嵌原 select，零 JS）、推导现读行、棒上的 claims 卡。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u9k6-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="屏6工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("屏6主题", self.ws)["unit_id"]
        topics_mod.create_topic("屏6主题")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "note.md").write_text("材料文件正文\n", encoding="utf-8", newline="\n")

    def _seed_digest(self, harnesses=("zed", "claude-code", "codex", "qoder")) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        stamps = {"zed": "2026-11-23T00:00:00Z", "claude-code": "2026-11-23T00:30:00Z",
                  "codex": "2026-11-23T01:00:00Z", "qoder": "2026-11-23T02:00:00Z"}
        entries = [{"source": s, "path": "sessions/%s.jsonl" % s, "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": stamps[s], "created_at": stamps[s], "last_ts": stamps[s]}
                   for s in harnesses]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _serve_with_claims(self, drift: bool = False) -> dict:
        """服役资产：一条材料活引用 + 一条冻结引文（vault 分片），两种引用同框。"""
        from lab.asset import attach_claims, build_asset
        from lab.inject import inject_theme
        from lab import vault as vault_mod
        from lab.vault import reindex as vault_reindex, vault_root as vault_root_of

        self._seed_digest()
        asset = build_asset(self.ws, "屏6资产", [("codex", "sessions/codex.jsonl")])
        key = "codex:sessions/codex.jsonl"
        vault_mod.append_records(vault_mod.vault_root(), "codex", key, [
            {"ts_utc": "2026-11-23T01:00:30Z", "granularity": "event", "session_key": key,
             "role": "assistant", "type": "message", "text": "冻结引文正文。",
             "reasoning": None, "tool": None, "strip": [], "agent_id": "agt-1",
             "source": {"path": "sessions/codex.jsonl", "line_no": 1, "bytes": 32,
                        "source_sha256": "0" * 64}},
        ])
        vault_reindex(vault_root_of())
        attach_claims(self.ws, asset["asset_id"], [
            {"title": "材料 claim", "material": {"locator": {"ref": "docs/note.md"},
                                                 "role": "task-spec"}},
            {"title": "引文 claim", "session_key": key,
             "from_ts": "2026-11-23T01:00:00Z", "to_ts": "2026-11-23T01:01:00Z"},
        ])
        if drift:
            (self.ws / "docs" / "note.md").write_text("文件被改过\n", encoding="utf-8", newline="\n")
        inject_theme(self.unit_id, asset["asset_id"])
        return asset

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def _configure_roles(self, client) -> None:
        # 验收站也给持棒 harness（屏 6 的三站齐配形状），health 行才有「下一棒」可指。
        client.post("/units/pipeline", data={"unit": self.unit_id, "role_codex": "任务书",
                                             "role_qoder": "施工", "role_claude-code": "验收"})

    def test_pipe_bar_shape_and_unconfigured_degradation(self) -> None:
        """K6-1：三站盒（棒位徽标/已完成 ✓/未开始）+ 未配表退化=引导句无 pipe 盒。"""
        client = self._client()
        self._seed_digest()
        body = client.get("/units").get_data(as_text=True)
        self.assertIn("接力：未启用——三站流水线（任务书→施工→验收）", body)
        self.assertNotIn('class="pipe ', body, "未配表也画了三站条")
        self._configure_roles(client)
        body = client.get("/units").get_data(as_text=True)
        self.assertIn('class="pipe current"', body)
        self.assertIn("棒位", body)
        # 残差修复改写：站态按序——施工已完成 ⇒ 任务书也是
        # 「已完成 ✓」，三站里不再有「未开始」。
        self.assertGreaterEqual(body.count("已完成 ✓"), 2, "站态没按序：任务书站没随施工完成")
        self.assertNotIn("未开始", body)
        self.assertIn("qoder · 施工", body, "持站 harness 没从角色表反查")

    def test_next_health_line_cast_button_two_states(self) -> None:
        """K6-2：health 行（将收到 <角色>简报 · 投给 <harness> 的<装载面>）；
        [现在就铸] 仅 pending 渲染；[设置…] 恒在。"""
        self._serve_with_claims()
        client = self._client()
        self._configure_roles(client)
        # 无 pending：末两场同 harness（诚实账——零伪造）
        self._seed_digest(("codex", "qoder", "qoder"))
        body = client.get("/units").get_data(as_text=True)
        self.assertIn("下一棒：", body)
        self.assertIn("将收到", body)
        self.assertIn("[设置…]", body)
        self.assertNotIn("[现在就铸]", body, "无 pending 也渲染了现在就铸——零伪造被破坏")
        self.assertIn("触发：digest 检出上一会话 harness → 新会话 harness", body)
        # pending 回来：[现在就铸] 渲染
        self._seed_digest(("codex", "qoder"))
        body = client.get("/units").get_data(as_text=True)
        self.assertIn("[现在就铸]", body)

    def test_projection_foreword_and_derivation_line(self) -> None:
        """K6-3/K6-5：投影预告句与推导现读行（⇒ 链路 + 零推断零额度）。"""
        self._serve_with_claims()
        client = self._client()
        self._configure_roles(client)
        body = client.get("/units").get_data(as_text=True)
        self.assertIn("验收阶段自动换投影：验收视角载荷以「施工产出/偏差」开头", body)
        self.assertIn("推导现读：最近会话 <b>qoder</b>", body)
        self.assertIn("全程零推断、零额度", body)

    def test_role_table_display_state_with_details_edit(self) -> None:
        """K6-4：展示态 badges + [改]/[指派…] 开 details 内嵌原 select+保存（零 JS，路由原样）。"""
        self._serve_with_claims()
        client = self._client()
        self._configure_roles(client)
        body = client.get("/units").get_data(as_text=True)
        self.assertIn('<span class="badge violet">任务书</span>', body)
        self.assertIn('<span class="badge violet">施工</span>', body)
        self.assertIn("[改]", body)
        self.assertIn("[指派…]", body)
        self.assertIn('action="/units/pipeline"', body)
        self.assertIn('name="role_codex"', body, "details 里没有内嵌原 select")
        self.assertIn("保存</button>", body)

    def test_claims_card_badge_mapping_and_empty_state(self) -> None:
        """K6-6：claims 三列徽标映射（活引用/冻结引文/核对三态）与空态；两路径链接。"""
        self._serve_with_claims()
        client = self._client()
        self._configure_roles(client)
        body = client.get("/units").get_data(as_text=True)
        self.assertIn('<span class="badge violet">活引用</span>', body)
        self.assertIn('<span class="badge on">冻结引文</span>', body)
        self.assertIn('<span class="badge on">核对有效 ✓</span>', body)
        self.assertIn('<span class="badge on">文件未变 ✓</span>', body)
        self.assertIn("去会话详情选段引用", body)
        self.assertIn("引用材料文件…", body)
        self.assertIn("落锚前整批核对，失败一个字节不改", body)
        # 文件已变（诚实账）
        self._serve_with_claims(drift=True)
        body = client.get("/units").get_data(as_text=True)
        self.assertIn('<span class="badge off">文件已变</span>', body)
        self.assertIn("读最新版即可；重铸刷新快照", body)

    def test_claims_card_empty_state(self) -> None:
        self._seed_digest()
        body = self._client().get("/units").get_data(as_text=True)
        self.assertIn("这份资产还没有 claims", body)


@unittest.skipUnless(HAVE_FLASK, "Flask is not installed in this interpreter")
class PayloadPreviewTests(unittest.TestCase):
    """屏 7：资产页「按受众预览载荷」details——并排投影盒（project_text_for 现算，
    角色视角句/净增益折叠自动在内），asset.json 字节不动、GET 两次一致（现算非存储）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u9k7-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="屏7工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("屏7主题", self.ws)["unit_id"]
        topics_mod.create_topic("屏7主题")

    def _seed(self) -> dict:
        """4 场 codex 会话 + 1 场 qoder；资产引用其中 3 场（净增益 3/3 达折叠阈值）。"""
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        paths = [("codex", "sessions/a.jsonl"), ("codex", "sessions/b.jsonl"),
                 ("codex", "sessions/c.jsonl"), ("codex", "sessions/d.jsonl"),
                 ("qoder", "sessions/e.jsonl")]
        entries = [{"source": s, "path": p, "events": 3, "cwd": str(self.ws),
                    "keys": ["source", "path", "events"], "transcript": True,
                    "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-23T01:00:00Z", "created_at": "2026-11-23T01:00:00Z",
                    "last_ts": "2026-11-23T02:00:00Z"} for s, p in paths]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")
        from lab.asset import build_asset
        from lab.inject import inject_theme

        asset = build_asset(self.ws, "屏7资产", paths[:3])
        inject_theme(self.unit_id, asset["asset_id"])
        return asset

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def test_payload_preview_boxes_render(self) -> None:
        """K7-1：折面在服役资产行下；盒标题=harness+角色视角+通道·字数徽标；视角句内插。"""
        from lab.asset import find_asset

        asset = self._seed()
        client = self._client()
        client.post("/units/pipeline", data={"unit": self.unit_id, "role_codex": "任务书",
                                             "role_qoder": "施工"})
        asset_file = find_asset(Path(unitmatch.home_path(self.unit_id)), asset["asset_id"])
        before = (asset_file / "asset.json").read_bytes()
        body = client.get("/assets").get_data(as_text=True)
        self.assertIn("按受众预览载荷 ▸", body)
        self.assertIn("① 给 codex（任务书视角）", body)
        self.assertIn("AGENTS.md · ", body)
        self.assertIn("字</span>", body)
        self.assertIn("本载荷按「任务书」视角渲染", body, "角色视角句没进投影")
        self.assertIn("① 给 qoder（施工视角）", body)
        self.assertIn("hook · ", body)
        after = (asset_file / "asset.json").read_bytes()
        self.assertEqual(before, after, "投影动了 asset.json——K7 红线")
        # GET 两次一致（现算非存储）
        self.assertEqual(client.get("/assets").get_data(as_text=True),
                         client.get("/assets").get_data(as_text=True))

    def test_net_gain_fold_sentence_appears(self) -> None:
        """测试：净增益折叠句出现例（占比≥50% 且 ≥3 条）——3/3 来自查看方。"""
        self._seed()
        client = self._client()
        body = client.get("/assets").get_data(as_text=True)
        self.assertIn("条里 3 条来自你——已折叠。", body)

    def test_no_serving_asset_no_preview(self) -> None:
        """无服役资产的块不渲染折面。"""
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": [], "file_count": 0,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")
        body = self._client().get("/assets").get_data(as_text=True)
        self.assertNotIn("按受众预览载荷 ▸", body)


class WorkspaceBoardTests(unittest.TestCase):
    """屏 8：工作区表 5→4 列（降级次级行不删信息）+ 立即重铸挪 hero 下
    + 表尾 footnote + 接力卡副句/空态句 + confirm 页 context() 恢复库存 strip。
    POST 路由零新增零改名、GET 零写入。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u9k8-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="屏8工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("屏8主题", self.ws)["unit_id"]
        topics_mod.create_topic("屏8主题")

    def _seed(self) -> None:
        """5 场 codex 会话（streak 可读）+ 服役资产注入。"""
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        paths = [("codex", "sessions/%s.jsonl" % ch) for ch in "abcde"]
        entries = [{"source": s, "path": p, "events": 3, "cwd": str(self.ws),
                    "keys": ["source", "path", "events"], "transcript": True,
                    "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-23T01:00:00Z", "created_at": "2026-11-23T01:00:00Z",
                    "last_ts": "2026-11-23T02:00:00Z"} for s, p in paths]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")
        from lab.asset import build_asset
        from lab.inject import inject_theme

        asset = build_asset(self.ws, "屏8资产", paths[:3])
        inject_theme(self.unit_id, asset["asset_id"])

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def test_workspace_table_four_columns_with_demoted_rows(self) -> None:
        """K8-1：4 列表头；身份/会话事件/采集根降为成员工作区格 muted 次级行（信息不删）；
        装载面格显示通道串；footnote 在表尾。"""
        self._seed()
        body = self._client().get("/units").get_data(as_text=True)
        plane = outside_details_blocks(body)
        for head in ("成员工作区", "装载面", "投递状态", "投放"):
            self.assertIn(head, plane, "4 列表头缺 %s" % head)
        self.assertNotIn("会话 / 事件", plane, "旧 5 列表头还在")
        # 降级信息仍在（不删只降）——会话/事件数与采集根 chip 都在成员工作区格
        self.assertIn("5 / ", body, "会话/事件降级行丢了")
        self.assertIn("未声明采集根", body, "采集根降级 chip 丢了")
        # 装载面格：本夹具无装载面 → 显「—」（不造假）
        # K8-4 footnote
        self.assertIn("重铸后自动投全体成员（除非某区被暂停）", body)
        self.assertIn("唯一保留的手工动作", body)

    def test_reforge_button_below_hero_with_hero_sentence(self) -> None:
        """K8-2/3：hero 下一行=「注入源 ≡ 服役资产」muted 句 + 立即重铸按钮；管理区摘要去「注入源」。"""
        self._seed()
        body = self._client().get("/units").get_data(as_text=True)
        self.assertIn("注入源 ≡ 服役资产（唯一，无需选择）", body)
        self.assertIn("立即重铸", body)
        self.assertLess(body.index("注入源 ≡ 服役资产"), body.index("立即重铸"),
                        "立即重铸没在 hero 句下方")
        self.assertIn("展开管理（家 · 改名 · 说明 · 可并入）", body)
        self.assertNotIn("展开管理（家 · 改名 · 说明 · 注入源", body)

    def test_relay_card_subtitle_and_empty_state(self) -> None:
        """K8-5：接力卡头副句；空态句=屏 5 形状，streak 信息降 muted 第二行不删。"""
        self._seed()
        body = self._client().get("/units").get_data(as_text=True)
        self.assertIn("主题资产管「地图」，接力简报管「换棒那一刻」——两个产物，同一套铸造与投放设施", body)
        self.assertIn("本协作主题暂无待处理的接力时机。规则：新 harness 首会话 / 角色切换出现时，"
                      "铸对应类型的简报并只投给接棒的一侧。", body)
        # streak 信息降级不删
        self.assertIn("都是同一 harness", body)

    def test_inject_confirm_page_carries_base_context(self) -> None:
        """K8-6①：确认页补 **context(...)——库存 strip 与版本号回到确认页（历史欠账并刀）。"""
        self._seed()
        client = self._client()
        body = client.get("/unit/inject/confirm", query_string={"unit": self.unit_id,
                                                                "path": str(self.ws)}).get_data(as_text=True)
        self.assertIn("确认注入", body)
        self.assertIn("库存：工作区", body, "确认页缺库存 strip——context() 没接上")
        self.assertIn("v0.", body, "确认页缺版本号——context() 没接上")
        self.assertIn("全体成员", body)
        self.assertIn('action="/unit/inject/apply"', body, "POST 路由被动了")


class ResidualFixTests(unittest.TestCase):
    """残差修复（0.64.0 真机走查三处）：①站态按序 ②mapped 即渲染 health 行
    （下一站无持棒 → 诚实变体）③装载面计入 hook。POST 零新增零改名、GET 零写入。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u10-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="残差工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("残差主题", self.ws)["unit_id"]
        topics_mod.create_topic("残差主题")

    def _seed_digest(self, harnesses=("codex", "qoder", "qoder")) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        stamps = {"codex": "2026-11-23T01:00:00Z", "qoder": "2026-11-23T02:00:00Z",
                  "zed": "2026-11-23T00:00:00Z"}
        entries = [{"source": s, "path": "sessions/%s.jsonl" % s, "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": stamps[s], "created_at": stamps[s], "last_ts": stamps[s]}
                   for s in harnesses]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def test_completed_station_marks_earlier_stations_done(self) -> None:
        """①站态按序：codex=任务书、qoder=施工、最近会话 qoder ⇒ completed=施工——
        任务书站也显「已完成 ✓」（旧账把之前的站标「未开始」）。"""
        self._seed_digest()
        client = self._client()
        client.post("/units/pipeline", data={"unit": self.unit_id, "role_codex": "任务书",
                                             "role_qoder": "施工"})
        body = client.get("/units").get_data(as_text=True)
        self.assertGreaterEqual(body.count("已完成 ✓"), 2, "任务书站没随施工完成标已完成")
        self.assertIn("— · 验收", body, "下一棒站没显棒位（验收无人持棒=—）")

    def test_mapped_without_next_holder_shows_honest_variant(self) -> None:
        """②门控放宽：mapped 且下一站「验收」无人持棒 → health 行诚实变体
        + 投影预告句 + 推导现读行（持棒 —）都渲染；[现在就铸] 仍仅 pending。"""
        self._seed_digest()
        client = self._client()
        client.post("/units/pipeline", data={"unit": self.unit_id, "role_codex": "任务书",
                                             "role_qoder": "施工"})
        body = client.get("/units").get_data(as_text=True)
        self.assertIn("下一棒：<b>验收</b>（站）· 还没人持棒", body, "诚实变体句没渲染")
        self.assertIn("在下面角色表给「验收」指派 harness", body)
        self.assertIn("验收阶段自动换投影", body, "投影预告句没渲染")
        self.assertIn("推导现读：最近会话 <b>qoder</b>", body)
        self.assertIn("⇒ 持棒 <b>—</b>", body)
        self.assertNotIn("[现在就铸]", body, "无 pending 也渲染了现在就铸")

    def test_hook_registration_counts_as_face(self) -> None:
        """③装载面计入 hook：hook_registered 且 inj.faces 空 → 装载面格显
        「qoder hook（已注册）」；不是「—」。"""
        self._seed_digest()
        client = self._client()
        from lab.asset import build_asset
        from lab.inject import inject_theme

        asset = build_asset(self.ws, "残差资产", [("codex", "sessions/codex.jsonl")])
        inject_theme(self.unit_id, asset["asset_id"])
        client.post("/unit/inject/hook", data={"path": str(self.ws), "confirm": "1"})
        body = client.get("/units").get_data(as_text=True)
        self.assertIn("qoder hook（已注册）", body, "hook 没计入装载面格")


class SingleSlotTests(unittest.TestCase):
    """单槽落盘+指纹语义：写固定槽 assets/current/；同指纹不写盘不进账本；
    新指纹覆写同一槽；注入读侧读当前槽（指纹不符=偏旧语义）；确定性指纹不变。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u11k1-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="K1工作区", owner="tester")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("K1主题", self.ws)["unit_id"]
        topics_mod.create_topic("K1主题")
        self._seed_digest()

    def _seed_digest(self, names=("a", "b")) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/%s.jsonl" % name, "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-23T01:00:00Z", "created_at": "2026-11-23T01:00:00Z",
                    "last_ts": "2026-11-23T02:00:00Z"} for name in names]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def test_same_fingerprint_skips_write(self) -> None:
        from lab.asset import build_asset, current_asset_dir

        first = build_asset(self.ws, "K1资产", [("codex", "sessions/a.jsonl")])
        slot = current_asset_dir(self.ws, self.unit_id)
        self.assertEqual(slot.name, "current", "没写固定槽")
        manifest_before = (slot / "manifest.json").read_bytes()
        again = build_asset(self.ws, "K1资产", [("codex", "sessions/a.jsonl")])
        self.assertTrue(again.get("unchanged"))
        self.assertIn("没变不重铸", again.get("reason") or "")
        self.assertEqual((slot / "manifest.json").read_bytes(), manifest_before,
                         "同指纹还动了盘")

    def test_new_fingerprint_overwrites_same_slot(self) -> None:
        from lab.asset import build_asset, current_asset_dir

        first = build_asset(self.ws, "K1资产", [("codex", "sessions/a.jsonl")])
        second = build_asset(self.ws, "K1资产乙", [("codex", "sessions/a.jsonl"),
                                                   ("codex", "sessions/b.jsonl")])
        self.assertNotEqual(first["asset_id"], second["asset_id"])
        slot = current_asset_dir(self.ws, self.unit_id)
        data = json.loads((slot / "asset.json").read_text(encoding="utf-8"))
        self.assertEqual(data["asset_id"], second["asset_id"], "槽里不是最新指纹")
        legacy = list((self.ws / ".groma" / "assets").glob("asset-*"))
        self.assertEqual(legacy, [], "又长出了 asset-* 多代目录")

    def test_injection_read_side_reads_slot_with_honest_stale(self) -> None:
        from lab.asset import build_asset
        from lab.inject import inject_theme, refresh

        asset_a = build_asset(self.ws, "K1资产", [("codex", "sessions/a.jsonl")])
        inject_theme(self.unit_id, asset_a["asset_id"])
        res = refresh(self.ws)
        self.assertNotEqual(res["state"], "paused")
        asset_b = build_asset(self.ws, "K1资产乙", [("codex", "sessions/a.jsonl"),
                                                    ("codex", "sessions/b.jsonl")])
        # 槽指纹已变：refresh 读当前槽（不按目录身份找旧 id），收敛投影、不报缺资产；
        # 账面 source 仍是人点定的旧 id（偏旧语义），payload 已按新槽收敛
        res2 = refresh(self.ws)
        self.assertNotEqual(res2["state"], "paused")
        self.assertTrue(res2.get("payload_sha256"))
        self.assertTrue(res2.get("changed"), "新指纹没收敛进 payload")

    def test_deterministic_fingerprint_unchanged(self) -> None:
        from lab.asset import build_asset

        one = build_asset(self.ws, "指纹", [("codex", "sessions/a.jsonl"),
                                            ("codex", "sessions/b.jsonl")])
        two = build_asset(self.ws, "指纹", [("codex", "sessions/b.jsonl"),
                                            ("codex", "sessions/a.jsonl")])
        self.assertEqual(one["asset_id"], two["asset_id"],
                         "同输入不同顺序算出不同指纹——确定性被破坏")
        self.assertTrue(two.get("unchanged"), "顺序不同的同输入没被识别为同指纹")


class ClaimInheritanceTests(unittest.TestCase):
    """claims 跨代继承：覆写前逐条核对带入——冻结引文对 vault 分片 sha、
    材料对文件指纹；可核对带入、失效按原因分桶（skipped_claims）进返回值与账本；
    首次重铸空继承。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u11k2-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="K2工作区", owner="tester")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "note.md").write_text("材料文件正文\n", encoding="utf-8", newline="\n")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("K2主题", self.ws)["unit_id"]
        topics_mod.create_topic("K2主题")
        self._seed_digest()
        self._vault_key = self._make_vault_shard()

    def _seed_digest(self) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/%s.jsonl" % name, "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-23T01:00:00Z", "created_at": "2026-11-23T01:00:00Z",
                    "last_ts": "2026-11-23T02:00:00Z"} for name in ("a", "b")]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _make_vault_shard(self) -> str:
        from lab import vault as vault_mod
        from lab.vault import reindex as vault_reindex

        key = "codex:sessions/a.jsonl"
        vault_mod.append_records(vault_mod.vault_root(), "codex", key, [
            {"ts_utc": "2026-11-23T01:00:30Z", "granularity": "event", "session_key": key,
             "role": "assistant", "type": "message", "text": "冻结引文正文。",
             "reasoning": None, "tool": None, "strip": [], "agent_id": "agt-1",
             "source": {"path": "sessions/a.jsonl", "line_no": 1, "bytes": 32,
                        "source_sha256": "0" * 64}},
        ])
        vault_reindex(vault_mod.vault_root())
        return key

    def _quote_claim(self) -> dict:
        return {"title": "引文 claim", "session_key": self._vault_key,
                "from_ts": "2026-11-23T01:00:00Z", "to_ts": "2026-11-23T01:01:00Z"}

    def _slot_json(self):
        from lab.asset import current_asset_dir

        return json.loads((current_asset_dir(self.ws, self.unit_id) / "asset.json")
                          .read_text(encoding="utf-8"))

    def test_verified_quote_survives_reforge(self) -> None:
        """可核对冻结引文跨代存活，payload claims 不断链。"""
        from lab.asset import attach_claims, build_asset

        first = build_asset(self.ws, "K2资产", [("codex", "sessions/a.jsonl")])
        attach_claims(self.ws, first["asset_id"], [self._quote_claim()])
        second = build_asset(self.ws, "K2资产乙", [("codex", "sessions/a.jsonl"),
                                                   ("codex", "sessions/b.jsonl")])
        self.assertNotEqual(first["asset_id"], second["asset_id"])
        self.assertFalse(second.get("skipped_claims"))
        self.assertEqual(second.get("inherited_claims"), 1)
        claims = self._slot_json()["claims"]
        self.assertEqual([c["title"] for c in claims], ["引文 claim"], "冻结引文没跨代存活")
        self.assertEqual(claims[0]["state"], "verified")

    def test_material_survival_and_drift_buckets(self) -> None:
        """材料活引用：文件未变跨代存活；文件已变→skipped_claims material_drift 分桶。"""
        from lab.asset import attach_claims, build_asset

        first = build_asset(self.ws, "K2材料", [("codex", "sessions/a.jsonl")])
        attach_claims(self.ws, first["asset_id"], [{
            "title": "材料 claim", "material": {"locator": {"ref": "docs/note.md"},
                                                "role": "task-spec"}}])
        # 文件未变：带入
        second = build_asset(self.ws, "K2材料乙", [("codex", "sessions/a.jsonl"),
                                                   ("codex", "sessions/b.jsonl")])
        self.assertFalse(second.get("skipped_claims"))
        self.assertEqual([c["title"] for c in self._slot_json()["claims"]], ["材料 claim"])
        # 文件已变：失效计数分桶
        (self.ws / "docs" / "note.md").write_text("被改过\n", encoding="utf-8", newline="\n")
        third = build_asset(self.ws, "K2材料丙", [("codex", "sessions/a.jsonl")])
        self.assertEqual(third.get("skipped_claims"), {"material_drift": 1})
        self.assertEqual(self._slot_json().get("claims") or [], [])

    def test_first_build_is_empty_inheritance(self) -> None:
        from lab.asset import build_asset

        asset = build_asset(self.ws, "首次", [("codex", "sessions/a.jsonl")])
        self.assertEqual(asset.get("inherited_claims"), 0)
        self.assertEqual(asset.get("skipped_claims"), {})
        self.assertIsNone(self._slot_json().get("claims"))

    def test_reforge_ledger_carries_skipped_claims(self) -> None:
        """skipped_claims 进 reforges 账本（与 skipped_stale 同款待遇）。"""
        from lab import topics as topics_mod

        topics_mod.select(self.unit_id, [{"source": "codex", "path": "sessions/a.jsonl"}],
                          decided_by="human", known_keys={("codex", "sessions/a.jsonl")})
        out = topics_mod.reforge(self.ws, self.unit_id)
        self.assertFalse(out["asset"].get("skipped_claims"))
        last = topics_mod.last_reforge(self.unit_id)
        self.assertIn("skipped_claims", last, "账本没记 claims 分桶字段")
        self.assertEqual(last["skipped_claims"], {})


class AssetLedgerTests(unittest.TestCase):
    """web 账面与动词退役：单资产账面+重铸账本摘要行；/assets/build 与
    /assets/delete 退役；落锚撤资产下拉恒写当前槽；verify 恒当前槽；护栏卡新话。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u11k3-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="K3工作区", owner="tester")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "note.md").write_text("材料文件正文\n", encoding="utf-8", newline="\n")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("K3主题", self.ws)["unit_id"]
        topics_mod.create_topic("K3主题")
        self._seed_digest()

    def _seed_digest(self) -> None:
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/%s.jsonl" % name, "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-23T01:00:00Z", "created_at": "2026-11-23T01:00:00Z",
                    "last_ts": "2026-11-23T02:00:00Z"} for name in ("a", "b")]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": len(entries),
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _client(self):
        from web.app import create_app

        return create_app(self.ws).test_client()

    def test_single_asset_account_with_reforge_ledger(self) -> None:
        """账面单资产 + 重铸账本摘要行（共 N 笔 · 最近笔）+ 最近 5 笔折面。"""
        from lab import topics as topics_mod

        topics_mod.select(self.unit_id, [{"source": "codex", "path": "sessions/a.jsonl"}],
                          decided_by="human", known_keys={("codex", "sessions/a.jsonl")})
        topics_mod.reforge(self.ws, self.unit_id)
        topics_mod.undo(self.unit_id, [("codex", "sessions/a.jsonl")], decided_by="human")
        topics_mod.select(self.unit_id, [{"source": "codex", "path": "sessions/b.jsonl"}],
                          decided_by="human", known_keys={("codex", "sessions/b.jsonl")})
        topics_mod.reforge(self.ws, self.unit_id)
        body = self._client().get("/assets").get_data(as_text=True)
        self.assertIn("重铸账本：共 2 笔", body, "账本摘要行没渲染")
        self.assertIn("最近 5 笔 ▸", body)
        self.assertNotIn("另有", body, "历史折叠区没退役")
        self.assertNotIn("高级：从会话手工生成草稿", body)

    def test_anchor_has_no_asset_dropdown_and_writes_slot(self) -> None:
        from lab.asset import build_asset, current_asset_dir

        build_asset(self.ws, "K3资产", [("codex", "sessions/a.jsonl")])
        client = self._client()
        body = client.get("/assets").get_data(as_text=True)
        self.assertNotIn('name="asset_id"', body.split("assets/anchor")[1],
                         "落锚表单还留着资产下拉")
        response = client.post("/assets/anchor", data={
            "title": "材料引文", "material_ref": "docs/note.md", "material_role": "task-spec"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("error=", response.headers["Location"])
        slot = current_asset_dir(self.ws, self.unit_id) / "asset.json"
        claims = json.loads(slot.read_text(encoding="utf-8")).get("claims") or []
        self.assertEqual([c.get("title") for c in claims], ["材料引文"], "落锚没写进当前槽")

    def test_verify_targets_current_slot_ignoring_posted_id(self) -> None:
        from lab.asset import attach_claims, build_asset

        asset = build_asset(self.ws, "K3核对", [("codex", "sessions/a.jsonl")])
        attach_claims(self.ws, asset["asset_id"], [{
            "title": "材料 claim", "material": {"locator": {"ref": "docs/note.md"},
                                                "role": "task-spec"}}])
        client = self._client()
        response = client.post("/assets/verify", data={
            "asset_id": "asset-bogus", "path": str(self.ws)})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("error=", response.headers["Location"], "verify 没恒验当前槽")
        self.assertIn("notice=", response.headers["Location"])

    def test_retired_routes_are_gone(self) -> None:
        client = self._client()
        for path, data in (
            ("/assets/build", {"title": "x", "session": ["codex:sessions/a.jsonl"]}),
            ("/assets/delete", {"asset_id": "asset-x", "confirm": "1"}),
        ):
            response = client.post(path, data=data)
            self.assertEqual(response.status_code, 404, "%s 没退役" % path)
        self.assertEqual(client.get("/assets/delete?asset_id=asset-x").status_code, 404)

    def test_guardrail_card_new_wording_in_visible_plane(self) -> None:
        from lab.asset import build_asset

        build_asset(self.ws, "护栏", [("codex", "sessions/a.jsonl")])
        plane = outside_details_blocks(self._client().get("/assets").get_data(as_text=True))
        self.assertIn("注入跟随机器设置自动走", plane)
        self.assertIn("唯一手工动作=重新投放", plane)
        self.assertIn("没变不重铸", plane)
        self.assertIn("别人的资产绝不顶掉", plane)


class AssetCliMigrationTests(unittest.TestCase):
    """CLI 收编+迁移：asset build --check 不落盘；migrate-singleton 不带 --yes
    只打印清单；带 --yes 合并去重 claims+核对+删历史代+报告计数；CLI 双模式幂等。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u11k4-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="K4工作区", owner="tester")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "note.md").write_text("材料文件正文\n", encoding="utf-8", newline="\n")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("K4主题", self.ws)["unit_id"]
        topics_mod.create_topic("K4主题")
        folder = self.ws / ".groma" / "digests"
        folder.mkdir(parents=True, exist_ok=True)
        entries = [{"source": "codex", "path": "sessions/a.jsonl", "events": 3,
                    "cwd": str(self.ws), "keys": ["source", "path", "events"],
                    "transcript": True, "transcript_lines": 12, "parse_errors": 0,
                    "first_ts": "2026-11-23T01:00:00Z", "created_at": "2026-11-23T01:00:00Z",
                    "last_ts": "2026-11-23T02:00:00Z"}]
        (folder / "digest-20261123T000000Z.json").write_text(
            json.dumps({"schema": 1, "files": entries, "file_count": 1,
                        "unattributed_outside": 0, "unattributed_no_cwd": 0}),
            encoding="utf-8", newline="\n")

    def _run_cli(self, *argv: str) -> tuple[int, dict]:
        import contextlib
        import io

        from lab import cli as cli_mod

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cli_mod.main(["--root", str(self.ws)] + list(argv))
        text = buffer.getvalue()
        return code, (json.loads(text) if text.strip() else {})

    def _seed_generations(self) -> list[Path]:
        """造两代历史目录：asset-old1（材料 claim 已漂移）、asset-old2（同一材料 claim 未变）。"""
        base = self.ws / ".groma" / "assets"
        base.mkdir(parents=True, exist_ok=True)
        import hashlib

        unchanged = hashlib.sha256((self.ws / "docs" / "note.md").read_bytes()).hexdigest()
        claims_a = [{"title": "漂移材料", "material": {"locator": {"ref": "docs/note.md"},
                                                    "role": "task-spec",
                                                    "snapshot_sha256": "0" * 64},
                     "state": "verified"}]
        claims_b = [{"title": "存活材料", "material": {"locator": {"ref": "docs/note.md"},
                                                    "role": "task-spec",
                                                    "snapshot_sha256": unchanged},
                     "state": "verified"}]
        gens = []
        for name, claims, keys in (("asset-old1", claims_a, ["codex", "a", "任务书"]),
                                   ("asset-old2", claims_b, ["codex", "a", "任务书"])):
            folder = base / name
            folder.mkdir()
            payload = {"schema": 1, "kind": "GROMA_LOCAL_CONTEXT_ASSET", "asset_id": name,
                       "title": name, "status": "draft", "generated_at": "2026-11-22T00:00:00Z",
                       "workspace_name": "K4工作区", "workspace_ref": "wsref",
                       "unit_id": self.unit_id, "org_path": None, "workspace_count": 1,
                       "digest": "d", "digest_sha256": "0" * 64, "digests": [],
                       "totals": {"sessions": 1, "events": 1}, "agents": [],
                       "sessions": [], "claims": claims, "profile": "claim-anchored",
                       "notes": "", "content_stored": False}
            (folder / "asset.json").write_text(json.dumps(payload, ensure_ascii=False),
                                               encoding="utf-8", newline="\n")
            gens.append(folder)
        return gens

    def test_check_mode_writes_nothing(self) -> None:
        from lab.asset import current_asset_dir

        code, out = self._run_cli("asset", "build", "--title", "预演", "--session",
                                  "codex:sessions/a.jsonl", "--check")
        self.assertEqual(code, 0)
        self.assertTrue(out["planned"])
        self.assertIn("预演", out["note"])
        self.assertFalse(current_asset_dir(self.ws, self.unit_id).exists(), "--check 落盘了")

    def test_migrate_without_yes_only_prints_plan(self) -> None:
        gens = self._seed_generations()
        code, out = self._run_cli("asset", "migrate-singleton", "--unit", self.unit_id)
        self.assertEqual(code, 0)
        self.assertFalse(out["applied"])
        self.assertIn("--yes", out["note"])
        self.assertEqual(out["candidate_claims"], 2)
        self.assertEqual(out["delete_dirs"], 2)
        for folder in gens:
            self.assertTrue(folder.is_dir(), "没带 --yes 却删了历史代")

    def test_migrate_with_yes_merges_dedupes_and_deletes(self) -> None:
        from lab.asset import current_asset_dir

        gens = self._seed_generations()
        code, out = self._run_cli("asset", "migrate-singleton", "--unit", self.unit_id, "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(out["applied"])
        # 三条 claims 里：漂移材料失效（material_drift），存活材料×2 去重后只留 1。
        self.assertEqual(out["merged_claims"], 1)
        self.assertEqual(out["skipped_claims"], {"material_drift": 1})
        self.assertEqual(out["removed_dirs"], 2)
        slot = current_asset_dir(self.ws, self.unit_id) / "asset.json"
        self.assertTrue(slot.is_file(), "单槽没写出来")
        titles = [c.get("title") for c in json.loads(slot.read_text(encoding="utf-8"))["claims"]]
        self.assertEqual(titles, ["存活材料"], "合并集不对")
        for folder in gens:
            self.assertFalse(folder.exists(), "历史代没删干净")
        self.assertEqual(list((self.ws / ".groma" / "assets").glob("asset-*")), [])

    def test_build_default_overwrites_slot_and_skips_same_fingerprint(self) -> None:
        code, first = self._run_cli("asset", "build", "--title", "K4", "--session",
                                    "codex:sessions/a.jsonl")
        self.assertEqual(code, 0)
        code, again = self._run_cli("asset", "build", "--title", "K4", "--session",
                                    "codex:sessions/a.jsonl")
        self.assertEqual(code, 0)
        self.assertEqual(first["asset_id"], again["asset_id"])
        self.assertTrue(again.get("unchanged"), "同指纹没提示没变不重铸")


class MigrationBaseTests(unittest.TestCase):
    """迁移基代选择：目录名字母序 ≠ 时间序——基代按
    当前槽 > reforges 账本最新笔 > generated_at 最新 选，报告写明 base_generation/base_reason。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-u12-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = base / "unit"
        self.ws.mkdir()
        init_workspace(self.ws, name="基代工作区", owner="tester")
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "note.md").write_text("材料文件正文\n", encoding="utf-8", newline="\n")
        env = mock.patch.dict(os.environ, {
            "GROMA_STARTUP_DIR": str(base / "startup"),
            "GROMA_STATE_DIR": str(base / "state"),
        })
        env.start()
        self.addCleanup(env.stop)
        from lab import topics as topics_mod

        self.unit_id = unitmatch.create_unit("基代主题", self.ws)["unit_id"]
        topics_mod.create_topic("基代主题")

    def _run_cli(self, *argv: str) -> tuple[int, dict]:
        import contextlib
        import io

        from lab import cli as cli_mod

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cli_mod.main(["--root", str(self.ws)] + list(argv))
        text = buffer.getvalue()
        return code, (json.loads(text) if text.strip() else {})

    def _gen(self, name: str, generated_at: str, title: str, claims=None) -> Path:
        folder = self.ws / ".groma" / "assets" / name
        folder.mkdir(parents=True)
        payload = {"schema": 1, "kind": "GROMA_LOCAL_CONTEXT_ASSET", "asset_id": name,
                   "title": title, "status": "draft", "generated_at": generated_at,
                   "workspace_name": "基代工作区", "workspace_ref": "wsref",
                   "unit_id": self.unit_id, "org_path": None, "workspace_count": 1,
                   "digest": "d", "digest_sha256": "0" * 64, "digests": [],
                   "totals": {"sessions": 1, "events": 1}, "agents": [], "sessions": [],
                   "claims": claims or [], "profile": "claim-anchored",
                   "notes": "", "content_stored": False}
        (folder / "asset.json").write_text(json.dumps(payload, ensure_ascii=False),
                                           encoding="utf-8", newline="\n")
        return folder

    def _material_claim(self, title: str) -> dict:
        import hashlib

        return {"title": title, "state": "verified",
                "material": {"locator": {"ref": "docs/note.md"}, "role": "task-spec",
                             "snapshot_sha256": hashlib.sha256(
                                 (self.ws / "docs" / "note.md").read_bytes()).hexdigest()}}

    def test_base_is_newest_generated_at_not_alphabetical_last(self) -> None:
        """①字母序末位=最旧；最新代持唯一 claims → 槽取最新代，base_reason=newest-generated-at。"""
        # sorted() 升序：0001mid < 1111newest < ff00oldest ⇒ 字母序末位=ff00oldest（最旧）
        self._gen("asset-0001mid", "2026-11-01T00:00:00Z", "中代")
        self._gen("asset-ff00oldest", "2026-10-30T00:00:00Z", "旧草稿")
        self._gen("asset-1111newest", "2026-11-20T00:00:00Z", "服役资产",
                  claims=[self._material_claim("唯一材料")])
        self.assertGreater("asset-ff00oldest", "asset-1111newest")
        code, out = self._run_cli("asset", "migrate-singleton", "--unit", self.unit_id, "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(out["base_reason"], "newest-generated-at")
        self.assertEqual(out["base_generation"], "asset-1111newest")
        slot = json.loads((self.ws / ".groma" / "assets" / "current" / "asset.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(slot["asset_id"], "asset-1111newest", "槽取了字母序末位（最旧代）")
        self.assertEqual(slot["title"], "服役资产")
        self.assertEqual([c["title"] for c in slot["claims"]], ["唯一材料"])
        self.assertEqual(list((self.ws / ".groma" / "assets").glob("asset-*")), [])

    def test_reforge_ledger_outranks_generated_at(self) -> None:
        """②账本最新笔指向非最新 generated_at 的代 → 账本优先，base_reason=reforge-ledger。"""
        from lab import topics as topics_mod

        self._gen("asset-0001mid", "2026-11-10T00:00:00Z", "账本指向的代")
        self._gen("asset-1111newest", "2026-11-20T00:00:00Z", "时间最新但账本没写它")
        ledger = topics_mod._reforges_path()
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"topic_id": self.unit_id, "asset_id": "asset-0001mid",
                                      "sessions": 1, "at": "2026-11-10T00:05:00Z"},
                                     ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        code, out = self._run_cli("asset", "migrate-singleton", "--unit", self.unit_id, "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(out["base_reason"], "reforge-ledger")
        self.assertEqual(out["base_generation"], "asset-0001mid")
        slot = json.loads((self.ws / ".groma" / "assets" / "current" / "asset.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(slot["asset_id"], "asset-0001mid", "账本口径没优先于 generated_at")


if __name__ == "__main__":
    unittest.main()

