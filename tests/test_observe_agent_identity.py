"""R19 / B：子 Agent 算独立会话——观测页的身份来自会话自己，不是合成 id。

A 类判据：打在 0.50.0（两处 `agt-<harness>-default`）上必须红。
"""

from __future__ import annotations

import json
import statebox  # noqa: F401  -- must land before any lab import touches state
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lab.agents import default_agent, derived_sub_agent

try:
    from web import app as web_app

    HAVE_FLASK = True
except ImportError:  # pragma: no cover - depends on the venv
    HAVE_FLASK = False


def build_rows(sessions, **kwargs):
    # 延迟导入：未修版本没有这个函数，测试要能跑到断言而不是 ImportError。
    from lab.observe_this_machine import digest_rows_from_index

    return digest_rows_from_index(sessions, **kwargs)


SESSIONS = [
    {"harness": "qoder-cn", "session_key": "qoder-cn:primary-1",
     "shards": [{"records": 3}]},
    {"harness": "qoder-cn", "session_key": "qoder-cn:sub-1",
     "shards": [{"records": 5}]},
    {"harness": "codex", "session_key": "codex:rollout-1",
     "shards": [{"records": 2}]},
]


class SessionIdentity(unittest.TestCase):
    def test_a_qoder_subagent_keeps_its_own_agent(self) -> None:
        rows = build_rows(
            SESSIONS, registry=[], sub_types={"sub-1": "CodeReview"})
        by_key = {row["session_key"]: row for row in rows}
        sub = by_key["qoder-cn:sub-1"]
        expected = derived_sub_agent("qoder-cn", "CodeReview")
        self.assertEqual(sub["agent_id"], expected["agent_id"])
        self.assertEqual(sub["agent_kind"], "sub")
        self.assertEqual(sub["parent_agent_id"], default_agent("qoder-cn")["agent_id"])
        primary = by_key["qoder-cn:primary-1"]
        self.assertEqual(primary["agent_id"], default_agent("qoder-cn")["agent_id"])
        self.assertEqual(primary["agent_kind"], "primary")

    def test_a_registered_subagent_name_wins(self) -> None:
        registered = derived_sub_agent("qoder-cn", "qoder-ctx-parser")
        rows = build_rows(
            SESSIONS, registry=[registered], sub_types={"sub-1": "qoder-ctx-parser"})
        sub = next(row for row in rows if row["session_key"] == "qoder-cn:sub-1")
        self.assertEqual(sub["agent_id"], registered["agent_id"])

    def test_no_row_carries_the_synthetic_harness_default(self) -> None:
        rows = build_rows(
            SESSIONS, registry=[], sub_types={"sub-1": "CodeReview"})
        synthetic = {"agt-%s-default" % row["harness"] for row in rows}
        for row in rows:
            self.assertNotIn(row["agent_id"], synthetic)
            self.assertTrue(row["agent_id"].startswith("agt-"))

    def test_other_harnesses_resolve_to_their_primary(self) -> None:
        rows = build_rows(SESSIONS, registry=[], sub_types={})
        codex = next(row for row in rows if row["harness"] == "codex")
        self.assertEqual(codex["agent_id"], default_agent("codex")["agent_id"])
        self.assertEqual(codex["agent_kind"], "primary")


class CallSitesUseOneResolver(unittest.TestCase):
    def test_neither_call_site_bakes_an_agent_id(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        targets = [repo / "web" / "app.py", repo / "lab" / "cli.py"]
        for path in targets:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn('"agt-%s-default"', source)
            self.assertIn("digest_rows_from_index", source)


@unittest.skipUnless(HAVE_FLASK, "Flask not installed")
class BakedPage(unittest.TestCase):
    def _fake_vault(self, sessions):
        self._dir = tempfile.TemporaryDirectory()
        vault = Path(self._dir.name)
        index = {"schema": 1, "kind": "GROMA_VAULT_INDEX",
                 "generated_at": "2026-11-17T00:00:00Z",
                 "session_count": len(sessions), "shard_count": 1,
                 "sessions": sessions}
        (vault / "index.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8")
        return vault

    def test_the_baked_page_receives_the_real_subagent(self) -> None:
        vault = self._fake_vault([
            {"harness": "qoder-cn", "session_key": "qoder-cn:sub-1",
             "shards": [{"records": 5}]},
        ])
        web_app._observe_html_path().parent.mkdir(parents=True, exist_ok=True)
        seen: list = []

        def fake_generate(vault_root, digest_rows, host_name, output_path=None):
            seen.extend(digest_rows)
            return "<!DOCTYPE html><html>ok</html>"

        with mock.patch("lab.vault.vault_root", return_value=vault), \
                mock.patch("lab.observe_this_machine.subagent_types",
                           return_value={"sub-1": "CodeReview"}, create=True), \
                mock.patch("lab.observe_this_machine.load_agents",
                           return_value=[], create=True), \
                mock.patch.object(web_app, "generate_observation_page", fake_generate):
            html, error = web_app._bake_observe_html()

        self.assertIsNone(error)
        self.assertTrue(html)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["agent_id"],
                         derived_sub_agent("qoder-cn", "CodeReview")["agent_id"])
        self.assertEqual(seen[0]["agent_kind"], "sub")


if __name__ == "__main__":
    unittest.main()
