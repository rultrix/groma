"""Groma 生成的 Qoder 注入 hook：脚本归我们，注册只在首次需要确认。

宿主提供 hook 能力（事件、stdin/stdout 协议）；本模块只产出符合协议的脚本、
把它注册进去，并记自己的账。两个事件都注册：

- `SessionStart` 与 `UserPromptSubmit` 在 IDE/CLI 上的触发组合，M8 的两处记录互相
  矛盾（注释说 SessionStart 归 CLI，验收记录写 `SessionStart@Windows Qoder IDE`）。
  与其赌一边，不如两边都挂、按 `session_id` 去重，保证"进上下文且只进一次"。
- 撤注册不等于立即失效：M8 机器侧实测发现同一 IDE 进程内新开会话仍会打一次
  `SessionStart`，真停用需要重启进程。本模块在 `remove()` 里直接删脚本，让缓存
  里的注册打不到东西，并把"要不要重启"如实报出来。

载荷来自 `.groma/injections/payload.md`（Groma 自己的目录，不往项目里多写文件，也不碰
ACPE 的 `context.md`）；回执写到 `.groma/injections/receipts/`，不去污染 ACPE 的账本。
Fail-open：任何异常静默 exit 0，绝不阻断会话。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from .contract import HOOK_FILE, LEGACY_HOOK_FILE
from .workspace import LabError, meta_dir, utc_stamp

HOOK_PATH = Path(".qoder") / "hooks" / HOOK_FILE
LEGACY_HOOK_PATH = Path(".qoder") / "hooks" / LEGACY_HOOK_FILE
SETTINGS_PATH = Path(".qoder") / "settings.local.json"
RECEIPT_DIR = "receipts"
# 载荷在 Groma 自己的目录里：注入不往项目里多写一个文件，也不碰 ACPE 的 context.md。
PAYLOAD = ".groma/injections/payload.md"

EVENTS = ("SessionStart", "UserPromptSubmit")
MAX_CONTEXT_CHARS = 60000

SCRIPT = '''#!/usr/bin/env python3
"""Groma 注入 hook（由 Groma 生成，请勿手改）。

把 {project}/.groma（迁移前为 .wb）/injections/payload.md 作为 additionalContext 送进会话。
协议: stdin JSON -> stdout {"hookSpecificOutput": {"hookEventName", "additionalContext"}}
去重: 每个 (session_id, source) 只送一次——SessionStart 与 UserPromptSubmit 会同秒都触发。
Fail-open: 任何异常静默 exit 0，绝不阻断会话。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_CHARS = __MAX_CHARS__
ALLOWED = __EVENTS__
ROOT = Path(__file__).resolve().parents[2]
META = ROOT / ".groma"
if not META.is_dir() and (ROOT / ".wb").is_dir():
    META = ROOT / ".wb"  # one release of compatibility; `groma migrate` moves it
STATE = META / "injections" / "receipts"
CONTEXT = META / "injections" / "payload.md"


def _force_utf8():
    # Windows 下 stdout 默认跟随 ANSI 代码页（cp936），中文背景会变成乱码；
    # 装载侧按 UTF-8 读，所以这里必须钉死编码。
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass


_force_utf8()


def record(event, session, action, extra):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (STATE / "inject.log").open("a", encoding="utf-8") as handle:
            handle.write(
                "%s event=%s session=%s action=%s %s\\n"
                % (stamp, event, session or "-", action, " ".join(extra))
            )
    except OSError:
        pass


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    event = data.get("hook_event_name") or data.get("hookEventName") or ""
    session = data.get("session_id") or ""
    source = data.get("source") or "-"
    try:
        if event not in ALLOWED:
            return 0
        # 一个会话只送一遍背景。真机实测：SessionStart 与 UserPromptSubmit 同一秒都触发，
        # 而 source 字段只有 SessionStart 带，所以不能拿 (session, source) 当去重键——
        # 那样两边键不相等，背景会被送两遍。只有上下文被重建（clear/compact）才再送一次。
        rebuild = event == "SessionStart" and source in ("clear", "compact")
        STATE.mkdir(parents=True, exist_ok=True)
        key = "%s|%s" % (session, source) if rebuild else session
        mark = STATE / ("injected-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16])
        if mark.is_file():
            record(event, session, "skip", ["reason=already-injected", "source=" + source])
            return 0
        if not CONTEXT.is_file():
            record(event, session, "skip", ["reason=no-context-file", "source=" + source])
            return 0
        text = CONTEXT.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            record(event, session, "skip", ["reason=empty-context", "source=" + source])
            return 0
        if len(text) > MAX_CHARS:
            # 超限不截断（截断=改内容），直接跳过，保持 hash 语义完整
            record(event, session, "skip", ["reason=over-limit", "chars=%d" % len(text)])
            return 0
        mark.write_text("%s %s" % (key, event), encoding="utf-8")
        record(
            event,
            session,
            "inject",
            [
                "chars=%d" % len(text),
                "sha256=%s" % hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source=" + source,
            ],
        )
        sys.stdout.write(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                ensure_ascii=False,
            )
        )
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 -- fail-open, never break a session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def interpreter() -> str:
    """The launcher the hook runs under. Never `sys.executable`: the frozen app
    would point the hook at wb.exe."""
    return "py" if os.name == "nt" else "python3"


