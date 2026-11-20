"""品牌资产的位置：托盘、exe、favicon、关于页都从这一处取，不各算各的。

冻结态（PyInstaller onedir）里资源在 `sys._MEIPASS` 下；源码态回仓根。缺文件**抛错**而不是
回退到旧临时圆标——品牌接线断了属于装配缺失，静默降级会让人以为新 Logo 已生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

RELATIVE = Path("assets") / "brand" / "groma.ico"


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def icon_path(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else bundle_root()
    path = base / RELATIVE
    if not path.is_file():
        raise FileNotFoundError("品牌图标未接入：%s（先运行 tools/brand/build_icons.py）" % path.as_posix())
    return path
