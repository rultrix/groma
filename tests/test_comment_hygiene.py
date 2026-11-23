"""代码与模板的注释里不许写施工编号（项目所有者 2026-11-20 立，全项目适用）。

判据是"命中集为空"，逐实例报名字（文件:行 + 那一行），因为一条"整文件有没有"的断言
挡不住同文件里的第二处残留。检索式与 `docs/RULES.md` 文档约定节那条一致。

这里把检索式拼出来而不是写成字面量：守卫自己也是被扫的源码，写死就会自己报自己。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("lab", "web", "tests", "tools", "groma_shell")
SCAN_SUFFIXES = {".py", ".html", ".css", ".js"}

# 发布管线自己的扫描器（tools/release/）**必须**写出它要抓的词，否则抓不到；
# 而那份文件不进公开树（公开快照脚本自己说明过）。所以它整体豁免，不参与本守卫。
SCAN_EXEMPT_DIRS = ("tools/release",)

BUILD_NUMBER = re.compile(
    "刀 ?[VWKR]" + r"\d+[a-z]?"          # 旧编号：字母+数字
    + "|(?<![A-Za-z0-9_])(?:U|T)" + r"\d{1,2}[a-z]?"   # 期号/刀号（字母+数字）
    + "|裁定 ?" + r"\d+"                 # 裁定编号
    + "|任务书 §" + r"\d+"               # 任务书章节号
)


def build_number_hits():
    """返回 [(相对路径, 行号, 行内容)]，按路径与行号排序。"""
    found = []
    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if any(rel == d or rel.startswith(d + "/") for d in SCAN_EXEMPT_DIRS):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if BUILD_NUMBER.search(line):
                    found.append((rel, lineno, line.strip()))
    return found


class BuildNumberGuard(unittest.TestCase):
    def test_the_code_and_templates_carry_no_build_numbers(self) -> None:
        hits = build_number_hits()
        report = "\n".join("%s:%d| %s" % item for item in hits)
        self.assertEqual(
            hits, [],
            "注释里写的是哪张任务书的第几刀，任务书一改版它就是假线索，而且读的人无法就地核实。"
            "只留「为什么」这半句：%d 处残留\n%s" % (len(hits), report),
        )


if __name__ == "__main__":
    unittest.main()