def script_text() -> str:
    return SCRIPT.replace("__MAX_CHARS__", str(MAX_CONTEXT_CHARS)).replace(
        "__EVENTS__", repr(list(EVENTS))
    )


def hook_file(root: Path) -> Path:
    return Path(root) / HOOK_PATH


def legacy_hook_file(root: Path) -> Path:
    return Path(root) / LEGACY_HOOK_PATH


def legacy_registered(root: Path) -> dict:
    """Where the wb-era command sits in the settings, if anywhere."""
    hooks = read_settings(Path(root)).get("hooks") or {}
    found = {}
    needle = legacy_hook_file(root).as_posix()
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            for action in entry.get("hooks", []):
                if needle in (action.get("command") or ""):
                    found.setdefault(event, []).append(action.get("command"))
    return found


def remove_legacy(root: Path) -> dict:
    """Drop the wb-era script and its registrations; other tools' hooks stay."""
    root = Path(root)
    script = legacy_hook_file(root)
    removed = script.is_file()
    if removed:
        script.unlink()
    needle = script.as_posix()
    settings = read_settings(root)
    hooks = settings.get("hooks") or {}
    dropped = []
    for event in list(hooks):
        kept_entries = []
        for entry in hooks[event]:
            actions = [a for a in entry.get("hooks", []) if needle not in (a.get("command") or "")]
            if len(actions) != len(entry.get("hooks", [])):
                dropped.append(event)
            if actions:
                kept_entries.append({**entry, "hooks": actions})
        if kept_entries:
            hooks[event] = kept_entries
        else:
            hooks.pop(event, None)
    path = root / SETTINGS_PATH
    if dropped and path.is_file():
        payload = json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    return {"script_removed": removed, "events_unhooked": sorted(set(dropped))}


def payload_file(root: Path) -> Path:
    return meta_dir(Path(root)) / "injections" / "payload.md"


def receipt_dir(root: Path) -> Path:
    return meta_dir(Path(root)) / "injections" / RECEIPT_DIR


def read_settings(root: Path) -> dict:
    path = Path(root) / SETTINGS_PATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise LabError(".qoder/settings.local.json 不是合法 JSON，Groma 不会覆盖它：%s" % exc)
    if not isinstance(data, dict):
        raise LabError(".qoder/settings.local.json 顶层不是对象，Groma 不会覆盖它")
    return data


def _command(root: Path) -> str:
    return "%s %s" % (interpreter(), hook_file(root).as_posix())


def registered(root: Path) -> dict:
    """Where our command sits in the settings, if anywhere."""
    hooks = read_settings(root).get("hooks") or {}
    found = {}
    needle = hook_file(root).as_posix()
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            for action in entry.get("hooks", []):
                if needle in (action.get("command") or ""):
                    found.setdefault(event, []).append(action.get("command"))
    return found


