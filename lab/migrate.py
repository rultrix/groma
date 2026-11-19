"""One-shot move from the wb-era names to Groma's.

Preview by default: every step reports what it would do, and only `--yes`
touches the disk. The operation is deliberately narrow -- only paths Groma
itself wrote are moved or renamed; project files, harness data and the old
installed program directory are left alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import contract, hooks, schedule, state, surface, trigger
from .workspace import LabError, load_unit, read_json, write_json

SCHEDULE_KINDS = ("scan", "classify")


def _move(src: Path, dst: Path) -> None:
    src.rename(dst)


def _state_plan() -> dict:
    if contract.env_value(contract.STATE_ENV, contract.LEGACY_STATE_ENV):
        return {"action": "skip", "reason": "environment override decides the state root"}
    base = state.state_base()
    legacy = base / contract.LEGACY_STATE_NAME
    canonical = base / contract.STATE_NAME
    if legacy.is_dir() and canonical.exists():
        return {
            "action": "conflict",
            "reason": "both directories exist; refusing to merge",
            "from": str(legacy),
            "to": str(canonical),
        }
    if legacy.is_dir():
        return {"action": "move", "from": str(legacy), "to": str(canonical)}
    return {"action": "current", "path": str(canonical)}


def _apply_state(plan: dict) -> dict:
    if plan["action"] != "move":
        return plan
    try:
        _move(Path(plan["from"]), Path(plan["to"]))
    except OSError as exc:
        return {**plan, "action": "failed", "error": str(exc)}
    _rewrite_vault_config(Path(plan["from"]), Path(plan["to"]))
    return {**plan, "action": "moved"}


def _rewrite_vault_config(old: Path, new: Path) -> None:
    path = state.state_root() / "vault.json"
    if not path.is_file():
        return
    try:
        config = read_json(path)
    except (OSError, ValueError):
        return
    root = config.get("root")
    if not root:
        return
    try:
        relative = Path(root).relative_to(old)
    except ValueError:
        return
    config["root"] = (new / relative).as_posix()
    write_json(path, config)


def _legacy_manifest(root: Path) -> dict:
    return surface.read_manifest(root)


def _workspace_plan(root: Path) -> dict:
    root = Path(root)
    legacy_meta = root / contract.LEGACY_META_DIR
    meta = root / contract.META_DIR
    legacy_card = root / contract.LEGACY_LINK_FILE
    card = root / contract.LINK_FILE
    actions: list[dict] = []
    conflicts: list[str] = []

    if legacy_meta.is_dir() and meta.exists():
        conflicts.append("both %s and %s exist" % (legacy_meta.name, meta.name))
    elif legacy_meta.is_dir():
        actions.append({"action": "move", "from": str(legacy_meta), "to": str(meta)})

    if legacy_card.is_file() and card.exists():
        conflicts.append("both %s and %s exist" % (legacy_card.name, card.name))
    elif legacy_card.is_file():
        actions.append({"action": "rename", "from": str(legacy_card), "to": str(card)})

    book = _legacy_manifest(root)
    if any(item.get("path") == contract.LEGACY_RULE_FILE for item in book.get("surfaces", [])):
        actions.append({"action": "rewrite-manifest", "path": contract.RULE_FILE})

    if hooks.legacy_registered(root) or hooks.legacy_hook_file(root).is_file():
        actions.append({"action": "replace-hook", "path": str(hooks.legacy_hook_file(root))})

    for kind in SCHEDULE_KINDS:
        record = schedule._load_record(root, kind)
        name = record.get("task_name") or ""
        if record.get("status") == "installed" and name.startswith(contract.LEGACY_TASK_PREFIX):
            actions.append(
                {
                    "action": "reinstall-task",
                    "kind": kind,
                    "from": name,
                    "period_minutes": record.get("period_minutes") or 60,
                }
            )
            continue
        # A task can outlive its record (the 11-13 incident removed the file but
        # not the OS entry). Find it by the name this Unit would have used.
        try:
            unit_id = load_unit(root)["unit_id"]
        except LabError:
            continue
        orphan = schedule.legacy_task_name(unit_id, kind)
        if schedule.task_present(orphan):
            actions.append(
                {
                    "action": "reinstall-orphan-task",
                    "kind": kind,
                    "from": orphan,
                    "period_minutes": schedule.task_period(orphan),
                }
            )

    return {"path": root.as_posix(), "actions": actions, "conflicts": conflicts}


def _apply_workspace(plan: dict) -> dict:
    root = Path(plan["path"])
    done: list[dict] = []
    errors: list[dict] = []
    for action in plan["actions"]:
        try:
            if action["action"] == "move":
                _move(Path(action["from"]), Path(action["to"]))
            elif action["action"] == "rename":
                _move(Path(action["from"]), Path(action["to"]))
            elif action["action"] == "rewrite-manifest":
                book = surface.read_manifest(root)
                moved = False
                for item in book.get("surfaces", []):
                    if item.get("path") == contract.LEGACY_RULE_FILE:
                        item["path"] = contract.RULE_FILE
                        moved = True
                if moved:
                    surface.write_manifest(root, book)
                old_rule = root / contract.LEGACY_RULE_FILE
                if old_rule.is_file():
                    surface.remove_block(root, contract.LEGACY_RULE_FILE)
                    if old_rule.is_file() and not old_rule.read_text(encoding="utf-8").strip():
                        old_rule.unlink()
            elif action["action"] == "replace-hook":
                was_registered = bool(hooks.legacy_registered(root))
                hooks.remove_legacy(root)
                if was_registered:
                    hooks.install(root, confirmed=True)
            elif action["action"] == "reinstall-task":
                kind = action["kind"]
                schedule.remove(root, kind)
                schedule.install(root, int(action["period_minutes"]), kind)
            elif action["action"] == "reinstall-orphan-task":
                kind = action["kind"]
                schedule.delete_task(action["from"])
                schedule.install(root, int(action["period_minutes"]), kind)
            done.append(action)
        except (LabError, OSError, ValueError) as exc:
            errors.append({**action, "error": str(exc)})
    if done:
        try:
            from .inject import refresh

            refresh(root)
        except (LabError, OSError, ValueError):
            pass
    return {**plan, "done": done, "errors": errors}


def _startup_plan() -> dict:
    try:
        legacy = trigger.legacy_entry()
    except LabError as exc:
        return {"action": "skip", "reason": str(exc)}
    if legacy.is_file():
        return {"action": "replace", "from": str(legacy), "to": str(trigger._entry())}
    return {"action": "current"}


def _apply_startup(plan: dict) -> dict:
    if plan["action"] != "replace":
        return plan
    try:
        trigger.remove_legacy()
        trigger.install()
        return {**plan, "action": "replaced"}
    except (LabError, OSError) as exc:
        return {**plan, "action": "failed", "error": str(exc)}


def run(apply: bool = False) -> dict:
    workspaces = [item["path"] for item in state.list_workspaces()]
    state_plan = _state_plan()
    workspace_plans = [_workspace_plan(Path(path)) for path in workspaces]
    startup_plan = _startup_plan()
    report = {
        "schema": 1,
        "applied": bool(apply),
        "state": state_plan,
        "workspaces": workspace_plans,
        "startup": startup_plan,
        "notes": [
            "old installed program (%LOCALAPPDATA%\\Programs\\wb) and its shortcuts are not touched",
            "project files, harness databases and sessions are never moved",
        ],
    }
    if not apply:
        return report
    report["state"] = _apply_state(state_plan)
    report["workspaces"] = [_apply_workspace(item) for item in workspace_plans]
    report["startup"] = _apply_startup(startup_plan)
    return report
