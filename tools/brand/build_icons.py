"""从 groma-mark.svg 的同一几何参数绘制 ICO 与 PNG 预览。

几何源 = assets/brand/groma-mark.svg（256×256 坐标）。本脚本不读 SVG、不依赖外部光栅器：
两边各持同一组常数，`tests/test_brand_assets.py` 盯住这组常数不被单方面改掉。每一档在
`SUPER` 倍画布上用**整数坐标**等比绘制，再 LANCZOS 收敛到目标尺寸——小档纯整数直绘会把圆
画成楼梯，正是 §5.2「16px 不能靠细线」要避免的脏边。

ICO 用自写容器（PNG 压缩条目）而**不用 Pillow 的 ICO 编码器**：编码器会拿单张 256 自己缩放，
"每一档按整数坐标绘制"就成了假话。容器写完当场用 `ExtractIconExW` 让 Windows 解析一次，
解析不出就退给 Pillow 编码器重做并在输出里说明——兼容风险当场判，不留到真机装机才发现。

用法：`./.venv/Scripts/python.exe tools/brand/build_icons.py`
"""

from __future__ import annotations

import hashlib
import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SPEC = 256
INSET = 16
RADIUS = 56
CENTER = 128
ARM = 84          # 十字臂端离圆心的距离（128-44）
STROKE = 24
DOT = 22          # 观测点半径
INK = (15, 118, 110, 255)      # #0f766e，与产品 token 同值
PAPER = (255, 255, 255, 255)

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
PNG_SIZES = (256, 512, 1024)
SUPER = 4


def draw(size: int) -> Image.Image:
    """按 SPEC 空间的常数绘制一档；坐标在 size*SUPER 画布上取整。"""
    canvas = size * SUPER
    scale = canvas / SPEC

    def at(value: int) -> int:
        return round(value * scale)

    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    inset, center, arm = at(INSET), at(CENTER), at(ARM)
    corner = min(at(RADIUS), (canvas - 2 * inset) // 2)
    pen.rounded_rectangle([inset, inset, canvas - inset - 1, canvas - inset - 1],
                          radius=corner, fill=INK)
    half = max(at(STROKE) // 2, SUPER)     # 笔画最细 2 个**最终**像素（夹在画布空间等于没夹）
    pen.rectangle([center - half, center - arm, center + half - 1, center + arm], fill=PAPER)
    pen.rectangle([center - arm, center - half, center + arm, center + half - 1], fill=PAPER)
    dot = max(at(DOT), 2 * SUPER)          # 观测点半径最小 2 个最终像素 ⇒ 比笔画粗，才读得出"点"
    for dx, dy in ((0, -arm), (arm, 0), (0, arm), (-arm, 0)):
        cx, cy = center + dx, center + dy
        pen.ellipse([cx - dot, cy - dot, cx + dot - 1, cy + dot - 1], fill=PAPER)
    return image.resize((size, size), Image.LANCZOS)


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_ico(path: Path, frames: dict[int, Image.Image]) -> None:
    """ICONDIR(6B) + N×ICONDIRENTRY(16B) + N 个 PNG blob。"""
    ordered = sorted(frames, reverse=True)
    blob_start = 6 + 16 * len(ordered)
    entries, bodies, offset = [], [], blob_start
    for size in ordered:
        blob = png_bytes(frames[size])
        # 256 在 1 字节的宽/高字段里写作 0（ICO 约定）
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                                   len(blob), offset))
        bodies.append(blob)
        offset += len(blob)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(ordered)) + b"".join(entries) + b"".join(bodies))


def windows_accepts(path: Path) -> int | None:
    """让 Windows 自己解析这个 ICO，返回它数到的图标数；非 Windows 返回 None。"""
    if not sys.platform.startswith("win"):
        return None
    import ctypes

    return ctypes.windll.shell32.ExtractIconExW(str(path), 0, None, None, 0)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    folder = Path(__file__).resolve().parents[2] / "assets" / "brand"
    folder.mkdir(parents=True, exist_ok=True)

    frames = {size: draw(size) for size in sorted(set(ICO_SIZES) | set(PNG_SIZES))}
    ico = folder / "groma.ico"
    # 只把 ICO_SIZES 那七档装进容器：512/1024 是预览用，塞进去会把 1 字节的宽字段截成 0，
    # Windows 与 Pillow 都会读到"声明 256、实际 512"的坏条目。
    write_ico(ico, {size: frames[size] for size in ICO_SIZES})

    with Image.open(ico) as reopened:
        available = sorted({w for (w, _h) in reopened.ico.sizes()})
    missing = [size for size in ICO_SIZES if size not in available]
    extra = [size for size in available if size not in ICO_SIZES]
    accepted = windows_accepts(ico)
    print("ICO %d 字节；Pillow 重开可见尺寸=%s；缺失=%s；多余=%s"
          % (ico.stat().st_size, available, missing or "无", extra or "无"))
    print("Windows ExtractIconExW 组数：%s（≥1 表示这个 .ico 能被 shell 解析；多尺寸只算一组，逐档正确性由上面的 Pillow 枚举钉）"
          % ("非 Windows，未验" if accepted is None else accepted))
    if missing or extra or accepted == 0:
        print("⇒ 自写容器不合格，改用 Pillow 编码器重做（每档几何由 Pillow 缩放 256 帧得到）")
        frames[max(ICO_SIZES)].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
        with Image.open(ico) as reopened:
            print("   重做后可见尺寸：%s" % sorted({w for (w, _h) in reopened.ico.sizes()}))
    for size in PNG_SIZES:
        path = folder / ("groma-%d.png" % size)
        frames[size].save(path, format="PNG")
        print("PNG %s %d 字节 sha256=%s" % (path.name, path.stat().st_size, digest(path)[:16]))
    print("SHA-256 groma.ico = %s" % digest(ico))


if __name__ == "__main__":
    main()
