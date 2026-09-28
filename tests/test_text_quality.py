"""PY-326: 源文件自带乱码必须在导入期可见（计数落进 extra_data + 日志点名）。

背景：解码链路统一 errors='replace'，《寻秦记》精排未删版 MOBI 的源载荷自带
20 枚 U+FFFD + 151 枚 U+25A1（字节取证见 qa/harness/run_encoding_matrix.py 输出），
解析器新增 0 —— 脏在原文件里，但产品一路无感地把它送进合成。

这里钉的是扫描器本身的口径（四类指标、逐章累加、不清洗），以及
`BookService.import_book` 这条真路径确实把计数写进 `extra_data.text_quality`。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.models.base as base
from modules.book_parser.text_quality import (
    INDICATORS, findings, merge, scan_chapters, scan_text, summarize)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'tq.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    base.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(base, "_engine", engine)
    monkeypatch.setattr(base, "_SessionLocal", Session)
    yield Session
    engine.dispose()


# ---------------------------------------------------------------- 扫描器口径

def test_scan_text_counts_four_indicators():
    text = "正常正文" + "\ufffd" * 3 + "□□" + "\x00" + "Ã¦st"
    stats = scan_text(text)
    assert stats["fffd"] == 3
    assert stats["cjk_box"] == 2
    assert stats["control_char"] == 1
    assert stats["double_encoding"] == 1
    assert stats["chars"] == len(text)


def test_clean_text_has_no_control_false_positive():
    """换行/制表/回车是正文合法控制符，不得计成乱码。"""
    stats = scan_text("第一章\t标题\r\n\r\n正文一行。\n第二行。")
    assert stats["control_char"] == 0
    assert findings(stats) == []


def test_scan_text_counts_only_real_double_encoding():
    """真小说里的 latin 正文（é è 单个）不算双重编码，避免误判。"""
    assert scan_text("café résumé naïve")["double_encoding"] == 0
    # latin-1 误读 UTF-8 的必然签名：â€œ / Ã©
    assert scan_text("â€œquotedâ€\x9d")["double_encoding"] >= 1
    assert scan_text("Ã©")["double_encoding"] == 1


def test_merge_is_per_key_sum():
    a = scan_text("正文" + "\ufffd")
    b = scan_text("正文" + "□")
    m = merge(a, b)
    assert m["fffd"] == 1 and m["cjk_box"] == 1
    assert m["chars"] == a["chars"] + b["chars"]


def test_scan_chapters_accumulates_without_joining_and_pins_worst():
    chapters = [
        {"index": 0, "title": "干净章", "content": "正常正文。"},
        {"index": 1, "title": "脏章", "content": "正常正文" + "\ufffd" * 5 + "□"},
        {"index": 2, "title": "次脏章", "content": "正文" + "\ufffd" * 2},
    ]
    report = scan_chapters(chapters, title="书名", author="作者")
    assert report["dirty"] is True
    assert report["stats"]["fffd"] == 7
    assert report["stats"]["cjk_box"] == 1
    assert report["stats"]["chapters_with_fffd"] == 2
    # 最脏章排第一，并带 index/标题，便于直接定位原文件
    assert report["worst_chapters"][0]["index"] == 1
    assert report["worst_chapters"][0]["fffd"] == 5
    assert len(report["worst_chapters"]) == 2


def test_scan_chapters_reports_metadata_mojibake_separately():
    report = scan_chapters([{"index": 0, "title": "章", "content": "正常正文。"}],
                           title="书名\ufffd", author="")
    assert report["stats"]["fffd"] == 0            # 正文干净
    assert report["meta"]["fffd"] == 1             # 元数据脏，单独记账
    assert report["dirty"] is True


def test_clean_book_is_not_dirty_and_summarize_is_readable():
    report = scan_chapters([{"index": 0, "title": "第一章", "content": "正文很干净。"}],
                           title="第一章", author="某人")
    assert report["dirty"] is False
    assert summarize(report) == "正文未见乱码"
    dirty = scan_chapters([{"index": 7, "title": "第七章", "content": "正文\ufffd\ufffd"}])
    text = summarize(dirty)
    assert "U+FFFD" in text and "[7]" in text
    assert set(INDICATORS) == {"fffd", "cjk_box", "control_char", "double_encoding"}


def test_scan_is_counting_only_and_never_rewrites_text():
    """本模块只测量：不得出现清洗/删段能力（清洗策略是另外的裁定）。"""
    content = "正文" + "\ufffd" * 4
    report = scan_chapters([{"index": 0, "title": "t", "content": content}])
    assert "cleaned" not in str(report).lower()
    assert report["stats"]["fffd"] == 4            # 原样计入，不吞不管


# ---------------------------------------------------------------- 真路径落库

class _Upload:
    """够用的 UploadFile 替身：book_service 只取 filename 与 .file。"""

    def __init__(self, name, payload: bytes):
        from io import BytesIO
        self.filename = name
        self.file = BytesIO(payload)


def _import(tmp_path, monkeypatch, name, payload):
    import backend.services.book_service as bs
    monkeypatch.setattr(bs.Config, "DATA_DIR", tmp_path / "data", raising=True)
    service = bs.BookService()
    return service.import_book(_Upload(name, payload))


def test_import_book_stores_text_quality_in_extra_data(tmp_path, monkeypatch, tmp_db):
    """走 BookService.import_book 真路径：自带乱码要回读得到。"""
    body = "第一章 起风\n\n" + "林昭走进客栈大堂，雨下了三天。" * 12
    dirty = body + "\n\n第二章 归人\n\n" + "此处原文缺字\ufffd\ufffd\ufffd□" * 3
    book = _import(tmp_path, monkeypatch, "carried_fffd.txt", dirty.encode("utf-8"))
    tq = (book.get("extra_data") or {}).get("text_quality") or {}
    assert tq.get("dirty") is True, book.get("extra_data")
    assert tq["stats"]["fffd"] == 9, tq["stats"]
    assert tq["stats"]["cjk_box"] == 3, tq["stats"]
    # TXT 导入期整本书就是一章（ChapterDetector 在管线里才细分），
    # 所以只钉"指认出来的章确实带着脏"，不硬编码章号。
    assert tq["worst_chapters"][0]["fffd"] == 9
    assert tq["worst_chapters"][0]["index"] == 0
    # 计数落库：直接从 DB 再读一次，不靠返回值自证
    session = tmp_db()
    try:
        from backend.models.book import Book
        row = session.query(Book).filter_by(id=book["id"]).one()
        assert ((row.extra_data or {}).get("text_quality") or {})["stats"]["fffd"] == 9
    finally:
        session.close()


def test_import_book_content_is_not_silently_cleaned(tmp_path, monkeypatch, tmp_db):
    """裁定前不得改正文：入库章节里的 U+FFFD 数量与源文件一致。"""
    body = "第一章 起风\n\n" + "他站在巷口攥着船票。" * 20 + "\ufffd\ufffd"
    book = _import(tmp_path, monkeypatch, "keep_as_is.txt", body.encode("utf-8"))
    session = tmp_db()
    try:
        from backend.models.book import Chapter
        stored = session.query(Chapter).filter_by(book_id=book["id"]).all()
        assert "".join(c.content for c in stored).count("\ufffd") == 2
    finally:
        session.close()


def test_import_book_clean_file_reports_not_dirty(tmp_path, monkeypatch, tmp_db):
    body = ("第一章 起风\n\n" + "林昭拖着行李走进客栈大堂，外面雨下得很大。" * 15
            + "\n\n第二章 朱雀大街\n\n" + "他走在长街上，风把落叶卷起来。" * 15)
    book = _import(tmp_path, monkeypatch, "clean.txt", body.encode("utf-8"))
    tq = (book.get("extra_data") or {}).get("text_quality") or {}
    assert tq.get("dirty") is False
    assert tq["stats"]["fffd"] == 0 and tq["stats"]["control_char"] == 0


def test_import_book_handles_unsupported_suffix_before_scan(tmp_path, monkeypatch, tmp_db):
    """扫描发生在解析之后：不支持的后缀仍旧干净拒绝，且不留文件。"""
    import backend.services.book_service as bs
    service = bs.BookService()
    with pytest.raises(ValueError):
        service.import_book(_Upload("probe.rtf", b"{\\rtf1 x}"))
    assert not list((tmp_path / "data" / "books").glob("*.rtf"))


def test_report_shape_is_json_safe_for_extra_data(tmp_path, monkeypatch, tmp_db):
    """extra_data 走 JSON 列：报告里只能是可序列化类型。"""
    import json
    report = scan_chapters([{"index": 0, "title": "t", "content": "正文\ufffd"}],
                           title="t", author="a")
    json.dumps(report, ensure_ascii=False)
    assert set(report) == {"stats", "meta", "dirty", "worst_chapters"}
