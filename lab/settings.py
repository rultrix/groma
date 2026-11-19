"""Machine-wide settings: one interval for the whole app.

The interval belongs to the collector, not to a folder. Two workspaces used to
hold two answers at the same time, and the rhythm that produced could not be
explained from either file. The store therefore lives in the machine state root;
`root` is still accepted so old call sites keep working, and is only used to
say whether a legacy per-workspace file is still lying around.
"""

from __future__ import annotations

from pathlib import Path

from .state import state_root
from .workspace import LabError, meta_dir, read_json, utc_stamp, write_json

SETTINGS_FILE = "settings.json"
CLASSIFY_DEFAULTS = {"classify_model": "Qwen3.8-Flash", "classify_provider": None,
                     "classify_signature": "Usage: qoderclicn",
                     "auto_reforge": True, "auto_inject": True}
DEFAULT_INTERVAL_MINUTES = 15
MIN_INTERVAL = 1
MAX_INTERVAL = 1440


def _path() -> Path:
    return state_root() / SETTINGS_FILE


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_settings(root: Path | None = None) -> dict:
    path = _path()
    data = read_json(path) if path.is_file() else {}
    record = {
        "schema": 1,
        "scope": "machine",
        "collect_interval_minutes": int(data.get("collect_interval_minutes", DEFAULT_INTERVAL_MINUTES)),
        "updated_at": data.get("updated_at"),
    }
    for key, fallback in CLASSIFY_DEFAULTS.items():
        raw = data.get(key)
        if isinstance(fallback, bool):
            record[key] = fallback if raw is None else _as_bool(raw)
        else:
            record[key] = fallback if raw in (None, "", "None") else raw
    if root is not None:
        legacy = meta_dir(Path(root)) / SETTINGS_FILE
        record["legacy_workspace_file"] = legacy.is_file()
    return record


def save_settings(root: Path | None, payload: dict) -> dict:
    previous = load_settings()
    minutes = payload.get("collect_interval_minutes", previous["collect_interval_minutes"])
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise LabError("collect_interval_minutes must be an integer")
    if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
        raise LabError("collect_interval_minutes must be between %d and %d" % (MIN_INTERVAL, MAX_INTERVAL))
    record = {"schema": 1, "collect_interval_minutes": minutes, "updated_at": utc_stamp()}
    for key in CLASSIFY_DEFAULTS:
        value = payload.get(key, previous.get(key))
        if isinstance(CLASSIFY_DEFAULTS[key], bool):
            record[key] = _as_bool(value)
        else:
            record[key] = None if value is None else (str(value).strip() or None)
    write_json(_path(), record)
    return {**record, "scope": "machine"}


def collect_interval(root: Path | None = None) -> int:
    return load_settings(root)["collect_interval_minutes"]
