"""
[P1] import 阶段实测: 书籍导入 API -> DB 行 + data/books 落盘文件
运行: python qa/harness/run_p1_import.py
覆盖: 正常导入 / 非法扩展名 / 空文件 / 损坏文件 / CHM(声称支持) /
      超大文件边界(MAX_BOOK_CHARS 是否生效) / 删除后章节残留
"""
import sys
import re
import time
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

TAG = "pyyP1"
_created = []


def upload(path: Path, fname=None, encoding=None):
    content = Path(path).read_bytes()
    fn = fname or Path(path).name
    url = "/books/import"
    if encoding:
        url += f"?encoding={encoding}"
    t = time.time()
    code, body = H.http("POST", url, files={"file": (fn, content, "application/octet-stream")},
                        timeout=600)
    return code, body, round(time.time() - t, 2)


def judge_oversize_rejection(code, detail, upper, up_mb, books_before, books_after,
                             chars_built, size_mb, max_chars, max_upload_mb):
    """用例7 **拒绝分支**的四条判据 (F-R4-5 同法拆成纯函数, 便于离线变异检验)。

    修订前这里是 `r.check("服务端拒绝超大文件", True, f"{code}")` —— 恒真占位。
    拆成函数不是为了改行为, 而是让"喂进退化输入"(泛化文案 / 写了脏行 / 构造文件其实
    没越界 / 返回 200) 时能离线看到红色, 而不必为了做变异检验去改一次生产码。

    返回 [(desc, ok, detail), ...]，由调用方逐条 r.check。
    """
    d = str(detail or "")
    named_chars = "MAX_BOOK_CHARS" in d and upper in d
    named_upload = ("MAX_UPLOAD" in d) or (up_mb in d and "MB" in d.upper())
    return [
        ("服务端以 4xx 拒绝超大文件 (不再是恒真占位)", 400 <= code < 500,
         f"code={code} detail={d[:160]}"),
        ("拒绝理由点名上限来源 + 上限数字 (可定位, 不是泛化 500)",
         named_chars or named_upload,
         f"detail={d[:200]} 需含 ('MAX_BOOK_CHARS' 且 {upper}) 或 ('MAX_UPLOAD' 且 {up_mb}MB)"),
        ("拒绝时不写入脏 book 行", books_before == books_after,
         f"books {books_before} -> {books_after}"),
        ("构造文件确实越过它被拒的那个上限 (用例有鉴别力)",
         chars_built > max_chars or size_mb > max_upload_mb,
         f"构造 {chars_built} 字 / {size_mb:.1f}MB vs 上限 {max_chars} 字 / {max_upload_mb}MB"),
    ]


