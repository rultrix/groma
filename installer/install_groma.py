"""Per-user installer for Groma. No admin, no service: copy, shortcuts, uninstall entry.

Ship as: install-groma.exe next to a `groma/` folder (the PyInstaller onedir output).
Silent mode exists so the install path can be tested without clicking dialogs.
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "Groma"
APP_FOLDER = "groma"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Programs" / APP_NAME
START_MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
DESKTOP = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
UNINSTALL_KEY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\groma"
FALLBACK_VERSION = "0.53.0"


def source_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / APP_FOLDER


def app_version(source: Path) -> str:
    for candidate in (source / "VERSION", source / "_internal" / "VERSION", source.parent / "VERSION"):
        try:
            if candidate.is_file():
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            pass
    return FALLBACK_VERSION


def run_ps(script: str) -> None:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        check=True,
    )


def make_shortcut(link: Path, target: Path, arguments: str, workdir: Path) -> None:
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%s')\n"
        "$s.TargetPath = '%s'\n"
        "$s.Arguments = '%s'\n"
        "$s.WorkingDirectory = '%s'\n"
        "$s.IconLocation = '%s'\n"
        "$s.Save()\n" % (link, target, arguments, workdir, target)
    )
    run_ps(script)


def write_uninstaller(install_dir: Path) -> Path:
    script = install_dir / "uninstall.ps1"
    script.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'SilentlyContinue'",
                "Stop-Process -Name groma -Force",
                "Remove-Item '%s'" % (START_MENU / "groma.lnk"),
                "Remove-Item '%s'" % (START_MENU / "Startup" / "groma-catchup-all.cmd"),
                "Remove-Item '%s'" % (DESKTOP / "groma.lnk"),
                "Remove-Item '%s' -Recurse" % UNINSTALL_KEY,
                # The folder contains this script, so delete it from a detached shell.
                "Start-Process cmd -ArgumentList '/c','timeout /t 2 >nul & rmdir /s /q \"%s\"' -WindowStyle Hidden"
                % install_dir,
                "",
            ]
        ),
        encoding="utf-8-sig",
    )
    return script


def register_uninstall(install_dir: Path, uninstaller: Path, size_kb: int, app_version: str) -> None:
    script = "\n".join(
        [
            "New-Item -Path '%s' -Force | Out-Null" % UNINSTALL_KEY,
            "Set-ItemProperty -Path '%s' -Name DisplayName -Value 'Groma (本地 AI Agent 观测与回执)'" % UNINSTALL_KEY,
            "Set-ItemProperty -Path '%s' -Name DisplayVersion -Value '%s'" % (UNINSTALL_KEY, app_version),
            "Set-ItemProperty -Path '%s' -Name Publisher -Value 'Groma'" % UNINSTALL_KEY,
            "Set-ItemProperty -Path '%s' -Name InstallLocation -Value '%s'" % (UNINSTALL_KEY, install_dir),
            "Set-ItemProperty -Path '%s' -Name DisplayIcon -Value '%s'" % (UNINSTALL_KEY, install_dir / "groma.exe"),
            "Set-ItemProperty -Path '%s' -Name EstimatedSize -Value %d -Type DWord" % (UNINSTALL_KEY, size_kb),
            "Set-ItemProperty -Path '%s' -Name UninstallString -Value 'powershell -NoProfile -ExecutionPolicy Bypass -File \"%s\"'"
            % (UNINSTALL_KEY, uninstaller),
            "Set-ItemProperty -Path '%s' -Name NoModify -Value 1 -Type DWord" % UNINSTALL_KEY,
            "",
        ]
    )
    run_ps(script)


def folder_size_kb(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total // 1024


def install(workspace: Path, explicit_version: str | None = None) -> dict:
    source = source_dir()
    if not (source / "groma.exe").is_file():
        raise SystemExit("找不到应用目录：%s（请把 install-groma.exe 和 groma/ 放在一起）" % source)
    if not workspace.is_dir():
        raise SystemExit("工作区不存在：%s" % workspace)

    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    INSTALL_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, INSTALL_DIR)

    target = INSTALL_DIR / "groma.exe"
    arguments = '--root "%s" shell' % workspace
    START_MENU.mkdir(parents=True, exist_ok=True)
    make_shortcut(START_MENU / "groma.lnk", target, arguments, INSTALL_DIR)
    if DESKTOP.is_dir():
        make_shortcut(DESKTOP / "groma.lnk", target, arguments, INSTALL_DIR)

    uninstaller = write_uninstaller(INSTALL_DIR)
    version = explicit_version or app_version(source)
    register_uninstall(INSTALL_DIR, uninstaller, folder_size_kb(INSTALL_DIR), version)
    try:
        subprocess.run([str(target), "trigger", "install"], check=False)
    except OSError:
        pass
    # Remove pre-0.26 per-unit entries (`groma-<unit>.cmd --root ...`).
    for legacy in (START_MENU / "Startup").glob("groma-*.cmd"):
        if legacy.name == "groma-catchup-all.cmd":
            continue
        try:
            if "--root" in legacy.read_text(encoding="utf-8", errors="ignore"):
                legacy.unlink()
        except OSError:
            pass
    return {"install_dir": str(INSTALL_DIR), "target": str(target), "workspace": str(workspace), "version": version}


def pick_workspace() -> Path | None:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo("Groma", "选择要接入的工作区（你正在用 Agent 做事的项目目录）。\n\n单元的 .groma/ 目录会建在这个目录里。")
    chosen = filedialog.askdirectory(title="选择工作区")
    root.destroy()
    return Path(chosen) if chosen else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install-groma")
    parser.add_argument("--version", action="version", version="install-groma %s" % FALLBACK_VERSION)
    parser.add_argument("--root", help="workspace path; skips the dialog")
    parser.add_argument("--app-version", help="override the installed DisplayVersion")
    parser.add_argument("--silent", action="store_true", help="no dialogs, no launch")
    parser.add_argument("--launch", action="store_true", help="start the app after install")
    parser.add_argument("--register-classify-task", action="store_true",
                        help="register the daily classification task (off by default: it spends model quota)")
    args = parser.parse_args(argv)

    workspace = Path(args.root) if args.root else pick_workspace()
    if not workspace:
        return 2
    result = install(workspace, args.app_version)
    registered = None
    if args.register_classify_task:
        try:
            proc = subprocess.run([result["target"], "--root", str(workspace),
                                   "schedule", "install", "--kind", "classify", "--period", "1440"],
                                  capture_output=True, text=True, timeout=120)
            registered = proc.returncode == 0
        except OSError:
            registered = False
    if not args.silent:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        hint = ("已安装到：\n%s\n\n桌面与开始菜单里会有 Groma 图标。\n\n要停用：开始菜单搜索 Groma → 卸载，或删除快捷方式。"
                % result["install_dir"])
        if registered:
            hint += "\n\n每天自动归类：已注册。"
        else:
            hint += ("\n\n每天自动归类：未启用（它会用你已登录的模型，默认不替你打开）。\n"
                     "需要时运行：groma schedule install --kind classify --period 1440")
        messagebox.showinfo("Groma", hint)
        root.destroy()
    if args.launch or not args.silent:
        subprocess.Popen([result["target"], "--root", str(workspace), "shell"])
    if sys.stdout is not None:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
