"""
Markdown 电子书解析器（WF-20）

先剥记号再走 TXT 链路：`#`/`**`/`` ` ``/`[]()` 这些是排版指令不是正文，
留着会被 TTS 当字念出来（红线③"音频无杂音"）。剥完之后 `## 第三章 xxx` 变成
独立一行 `第三章 xxx`，ChapterDetector 的中文章题模式照样命中。
"""

import re

from modules.book_parser.parser_factory import BookData
from modules.book_parser.parsers.txt_parser import TxtParser

# 顺序即语义：先去成块结构（代码块/注释），再去行内标记，最后收空白。
_RULES = [
    (re.compile(r"<!--.*?-->", re.S), ""),
    (re.compile(r"^[ \t]{0,3}(```|~~~)[^\n]*\n.*?(?:^[ \t]{0,3}\1[ \t]*$\n?|\Z)",
                re.M | re.S), ""),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),                 # 图片：alt 不是正文
    (re.compile(r"\[([^\]]*)\]\(([^)]*)\)"), r"\1"),           # 链接留文字丢 URL
    (re.compile(r"\[([^\]]*)\]\[[^\]]*\]"), r"\1"),            # 引用式链接
    (re.compile(r"^[ \t]{0,3}\[[^\]]+\]:[ \t]*\S.*$", re.M), ""),    # 链接定义行
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M), ""),              # ATX 标题
    (re.compile(r"^[ \t]{0,3}>[ \t]?", re.M), ""),                   # 引用
    (re.compile(r"^[ \t]*[-*+][ \t]+", re.M), ""),                   # 无序列表项
    (re.compile(r"^[ \t]*\d+[.)][ \t]+", re.M), ""),                 # 有序列表项
    (re.compile(r"^[ \t]*\|?[ \t]*[:\-|]{3,}[ \t]*\|?[ \t]*$", re.M), ""),  # 表头分隔行
    (re.compile(r"^[ \t]{0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$", re.M), ""),   # 分割线
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<![0-9A-Za-z])\*(?!\s)(.+?)(?<!\s)\*(?![0-9A-Za-z])"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"(?<![0-9A-Za-z])_(?!\s)(.+?)(?<!\s)_(?![0-9A-Za-z])"), r"\1"),
    (re.compile(r"`{1,3}([^`]*)`{1,3}"), r"\1"),               # 行内代码只留内容
    (re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|%&])"), r"\1"),   # 反斜杠转义
    (re.compile(r"\n{3,}"), "\n\n"),
]

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _flatten_table_row(line: str) -> str:
    """表格行 `| 甲 | 乙 |` -> `甲，乙`：竖线本身念不出来，单元格用顿号连。"""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return "，".join(c for c in cells if c)


def strip_markdown(text: str) -> str:
    """把 Markdown 源文剥成可朗读的纯文本（不改变任何汉字编码）。"""
    out = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    lines = [_flatten_table_row(ln) if _TABLE_ROW_RE.match(ln) else ln
             for ln in out.split("\n")]
    return "\n".join(lines).strip()


class MarkdownParser(TxtParser):
    """.md 解析器：读取/编码探测/书名回退全部继承 TXT，只多一道剥记号。"""

    def _read_with_encoding_detection(self):
        raw, encoding = super()._read_with_encoding_detection()
        return strip_markdown(raw), encoding

    def parse(self) -> BookData:
        data = super().parse()
        data.format = "md"
        return data
