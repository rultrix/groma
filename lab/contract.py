"""Names that outlive a single release, plus the one-version legacy aliases.

The rename from wb to Groma ships in one release: new writes use the names on
the left, reads accept the old ones until `groma migrate` has swept a machine.
Keeping both spellings here means no other module has to invent a fallback.
"""

from __future__ import annotations

import os

APP_NAME = "Groma"
CLI_NAME = "groma"
LEGACY_CLI_NAME = "wb"

STATE_NAME = "groma"
LEGACY_STATE_NAME = "wb"
META_DIR = ".groma"
LEGACY_META_DIR = ".wb"
LINK_FILE = "groma-unit.json"
LEGACY_LINK_FILE = "wb-unit.json"

STATE_ENV = "GROMA_STATE_DIR"
LEGACY_STATE_ENV = "WB_STATE_DIR"
VAULT_ENV = "GROMA_VAULT_DIR"
LEGACY_VAULT_ENV = "WB_VAULT_DIR"
STARTUP_ENV = "GROMA_STARTUP_DIR"
LEGACY_STARTUP_ENV = "WB_STARTUP_DIR"
TRIGGER_ENV = "GROMA_TRIGGER"
LEGACY_TRIGGER_ENV = "WB_TRIGGER"
PROVIDER_ENV = "GROMA_QODERCLICN"
LEGACY_PROVIDER_ENV = "WB_QODERCLICN"
SCHEDULER_MOCK_ENV = "GROMA_TEST_MOCK_SCHEDULER"
LEGACY_SCHEDULER_MOCK_ENV = "WB_TEST_MOCK_SCHEDULER"

TASK_PREFIX = "GROMA-"
LEGACY_TASK_PREFIX = "WBLOCAL-"
STARTUP_ENTRY = "groma-catchup-all.cmd"
LEGACY_STARTUP_ENTRY = "wb-catchup-all.cmd"

HOOK_FILE = "groma_inject.py"
LEGACY_HOOK_FILE = "wb_inject.py"
RULE_FILE = ".qoder/rules/groma-unit.md"
LEGACY_RULE_FILE = ".qoder/rules/wb-unit.md"
MARKER_START = "<!-- groma:inject unit="
MARKER_END = "<!-- /groma:inject -->"
LEGACY_MARKER_START = "<!-- wb:inject unit="
LEGACY_MARKER_END = "<!-- /wb:inject -->"
MUTEX_NAME = "Local\\groma-shell"


def env_value(name: str, legacy: str | None = None, default: str | None = None) -> str | None:
    """New name wins; one release of aliases keeps pre-migration scripts working."""
    value = os.environ.get(name)
    if value:
        return value
    if legacy:
        value = os.environ.get(legacy)
        if value:
            return value
    return default


def legacy_kind(value: str) -> str:
    return "WB_" + value[len("GROMA_"):] if value.startswith("GROMA_") else value


def has_kind(payload: dict, expected: str) -> bool:
    """Readers accept both namespaces; writers only emit `expected`."""
    return payload.get("kind") in (expected, legacy_kind(expected))
