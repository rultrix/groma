"""The harness we shell out to for classification: find it, prove which one it is.

Three facts from this machine shape this module:

* `qoderclicn` is not on PATH right after a fresh install (the shell keeps the
  PATH it started with), so known install locations are probed -- and whichever
  path wins is reported, never assumed.
* Inherited `QODER*` variables drag a normal invocation into SDK mode and it
  dies with `sdk_worker_removed`; clearing them is the difference between a
  working unattended tick and a silent one. A/B proven on this machine.
* The first line of `--help` is the only cheap identity proof available: the
  path can be right while the binary behind it is somebody else's.

Nothing here stores credentials, and nothing here calls a model API: Groma only
runs the harness the user already installed and logged in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .contract import LEGACY_PROVIDER_ENV, env_value

PROVIDER_ENV = "GROMA_QODERCLICN"
ENV_PREFIX = "QODER"
SIGNATURE = "Usage: qoderclicn"
CANDIDATES = (
    "~/.qoder-cn/bin/qoderclicn/qoderclicn.exe",
    "%LOCALAPPDATA%/Programs/Common/qoderclicn/qoderclicn.exe",
    "~/.qoder-cn/bin/qoderclicn/qoderclicn",
)


def scrubbed_env(base: dict | None = None) -> tuple[dict, list[str]]:
    """The environment a provider subprocess may see: no inherited QODER*.

    Returns the cleaned mapping and the names it dropped, so a receipt can say
    what was cleared instead of quietly sanitising.
    """
    env = dict(os.environ if base is None else base)
    removed = sorted(key for key in env if key.upper().startswith(ENV_PREFIX))
    for key in removed:
        env.pop(key, None)
    return env, removed


def candidates() -> list[str]:
    out: list[str] = []
    override = (env_value(PROVIDER_ENV, LEGACY_PROVIDER_ENV) or "").strip()
    if override:
        out.append(override)
    found = shutil.which("qoderclicn") or shutil.which("qoderclicn.exe")
    if found:
        out.append(found)
    for raw in CANDIDATES:
        path = Path(os.path.expandvars(raw)).expanduser()
        if path.is_file():
            out.append(str(path))
    deduped: list[str] = []
    for item in out:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _output(argv: list[str], timeout: int = 20) -> tuple[int, str]:
    env, _ = scrubbed_env()
    try:
        proc = subprocess.run(
            [str(item) for item in argv],
            capture_output=True, text=True, errors="replace", timeout=timeout, env=env,
        )
    except (FileNotFoundError, OSError):
        return 127, ""
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))


def first_line(argv: list[str], timeout: int = 20) -> tuple[int, str]:
    code, text = _output(argv, timeout)
    for line in text.splitlines():
        if line.strip():
            return code, line.strip()
    return code, ""


def check(path: str | None = None, signature: str | None = None) -> dict:
    """Locate the harness and prove its identity. States: ok / identity_mismatch / not_found."""
    tried = []
    for candidate in ([path] if path else candidates()):
        code, line = first_line([candidate, "--help"])
        tried.append({"path": candidate, "returncode": code, "first_line": line})
        wanted = (signature or SIGNATURE).lower()
        if code == 0 and wanted in line.lower():
            return {"state": "ok", "path": candidate, "first_line": line, "tried": tried,
                    "env_prefix_dropped": ENV_PREFIX}
        if code != 127:
            return {"state": "identity_mismatch", "path": candidate, "first_line": line,
                    "tried": tried, "env_prefix_dropped": ENV_PREFIX}
    return {"state": "not_found", "tried": tried, "env_prefix_dropped": ENV_PREFIX}


def run(argv: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """Run the provider with the scrubbed environment. Callers pass the provider last."""
    env, _ = scrubbed_env()
    return subprocess.run([str(item) for item in argv], capture_output=True, text=True,
                          errors="replace", timeout=timeout, env=env)


def models(path: str | None = None) -> dict:
    """The model names this account may pin with `-m`. Listing costs no tokens."""
    target = path or (check().get("path") or "")
    if not target:
        return {"state": "not_found", "models": []}
    code, text = _output([target, "--list-models"])
    names = [line.strip() for line in text.splitlines()
             if line.strip() and line.strip().upper() != "MODEL"]
    return {"state": "ok" if code == 0 else "unreadable", "models": names, "returncode": code}


# `qoderclicn status` prints the account's display name and avatar URL; neither
# belongs in Groma's records, so only these fields survive parsing.
STATUS_FIELDS = {"version": "Version", "user_type": "User Type",
                 "login_method": "Login Method", "auth_source": "Auth Source"}


def login_state(path: str | None = None) -> dict:
    """Whether the harness is logged in -- read from its own status, no model call, no tokens."""
    target = path or (check().get("path") or "")
    if not target:
        return {"state": "not_found", "logged_in": None}
    code, text = _output([target, "status"])
    seen = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        name = key.strip().lower()
        for field, label in STATUS_FIELDS.items():
            if name == label.lower():
                seen[field] = value.strip()
    logged_in = bool(seen.get("login_method") or seen.get("auth_source"))
    return {"state": "ok" if code == 0 else "unreadable", "logged_in": logged_in, **seen}
