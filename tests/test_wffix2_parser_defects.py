# -*- coding: utf-8 -*-
"""
WFFIX-2 回归测试：WF-02 / WF-03 / WF-14 / WF-18 四个解析与封装缺陷。

缺陷与断言对应关系:
  WF-02 EPUB 章题被复制进正文  -> 正文不再以章题开头，逐章字数与源正文守恒
  WF-03 MOBI 同类缺陷          -> _split_by_headings 不把标题元素自身文本回灌
  WF-14 DOCX 书名回退 UUID / 作者回退 python-docx -> 回退链与占位值过滤
  WF-18 m4b major_brand=M4A    -> 封装显式写品牌，产物 ftyp 为 M4B

审计样书已收进仓内 qa/harness/fixtures/wf_inputs（09-28），本文件不依赖仓库外路径；
仍保留"目录缺失即 skip"的兜底，便于把样书单独摘出去跑纯文本用例。
"""
import io
import math
import os
import re
import shutil
import struct
import subprocess
import uuid
import wave
from pathlib import Path

import pytest

# ----------------------------------------------------------------------
# 共用工具
# ----------------------------------------------------------------------

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                     re.IGNORECASE)

TITLE = "雨夜药铺"
CHAPTERS = [
    ("第一章 雨夜来客", [
        "雨点砸在青石板上，溅起一层白雾。林晚晴收了伞，站在药铺檐下，她抬手叩了三扇门环。",
        "“这么晚了，谁啊？”里头传来拖长的问话。",
        "“我姓林，找我爹留下的方子。”她的声音很轻，却一字一顿。",
        "门轴响了半声，灯光从门缝里淌出来。周建国站在门后，他手里还捏着半张药方。",
    ]),
    ("第二章 后院竹筐", [
        "第二天一早，后院堆着的竹筐被翻得乱七八糟。周队长站在井台边，他皱着眉数了两遍。",
        "“少了三个筐。”他说，“昨天收药时还在的。”",
        "小满端着热汤跨过门槛，她笑着把汤塞进林晚晴手里，“姐姐先喝口热的。”",
        "林晚晴接过瓦罐，她低头抿了一口，眼睛却盯着后院那扇虚掩的木门。",
    ]),
]

# 审计样书目录，存在才做实书对账。样书已从 PYY/wf-test 收进仓内 fixtures，
# 不再依赖仓库外的目录（缺目录会让三条实书用例静默 skip）。
SAMPLE_DIR = Path(os.environ.get(
    "WF_INPUTS_DIR",
    str(Path(__file__).resolve().parents[1] / "qa" / "harness" / "fixtures" / "wf_inputs"),
))


def squash(s):
    """去掉所有空白后比较/计数（与审计口径一致）"""
    return re.sub(r"\s+", "", s or "")


def chapter_body_chars():
    return {h: sum(len(p) for p in ps) for h, ps in CHAPTERS}


def sample_chapter_body_chars():
    """样书 txt -> {章题: 去空白正文字数}，跨格式统一的守恒基准。"""
    text = (SAMPLE_DIR / "wf_utf8.txt").read_text(encoding="utf-8")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out, cur = [], None
    for line in lines[1:]:  # 第 1 行是书名
        if re.match(r"^第[一二三四五六七八九十]+章\s", line):
            cur = (line, [])
            out.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return {h: len(squash("".join(ps))) for h, ps in out}


def build_epub(path, with_toc=True, toc_spacing=" "):
    """生成最小 EPUB：目录章题与正文 h1 的空白写法可故意不一致。"""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("wffix2-epub")
    book.set_title(TITLE)
    book.set_language("zh")
    book.add_author("WF测试")
    items = []
    for i, (heading, paras) in enumerate(CHAPTERS, 1):
        toc_title = heading.replace(" ", toc_spacing)
        it = epub.EpubHtml(title=toc_title, file_name="chap_%02d.xhtml" % i, lang="zh")
        it.content = ("<html><head><title>%s</title></head><body><h1>%s</h1>%s</body></html>"
                      % (toc_title, heading, "".join("<p>%s</p>" % p for p in paras)))
        book.add_item(it)
        items.append(it)
    if with_toc:
        book.toc = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + items
    epub.write_epub(str(path), book, {})
    return path


