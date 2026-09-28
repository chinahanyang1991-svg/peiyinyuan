"""
电子书解析器单元测试
"""
import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.book_parser.text_cleaner import TextCleaner


class TestTextCleaner:
    """文本清洗器测试"""

    def setup_method(self):
        self.cleaner = TextCleaner()

    def test_clean_removes_ad_patterns(self, sample_book_text):
        """应移除广告/推广内容"""
        result = self.cleaner.clean(sample_book_text)
        assert "更多精彩内容请访问" not in result
        assert "www.example.com" not in result
        assert "扫码下载APP" not in result

    def test_clean_removes_watermark(self):
        """应移除水印内容"""
        text = "这是一段正常文本\n本文由某某整理发布\n这是另一段文本"
        result = self.cleaner.clean(text)
        assert "本文由某某整理发布" not in result

    def test_clean_number_lines_py106_contract(self):
        """[2H 裁定: 契约已变更 → 按新契约改写测试，产品码不动] PY-106 / PY-204

        本用例原名 test_clean_preserves_number_lines，断言 `"123" in clean("正文\\n  123  \\n正文")`，
        依据的是清洗器 **v2** 的注释"不删除纯数字行（可能是小节编号，如 1 2 3 是小说小节分隔）"。
        PY-106(commit 346582c) 把清洗器升到 v3，PAGE_PATTERNS 末条 `^\\d{1,4}\\s*$`
        明确"整行只有 <=4 位数字"= 扫描版书页码/页脚 → **删**（同批 PY-204 在真实民法典 PDF 上
        删 258 条纯数字页脚、0 误删，见 qa/verify-round2.md §PDF 民法典 表）。
        qa/harness/run_p3_clean.py 把 `3`(噪声键 chapter_num_line) 与 `- 12 -` 同列为 NOISE，
        且 §3 断言真实 MOBI 清洗后 `^\\d{1,4}$` 残留行数 == 0。
        → 测试场景 `"正文内容\\n  123  \\n更多正文"` 属**纯数字页脚行**一类，不是小节编号：
          真小节编号在 ChapterDetector 里要求"数字 + 分隔符 + 文字"(`一、初见`/`1、春雨`)或
          `第一节 春猎`，含非数字字符，不整行命中 PAGE_PATTERNS → 仍保留（下面有断言）；
          "1 2 3" 这类序列语境行因 `^\\d{1,4}\\s*$` 要求整行仅数字+尾部空白，也不会命中。
        红线(不许误删正文数字句)由下面的 prose_keep 断言守住，不靠放宽断言换取通过。
        """
        text = "正文内容\n  123  \n更多正文"
        result = self.cleaner.clean(text)

        # 新契约：整行纯数字(<=4 位) = 页码/页脚噪声 → 删除，前后正文保留
        assert "123" not in result
        assert "正文内容" in result and "更多正文" in result

        # PAGE_PATTERNS 家族逐条整行删除（PY-106/PY-204）
        for noise in ["3", "- 12 -", "— 8 —", "第 1 页 共 200 页", "共 200 页",
                      "页码：12", "12 / 300"]:
            cleaned = self.cleaner.clean("上文\n" + noise + "\n下文")
            assert noise not in cleaned, f"页码/页脚噪声未删: {noise!r}"
            assert "上文" in cleaned and "下文" in cleaned

        # 红线：数字/页码出现在正文句内、小节编号标题、多位数字行一律保留
        prose_keep = [
            "他等了 123 天才回到京城。",
            "全书共 200 页，编者附注说明。",
            "第 3 页的内容已被删去",
            "第一节 春猎",
            "一、初见",
            "1、春雨",
            "1 2 3",
            "123456789",
        ]
        cleaned = self.cleaner.clean("\n".join(prose_keep))
        for keep in prose_keep:
            assert keep in cleaned, f"误删正文/小节编号: {keep!r}"

    def test_clean_removes_urls(self):
        """应移除短URL行"""
        text = "正文\nhttps://example.com/abc\n更多正文"
        result = self.cleaner.clean(text)
        assert "https://" not in result

    def test_clean_preserves_normal_text(self):
        """应保留正常文本内容"""
        text = "这是第一行正常文本。\n\n这是第二行正常文本。"
        result = self.cleaner.clean(text)
        assert "这是第一行正常文本" in result
        assert "这是第二行正常文本" in result

    def test_clean_normalizes_whitespace(self):
        """应规范化空白字符"""
        text = "第一行\n\n\n\n第二行\n\n第三行"
        result = self.cleaner.clean(text)
        lines = [l for l in result.split('\n') if l]
        assert len(lines) == 3

    def test_quick_clean_preserves_structure(self, sample_book_text):
        """快速清洗应保留段落结构"""
        result = self.cleaner.quick_clean(sample_book_text)
        assert "第一章" in result
        assert "第二章" in result
        assert "第三章" in result

    def test_empty_text(self):
        """空文本应返回空字符串"""
        assert self.cleaner.clean("") == ""
        assert self.cleaner.clean("   ") == ""

    def test_ad_patterns_coverage(self):
        """验证所有广告模式都能匹配"""
        ad_texts = [
            "更多精彩内容请访问我们的网站",
            "请记住我们的网址是 example.com",
            "关注我们的公众号获取更多",
            "加入书友群一起讨论",
            "求推荐票支持作者",
            "求月票支持作者",
            "打赏作者支持创作",
        ]
        for ad in ad_texts:
            result = self.cleaner.clean(ad)
            assert result == "", f"Ad pattern not caught: {ad}"


class TestChapterDetector:
    """章节检测器测试"""

    def setup_method(self):
        from modules.book_parser.chapter_detector import ChapterDetector
        self.detector = ChapterDetector

    def test_detect_chinese_chapters(self):
        """应检测中文章节标题"""
        text = "第一章 初遇\n内容1\n第二章 重逢\n内容2"
        result = self.detector(text).detect()
        assert len(result) >= 2
        assert result[0]["title"] == "第一章 初遇"
        assert result[1]["title"] == "第二章 重逢"

    def test_detect_numbered_chapters(self):
        """应检测数字编号章节"""
        text = "一、相遇\n内容1\n二、相知\n内容2"
        result = self.detector(text).detect()
        assert len(result) >= 2

    def test_filter_short_chapters(self):
        """应过滤过短的章节"""
        text = "第一章 标题\n短\n第二章 正文\n这是一段足够长的内容"
        result = self.detector(text).detect()
        assert len(result) == 1


class TestParserFactory:
    """解析器工厂测试"""

    def test_get_parser_epub(self):
        from modules.book_parser.parser_factory import ParserFactory
        parser = ParserFactory.get_parser("test.epub")
        assert parser is not None

    def test_get_parser_txt(self):
        from modules.book_parser.parser_factory import ParserFactory
        parser = ParserFactory.get_parser("test.txt")
        assert parser is not None

    def test_get_parser_pdf(self):
        from modules.book_parser.parser_factory import ParserFactory
        parser = ParserFactory.get_parser("test.pdf")
        assert parser is not None

    def test_get_parser_unsupported(self):
        from modules.book_parser.parser_factory import ParserFactory
        parser = ParserFactory.get_parser("test.doc")
        assert parser is None