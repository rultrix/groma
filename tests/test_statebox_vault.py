"""statebox must sandbox the vault too -- not just state/startup dirs.

Without this, any test that calls `vault_root()` / `reindex()` / `_bake_observe_html()`
reads and writes the developer's real `%LOCALAPPDATA%\\Groma\\vault` (~1.8 GB / 249 shards).
`lab.vault.default_root()` builds off `state_base()` (not `state_root()`), so redirecting
`GROMA_STATE_DIR` alone does *not* move the vault -- `GROMA_VAULT_DIR` has to be set explicitly.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import statebox  # noqa: F401  -- must be first real import
from lab import vault


class StateboxVaultSandboxTest(unittest.TestCase):
    def test_vault_root_lands_under_the_tempdir(self) -> None:
        root = vault.vault_root()
        self.assertTrue(
            str(root).lower().startswith(tempfile.gettempdir().lower()),
            "vault_root escaped the sandbox: %s (should be under %s)" % (root, tempfile.gettempdir()),
        )

    def test_vault_root_is_not_the_real_default(self) -> None:
        root = vault.vault_root()
        self.assertNotEqual(root, vault.default_root(),
                            "vault_root still equals the per-user default: %s" % root)

    def test_env_override_still_wins(self) -> None:
        import os
        saved = os.environ.get("GROMA_VAULT_DIR")
        fake = Path(tempfile.mkdtemp()) / "explicit-vault"
        os.environ["GROMA_VAULT_DIR"] = str(fake)
        try:
            self.assertEqual(vault.vault_root(), fake)
        finally:
            if saved is None:
                os.environ.pop("GROMA_VAULT_DIR", None)
            else:
                os.environ["GROMA_VAULT_DIR"] = saved
            self.assertTrue(
                str(vault.vault_root()).lower().startswith(tempfile.gettempdir().lower()),
                "vault_root escaped after restore: %s" % vault.vault_root(),
            )


if __name__ == "__main__":
    unittest.main()