def write_docx(path, with_title_para=True, author=None, title_prop=None, chapters=None):
    from docx import Document

    doc = Document()
    if with_title_para:
        doc.add_heading(TITLE, level=0)  # python-docx: level 0 -> 样式 "Title"
    for heading, paras in (chapters or CHAPTERS):
        doc.add_heading(heading, level=1)
        for p in paras:
            doc.add_paragraph(p)
    props = doc.core_properties
    if author is not None:
        props.author = author
    if title_prop is not None:
        props.title = title_prop
    doc.save(str(path))
    return path


def tone_wav(ms, freq=300, sr=22050):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        n = int(sr * ms / 1000)
        wf.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * k / sr)))
            for k in range(n)))
    return buf.getvalue()


# ----------------------------------------------------------------------
# WF-02: EPUB 章题回灌正文
# ----------------------------------------------------------------------

class TestWf02EpubHeadingDedup:

    def test_helper_strips_only_the_matching_first_line(self):
        from modules.book_parser.parsers.epub_parser import strip_leading_duplicate

        body = "第一章 雨夜来客\n正文第一段。\n正文第二段。"
        assert strip_leading_duplicate(body, "第一章 雨夜来客") == "正文第一段。\n正文第二段。"
        # 全角空格 / 大小写差异也算同名（NFKC + casefold 归一化）
        assert strip_leading_duplicate("第一章　雨夜来客\n正文。", "第一章 雨夜来客") == "正文。"
        assert strip_leading_duplicate("CHAPTER ONE\nBody.", "chapter one") == "Body."
        # 不同名 -> 原样返回；章题出现在第二行 -> 不动
        assert strip_leading_duplicate("正文。", "第一章 雨夜来客") == "正文。"
        assert strip_leading_duplicate("引子\n第一章 雨夜来客\n正文。",
                                       "第一章 雨夜来客") == "引子\n第一章 雨夜来客\n正文。"
        # 只剩章题的极端情况不能产出空章节
        assert strip_leading_duplicate("第一章 雨夜来客", "第一章 雨夜来客") == "第一章 雨夜来客"

    def test_toc_chapter_body_no_longer_repeats_title(self, tmp_path):
        from modules.book_parser.parsers.epub_parser import EpubParser

        # 目录用全角空格、正文 h1 用半角 -> 归一化后仍应识别为重复章题
        path = build_epub(tmp_path / "wffix2.epub", toc_spacing="　")
        data = EpubParser(str(path)).parse()
        expected = chapter_body_chars()

        assert [c["title"] for c in data.chapters] == \
            [h.replace(" ", "　") for h, _ in CHAPTERS]
        for ch in data.chapters:
            head = squash(ch["title"])
            assert not squash(ch["content"]).startswith(head), \
                "章题仍被复制进正文: %r" % ch["content"][:20]
            source = expected[ch["title"].replace("　", " ")]
            assert len(squash(ch["content"])) == source, \
                "%s 字数不守恒: 解析 %d / 源 %d" % (
                    ch["title"], len(squash(ch["content"])), source)
            assert ch["char_count"] == len(ch["content"])
        assert data.total_chars == sum(c["char_count"] for c in data.chapters)

    def test_spine_fallback_path_also_dedups_heading(self, tmp_path):
        """无 TOC 时走 spine 兜底：章题取自 h1，正文同样不该再含章题。"""
        from modules.book_parser.parsers.epub_parser import EpubParser

        path = build_epub(tmp_path / "notoc.epub", with_toc=False)
        data = EpubParser(str(path)).parse()
        expected = chapter_body_chars()
        assert len(data.chapters) == len(CHAPTERS)
        for ch in data.chapters:
            assert len(squash(ch["content"])) == expected[ch["title"]]

    @pytest.mark.skipif(not (SAMPLE_DIR / "wf_epub.epub").exists(),
                        reason="仓内样书夹具缺失 (qa/harness/fixtures/wf_inputs/wf_epub.epub)")
    def test_sample_epub_per_chapter_diff_is_zero(self):
        from modules.book_parser.parsers.epub_parser import EpubParser

        data = EpubParser(str(SAMPLE_DIR / "wf_epub.epub")).parse()
        source = sample_chapter_body_chars()
        assert len(data.chapters) == len(source) == 4
        diffs = [len(squash(c["content"])) - source[c["title"]] for c in data.chapters]
        assert diffs == [0, 0, 0, 0], "逐章去空白字数差应为 0，实际 %s" % diffs
        assert sum(len(squash(c["content"])) for c in data.chapters) == sum(source.values())


