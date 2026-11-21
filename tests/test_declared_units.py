"""`units.json` 每行都得自报来源：机器折身份卡的写 `card`，人建的写 `human`。

Owner 2026-11-20 的口径是「人不主动建立就不应该有 Unit」。界面上"算不算协作主题"已经收在
`unitmatch.is_theme()` 一处判（那格见 `test_web.py` 里那批），但今天这条账里**没有来源这个事实**——
谓词只能靠"没有 `origin` 又没有主题行"去推。这里钉的是把推断变成账。

另一头也要钉住：这一刀**只标来源，不缩折叠范围**。一张遗留的卡让目录"重开即复活"是
已拍并实现过的形状，要关掉它得另立一刀（代价与判据见本文件最后那条）。
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab import state, topics, unitmatch
from lab.workspace import LabError, init_workspace


class LedgerOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _folder(self, name: str) -> Path:
        folder = self.base / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _row(self, unit_id: str) -> dict:
        return next(item for item in state.list_units() if item["unit_id"] == unit_id)

    def _rewrite(self, file_name: str, keep) -> None:
        path = state.state_root() / file_name
        data = json.loads(path.read_text(encoding="utf-8"))
        key = "workspaces" if file_name == state.WORKSPACES else "units"
        data[key] = keep(data[key])
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_a_folded_identity_card_says_the_machine_made_it(self) -> None:  # 第一入口
        home = self._folder("card-ws")
        init_workspace(home, name="card-ws", owner="tester")
        state.register_workspace(home, name="card-ws")
        unit_id = unitmatch.folder_unit(home)
        unitmatch.sync_units()
        self.assertEqual("card", self._row(unit_id).get("origin"),
                         "机器从身份卡折出来的行没说自己是怎么来的 ⇒ 界面只能靠推")

    def test_a_registry_row_without_a_card_says_the_machine_made_it(self) -> None:  # 第二入口
        """卡之外还有这一支：注册表行自己带 `unit_id`（本仓 `workspaces.json` 今天就是这形）。

        只标一支等于没标——那张卡没了，注册表行照样能把一条没人建过的记录折出来。
        """
        home = self._folder("registry-ws")
        state.register_workspace(home, name="registry-ws")
        self._rewrite(state.WORKSPACES, lambda rows: [
            dict(row, unit_id="unit_registryonly1") if row.get("name") == "registry-ws" else row
            for row in rows])
        unitmatch.sync_units()
        self.assertEqual("card", self._row("unit_registryonly1").get("origin"))

    def test_a_folded_membership_row_says_the_machine_made_it(self) -> None:  # 第三入口
        home = self._folder("member-ws")
        init_workspace(home, name="member-ws", owner="tester")
        unit_id = unitmatch.create_unit("成员行折回来", home)["unit_id"]
        topics.create_topic("成员行折回来")
        member = self._folder("member-2")
        state.add_membership(unit_id, state.workspace_ref(member), role="evidence",
                             signal="test", source="test")
        self._rewrite(state.UNITS, lambda rows: [r for r in rows if r["unit_id"] != unit_id])
        unitmatch.sync_units()
        self.assertEqual("card", self._row(unit_id).get("origin"))
        self.assertTrue(unitmatch.is_theme(self._row(unit_id)),
                        "有主题行的那个折回来后仍要算协作主题（标源不许改判）")

    def test_a_theme_built_by_hand_says_a_human_made_it(self) -> None:  # 反向半边
        home = self._folder("human-ws")
        init_workspace(home, name="human-ws", owner="tester")
        unit_id = unitmatch.create_unit("人建的主题", home)["unit_id"]
        self.assertEqual("human", self._row(unit_id).get("origin"))
        self.assertTrue(unitmatch.is_theme(self._row(unit_id)))

    def test_a_home_chosen_by_a_person_is_the_human_kind_of_call(self) -> None:
        """指定家有两条路：人指着它当家，和"工作区被移除后换个家"。只有前者算人建的。"""
        home = self._folder("home-ws")
        init_workspace(home, name="home-ws", owner="tester")
        unit_id = unitmatch.create_unit("要换家的", home)["unit_id"]
        self._rewrite(state.UNITS, lambda rows: [
            {k: v for k, v in row.items() if k != "origin"} if row["unit_id"] == unit_id else row
            for row in rows])
        unitmatch.set_home(unit_id, self._folder("home-ws-2"))
        self.assertIsNone(self._row(unit_id).get("origin"),
                          "内部那处换家把一条没有来源的行凭空标成了人建的")
        unitmatch.set_home(unit_id, self._folder("home-ws-3"), by_human=True)
        self.assertEqual("human", self._row(unit_id).get("origin"))

    def test_syncing_never_relabels_what_a_human_made(self) -> None:
        home = self._folder("keep-ws")
        init_workspace(home, name="keep-ws", owner="tester")
        unit_id = unitmatch.create_unit("保住人建标记", home)["unit_id"]
        unitmatch.sync_units()
        unitmatch.sync_units()
        self.assertEqual("human", self._row(unit_id).get("origin"))

    def test_a_leftover_card_still_revives_its_unit(self) -> None:
        """**这条钉的是"没有多挡一道闸门"**：没有主题行的遗留卡，重开目录仍折得回来。

        挡折会让 `units.json` 更干净，但会把"重开即复活"这条已拍的形状关掉，所以本刀只标来源。
        谁哪天给折叠加谓词闸门，这条会先红——那时请连复活判据一起重新拍，别让它静默变味。
        """
        home = self._folder("revive-ws")
        init_workspace(home, name="revive-ws", owner="tester")
        state.register_workspace(home, name="revive-ws")
        unit_id = unitmatch.folder_unit(home)
        unitmatch.sync_units()
        self._rewrite(state.UNITS, lambda rows: [r for r in rows if r["unit_id"] != unit_id])
        unitmatch.sync_units()
        self.assertIn(unit_id, [item["unit_id"] for item in state.list_units()])


class RetireTests(unittest.TestCase):
    """「退成纯工作区」这个出口：改的是来源，不是删数据。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.env = mock.patch.dict(os.environ, {"GROMA_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home = self.base / "shop"
        self.home.mkdir()
        init_workspace(self.home, name="要退的主题", owner="tester")
        self.unit_id = unitmatch.create_unit("要退的主题", self.home)["unit_id"]
        topics.create_topic("要退的主题", intent=None)
        topics.set_intent("要退的主题", "只看这个仓的重构讨论", decided_by="human")

    def _row(self, unit_id: str = "") -> dict:
        wanted = unit_id or self.unit_id
        return next(item for item in state.list_units() if item["unit_id"] == wanted)

    def test_retiring_keeps_the_card_the_row_and_the_intent(self) -> None:
        """A：退完之后，能丢的东西一样都不能丢——这条是本动词与「删除协作主题」的分界。"""
        unitmatch.retire_to_workspace(self.unit_id)
        self.assertEqual("retired", self._row().get("origin"),
                         "退过要写成「人退的」，写成 card 会被谓词的存量那一支重新认成主题")
        self.assertTrue((self.home / "groma-unit.json").is_file(), "身份卡被动了")
        self.assertIn(self.home.as_posix(),
                      [row["path"] for row in state.list_workspaces()], "注册表行被动了")
        self.assertEqual("只看这个仓的重构讨论", topics.topic_record(self.unit_id)["intent"],
                         "用户写的那句判据被退了就没回来了")

    def test_a_retired_theme_is_no_longer_a_theme(self) -> None:
        unitmatch.retire_to_workspace(self.unit_id)
        self.assertNotIn(self.unit_id, [item["unit_id"] for item in unitmatch.themes()])
        self.assertFalse(unitmatch.is_theme(self._row()))

    def test_reclaiming_the_same_name_makes_it_a_theme_again(self) -> None:
        """可逆性：在那个区上重新建一次同名主题，应落回同一个身份并标成人建的。"""
        unitmatch.retire_to_workspace(self.unit_id)
        again = unitmatch.create_unit("要退的主题", self.home)
        self.assertEqual(self.unit_id, again["unit_id"], "重新认领铸了新身份 ⇒ 之前的资产与摘要成了孤儿")
        self.assertEqual("human", self._row(self.unit_id).get("origin"))

    def test_retiring_something_that_is_not_a_theme_is_refused(self) -> None:
        """空态与多元素态各钉一次：已经退过的、以及压根没建过的。"""
        unitmatch.retire_to_workspace(self.unit_id)
        with self.assertRaises(LabError):
            unitmatch.retire_to_workspace(self.unit_id)
        with self.assertRaises(LabError):
            unitmatch.retire_to_workspace("unit_never_seen")

    def test_the_face_stops_calling_a_retired_theme_a_topic(self) -> None:
        """装载面与界面读同一个判断：退过之后那句主语回到「本机登记」。

        说明是刻意留着的（不丢用户写的字），所以只看 `topics.json` 有没有行会把一个已退的主题
        继续报成协作主题——那等于第五处自己写了一份过滤。
        """
        from lab import surface

        self.assertIn("协作主题：要退的主题", surface.scope_phrase(self._row()))
        unitmatch.retire_to_workspace(self.unit_id)
        self.assertIn("本机登记：要退的主题", surface.scope_phrase(self._row()),
                      "主题页已经不列它了，装载面还在替它报协作主题")

    def test_syncing_never_relabels_what_a_human_retired(self) -> None:
        """退过的标记不许被下一次折卡洗掉——洗掉就等于把那句留着的选择重新认成主题。"""
        unitmatch.retire_to_workspace(self.unit_id)
        unitmatch.sync_units()
        unitmatch.sync_units()
        self.assertEqual("retired", self._row().get("origin"))
        self.assertFalse(unitmatch.is_theme(self._row()))

    def test_the_nightly_pass_skips_a_retired_theme(self) -> None:
        """A（本动词真正管用那格）：退完之后夜里不再为它跑归类——那是花额度的事。

        说明留着是刻意的（不丢用户写的字），所以"不再跑"不能靠删数据实现，
        只能靠归类那条链与界面读同一个判断。这里把 `run_once` 换成探针，数它被叫了几次。
        """
        from lab import classify_run

        seen: list[str] = []
        with mock.patch.object(classify_run, "run_once",
                               side_effect=lambda root, name, **kw: seen.append(name) or {
                                   "state": "applied", "added": 0}):
            classify_run.run_all(self.home)
        self.assertIn("要退的主题", seen, "对照组就不该是空的——这条测试的探针没接上")
        seen.clear()
        unitmatch.retire_to_workspace(self.unit_id)
        with mock.patch.object(classify_run, "run_once",
                               side_effect=lambda root, name, **kw: seen.append(name) or {
                                   "state": "applied", "added": 0}):
            classify_run.run_all(self.home)
        self.assertEqual([], seen, "退成纯工作区之后，夜里还会为它跑一次归类")


if __name__ == "__main__":
    unittest.main()
