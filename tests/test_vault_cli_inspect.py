"""R3：`groma vault inspect` 的分片数必须与索引顶层 `shard_count` 同口径。

索引顶层**没有** `shards` 键（`lab/vault.py` 把 `shard_count` 建成对 sessions 的求和），
所以读 `index.get("shards", [])` 的那半边永远得 0——界面已改对，CLI 这半边是残留。
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import statebox  # noqa: F401  (把整轮跑导到沙箱目录)

from lab import vault
from lab.cli import _vault

from test_vault_index import record

KEY = "qoder:11111111-2222-3333-4444-555555555555"


class InspectShardCount(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name).resolve() / "vault"
        self.root.mkdir(parents=True)
        self.addCleanup(self._dir.cleanup)
        saved = os.environ.get(vault.VAULT_ENV)
        os.environ[vault.VAULT_ENV] = str(self.root)
        self.addCleanup(lambda: self._restore(saved))

    def _restore(self, saved):
        if saved is None:
            os.environ.pop(vault.VAULT_ENV, None)
        else:
            os.environ[vault.VAULT_ENV] = saved

    def inspect(self):
        return _vault(SimpleNamespace(vault_command="inspect", session=None))

    def test_inspect_reports_the_index_top_level_shard_count(self) -> None:
        vault.append_records(self.root, "qoder", KEY, [record(1), record(2)])
        vault.append_records(self.root, "qoder", KEY, [record(3)])
        index = vault.reindex(self.root)
        self.assertEqual(index["shard_count"], 2)

        report = self.inspect()
        self.assertEqual(report["shard_count"], index["shard_count"])
        self.assertEqual(report["session_count"], index["session_count"])
