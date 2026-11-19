"""Import this from every test module: keeps the whole run out of the real dirs.

`lab.state` reads GROMA_STATE_DIR lazily, so setting it at import time is enough.
Without this, any test that calls `init_workspace` (which registers the folder)
or `register_workspace` writes straight into the developer's own registry.
The vault (`lab/vault.py`) resolves its default off `state_base()`, not the
sandboxed `state_root()`, so `GROMA_VAULT_DIR` has to be set here as well -- otherwise
`vault_root()` / `reindex()` / `_bake_observe_html()` read and write the real
`%LOCALAPPDATA%\Groma\vault`.
"""

from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

_box = tempfile.TemporaryDirectory(prefix="wb-test-state-")
_ROOT = Path(_box.name)

os.environ["GROMA_STATE_DIR"] = str(_ROOT / "state")
os.environ["GROMA_STARTUP_DIR"] = str(_ROOT / "startup")
# Vault escapes state_base(), not state_root(), so GROMA_STATE_DIR alone does not sandbox it.
os.environ["GROMA_VAULT_DIR"] = str(_ROOT / "vault")

atexit.register(_box.cleanup)