# ----------------------------------------------------------------------
# WF-03: MOBI 章题回灌正文
# ----------------------------------------------------------------------

class TestWf03MobiHeadingDedup:

    @staticmethod
    def _soup(html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    def test_split_by_headings_does_not_reinject_heading_text(self):
        from modules.book_parser.parsers.mobi_parser import MobiParser

        body = "".join("<h1>%s</h1>%s" % (h, "".join("<p>%s</p>" % p for p in ps))
                       for h, ps in CHAPTERS)
        soup = self._soup("<html><head><title>%s</title></head><body>%s</body></html>"
                          % (TITLE, body))
        chapters = MobiParser("nowhere.mobi")._split_by_headings(soup)
        expected = chapter_body_chars()

        assert [c["title"] for c in chapters] == [h for h, _ in CHAPTERS]
        for ch in chapters:
            assert not squash(ch["content"]).startswith(squash(ch["title"]))
            assert ch["title"] not in ch["content"], "章题仍在正文里被读一遍"
            assert len(squash(ch["content"])) == expected[ch["title"]]
            assert ch["char_count"] == len(ch["content"])

    def test_nested_heading_still_opens_a_chapter(self):
        """跳过标题自身的文本，但嵌套的下一级标题仍要开新章。"""
        from modules.book_parser.parsers.mobi_parser import MobiParser

        html = ("<html><body><h1>第一章 标题甲<h2>第一节 小节乙</h2></h1>"
                "<p>正文甲段：檐下的雨声连成一片，她把手掌贴在门环上，指节被铜锈染得发青，"
                "久久没有松开。</p>"
                "<h1>第二章 标题丙</h1><p>正文丙段：院子里的竹筐被翻得乱七八糟，井台上的水"
                "渍还没干，他皱着眉又数了两遍。</p>"
                "</body></html>")
        chapters = MobiParser("x.mobi")._split_by_headings(self._soup(html))
        titles = [c["title"] for c in chapters]
        assert "第一节 小节乙" in titles, "嵌套标题被整块跳过，丢了章节"
        for ch in chapters:
            assert not squash(ch["content"]).startswith(squash(ch["title"]))

    def test_helper_strips_duplicate_line(self):
        from modules.book_parser.parsers.mobi_parser import strip_leading_duplicate

        assert strip_leading_duplicate("第一章 甲\n正文。", "第一章 甲") == "正文。"
        assert strip_leading_duplicate("正文。", "第一章 甲") == "正文。"

    @pytest.mark.skipif(not (SAMPLE_DIR / "wf_mobi.mobi").exists(),
                        reason="仓内样书夹具缺失 (qa/harness/fixtures/wf_inputs/wf_mobi.mobi)")
    def test_sample_mobi_per_chapter_diff_is_zero(self):
        pytest.importorskip("mobi")
        from modules.book_parser.parsers.mobi_parser import MobiParser

        data = MobiParser(str(SAMPLE_DIR / "wf_mobi.mobi")).parse()
        source = sample_chapter_body_chars()
        assert len(data.chapters) == 4
        diffs = [len(squash(c["content"])) - source[c["title"]] for c in data.chapters]
        assert diffs == [0, 0, 0, 0], "逐章去空白字数差应为 0，实际 %s" % diffs


# ----------------------------------------------------------------------
# WF-14: DOCX 书名 / 作者回退
# ----------------------------------------------------------------------

class TestWf14DocxMetadata:

    def test_resolve_docx_title_fallback_chain(self):
        from modules.mineru_tools.office_parser import resolve_docx_title

        uuid_stem = "aeb14b12-aeb4-49a4-9883-555361036709"
        paras_title = [
            {"text": TITLE, "style": "Title", "level": 0},
            {"text": "第一章 雨夜来客", "style": "Heading 1", "level": 1},
        ]
        paras_only_chapters = [
            {"text": "第一章 雨夜来客", "style": "Heading 1", "level": 1},
            {"text": "雨点砸在青石板上。", "style": "Normal", "level": 0},
        ]
        paras_first_line_is_title = [
            {"text": TITLE, "style": "Normal", "level": 0},
            {"text": "第一章 雨夜来客", "style": "Heading 1", "level": 1},
        ]

        # 1 核心属性最优先
        assert resolve_docx_title(paras_title, "核心书名") == "核心书名"
        # 2 Title 样式段胜过一切正文
        assert resolve_docx_title(paras_title, "", "", uuid_stem) == TITLE
        # 3 首个 Heading 是章题时不当书名（避免"第一章 …"变成书名）
        # 4 第一段/原始名依次兜底
        assert resolve_docx_title(paras_first_line_is_title, "", "", uuid_stem) == TITLE
        assert resolve_docx_title(paras_only_chapters, "", "上传名.docx", uuid_stem) == "上传名"
        # 5 只有 UUID 落盘名可用时留空，绝不返回 UUID
        assert resolve_docx_title(paras_only_chapters, "", "", uuid_stem) == ""
        assert resolve_docx_title([], "", "", uuid_stem) == ""
        # 6 非 UUID 的落盘名仍是合法兜底
        assert resolve_docx_title([], "", "", "本地书名") == "本地书名"
        # Subtitle 样式不算"书名级"标题（子串误伤回归锚）
        from modules.mineru_tools.office_parser import is_title_style
        assert is_title_style("Title") is True
        assert is_title_style("Subtitle") is False
        assert is_title_style("Heading 1") is False
        assert is_title_style("标题") is True
        assert is_title_style("标题 1") is False  # 中文版 Word 的 Heading 1
        # Subtitle 行没有 Title 样式，但作为"第一段"仍被第 4 步采信
        assert resolve_docx_title([{"text": "副标题一行", "style": "Subtitle", "level": 0}],
                                 "", "", uuid_stem) == "副标题一行"

    def test_placeholder_author_and_uuid_helpers(self):
        from modules.mineru_tools.office_parser import (clean_author, is_placeholder_value,
                                                        looks_like_uuid)

        assert clean_author("python-docx") == ""
        assert clean_author("Python-DocX") == ""
        assert clean_author("鲁迅") == "鲁迅"
        assert is_placeholder_value("generated by python-docx") is True
        assert is_placeholder_value("") is True
        assert looks_like_uuid("aeb14b12-aeb4-49a4-9883-555361036709") is True
        assert looks_like_uuid("雨夜药铺") is False

    def test_uuid_stored_docx_gets_real_title_and_no_fake_author(self, tmp_path):
        from modules.book_parser.parsers.docx_parser import DocxParser

        stored = tmp_path / ("%s.docx" % uuid.uuid4())
        write_docx(stored)  # python-docx 默认 core.author == "python-docx"
        data = DocxParser(str(stored)).parse()

        assert data.title == TITLE
        assert UUID_RE.match(data.title or "") is None, "书名仍是 UUID"
        assert (data.author or "") == ""
        assert data.metadata.get("author") != "python-docx"
        assert [c["title"] for c in data.chapters] == [h for h, _ in CHAPTERS]

    def test_real_core_metadata_is_preserved(self, tmp_path):
        from modules.book_parser.parsers.docx_parser import DocxParser

        stored = tmp_path / ("%s.docx" % uuid.uuid4())
        write_docx(stored, author="周闻笛", title_prop="雨夜药铺")
        data = DocxParser(str(stored)).parse()
        assert data.author == "周闻笛"
        assert data.title == "雨夜药铺"

    def test_title_uses_original_upload_name(self, tmp_path):
        """无 Title 段、Heading 又是章题时 -> 上传原始文件名 stem。"""
        from modules.book_parser.parsers.docx_parser import DocxParser

        stored = tmp_path / ("%s.docx" % uuid.uuid4())
        write_docx(stored, with_title_para=False)
        data = DocxParser(str(stored), original_name="雨夜药铺.docx").parse()
        assert data.title == "雨夜药铺"
        assert UUID_RE.match(data.title or "") is None

    def test_uuid_title_never_leaks_even_without_original_name(self, tmp_path):
        """连原始文件名都不知道时留空，由导入服务用上传名兜底，UUID 不进书名。"""
        from modules.book_parser.parsers.docx_parser import DocxParser

        stored = tmp_path / ("%s.docx" % uuid.uuid4())
        write_docx(stored, with_title_para=False, chapters=[CHAPTERS[0]])
        data = DocxParser(str(stored)).parse()
        assert UUID_RE.match(data.title or "") is None, "书名回退成了 UUID"

    def test_meaningful_file_name_is_used_when_content_has_no_title(self, tmp_path):
        """直接解析本地文件（非导入链路）时文件名有意义 -> 用它，不用 UUID。"""
        from modules.book_parser.parsers.docx_parser import DocxParser

        stored = tmp_path / "深夜药庐.docx"
        write_docx(stored, with_title_para=False, chapters=[CHAPTERS[0]])
        data = DocxParser(str(stored)).parse()
        assert data.title == "深夜药庐"

    @pytest.mark.skipif(not (SAMPLE_DIR / "wf_doc.docx").exists(),
                        reason="仓内样书夹具缺失 (qa/harness/fixtures/wf_inputs/wf_doc.docx)")
    def test_sample_docx_title_is_book_name(self, tmp_path):
        from modules.book_parser.parsers.docx_parser import DocxParser

        stored = tmp_path / "aeb14b12-aeb4-49a4-9883-555361036709.docx"
        shutil.copyfile(SAMPLE_DIR / "wf_doc.docx", stored)
        data = DocxParser(str(stored)).parse()
        assert data.title == "雨夜药铺"
        assert data.author != "python-docx"
        assert data.total_chars == 667
        assert len(data.chapters) == 4


# ----------------------------------------------------------------------
# WF-18: m4b 容器品牌
# ----------------------------------------------------------------------

class TestWf18M4bBrand:

    def test_brand_args_constant_and_ftyp_reader(self, tmp_path):
        from modules.tts_engine.m4b_builder import BRAND_ARGS, read_ftyp

        assert BRAND_ARGS == ["-brand", "M4B "]

        good = tmp_path / "good.m4b"
        box = (struct.pack(">I", 28) + b"ftyp" + b"M4B " + struct.pack(">I", 0)
               + b"M4B " + b"isom" + b"iso2")
        good.write_bytes(box)
        got = read_ftyp(str(good))
        assert got["error"] is None
        assert got["major_brand"] == "M4B"
        assert got["compatible_brands"] == ["M4B", "isom", "iso2"]

        bad = tmp_path / "junk.bin"
        bad.write_bytes(b"\x00" * 64)
        assert read_ftyp(str(bad))["major_brand"] == ""
        assert read_ftyp(str(tmp_path / "missing.m4b"))["major_brand"] == ""

    def test_ffmpeg_without_brand_option_retries_without_it(self, tmp_path, monkeypatch):
        """老 ffmpeg 不认 -brand：去掉该参数重试一次，品牌退化不能变成没有产物。"""
        from modules.tts_engine import ffmpeg_utils, m4b_builder

        calls = []

        def fake_run(args, **kw):
            args = [str(a) for a in args]
            calls.append(args)
            if "-brand" in args:
                raise RuntimeError("Option not found")
            Path(args[-1]).write_bytes(b"stub-container")
            return None

        monkeypatch.setattr(ffmpeg_utils, "run_ffmpeg", fake_run)
        monkeypatch.setattr(m4b_builder, "read_ftyp",
                            lambda p: {"major_brand": "M4A", "compatible_brands": ["M4A"]})
        out = m4b_builder.M4BBuilder().build(
            [{"audio_data": b"RIFFfake", "chapter_title": "第一章 甲", "duration_ms": 10}],
            {"title": "t"}, str(tmp_path / "o.m4b"))

        assert len(calls) == 2, "应只重试一次: %s" % calls
        assert "-brand" in calls[0] and "-brand" not in calls[1]
        assert Path(out).exists()

    def test_missing_ffmpeg_is_not_retried_as_generic_failure(self, tmp_path, monkeypatch):
        """ffmpeg 缺失仍是 FFmpegUnavailableError，不降级成普通 RuntimeError。"""
        from modules.tts_engine import ffmpeg_utils, m4b_builder

        def boom(args, **kw):
            raise ffmpeg_utils.FFmpegUnavailableError("m4b")

        monkeypatch.setattr(ffmpeg_utils, "run_ffmpeg", boom)
        b = m4b_builder.M4BBuilder()
        if b.available:
            with pytest.raises(ffmpeg_utils.FFmpegUnavailableError):
                b.build([{"audio_data": b"RIFFfake", "chapter_title": "第一章 甲"}],
                        {"title": "t"}, str(tmp_path / "o.m4b"))

    def test_built_product_has_m4b_major_brand(self, tmp_path):
        """真封装一个成品：ftyp/ffmpeg 双侧都必须是 M4B，章节与元数据不受影响。"""
        from modules.tts_engine import ffmpeg_utils
        from modules.tts_engine.m4b_builder import M4BBuilder, read_ftyp

        exe = ffmpeg_utils.resolve_ffmpeg()
        if not exe:
            pytest.skip("ffmpeg 不可用: %s" % ffmpeg_utils.unavailable_reason())

        out = tmp_path / "成品.m4b"
        segs = [{"audio_data": tone_wav(400, 240 + 90 * i), "chapter_title": h,
                 "duration_ms": 400} for i, (h, _p) in enumerate(CHAPTERS)]
        b = M4BBuilder()
        b.build(segs, {"title": TITLE, "author": "周闻笛", "year": "2026"}, str(out))

        got = read_ftyp(str(out))
        assert got["error"] is None
        assert got["major_brand"] == "M4B", "major_brand=%r 应为 M4B" % got["major_brand"]
        assert "M4B" in got["compatible_brands"]
        assert b.last_major_brand == "M4B"
        assert len(b.last_chapters) == len(CHAPTERS)
        assert b.last_chapters[1]["start_ms"] > 0, "章节时间轴退化 (PY-126 回归)"

        res = subprocess.run([exe, "-hide_banner", "-i", str(out)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        err = res.stderr or ""
        assert "major_brand     : M4B" in err, err[-600:]
        assert "compatible_brands: M4B" in err, err[-600:]
        assert err.count("Chapter #") == len(CHAPTERS), err[-600:]
        assert "周闻笛" in err, "artist 元数据丢失"
