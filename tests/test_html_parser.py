"""HTML 解析器测试"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestHtmlParser:
    def test_basic_parse(self):
        from modules.book_parser.parsers.html_parser import HtmlParser
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as f:
            f.write("<html><head><title>测试书</title></head><body><h1>第一章</h1><p>这是第一章的内容。</p><h2>第二章</h2><p>这是第二章的内容。</p></body></html>")
            path = f.name
        try:
            parser = HtmlParser(path)
            data = parser.parse()
            assert data.format == "html"
            assert data.title == "测试书"
            assert data.total_chars > 0
            assert len(data.chapters) >= 1
        finally:
            os.unlink(path)

    def test_no_title_uses_filename(self):
        from modules.book_parser.parsers.html_parser import HtmlParser
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as f:
            f.write("<html><body><p>Hello World</p></body></html>")
            path = f.name
        try:
            parser = HtmlParser(path)
            data = parser.parse()
            assert data.title  # should use filename
            assert any(ch["content"] for ch in data.chapters)
        finally:
            os.unlink(path)

    def test_in_parser_factory(self):
        from modules.book_parser.parser_factory import ParserFactory
        parser = ParserFactory.get_parser("test.html")
        assert parser is not None
        assert parser.__class__.__name__ == "HtmlParser"

        parser2 = ParserFactory.get_parser("test.htm")
        assert parser2 is not None
        assert parser2.__class__.__name__ == "HtmlParser"


class TestTextNormalizer:
    def test_import(self):
        from modules.text_processor.text_normalizer import TextNormalizer
        n = TextNormalizer()
        assert n is not None

    def test_normalize_whitespace(self):
        from modules.text_processor.text_normalizer import TextNormalizer
        n = TextNormalizer()
        result = n.normalize("Hello   世界！  ")
        assert "Hello 世界！" == result

    def test_quick_normalize(self):
        from modules.text_processor.text_normalizer import TextNormalizer
        n = TextNormalizer()
        result = n.quick_normalize("  测试 文本  ")
        assert result == "测试 文本"