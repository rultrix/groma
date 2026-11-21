"""W29 · 重铸的身份与作用域：成员区发起、源改名、退出码保真。

三件事各自有独立的失败面：
* 从成员区跑 `topic maintain` 时，资产构建拿不到那个工作区的身份卡（成员目录没有
  `.groma/`）——重铸必须先归到主题的家；
* 采集源名是用户可改的显示名，却被当成证据键的一半——源一改名，账本里的旧键
  在 digest 里精确匹配不上，而存活判据按会话 id 说它"活"；
* wrapper 最后一条是 `echo`，计划任务的 Last Result 从此恒 0——日志里的红点
  在调度器里变成假绿。
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lab import asset, classify_run, schedule, state, topics, unitmatch
from lab.digest import normalise_session_id
from lab.workspace import init_workspace, load_unit

SESSION_RENAMED = "2f130a4e-0000-4000-8000-000000000001"
SESSION_PLAIN = "0abc585f-0000-4000-8000-000000000002"
SESSION_MEMBER = "52742f1c-0000-4000-8000-000000000003"


def row(session_id: str, source: str = "qoder", harness: str = "qoder-cn") -> dict:
    return {"source": source, "path": "db/" + session_id,
            "session_key": "%s:%s" % (harness, session_id), "harness": harness,
            "agent_kind": "primary", "events": 42,
            "first_ts": "2026-11-01T00:00:00Z", "last_ts": "2026-11-02T00:00:00Z"}


def write_digest(root: Path, rows: list[dict]) -> Path:
    folder = root / ".groma" / "digests"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1, "kind": "GROMA_LOCAL_DIGEST", "created_at": "2026-11-20T03:29:13Z",
        "workspace_root": root.as_posix(), "file_count": len(rows),
        "event_count": sum(item.get("events") or 0 for item in rows),
        "parse_errors": 0, "files": rows,
    }
    path = folder / "digest-20261120T032913Z.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


def asset_sha(root: Path) -> str | None:
    files = sorted((root / ".groma" / "assets").glob("*/asset.json"))
    return hashlib.sha256(files[-1].read_bytes()).hexdigest() if files else None


class TwoWorkspaces(unittest.TestCase):
    """真机形状：家在工作树 A；成员区 B 自带一张卡（它先被 init 过，后来才并入 A 的主题）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        before = os.environ.get(state.STATE_ENV)
        self.addCleanup(lambda: os.environ.pop(state.STATE_ENV, None) if before is None
                        else os.environ.__setitem__(state.STATE_ENV, before))
        os.environ[state.STATE_ENV] = str((Path(self.tmp.name) / "state").resolve())
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        init_workspace(self.home, name="ctx-parser", owner="tester")
        self.member = Path(self.tmp.name) / "member"
        self.member.mkdir()
        init_workspace(self.member, name="member-self", owner="tester")
        unitmatch.join_unit(load_unit(self.home)["unit_id"], self.member,
                            role=unitmatch.ROLE_EVIDENCE, authorized_by="w29-tester")


class MemberOriginTests(TwoWorkspaces):
    def test_maintaining_from_a_member_reports_not_home_and_writes_nothing(self) -> None:  # A-1（W29c）
        write_digest(self.home, [row(SESSION_PLAIN)])
        write_digest(self.member, [row(SESSION_MEMBER)])  # 成员区也真的在采，只是采的是别人的
        topic = topics.create_topic("ctx-parser")
        topics.select(topic["topic_id"],
                      [{"source": "qoder", "path": "db/" + SESSION_PLAIN}],
                      decided_by="human", known_keys={("qoder", "db/" + SESSION_PLAIN)})
        before = asset_sha(self.home)
        result = classify_run.maintain(self.member)
        steps = {step["topic"]: step for step in result["steps"]}
        self.assertEqual(steps["ctx-parser"]["state"], "not_home")
        self.assertFalse((self.member / ".groma" / "assets").exists(), "成员区长出了资产")
        self.assertEqual(asset_sha(self.home), before, "家区资产被从成员区改写了")


class RenamedSourceTests(TwoWorkspaces):
    def _renamed_topic(self) -> dict:
        write_digest(self.home, [row(SESSION_RENAMED, source="qoder-projects")])
        topic = topics.create_topic("ctx-parser")
        topics.select(topic["topic_id"],
                      [{"source": "qoder", "path": "db/" + SESSION_RENAMED}],
                      decided_by="human",
                      known_keys={("qoder", "db/" + SESSION_RENAMED)})
        return topic

    def test_a_renamed_source_resolves_by_session_identity(self) -> None:  # A-2（W29a）
        topic = self._renamed_topic()
        built = topics.reforge(self.home, topic["topic_id"])
        self.assertIsNotNone(built["asset_id"])
        loaded = asset.load_asset(self.home, built["asset_id"])
        self.assertEqual([item["source"] for item in loaded["sessions"]], ["qoder-projects"],
                         "引用里没写 digest 的现标签")

    def test_both_filters_keep_the_same_sessions(self) -> None:  # A-4（W29a）
        topic = self._renamed_topic()
        selection, _ = topics.evidence_split(topic["topic_id"])
        built = topics.reforge(self.home, topic["topic_id"])
        loaded = asset.load_asset(self.home, built["asset_id"])
        cited = {normalise_session_id(Path(item["path"]).name) for item in loaded["sessions"]}
        kept = {normalise_session_id(Path(path).name) for _, path in selection}
        self.assertEqual(cited, kept, "存活判据与引用判据不是同一个分母")


class WrapperExitCodeTests(unittest.TestCase):
    """真的把 wrapper 跑起来：退出码要么等于最后一步，要么这刀没修好。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        before = os.environ.get(state.STATE_ENV)
        self.addCleanup(lambda: os.environ.pop(state.STATE_ENV, None) if before is None
                        else os.environ.__setitem__(state.STATE_ENV, before))
        os.environ[state.STATE_ENV] = str((Path(self.tmp.name) / "state").resolve())
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        init_workspace(self.root, name="wrap-unit", owner="tester")

    def _run(self, path: Path) -> subprocess.CompletedProcess:
        return subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/c", str(path)],
                              cwd=str(self.root), capture_output=True, text=True, errors="replace")

    def _failure_wrapper(self) -> Path:
        if os.name != "nt":
            self.skipTest("wrapper 的退出码形状按 cmd.exe 写")
        path = schedule.write_wrapper(self.root, kind="classify")
        text = path.read_text(encoding="utf-8")
        replaced = []
        for line in text.splitlines():
            if "topic maintain" in line:
                line = 'cmd /c exit 5 >>"%GROMA_LOG%" 2>&1'
            replaced.append(line)
        path.write_text("\r\n".join(replaced) + "\r\n", encoding="utf-8", newline="")
        return path

    def test_a_failing_step_becomes_the_process_exit_code(self) -> None:  # A-3（W29d）
        done = self._run(self._failure_wrapper())
        self.assertEqual(done.returncode, 5, "计划任务会把这次失败记成 %d" % done.returncode)

    def test_a_successful_step_still_exits_zero(self) -> None:  # B-1
        path = schedule.write_wrapper(self.root, kind="classify")
        done = self._run(path)
        self.assertEqual(done.returncode, 0, (done.stdout or "") + (done.stderr or ""))


if __name__ == "__main__":
    unittest.main()
