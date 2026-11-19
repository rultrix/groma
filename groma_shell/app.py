"""Desktop shell: one process owns the server, a tray owns visibility.

Window + tray only. No installer yet; that is the next increment and is
where macOS signing and packaging costs show up.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path
from urllib.parse import quote

from . import controller
from .server import LocalServer

_MUTEX = None


def _log(message: str) -> None:
    if sys.stdout is not None:
        print(message, flush=True)


def _claim_single_instance() -> bool:
    """One shell per machine; workspaces are switched inside it, not by relaunching."""
    global _MUTEX
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    from lab.contract import MUTEX_NAME

    _MUTEX = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _icon_image():
    from PIL import Image

    from lab.brand import icon_path

    with Image.open(icon_path()) as icon:
        return icon.convert("RGBA").resize((64, 64), Image.LANCZOS)


def _fatal(message: str) -> None:
    """A windowed exe has no console; failing silently looks like 'nothing happened'."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Groma 启动失败", message)
        root.destroy()
    except Exception:
        pass


def run(unit_root: Path, port: int = 8787) -> None:
    import pystray
    import webview

    from lab import __version__
    from lab.agents import sync_agents
    from lab.collect import catchup, catchup_all
    from lab.workspace import ensure_unit

    from lab.brand import icon_path as brand_icon_path

    try:
        brand_icon_path()  # 品牌装配先判：缺图标就别先起服务再报错（刀 W25a §5.4）
        ensure_unit(unit_root)  # a freshly chosen workspace has no .groma yet
        if not _claim_single_instance():
            _log("groma shell: already running for this unit")
            return
        sync_agents(unit_root)
        catchup(unit_root, written_by="shell:start")  # app-start trigger, rate-limited and paused-aware
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then re-raised
        _fatal("无法启动：%s" % exc)
        raise

    shell_state = {"root": unit_root}
    icon_holder: dict = {}

    def on_switch(new_root) -> None:
        shell_state["root"] = new_root
        icon = icon_holder.get("icon")
        if icon is not None:
            icon.title = "Groma v%s · " % __version__ + controller.status(new_root)["label"]
            icon.update_menu()

    def collector_loop() -> None:
        import time

        while True:
            time.sleep(60)
            try:
                catchup_all(written_by="shell:timer")  # each workspace keeps its own interval
            except Exception:
                pass

    threading.Thread(target=collector_loop, daemon=True).start()

    server = LocalServer(unit_root, port=port, on_switch=on_switch).start()
    url = "http://127.0.0.1:%d/" % server.port
    _log("groma shell: serving %s" % url)
    window = webview.create_window("Groma v%s" % __version__, url, width=1120, height=800)
    _log("groma shell: window created")

    # Closing the window hides it; the tray keeps collecting. Quitting is an
    # explicit tray action, otherwise "close" becomes an accidental stop.
    allow_close = {"value": False}

    def on_closing():
        if allow_close["value"]:
            return True
        try:
            window.hide()
        except Exception:
            pass
        return False

    try:
        window.events.closing += on_closing
    except Exception:
        pass

    def open_window(icon=None, item=None):
        try:
            window.show()
        except Exception:
            pass

    def toggle(icon, item):
        state = controller.toggle(unit_root)
        icon.title = "Groma v%s · " % __version__ + state["label"]
        icon.update_menu()

    def quit_app(icon, item):
        allow_close["value"] = True
        try:
            window.destroy()
        except Exception:
            pass

    def show_about(icon=None, item=None):
        open_window()
        try:
            window.load_url(controller.about_url(port))
        except Exception as exc:  # noqa: BLE001 - 托盘动作失败不能把壳带崩
            _log("groma shell: 打开关于页失败：%s" % exc)

    def check_update(icon=None, item=None):
        """用户点一次才查一次：查完落到关于页，结果走 notice（刀 W25d）。"""
        from lab import update

        try:
            result = update.check_latest()
        except Exception as exc:  # noqa: BLE001 - 网络层怎么炸都要变成看得见的一句话
            result = update.record(state="network_error", error=str(exc))
        update.save(unit_root, result)
        open_window()
        try:
            window.load_url("%s?notice=%s" % (controller.about_url(port),
                                              quote(update.describe(result))))
        except Exception as exc:  # noqa: BLE001
            _log("groma shell: 检查更新后跳转失败：%s" % exc)

    handlers = {
        "open_window": open_window,
        "open_about": show_about,
        "check_update": check_update,
        "toggle_capture": toggle,
        "quit": quit_app,
    }
    menu = pystray.Menu(*[
        # 暂停项的文案必须跟着机器状态走，所以这一条给 pystray 传 callable 而不是定值（§7）
        pystray.MenuItem(
            (lambda _i: controller.status(unit_root)["menu_label"])
            if spec["action"] == "toggle_capture" else spec["label"],
            handlers[spec["action"]],
            default=spec["default"],
        )
        for spec in controller.menu_items(port, controller.status(unit_root))
    ])
    icon = pystray.Icon("Groma", _icon_image(), "Groma v%s · " % __version__ + controller.status(unit_root)["label"], menu)
    icon_holder["icon"] = icon
    threading.Thread(target=icon.run, daemon=True).start()
    _log("groma shell: tray started")

    try:
        webview.start()
        _log("groma shell: webview returned")
    finally:
        try:
            icon.stop()
        finally:
            server.stop()
            _log("groma shell: stopped")
