"""OS-level scheduling for `groma scan`, with stop semantics.

Kept deliberately smaller than the package scheduler: no policy gating, no
identity, no log rotation (each run already leaves a timestamped scan).
What is borrowed is the hard-won part: a wrapper script instead of an inline
command, a task name bound to the unit, idempotent install, and a mock switch
so tests never touch the real OS task table.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .contract import LEGACY_SCHEDULER_MOCK_ENV, LEGACY_TASK_PREFIX, env_value
from .workspace import LabError, load_unit, meta_dir, read_json, require_init, utc_stamp, write_json

RECORD_FILE = "scheduler.json"
WRAPPER_DIR = "scheduler"
TASK_PREFIX = "GROMA-"
TEST_MOCK_ENV = "GROMA_TEST_MOCK_SCHEDULER"

# Known schtasks Last Result values worth translating; anything else is shown as hex.
RESULT_MEANINGS = {
    0: "ok",
    267009: "running",
    267011: "never run",
    -2147020576: "refused: interactive-only task while the session was locked",
    -2147024894: "task not found",
}


KINDS = {"scan": {"record": RECORD_FILE, "suffix": "", "wrapper": "scan"},
         "classify": {"record": "scheduler-topic.json", "suffix": "-TOPIC", "wrapper": "classify"}}


def _record_path(root: Path, kind: str = "scan") -> Path:
    return meta_dir(root) / KINDS[kind]["record"]


def _load_record(root: Path, kind: str = "scan") -> dict:
    path = _record_path(root, kind)
    if not path.is_file():
        return {}
    return read_json(path)


def _task_name(root: Path, kind: str = "scan") -> str:
    unit_id = load_unit(root)["unit_id"]
    return TASK_PREFIX + unit_id.replace("unit_", "") + KINDS[kind]["suffix"]


def legacy_task_name(unit_id: str, kind: str = "scan") -> str:
    """The wb-era name for the same task; used to find tasks whose record vanished."""
    return LEGACY_TASK_PREFIX + unit_id.replace("unit_", "") + KINDS[kind]["suffix"]


def task_present(name: str) -> bool:
    if env_value(TEST_MOCK_ENV, LEGACY_SCHEDULER_MOCK_ENV) == "1":
        return False
    if _backend() == "schtasks":
        return _query_schtasks(name) is not None
    rc, out, _err = _capture(["crontab", "-l"])
    return any(("# %s:start" % name) in line for line in (out.splitlines() if rc in (0, 1) else []))


def task_period(name: str, default: int = 1440) -> int:
    """Read the repetition interval out of the live task; fall back to a day."""
    if env_value(TEST_MOCK_ENV, LEGACY_SCHEDULER_MOCK_ENV) == "1" or _backend() != "schtasks":
        return default
    rc, out, _err = _capture(["schtasks", "/Query", "/TN", name, "/XML"])
    if rc != 0:
        return default
    match = re.search(r"<Interval>PT(\d+)M</Interval>", out)
    return int(match.group(1)) if match else default


def delete_task(name: str) -> dict:
    """Delete by name, for tasks that no longer have a record to point at them."""
    if _backend() == "schtasks":
        if _query_schtasks(name) is None:
            return {"task_name": name, "status": "not_found"}
        rc, out, err = _capture(["schtasks", "/Delete", "/TN", name, "/F"])
        if rc != 0:
            raise LabError("schtasks /Delete failed [%d]: %s" % (rc, (err or out).strip()))
        return {"task_name": name, "status": "removed"}
    rc, out, _err = _capture(["crontab", "-l"])
    lines = out.splitlines() if rc in (0, 1) else []
    start, end = "# %s:start" % name, "# %s:end" % name
    kept, skipping, removed = [], False, False
    for line in lines:
        if start in line:
            skipping, removed = True, True
            continue
        if end in line:
            skipping = False
            continue
        if not skipping:
            kept.append(line)
    if not removed:
        return {"task_name": name, "status": "not_found"}
    proc = subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise LabError("crontab write failed [%d]: %s" % (proc.returncode, proc.stderr.strip()))
    return {"task_name": name, "status": "removed"}


def _capture(argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, text=True, errors="replace")
    except FileNotFoundError as exc:
        raise LabError("scheduler backend unavailable: %s" % argv[0]) from exc
    return proc.returncode, proc.stdout, proc.stderr


def _backend() -> str:
    return "schtasks" if os.name == "nt" else "crontab"


def _launcher_py() -> Path:
    return Path(__file__).resolve().parents[1] / "groma.py"


def _launch_prefix() -> str:
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    return '"%s" "%s"' % (sys.executable or "python", _launcher_py())


def _wrapper_path(root: Path, kind: str = "scan") -> Path:
    name = "%s.cmd" % KINDS[kind]["wrapper"] if os.name == "nt" else "%s.sh" % KINDS[kind]["wrapper"]
    return meta_dir(root) / WRAPPER_DIR / name


LOG_SUFFIX = ".log"
LOG_LIMIT_BYTES = 200 * 1024


def write_wrapper(root: Path, kind: str = "scan") -> Path:
    """A script, not an inline command: unit paths may contain spaces.

    `scan` runs scan then digest so the chain closes on its own; `classify` runs
    `topic maintain`: classify, re-forge when evidence moved, then
    re-point our own injection source (per settings).

    每一步的 stdout/stderr 与退出码追写同目录的 `<kind>.log`。丢进
    `nul` 的那版让夜里的 rc=2 连续六天无人知道原因——黑箱的失败不是失败，是不可诊断。
    """
    target = _wrapper_path(root, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = _launch_prefix()
    steps = ("scan", "digest") if kind == "scan" else ("topic maintain",)
    if os.name == "nt":
        log = str(target.with_suffix(LOG_SUFFIX))
        lines = [
            "@echo off\r\n",
            "set PYTHONUTF8=1\r\n",
            f'set "GROMA_LOG={log}"\r\n',
            f'if exist "%GROMA_LOG%" for %%I in ("%GROMA_LOG%") do if %%~zI GTR {LOG_LIMIT_BYTES} '
            'move /y "%GROMA_LOG%" "%GROMA_LOG%.prev"\r\n',
            'echo [%date% %time%] wrapper start>>"%GROMA_LOG%"\r\n',
        ]
        for step in steps:
            label = step.replace(" ", "-")
            lines.append(f'{prefix} --root "{root}" {step} >>"%GROMA_LOG%" 2>&1\r\n')
            # 数字紧跟 `>>` 会被 cmd 读成"重定向句柄 0"而把退出码吞掉——这个空格是判据。
            lines.append(f'echo [%date% %time%] step={label} exit=%ERRORLEVEL% >>"%GROMA_LOG%"\r\n')
        body = "".join(lines)
    else:
        log = str(target.with_suffix(LOG_SUFFIX))
        lines = [
            "#!/bin/sh\n",
            "export PYTHONUTF8=1\n",
            f'LOG="{log}"\n',
            f'if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt {LOG_LIMIT_BYTES} ]; then '
            'mv -f "$LOG" "$LOG.prev"; fi\n',
            'echo "[$(date -u +%FT%TZ)] wrapper start" >>"$LOG" 2>&1\n',
        ]
        for step in steps:
            label = step.replace(" ", "-")
            lines.append(f'{prefix} --root "{root}" {step} >>"$LOG" 2>&1\n')
            lines.append(f'echo "[$(date -u +%FT%TZ)] step={label} exit=$?" >>"$LOG" 2>&1\n')
        body = "".join(lines)
    target.write_text(body, encoding="utf-8", newline="")
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _query_schtasks(task: str) -> dict | None:
    rc, out, _err = _capture(["schtasks", "/Query", "/TN", task, "/FO", "LIST", "/V"])
    if rc != 0:
        return None
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            info[key.strip()] = value.strip()
    return info or None


def _mock(result: dict) -> dict:
    result.setdefault("mock", True)
    return result


def _task_user() -> str:
    domain = os.environ.get("USERDOMAIN") or ""
    user = os.environ.get("USERNAME") or ""
    return "%s\\%s" % (domain, user) if domain else user


def task_xml(wrapper: Path, period_minutes: int) -> str:
    """Interactive task that starts as soon as the session allows (`StartWhenAvailable`).

    No stored password and no admin: `InteractiveToken` + `LeastPrivilege` is
    exactly what a normal user can register. The cost is that ticks are refused
    while the session is locked; `StartWhenAvailable` catches them up on unlock,
    and `status` reports the refusal instead of hiding it.
    """
    start = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo><Description>groma scheduled scan and digest</Description></RegistrationInfo>\n"
        "  <Triggers><TimeTrigger><Repetition><Interval>PT%dM</Interval>"
        "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
        "<StartBoundary>%s</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>\n"
        '  <Principals><Principal id="Author"><UserId>%s</UserId>'
        "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings><StartWhenAvailable>true</StartWhenAvailable>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><Enabled>true</Enabled></Settings>\n"
        '  <Actions Context="Author"><Exec><Command>cmd.exe</Command>'
        '<Arguments>/c "%s"</Arguments></Exec></Actions>\n'
        "</Task>\n" % (period_minutes, start, escape(_task_user()), escape(str(wrapper)))
    )


def write_task_xml(root: Path, wrapper: Path, period_minutes: int) -> Path:
    path = meta_dir(root) / WRAPPER_DIR / "task.xml"
    path.write_text(task_xml(wrapper, period_minutes), encoding="utf-16")
    return path


def _parse_task_time(text: object):
    """schtasks prints `11/10/2026 11:14:47 AM`; anything else is "cannot tell"."""
    from datetime import datetime as _dt

    value = str(text or "").strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return _dt.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _freshness(root: Path, kind: str, raw: dict | None) -> dict:
    """Did the last run actually write something? "Ran" and "wrote" are different facts."""
    from datetime import datetime as _dt

    artifact = None
    if kind == "scan":
        digests = sorted((meta_dir(root) / "digests").glob("digest-*.json")) if (meta_dir(root) / "digests").is_dir() else []
        if digests:
            artifact = _dt.fromtimestamp(digests[-1].stat().st_mtime)
    else:
        from .classify_run import last_runs

        runs = last_runs(1)
        if runs:
            try:
                artifact = _dt.strptime(runs[-1]["at"], "%Y-%m-%dT%H:%M:%SZ")
            except (KeyError, ValueError):
                artifact = None
    if _explain_result(raw) == "never run":
        return {"latest_artifact": artifact.strftime("%Y-%m-%dT%H:%M:%SZ") if artifact else None,
                "wrote_since_last_run": None}
    last_run = _parse_task_time((raw or {}).get("Last Run Time"))
    if last_run is None or artifact is None:
        return {"latest_artifact": artifact.strftime("%Y-%m-%dT%H:%M:%SZ") if artifact else None,
                "wrote_since_last_run": None}
    return {"latest_artifact": artifact.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wrote_since_last_run": artifact >= last_run}


def _explain_result(raw: dict | None) -> str | None:
    if not raw:
        return None
    value = raw.get("Last Result")
    if value is None:
        return None
    try:
        code = int(str(value))
    except ValueError:
        return None
    return RESULT_MEANINGS.get(code, "0x%08X" % (code & 0xFFFFFFFF))


def install(root: Path, period_minutes: int = 60, kind: str = "scan") -> dict:
    require_init(root)
    if isinstance(period_minutes, bool) or not isinstance(period_minutes, int) or period_minutes < 1:
        raise LabError("period must be a positive integer (minutes)")

    if kind not in KINDS:
        raise LabError("unknown task kind: %s" % kind)
    task = _task_name(root, kind)
    wrapper = write_wrapper(root, kind)

    if env_value(TEST_MOCK_ENV, LEGACY_SCHEDULER_MOCK_ENV) == "1":
        record = {
            "backend": "test-mock",
            "task_name": task,
            "period_minutes": period_minutes,
            "wrapper": str(wrapper),
            "installed_at": utc_stamp(),
            "status": "installed",
        }
        write_json(_record_path(root, kind), record)
        return _mock(dict(record))

    if _backend() == "schtasks":
        xml_path = write_task_xml(root, wrapper, period_minutes)
        rc, out, err = _capture(["schtasks", "/Create", "/TN", task, "/XML", str(xml_path), "/F"])
        if rc != 0:
            raise LabError("schtasks /Create /XML failed [%d]: %s" % (rc, (err or out).strip()))
    else:
        rc, out, err = _capture(
            ["crontab", "-l"]
        )
        lines = out.splitlines() if rc in (0, 1) else []
        start, end = "# %s:start" % task, "# %s:end" % task
        kept: list[str] = []
        skipping = False
        for line in lines:
            if start in line:
                skipping = True
                continue
            if end in line:
                skipping = False
                continue
            if not skipping:
                kept.append(line)
        kept += [start, "*/%d * * * * sh \"%s\"" % (period_minutes, wrapper), end]
        try:
            proc = subprocess.run(
                ["crontab", "-"],
                input="\n".join(kept) + "\n",
                capture_output=True,
                text=True,
                timeout=60,
            )
        except OSError as exc:
            raise LabError("crontab unavailable: %s" % exc) from exc
        if proc.returncode != 0:
            raise LabError("crontab write failed [%d]: %s" % (proc.returncode, proc.stderr.strip()))

    record = {
        "backend": _backend(),
        "task_name": task,
        "period_minutes": period_minutes,
        "wrapper": str(wrapper),
        "installed_at": utc_stamp(),
        "status": "installed",
    }
    write_json(_record_path(root, kind), record)
    return dict(record)


def _provider_status() -> dict:
    """Whether the harness behind a future classify task exists and is itself.

    Identity only: we do not run a query to test the login, because that spends
    the user's quota and is not ours to spend.
    """
    from .provider import check, login_state

    result = check()
    state = result.get("state")
    reason = {
        "ok": "身份已确认；登录态取自它自己的 status，不跑模型",
        "not_found": "不在 PATH，也不在已知安装位置",
        "identity_mismatch": "路径上有可执行文件，但不是 qoderclicn",
    }.get(state, str(state))
    login = login_state() if state == "ok" else {"logged_in": None}
    return {"state": state, "path": result.get("path"), "reason": reason,
            "logged_in": login.get("logged_in"), "login_method": login.get("login_method")}


def status(root: Path, kind: str = "scan") -> dict:
    result = _status(root, kind)
    result["provider"] = _provider_status()
    return result


def _status(root: Path, kind: str = "scan") -> dict:
    require_init(root)
    record = _load_record(root, kind)
    task = record.get("task_name") or _task_name(root, kind)

    if env_value(TEST_MOCK_ENV, LEGACY_SCHEDULER_MOCK_ENV) == "1" or record.get("backend") == "test-mock":
        present = bool(record) and record.get("status") == "installed"
        return _mock(
            {
                "backend": "test-mock",
                "task_name": task,
                "status": "present" if present else "absent",
                "period_minutes": record.get("period_minutes"),
                "record": bool(record),
            }
        )

    if _backend() == "schtasks":
        info = _query_schtasks(task)
        if info is None:
            return {"backend": "schtasks", "task_name": task, "status": "absent"}
        return {
            "backend": "schtasks",
            "task_name": task,
            "status": "present",
            "period_minutes": record.get("period_minutes"),
            "next_run": info.get("Next Run Time"),
            "last_run": info.get("Last Run Time"),
            "last_result_meaning": _explain_result(info),
            **_freshness(root, kind, info),
            "raw": info,
        }

    rc, out, _err = _capture(["crontab", "-l"])
    lines = out.splitlines() if rc in (0, 1) else []
    present = any(("# %s:start" % task) in line for line in lines)
    return {
        "backend": "crontab",
        "task_name": task,
        "status": "present" if present else "absent",
        "period_minutes": record.get("period_minutes"),
    }


def remove(root: Path, kind: str = "scan") -> dict:
    require_init(root)
    record = _load_record(root, kind)
    task = record.get("task_name") or _task_name(root, kind)

    if env_value(TEST_MOCK_ENV, LEGACY_SCHEDULER_MOCK_ENV) == "1" or record.get("backend") == "test-mock":
        if not record:
            return _mock({"backend": "test-mock", "task_name": task, "status": "not_found"})
        record["status"] = "removed"
        record["removed_at"] = utc_stamp()
        write_json(_record_path(root, kind), record)
        return _mock({"backend": "test-mock", "task_name": task, "status": "removed"})

    if _backend() == "schtasks":
        if _query_schtasks(task) is None:
            return {"backend": "schtasks", "task_name": task, "status": "not_found"}
        rc, out, err = _capture(["schtasks", "/Delete", "/TN", task, "/F"])
        if rc != 0:
            raise LabError("schtasks /Delete failed [%d]: %s" % (rc, (err or out).strip()))
        result = {"backend": "schtasks", "task_name": task, "status": "removed"}
    else:
        rc, out, _err = _capture(["crontab", "-l"])
        lines = out.splitlines() if rc in (0, 1) else []
        start, end = "# %s:start" % task, "# %s:end" % task
        kept: list[str] = []
        skipping = False
        removed = False
        for line in lines:
            if start in line:
                skipping, removed = True, True
                continue
            if end in line:
                skipping = False
                continue
            if not skipping:
                kept.append(line)
        if not removed:
            return {"backend": "crontab", "task_name": task, "status": "not_found"}
        proc = subprocess.run(
            ["crontab", "-"], input="\n".join(kept) + "\n", capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise LabError("crontab write failed [%d]: %s" % (proc.returncode, proc.stderr.strip()))
        result = {"backend": "crontab", "task_name": task, "status": "removed"}

    if record:
        record["status"] = "removed"
        record["removed_at"] = utc_stamp()
        write_json(_record_path(root, kind), record)
    return result
