"""Tray-facing actions, kept free of GUI imports so they can be tested."""

from __future__ import annotations

from pathlib import Path

from lab.control import machine_state, pause_machine, resume_machine
from lab.workspace import load_unit


def status(root: Path) -> dict:
    paused = machine_state()["paused"]
    unit = load_unit(root)
    return {
        "unit": unit.get("name"),
        "paused": paused,
        "label": "已暂停" if paused else "采集中",
        "menu_label": "恢复采集" if paused else "暂停采集",
    }


def toggle(root: Path) -> dict:
    if machine_state()["paused"]:
        resume_machine()
    else:
        pause_machine()
    return status(root)


def about_url(port: int) -> str:
    """本机界面上的关于页地址——托盘和「检查更新」的落点都从这里算，别各写一份。"""
    return "http://127.0.0.1:%d/about" % int(port)


def menu_items(port: int, capture: dict) -> list[dict]:
    """托盘菜单的形状：纯数据，`app.py` 只负责接到 pystray。

    这一层存在的理由是**可测**：菜单要是只写在 `pystray.Menu(...)` 里，判据就得把 GUI 拉起来。
    `capture` 传 `status()` 的结果，暂停项的文案在这里就定下来，语义与改造前一致。
    """
    return [
        {"label": "打开窗口", "action": "open_window", "default": True, "url": None},
        {"label": "关于 Groma", "action": "open_about", "default": False, "url": about_url(port)},
        {"label": "检查更新", "action": "check_update", "default": False, "url": about_url(port)},
        {"label": capture.get("menu_label", "暂停采集"), "action": "toggle_capture",
         "default": False, "url": None},
        {"label": "退出", "action": "quit", "default": False, "url": None},
    ]
