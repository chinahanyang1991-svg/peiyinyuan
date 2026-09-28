"""
WF-20 回归测试：`.md` 进入支持格式表，且"支持清单"三处不再各说各话。

三件事实源必须一致，缺一处就是用户可见缺陷：
  1. ParserFactory.FORMAT_MAP —— 唯一生产事实源（backend/api/books.py 的上传白名单读它）
  2. Config.SUPPORTED_EBOOK_FORMATS —— 旧版手抄，实测已缺 .html/.docx
  3. frontend/src/routes/ImportPage.tsx 的 SUPPORTED_FORMATS —— 旧版还留着后端
     已删的 .chm（选了必被 400），同时把后端本就支持的 .docx/.html 藏住不给选

Markdown 剥记号本身的判据是红线③：`#`/`**`/`|`/反引号是排版指令不是正文，
留在正文里会被 TTS 逐字念出来。
"""

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from modules.book_parser.parser_factory import ParserFactory          # noqa: E402
from modules.book_parser.parsers.md_parser import MarkdownParser, strip_markdown  # noqa: E402

SAMPLE = """# 星辰之海

> 题记：献给所有夜航的人

## 第一章 起航

那是**一个*平常*的清晨**，老船长在 `驾驶舱` 里看着[海图](http://example.com/chart)。
他数着桅杆上的绳结，一个结代表一年，二十年过去了，海还是那片海，人却已经不是当年
一起下海的那批人。学徒在码头上喊他，他才回过神来，把海图卷起来塞进油布里。

![封面](cover.png)

| 角色 | 音色 |
| --- | --- |
| 船长 | 成年男 |
| 学徒 | 少年 |

- 备帆
-  check 罗盘
1. 出发

```python
def noise(): return "这段不该被念出来"
```

## 第二章 风暴

海面像一堵墙。**浪**扑了过来。学徒第一次见到 `max_retry_count` 这样的字样被写在
航海日志的页边——老船长说那是他给自己定的规矩，风浪超过三次就要掉头，可那天他们
谁都没想起这条规矩。

---

尾声见[后记][ref]。

[ref]: http://example.com/postscript
"""


class TestStripMarkdown:
    """剥记号的每条规则都要能被单独判死，故逐类断言。"""

    def test_headings_drop_hashes_but_keep_text_as_standalone_line(self):
        out = strip_markdown(SAMPLE)
        assert "#" not in out
        assert "第一章 起航" in out.split("\n")
        assert "第二章 风暴" in out.split("\n")

    def test_emphasis_and_inline_code_are_markers_not_content(self):
        out = strip_markdown(SAMPLE)
        for junk in ("**", "__", "`"):
            assert junk not in out
        assert "一个平常的清晨" in out          # 星号贴着汉字也要剥
        assert "浪扑了过来" in out
        assert "max_retry_count" in out         # 下划线守卫只挡 ASCII，蛇形命名不得被吃掉
        assert "驾驶舱" in out

    def test_links_keep_text_and_drop_url_and_anchor_syntax(self):
        out = strip_markdown(SAMPLE)
        assert "[海图]" not in out and "(http" not in out and "http://example.com/chart" not in out
        assert "海图" in out
        # 引用式链接与其定义行：文字留下，定义行整行消失
        assert "后记" in out and "[ref]" not in out and "example.com/postscript" not in out

    def test_images_leave_no_alt_text(self):
        out = strip_markdown(SAMPLE)
        assert "![" not in out and "cover.png" not in out and "封面" not in out

    def test_html_comments_are_dropped(self):
        assert strip_markdown("<!-- 注释不留痕 -->正文") == "正文"

    def test_fenced_code_block_content_is_dropped(self):
        out = strip_markdown(SAMPLE)
        assert "这段不该被念出来" not in out and "def noise()" not in out and "```" not in out

    def test_table_becomes_cells_joined_not_pipe_soup(self):
        out = strip_markdown(SAMPLE)
        assert "|" not in out
        assert "船长，成年男" in out

    def test_list_and_rule_and_blockquote_markers_gone(self):
        out = strip_markdown(SAMPLE)
        assert "- 备帆" not in out and "备帆" in out
        assert "check 罗盘" in out
        assert "题记：献给所有夜航的人" in out and "> " not in out
        assert not [ln for ln in out.split("\n") if ln.strip() in ("---", "***", "___")]

    def test_escaped_punctuation_unwraps(self):
        assert strip_markdown(r"100\% 完成") == "100% 完成"

    def test_cjk_bytes_are_untouched_by_stripping(self):
        """红线"全格式无乱码"：剥记号只做替换，不得改动汉字本体。"""
        text = "第三章　风与浪，「他」说——好！"
        assert strip_markdown(text) == text


class TestFactoryRegistration:
    def test_md_maps_to_markdown_parser(self):
        assert ParserFactory.FORMAT_MAP[".md"] == "md"
        parser = ParserFactory.get_parser("book.md")
        assert isinstance(parser, MarkdownParser)

    def test_chm_still_has_no_parser(self):
        """PY-102 的裁定不能被 WF-20 顺带放行。"""
        assert ParserFactory.get_parser("book.chm") is None

    def test_end_to_end_parse_splits_chapters_and_is_clean(self, tmp_path):
        path = tmp_path / "sample.md"
        path.write_text(SAMPLE, encoding="utf-8")
        data = ParserFactory.parse(str(path))
        assert data.format == "md"
        assert data.title == "星辰之海"
        titles = [ch["title"] for ch in data.chapters]
        assert any("第一章" in t for t in titles), titles
        assert any("第二章" in t for t in titles), titles
        body = "\n".join(ch["content"] for ch in data.chapters)
        for junk in ("#", "**", "|", "```", "http"):
            assert junk not in body, junk
        assert any("起航" in t for t in titles) and any("风暴" in t for t in titles)
        assert "一个平常的清晨" in body and "浪扑了过来" in body

    def test_gbk_markdown_reads_without_mojibake(self, tmp_path):
        path = tmp_path / "gbk.md"
        path.write_bytes("# 旧编码\n\n## 第一章 测试\n\n正文内容一，二，三。\n".encode("gbk"))
        data = MarkdownParser(str(path)).parse()
        assert "旧编码" in data.title
        assert "第一章 测试" in data.chapters[0]["content"]


class TestSingleSourceOfTruth:
    """三处清单两两比对：任何一处漂移都在这里红。"""

    def test_config_is_projection_of_format_map(self):
        from backend.config import Config
        assert Config.SUPPORTED_EBOOK_FORMATS == sorted(ParserFactory.FORMAT_MAP.keys())

    def test_frontend_list_mirrors_format_map(self):
        tsx = os.path.join(ROOT, "frontend", "src", "routes", "ImportPage.tsx")
        if not os.path.exists(tsx):
            pytest.skip("前端源码不在本工作树")
        with open(tsx, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"const SUPPORTED_FORMATS\s*=\s*\[(.*?)\]", src, re.S)
        assert m, "ImportPage.tsx 里的 SUPPORTED_FORMATS 结构变了，契约断言需要跟着改"
        listed = sorted(set(re.findall(r'"(\.[A-Za-z0-9]+)"', m.group(1))))
        assert listed == sorted(ParserFactory.FORMAT_MAP.keys()), listed
