"""Claim-anchored assets: human-supplied claims, located and hashed, never silent.

Vault side is synthetic (`append_records`, same shape as test_vault_shards);
nothing here reads a real harness transcript.
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must land before any lab import touches state
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lab import vault
from lab.asset import attach_claims, build_asset, find_asset, verify_claims
from lab.collect import catchup
from lab.inject import export_context
from lab.sources import add_source
from lab.workspace import LabError, init_workspace


KEY = "qoder:11111111-2222-3333-4444-555555555555"


def vault_record(line_no: int, text: str) -> dict:
    return {
        "ts_utc": "2026-11-10T08:%02d:00Z" % line_no,
        "granularity": "event",
        "session_key": KEY,
        "role": "user",
        "type": "message",
        "text": text,
        "reasoning": None,
        "tool": None,
        "strip": [],
        "agent_id": "agt-1",
        "source": {
            "path": "projects/x.jsonl",
            "line_no": line_no,
            "bytes": 128,
            "source_sha256": hashlib.sha256(b"%d" % line_no).hexdigest(),
        },
    }


def _tree_digest(root: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        sha.update(str(path.relative_to(root)).encode("utf-8"))
        sha.update(path.read_bytes())
    return sha.hexdigest()


class ClaimAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-asset-claims-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="引文资产")
        src = base / "src"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-10T09:00:00Z", "cwd": "%s"}\n' % self.home.as_posix(),
            encoding="utf-8", newline="\n",
        )
        add_source(self.home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(self.home, force=True)
        self.asset = build_asset(self.home, "引文试跑", [("qoder", "projects/s.jsonl")])
        self.asset_id = self.asset["asset_id"]
        # a synthetic vault session, in its own temp vault root
        self.vault_root = base / "vault"
        self.vault_root.mkdir()
        vault.append_records(self.vault_root, "qoder", KEY, [
            vault_record(1, "第一句：修复从注册表读数开始。"),
            vault_record(2, "第二句：安装器静默不落盘。"),
            vault_record(3, "第三句：收口时补了装机判据。"),
        ])
        vault.reindex(self.vault_root)
        self.claim = {
            "title": "安装器静默不落盘是真缺陷",
            "session_key": KEY,
            "from_ts": "2026-11-10T08:01:00Z",
            "to_ts": "2026-11-10T08:02:00Z",
        }

    def _asset_file(self) -> dict:
        return json.loads((find_asset(self.home, self.asset_id) / "asset.json").read_text(encoding="utf-8"))

    def test_a_claim_with_no_such_session_refuses_whole_and_writes_nothing(self) -> None:
        before = (find_asset(self.home, self.asset_id) / "asset.json").read_bytes()
        bad = dict(self.claim, session_key="qoder:dead0000-0000-0000-0000-000000000000")
        with self.assertRaises(LabError) as caught:
            attach_claims(self.home, self.asset_id, [self.claim, bad], vault_root=self.vault_root)
        # the good claim is refused too: one bad window means no half-verified asset
        self.assertIn("dead0000", str(caught.exception))
        self.assertEqual((find_asset(self.home, self.asset_id) / "asset.json").read_bytes(), before)
        self.assertIsNone(self._asset_file().get("claims"))
        self.assertEqual(self._asset_file().get("profile"), "index-only")

    def test_an_attached_claim_hashes_the_quote_itself_and_repeats_stably(self) -> None:
        out = attach_claims(self.home, self.asset_id, [self.claim], vault_root=self.vault_root)
        self.assertEqual(out["attached"], 1)
        claim = self._asset_file()["claims"][0]
        # records land at 08:01 / 08:02 / 08:03; the window 08:01→08:02 takes the first two
        expected = "第一句：修复从注册表读数开始。\n第二句：安装器静默不落盘。"
        self.assertEqual(claim["quote"], expected)
        self.assertEqual(claim["quote_sha256"],
                         hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertEqual(claim["state"], "verified")
        # the same span resolves to the same hash, twice
        again = attach_claims(self.home, self.asset_id, [self.claim], vault_root=self.vault_root)
        self.assertEqual(again["claims"], 1)  # replaced, not duplicated
        self.assertEqual(self._asset_file()["claims"][0]["quote_sha256"], claim["quote_sha256"])

    def test_attaching_never_touches_the_vault_or_the_asset_id(self) -> None:
        before = _tree_digest(self.vault_root)
        asset_id_before = self.asset_id
        attach_claims(self.home, self.asset_id, [self.claim], vault_root=self.vault_root)
        self.assertEqual(_tree_digest(self.vault_root), before)  # vault stays read-only
        self.assertEqual(self._asset_file()["asset_id"], asset_id_before)  # id derivation untouched
        self.assertEqual(self._asset_file()["profile"], "claim-anchored")

    def test_a_drifted_shard_degrades_the_claim_to_stale_and_records_it(self) -> None:
        attach_claims(self.home, self.asset_id, [self.claim], vault_root=self.vault_root)
        shard = self.vault_root / "qoder" / "11111111-2222-3333-4444-555555555555" / "s000001_20261110.ndjson"
        body = shard.read_bytes()
        shard.write_bytes(body + b"tampered\n")  # simulate source drift
        out = verify_claims(self.home, self.asset_id, vault_root=self.vault_root)
        self.assertEqual(out["stale"], [self.claim["title"]])
        validation = (find_asset(self.home, self.asset_id) / "VALIDATION.md").read_text(encoding="utf-8")
        self.assertIn("锚点核对", validation)
        self.assertIn("未核对（stale）", validation)
        # a stale claim is not rendered again
        result = export_context(self.home, self.asset_id)
        exported = Path(result["context"]).read_text(encoding="utf-8")
        self.assertNotIn("第二句：安装器静默不落盘。", exported)
        self.assertNotIn(self.claim["title"], exported)

    def test_export_carries_quotes_only_from_verified_claims(self) -> None:
        attach_claims(self.home, self.asset_id, [self.claim], vault_root=self.vault_root)
        result = export_context(self.home, self.asset_id)
        exported = Path(result["context"]).read_text(encoding="utf-8")
        self.assertIn("## 结论与引文", exported)
        self.assertIn(self.claim["title"], exported)
        self.assertIn("第二句：安装器静默不落盘。", exported)
        # every quote is preceded by its anchor line (session + span + hash head)
        claim = self._asset_file()["claims"][0]
        anchor = "> 锚点：`%s` · %s → %s · sha256 `%s`" % (
            KEY, claim["span_first_ts"], claim["span_last_ts"], claim["quote_sha256"][:12])
        self.assertIn(anchor, exported)

    def test_index_only_asset_and_export_stay_content_free(self) -> None:
        text = self._asset_file()
        self.assertIsNone(text.get("claims"))
        readme = (find_asset(self.home, self.asset_id) / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("安装器静默不落盘", readme)
        self.assertNotIn("第一句", readme)
        result = export_context(self.home, self.asset_id)
        exported = Path(result["context"]).read_text(encoding="utf-8")
        self.assertNotIn("结论与引文", exported)
        self.assertNotIn("安装器静默不落盘", exported)

    def test_quotes_live_only_in_the_claims_field_of_asset_json(self) -> None:
        attach_claims(self.home, self.asset_id, [self.claim], vault_root=self.vault_root)
        raw = (find_asset(self.home, self.asset_id) / "asset.json").read_text(encoding="utf-8")
        quote = "第二句：安装器静默不落盘。"
        self.assertEqual(raw.count(quote), 1)  # exactly its own claims[].quote slot
        folder = find_asset(self.home, self.asset_id)
        for name in ("README.md", "VALIDATION.md", "manifest.json"):
            self.assertNotIn(quote, (folder / name).read_text(encoding="utf-8"), name)

    def test_rebuilding_the_same_selection_keeps_the_same_asset_id(self) -> None:
        twin = build_asset(self.home, "引文试跑", [("qoder", "projects/s.jsonl")])
        self.assertEqual(twin["asset_id"], self.asset_id)

    def test_verify_without_claims_is_an_error_not_a_green_silence(self) -> None:
        with self.assertRaises(LabError):
            verify_claims(self.home, self.asset_id, vault_root=self.vault_root)

    def test_a_draft_can_be_deleted_whole_and_only_a_draft(self) -> None:
        from lab.asset import delete_asset

        folder = find_asset(self.home, self.asset_id)
        self.assertIsNotNone(folder)
        out = delete_asset(self.home, self.asset_id)
        self.assertTrue(out["removed"])
        self.assertFalse(folder.exists())
        with self.assertRaises(LabError):
            delete_asset(self.home, self.asset_id)  # already gone
        # a claim-anchored asset is still a draft -- deletable, quotes and all
        twin = build_asset(self.home, "可删的引文草稿", [("qoder", "projects/s.jsonl")])
        attach_claims(self.home, twin["asset_id"], [self.claim], vault_root=self.vault_root)
        delete_asset(self.home, twin["asset_id"])
        self.assertIsNone(find_asset(self.home, twin["asset_id"]))


if __name__ == "__main__":
    unittest.main()


class MaterialReferenceTests(unittest.TestCase):
    """材料活引用：指针不搬运；locator 红线延伸；snapshot 诚实账不阻断。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="wb-asset-material-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        init_workspace(self.home, name="材料资产")
        (self.home / "docs" / "tasks").mkdir(parents=True)
        (self.home / "docs" / "tasks" / "W42.md").write_text("任务书正文 v1\n", encoding="utf-8")
        src = self.home / "src-sessions"
        (src / "projects").mkdir(parents=True)
        (src / "projects" / "s.jsonl").write_text(
            '{"timestamp": "2026-11-10T09:00:00Z", "cwd": "%s"}\n' % self.home.as_posix(),
            encoding="utf-8", newline="\n")
        from lab.collect import catchup
        from lab.sources import add_source

        add_source(self.home, "qoder", str(src), "qoder-cn", ["projects"])
        catchup(self.home, force=True)
        self.asset = build_asset(self.home, "材料试跑", [("qoder", "projects/s.jsonl")])
        self.asset_id = self.asset["asset_id"]

    def _attach(self, claim: dict) -> dict:
        return attach_claims(self.home, self.asset_id, [claim])

    def test_a_material_claim_attaches_with_a_computed_snapshot(self) -> None:
        out = self._attach({"title": "任务书要求注入源升主题级",
                            "material": {"locator": {"scheme": "ws-rel", "ref": "docs/tasks/W42.md"},
                                         "role": "task-spec"}})
        self.assertEqual(out["attached"], 1)
        asset = json.loads((find_asset(self.home, self.asset_id) / "asset.json").read_text(encoding="utf-8"))
        claim = asset["claims"][0]
        self.assertEqual(claim["material"]["locator"]["scheme"], "ws-rel")
        self.assertEqual(claim["material"]["locator"]["ref"], "docs/tasks/W42.md")
        self.assertEqual(claim["material"]["role"], "task-spec")
        self.assertEqual(claim["material"]["snapshot_sha256"],
                         hashlib.sha256((self.home / "docs" / "tasks" / "W42.md").read_bytes()).hexdigest())
        self.assertEqual(asset["asset_id"], self.asset_id, "材料不参与 asset_id 推导")

    def test_the_locator_red_lines_refuse(self) -> None:
        """红线延伸：绝对路径 / URL / 凭据 / 越出工作区，全部拒，资产一个字节不改。"""
        for bad in ("C:/abs/path.md", "/abs/path.md", "https://x/y.md", "user@host/x.md", "../outside.md"):
            with self.assertRaises(LabError):
                self._attach({"title": "坏指针", "material": {"locator": {"scheme": "ws-rel", "ref": bad}}})
        with self.assertRaises(LabError):
            self._attach({"title": "坏 scheme", "material": {"locator": {"scheme": "org", "ref": "a.md"}}})
        asset = json.loads((find_asset(self.home, self.asset_id) / "asset.json").read_text(encoding="utf-8"))
        self.assertEqual(asset.get("claims") or [], [], "被拒的 claim 不许留半个")

    def test_a_claim_without_quote_or_material_is_invalid(self) -> None:
        """claim 无 quote 且无 material ⇒ 无效，整批拒。"""
        with self.assertRaises(LabError):
            self._attach({"title": "空 claim"})

    def test_snapshot_drift_is_an_honest_ledger_not_a_blocker(self) -> None:
        """snapshot 对不上：诚实账标记（material_drift），不阻断、不降级 stale、不阻止文件变化。"""
        self._attach({"title": "任务书", "material": {"locator": {"scheme": "ws-rel", "ref": "docs/tasks/W42.md"},
                                                      "role": "task-spec"}})
        (self.home / "docs" / "tasks" / "W42.md").write_text("任务书正文 v2\n", encoding="utf-8")
        result = verify_claims(self.home, self.asset_id)
        self.assertEqual(result["material_drift"], ["任务书"], "文件变了要记账")
        self.assertEqual(result["stale"], [])
        asset = json.loads((find_asset(self.home, self.asset_id) / "asset.json").read_text(encoding="utf-8"))
        self.assertEqual(asset["claims"][0]["state"], "verified", "指针不搬运，不降级")
        # 第二次核对（文件没再变）不再记新账
        result = verify_claims(self.home, self.asset_id)
        self.assertEqual(result["material_drift"], ["任务书"])