def main():
    log = H.LOGS / "p1_import.log"
    with H.Tee(log):
        su = H.Suite("P1-import", "run_p1_import.py")
        be = H.start_backend(H.LOGS / "p1_backend.log")
        try:
            # ---- 用例1: 正常 TXT 导入 ----
            r = su.new("TXT 导入: 200 + books/chapters 落库 + 文件落盘")
            r.evidence_add(log)
            code, body, dt = upload(H.FIXTURES / "sample_small.txt")
            H.kv("http", f"{code} in {dt}s")
            if code != 200:
                r.fail("导入返回非 200", str(body)[:300])
            else:
                book = body["book"]
                _created.append(book["id"])
                bid = book["id"]
                rows = H.db_rows("SELECT * FROM books WHERE id=?", (bid,))
                r.check("books 表出现新行", len(rows) == 1, f"title={rows[0]['title']}")
                chs = H.db_rows(f'SELECT * FROM chapters WHERE book_id=? ORDER BY "index"', (bid,))
                r.check("chapters 表 >0 行", len(chs) > 0, f"{len(chs)} 章")
                fp = Path(rows[0]["file_path"])
                r.check("data/books 文件存在", fp.exists() and fp.stat().st_size > 0,
                        f"{fp} ({fp.stat().st_size if fp.exists() else 0}B)")
                r.check("API book 响应包含 file_path", "file_path" in book,
                        f"Book.to_dict() 字段={sorted(book.keys())} -> 前端拿不到源文件路径")
                mismatch = [(c["title"], c["char_count"], len(c["content"] or ""))
                            for c in chs if c["char_count"] != len(c["content"] or "")]
                r.check("char_count 与 content 实际长度一致", not mismatch, str(mismatch)[:200])
                api_book = H.http("GET", f"/books/{bid}")[1]
                H.kv("GET /books/id", str(api_book)[:200])
                st = sum(c["char_count"] for c in chs)
                r.check("book.total_chars == sum(chapters)",
                        book["total_chars"] == st, f"{book['total_chars']} vs {st}")
                H.kv("样本原文字符数", len((H.FIXTURES / 'sample_small.txt')
                                           .read_text(encoding='utf-8')))
                H.kv("入库后总字符", st)

            # ---- 用例2: GBK 编码 TXT ----
            r = su.new("TXT GBK: encoding 参数生效, 无乱码")
            r.evidence_add(log)
            code, body, dt = upload(H.FIXTURES / "sample_gbk.txt", encoding="gbk")
            H.kv("http", f"{code} in {dt}s")
            if code == 200:
                bid = body["book"]["id"]
                _created.append(bid)
                ch = H.db_rows("SELECT content FROM chapters WHERE book_id=? LIMIT 1", (bid,))[0]
                snippet = (ch["content"] or "")[:40]
                H.kv("首章片段", snippet)
                ok = ("林昭" in (ch["content"] or "")) and ("\ufffd" not in (ch["content"] or ""))
                r.check("GBK 解码后中文正确", ok, snippet)
            else:
                r.fail("GBK 导入失败", str(body)[:300])

            # ---- 用例3: 非法扩展名 ----
            r = su.new("非法扩展名 .xyz -> 400")
            code, body, dt = upload(H.FIXTURES / "sample_bad.xyz")
            H.kv("http", f"{code} in {dt}s -> {str(body)[:160]}")
            r.check("返回 400 拒绝", code == 400, f"got {code}")

            # ---- 用例4: 空文件 ----
            r = su.new("空 TXT 文件 -> 明确错误且无脏数据")
            before = len(H.db_rows("SELECT id FROM books"))
            code, body, dt = upload(H.FIXTURES / "sample_empty.txt")
            after = len(H.db_rows("SELECT id FROM books"))
            H.kv("http", f"{code} in {dt}s -> {str(body)[:200]}")
            r.check("被拒绝 (非 200)", code != 200, f"code={code}")
            r.check("未写入脏 book 行", before == after, f"{before} -> {after}")
            orphan = [p for p in H.BOOKS_DIR.glob("*.txt")
                      if p.stat().st_size == 0 and
                      not H.db_rows("SELECT id FROM books WHERE file_path=?", (str(p),))]
            r.check("未在 data/books 残留空文件", not orphan, str(orphan)[:200])

            # ---- 用例5: 损坏 EPUB ----
            r = su.new("损坏 .epub -> 明确错误, 文件已清理")
            code, body, dt = upload(H.FIXTURES / "sample_corrupt.epub")
            H.kv("http", f"{code} in {dt}s -> {str(body)[:250]}")
            r.check("返回 4xx/5xx 且带可读信息", code in (400, 500) and
                    isinstance(body, dict) and "detail" in body, str(body)[:200])

            # ---- 用例6: .chm (PY-102 裁定后契约) ----
            # 旧断言 r.check("CHM 导入可用 (期望)", False, ...) 为硬编码 False:
            # PY-102 的裁定是"移除虚假声明"(chm 无解析器), 故该用例数学上永不可绿。
            # 修订为断言 *裁定后的契约*: 4xx 拒绝 + detail 支持列表与
            # ParserFactory.FORMAT_MAP 完全一致(期望值动态取自生产单一事实源,
            # API 与工厂两侧一旦漂移即 FAIL —— 这才是 PY-102 的有效回归护栏)。
            r = su.new(".chm 无解析器 -> 4xx 且支持列表与 FORMAT_MAP 完全一致 (PY-102)")
            fake_chm = H.TMP / "probe.chm"
            fake_chm.write_bytes(b"ITSF\x03\x00\x00\x00" + b"\x00" * 512)
            books_before = len(H.db_rows("SELECT id FROM books"))
            code, body, dt = upload(fake_chm)
            books_after = len(H.db_rows("SELECT id FROM books"))
            msg = str(body)
            detail = body.get("detail", "") if isinstance(body, dict) else str(body)
            H.kv("http", f"{code} -> {msg[:250]}")
            from modules.book_parser.parser_factory import ParserFactory
            from backend.config import Config
            expected = sorted(ParserFactory.FORMAT_MAP.keys())
            m = re.search(r"支持[:：]\s*([^）)\n]+)", detail or "")
            listed = sorted(x.strip() for x in m.group(1).split(",") if x.strip()) if m else []
            H.kv("detail 声明的支持列表", listed)
            H.kv("ParserFactory.FORMAT_MAP keys", expected)
            r.check("返回 4xx 明确拒绝 (不再 500)", 400 <= code < 500,
                    f"code={code} detail={str(detail)[:160]}")
            r.check("detail 支持列表 == ParserFactory.FORMAT_MAP (单一事实源, 漂移即 FAIL)",
                    bool(listed) and listed == expected,
                    f"detail={listed} vs FORMAT_MAP={expected}")
            r.check("契约自洽: .chm 未被声明支持 且工厂确实返回 None",
                    ".chm" not in listed and ParserFactory.get_parser("probe.chm") is None,
                    f"listed={listed}")
            r.check("拒绝时不写入脏 book 行", books_before == books_after,
                    f"books {books_before} -> {books_after}")
            claimed = sorted(getattr(Config, "SUPPORTED_EBOOK_FORMATS", []))
            orphan_claim = [e for e in claimed if e not in expected]
            r.check("Config.SUPPORTED_EBOOK_FORMATS 无孤儿声明 (每项都有已注册解析器)",
                    not orphan_claim,
                    f"Config 声明={claimed}, 缺解析器={orphan_claim}")
            r.notes.append("PY-102 裁定=移除虚假声明: .chm 已从 FORMAT_MAP/Config/导入白名单移除, "
                           "白名单以 FORMAT_MAP 为单一来源, ValueError->400。"
                           "本用例按该契约判定; 若将来实现 chm_parser.py 并登记进 FORMAT_MAP, "
                           "上述断言会自动跟随新支持列表, 无需改用例。")

            # ---- 用例7: 超大文件边界 ----
            # 治理(round-2 续测1g): 旧代码拒绝分支写的是
            #   r.check("服务端拒绝超大文件", True, f"{code}")
            # —— 第二个实参是字面量 True, 与产品行为无关的**恒真占位**(任何实现都恒绿),
            # 由 1c 在 qa/harness/README-assertions.md 附录登记。本轮改为可判定的行为断言:
            #   4xx + detail 点名上限来源与上限数字 + 不写脏行 + 构造文件确实超上限。
            # 口径(F-R4-7): MAX_BOOK_CHARS 是"可达最大真书"的上界 —— 实测上界内最大真书为
            #   1,591,303 字《寻秦记》MOBI, 270 万字 TXT 必须被拒; 盘点表见
            #   qa/round-2/artifacts/result_R4_BIGBOOK.json 的 data.inventory。
            r = su.new("超大 TXT: MAX_UPLOAD_SIZE_MB / MAX_BOOK_CHARS 边界")
            big = H.TMP / "probe_big.txt"
            unit = "这是一句用于压力边界的中文正文，用来验证超大文件的导入行为与耗时。"
            n_unit = int(7.5 * 1024 * 1024 / len(unit.encode("utf-8"))) + 1
            with open(big, "w", encoding="utf-8") as f:
                f.write("第一章 超长边界\n\n")
                for _ in range(n_unit):
                    f.write(unit + "\n")
            size_mb = big.stat().st_size / 1024 / 1024
            H.kv("构造文件", f"{size_mb:.1f} MB, 字符数约 {n_unit*len(unit)}")
            from backend.config import Config
            H.kv("Config.MAX_BOOK_CHARS", Config.MAX_BOOK_CHARS)
            H.kv("Config.MAX_UPLOAD_SIZE_MB", Config.MAX_UPLOAD_SIZE_MB)
            books_before = len(H.db_rows("SELECT id FROM books"))
            code, body, dt = upload(big)
            books_after = len(H.db_rows("SELECT id FROM books"))
            detail = body.get("detail", "") if isinstance(body, dict) else str(body)
            H.kv("http", f"{code} in {dt}s -> {str(body)[:200]}")
            if code == 200:
                bid = body["book"]["id"]
                _created.append(bid)
                st = H.db_rows("SELECT SUM(char_count) s, COUNT(*) c FROM chapters WHERE book_id=?",
                               (bid,))[0]
                H.kv("入库", f"{st['c']} 章 / {st['s']} 字")
                if st["s"] and st["s"] > Config.MAX_BOOK_CHARS:
                    r.fail("MAX_BOOK_CHARS 应限制入库字数",
                           f"实际入库 {st['s']} > 上限 {Config.MAX_BOOK_CHARS} -> 校验未实现")
                else:
                    r.fail("超大文件应被限制或分章",
                           f"文件 {size_mb:.1f}MB / 入库 {st['s']} 字 / {st['c']} 章, "
                           f"未触发任何限制, 导入耗时 {dt}s, 全量常驻内存")
            else:
                for desc, ok, dtl in judge_oversize_rejection(
                        code, detail, str(Config.MAX_BOOK_CHARS), str(Config.MAX_UPLOAD_SIZE_MB),
                        books_before, books_after, n_unit * len(unit), size_mb,
                        Config.MAX_BOOK_CHARS, Config.MAX_UPLOAD_SIZE_MB):
                    r.check(desc, ok, dtl)
                r.notes.append("PY-103 裁定: MAX_BOOK_CHARS=2,000,000 为入库上界, 超界直接 ValueError->400。"
                               f"实测 detail={str(detail)[:120]}")

            # ---- 用例8: 删除书籍后的章节残留 ----
            r = su.new("DELETE /books/{id} 级联清理")
            if _created:
                bid = _created[0]
                fpath = H.db_rows("SELECT file_path p FROM books WHERE id=?", (bid,))[0]["p"]
                before_ch = len(H.db_rows("SELECT id FROM chapters WHERE book_id=?", (bid,)))
                code, body = H.http("DELETE", f"/books/{bid}")
                H.kv("http", f"{code}")
                after_ch = len(H.db_rows("SELECT id FROM chapters WHERE book_id=?", (bid,)))
                after_book = len(H.db_rows("SELECT id FROM books WHERE id=?", (bid,)))
                file_left = Path(fpath).exists()
                H.kv("删除前章节", before_ch)
                H.kv("删除后 chapters 行", after_ch)
                H.kv("删除后 books 行", after_book)
                H.kv("磁盘文件仍存在", file_left)
                r.check("book 行被删除", after_book == 0, f"books 剩余 {after_book} 行")
                r.check("章节随书籍删除", after_ch == 0,
                        f"删除前 {before_ch} 章, 删除后 chapters 表仍有 {after_ch} 行孤儿")
                if after_ch:
                    r.notes.append(f"book_id={bid} 残留 {after_ch} 行 chapters 孤儿数据")
            else:
                r.block("无可用 book", "前置用例失败")
        finally:
            H.kv("清理测试书籍", len(_created))
            for bid in _created:
                H.http("DELETE", f"/books/{bid}")
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
