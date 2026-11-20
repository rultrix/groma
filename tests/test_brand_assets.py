"""品牌资产判据（B1–B4）：几何、ICO 档位、spec 接线、托盘资源解析。

跑法：`./.venv/Scripts/python.exe -m unittest discover -s tests -p test_brand_assets.py -v`
"""

from __future__ import annotations

import statebox  # noqa: F401  -- must be first real import
import re
import unittest
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover -- 取决于解释器装没装 Pillow
    Image = None

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
REQUIRED_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def geometry(text: str) -> dict:
    """从 SVG 源码里取几何——判据读源码而不是读渲染结果，因为渲染要引外部光栅器。"""
    return {
        "rect": re.findall(r"<rect\b[^>]*>", text),
        "line": re.findall(r"<line\b[^>]*>", text),
        "circle": re.findall(r"<circle\b[^>]*>", text),
    }


class MarkGeometryTests(unittest.TestCase):
    def test_mark_svg_has_four_observation_points(self) -> None:  # B1
        parts = geometry((BRAND / "groma-mark.svg").read_text(encoding="utf-8"))
        self.assertEqual(len(parts["rect"]), 1, "徽标底要恰好一个圆角方形")
        self.assertIn('rx="56"', parts["rect"][0])
        self.assertIn('fill="#0f766e"', parts["rect"][0], "主色必须是产品 token，不许另发明一版 teal")
        self.assertEqual(len(parts["line"]), 2, "十字要恰好两条基准线")
        for line in parts["line"]:
            self.assertIn('stroke="#ffffff"', line)
            self.assertIn('stroke-width="24"', line)
        self.assertEqual(len(parts["circle"]), 4, "四个观测点，不多不少")
        radii = {re.search(r'r="(\d+)"', dot).group(1) for dot in parts["circle"]}
        self.assertEqual(radii, {"22"}, "四个观测点必须等大")
        # 严格上下左右对称：四个圆心到 (128,128) 的偏移必须是 ±84 的两两组合
        centers = {(int(re.search(r'cx="(\d+)"', d).group(1)), int(re.search(r'cy="(\d+)"', d).group(1)))
                   for d in parts["circle"]}
        self.assertEqual(centers, {(128, 44), (212, 128), (128, 212), (44, 128)})
        for line in parts["line"]:
            self.assertTrue(("y1=\"44\"" in line and "y2=\"212\"" in line)
                            or ("x1=\"44\"" in line and "x2=\"212\"" in line),
                            "十字线端必须与圆心重合（44↔212），否则观测点是浮空的")
        self.assertNotIn("<text", (BRAND / "groma-mark.svg").read_text(encoding="utf-8"),
                         "徽标里不许有文字（D5）")

    def test_logo_svg_shares_the_mark_geometry(self) -> None:
        """文档字标与应用图标同源；两份几何一旦分叉，16px 与 README 就是两个产品。"""
        mark = geometry((BRAND / "groma-mark.svg").read_text(encoding="utf-8"))
        logo = geometry((BRAND / "groma-logo.svg").read_text(encoding="utf-8"))
        for kind in ("rect", "line", "circle"):
            self.assertEqual(sorted(mark[kind]), sorted(logo[kind]), "%s 的形状与徽标不同源" % kind)


@unittest.skipUnless(Image, "Pillow is not installed in this interpreter")
class IcoTests(unittest.TestCase):
    def test_ico_contains_required_sizes(self) -> None:  # B2
        path = BRAND / "groma.ico"
        self.assertTrue(path.is_file(), "先跑 tools/brand/build_icons.py")
        with Image.open(path) as icon:
            sizes = sorted({width for (width, _height) in icon.ico.sizes()})
        self.assertEqual(sizes, list(REQUIRED_ICO_SIZES),
                         "ICO 必须正好七档且不多装预览尺寸——1 字节宽字段塞不下 512，会写成 0")

    def test_small_frames_keep_the_cross_readable(self) -> None:
        """16/24/32 档要仍能认出「十字 + 四观测点」：两条臂都成线、四个端点都落在白上、笔画不是发丝。

        这条是被自己的假测量逼出来的：第一版把"最小笔画 2px"夹在 4 倍画布空间里，最终像素只剩
        0.5px；而第一版判据又拿 `== (255,255,255)` 认白，把重采样后的 (250,252,252) 误记成底色，
        量出来"十字不见了"。现在分类器带容差，阈值来自实测（16px 实测竖臂 14/16、笔画 2px）。
        """
        import sys

        sys.path.insert(0, str(ROOT / "tools" / "brand"))
        from build_icons import draw

        for size in (16, 24, 32):
            image = draw(size).convert("RGBA")
            pixels = image.load()

            def white(px) -> bool:
                return px[3] > 40 and min(px[:3]) >= 170 and max(px[:3]) - min(px[:3]) < 60

            center = size // 2
            arm = min(round(84 * size / 256), center - 1)
            vertical = sum(1 for y in range(size) if white(pixels[center, y]))
            horizontal = sum(1 for x in range(size) if white(pixels[x, center]))
            self.assertGreaterEqual(vertical, size * 0.6, "size=%d 竖臂断了" % size)
            self.assertGreaterEqual(horizontal, size * 0.6, "size=%d 横臂断了" % size)
            for x, y in ((center, center - arm), (center + arm, center),
                         (center, center + arm), (center - arm, center)):
                self.assertTrue(white(pixels[x, y]), "size=%d 的观测点 (%d,%d) 没落在白上" % (size, x, y))
            stroke = sum(1 for x in range(size) if white(pixels[x, center - arm // 2]))
            self.assertGreaterEqual(stroke, 2, "size=%d 的笔画退化成发丝线" % size)


class WiringTests(unittest.TestCase):
    def test_specs_embed_brand_icon_and_bundle_assets(self) -> None:  # B3
        main = (ROOT / "groma.spec").read_text(encoding="utf-8")
        installer = (ROOT / "install-groma.spec").read_text(encoding="utf-8")
        self.assertIn("icon='assets/brand/groma.ico'", main, "主 exe 没内嵌品牌图标")
        self.assertIn("icon='assets/brand/groma.ico'", installer, "安装器没内嵌品牌图标")
        self.assertIn("('assets', 'assets')", main, "冻结态拿不到 assets/ ⇒ 托盘资源解析会抛错")

    def test_tray_icon_resolves_packaged_asset_without_gui(self) -> None:  # B4
        from lab.brand import icon_path

        self.assertEqual(icon_path().name, "groma.ico")
        self.assertTrue(icon_path().is_file())

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                icon_path(Path(tmp))      # 缺资源必须抛，不许静默回旧圆标

        from groma_shell import app as shell_app

        image = shell_app._icon_image()   # 这里不启动 pystray
        self.assertEqual(image.size, (64, 64))
        self.assertGreater(sum(1 for pixel in image.getdata() if pixel[3] > 40), 1000)


if __name__ == "__main__":
    unittest.main()