def install(root: Path, confirmed: bool = False) -> dict:
    """Write the hook and register it. The first registration needs `confirmed`.

    Only our own command lines are added; any other tool's hooks in the same file
    are left alone.
    """
    root = Path(root)
    if not confirmed:
        raise LabError(
            "首次注册需要明确确认：Groma 将写入 .qoder/hooks/groma_inject.py 并在 .qoder/settings.local.json "
            "注册 %s（撤注册后同一 IDE 进程可能仍会打一次 SessionStart，真停用需重启）" % "、".join(EVENTS)
        )
    settings = read_settings(root)  # refuse a file we cannot parse before writing anything
    script = hook_file(root)
    script.parent.mkdir(parents=True, exist_ok=True)
    body = script_text()
    created_script = not script.is_file()
    script.write_text(body, encoding="utf-8", newline="\n")

    path = root / SETTINGS_PATH
    existed = path.is_file()
    original = path.read_text(encoding="utf-8") if existed else ""
    hooks = settings.setdefault("hooks", {})
    command = _command(root)
    added = []
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        present = any(
            action.get("command") == command
            for entry in entries
            for action in entry.get("hooks", [])
        )
        if not present:
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
            added.append(event)
    payload = json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    backup = None
    if existed and original != payload:
        slot = meta_dir(root) / "injections" / "settings.local.json.pre-hook"
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_text(original, encoding="utf-8", newline="\n")
        backup = slot
    if original != payload:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    return {
        "script": script.as_posix(),
        "script_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "script_created": created_script,
        "settings": path.as_posix(),
        "events_added": added,
        "events_present": sorted(registered(root)),
        "backup": str(backup) if backup else None,
        "payload_missing": not payload_file(root).is_file(),
    }


def remove(root: Path) -> dict:
    """Delete our script and unhook ourselves. Other tools' hooks stay."""
    root = Path(root)
    script = hook_file(root)
    script_removed = script.is_file()
    if script_removed:
        script.unlink()
    path = root / SETTINGS_PATH
    settings = read_settings(root)
    hooks = settings.get("hooks") or {}
    needle = script.as_posix()
    dropped = []
    for event in list(hooks):
        kept_entries = []
        for entry in hooks[event]:
            actions = [a for a in entry.get("hooks", []) if needle not in (a.get("command") or "")]
            if len(actions) != len(entry.get("hooks", [])):
                dropped.append(event)
            if actions:
                kept_entries.append({**entry, "hooks": actions})
        if kept_entries:
            hooks[event] = kept_entries
        else:
            hooks.pop(event, None)
    if path.is_file():
        payload = json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    return {
        "script_removed": script_removed,
        "events_unhooked": sorted(set(dropped)),
        "restart_required": True,
        "note": "同一 IDE 进程内可能仍会打一次 SessionStart；重启进程或保持脚本已删除即为真停用",
    }


def last_delivery(root: Path) -> dict | None:
    """The newest `action=inject` line, parsed: what the host actually took."""
    path = receipt_dir(root) / "inject.log"
    if not path.is_file():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        parts = line.split()
        if "action=inject" not in parts:
            continue
        fields = dict(
            item.split("=", 1) for item in parts[1:] if "=" in item
        )
        return {
            "at": parts[0],
            "event": fields.get("event"),
            "session": (fields.get("session") or "")[:8],
            "chars": fields.get("chars"),
            "sha256": fields.get("sha256"),
        }
    return None


def status(root: Path) -> dict:
    root = Path(root)
    script = hook_file(root)
    receipts = sorted(receipt_dir(root).glob("inject.log")) if receipt_dir(root).is_dir() else []
    log = receipts[0].read_text(encoding="utf-8").strip().splitlines()[-3:] if receipts else []
    return {
        "script": script.is_file(),
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest() if script.is_file() else None,
        "registered": sorted(registered(root)),
        "payload": payload_file(root).is_file(),
        "receipts": log,
        "restart_hint": "撤注册后需重启 IDE 进程才算真停用（M8 机器侧实测）",
    }


def run_hook(root: Path, event: str, session_id: str, source: str | None = None) -> dict:
    """Drive the generated script exactly as the host would. Test-only helper.

    The host contract is stdin JSON in, one JSON object out. `source` is only sent
    when given, because that is how the real host differs: `SessionStart` carries
    it, `UserPromptSubmit` does not.
    """
    import subprocess

    payload = {"hook_event_name": event, "session_id": session_id}
    if source is not None:
        payload["source"] = source
    stdin = json.dumps(payload)
    done = subprocess.run(
        [sys.executable, str(hook_file(root))],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    body = (done.stdout or "").strip()
    injected = None
    if body:
        try:
            injected = json.loads(body)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError, TypeError):
            injected = None
    return {"returncode": done.returncode, "additionalContext": injected, "stderr": (done.stderr or "").strip()}
