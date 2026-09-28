"""
[P2] parse 阶段实测: 各格式真实解析 (TXT/EPUB/PDF/MOBI/AZW3) + 自动分章
运行: python qa/harness/run_p2_parse.py

说明: 生产链路里 parse 发生在 *导入时* (ParserFactory.parse)。
      PY-104 裁定后 pipeline 的 _stage_parse 负责"章节校验 + 缓存重建"
      (可用章数为 0 时按 book.file_path 从导入期原文件重建内存章节), 见用例 0。

真书规模口径 (F-R4-7, round-2 续测1g 记账修订):
  用例4/5 的 fixtures/real/real_sample.{mobi,azw3} 是 **33 万字级**《盐战》样本
  (total_chars=331,373), 只作"真实格式覆盖"用, **不是** data/books 里最大的真书。
  "可达最大真书"一律表述为 **"MAX_BOOK_CHARS(=2,000,000) 上界内的最大真书"**
  (= 实测《寻秦记》精排未删版 1,591,303 字 / 291 章 MOBI), 盘点表引用
  qa/round-2/artifacts/result_R4_BIGBOOK.json 的 data.inventory。
  旧口径"330 万字 MOBI"属记账错误 (330 万字级实为 EPUB 3,870,692 / TXT 2,700,901,
  且 >2,000,000 字被 PY-103 拒收), 本目录内不得再引用。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

_created = []


def import_file(path, fname=None, encoding=None):
    content = Path(path).read_bytes()
    fn = fname or Path(path).name
    url = "/books/import" + (f"?encoding={encoding}" if encoding else "")
    code, body = H.http("POST", url, files={"file": (fn, content, "application/octet-stream")},
                        timeout=600)
    if code != 200:
        return None, body
    _created.append(body["book"]["id"])
    return body["book"], None


def chapters_of(bid):
    return H.db_rows(f'SELECT * FROM chapters WHERE book_id=? ORDER BY "index"', (bid,))


def main():
    log = H.LOGS / "p2_parse.log"
    with H.Tee(log):
        su = H.Suite("P2-parse", "run_p2_parse.py")
        be = H.start_backend(H.LOGS / "p2_backend.log")
        try:
            # ---------- 0: pipeline._stage_parse 职责=校验+缓存重建 (PY-104 裁定后语义) ----------
            # 旧断言 r.check("parse 阶段调用真实解析器", False, ...) 硬编码 False,
            # 且 detail 描述的是"仅 logger.info + 原样返回"的空壳(该空壳已被 PY-104 修复),
            # 属"账本绿 vs 测试红"矛盾源。裁定语义: 导入期解析保持真实路径,
            # P2 阶段职责 = 1) 章节校验(char_count/content 一致, 必要时按 id 从 DB 回填)
            #               2) 可用章数为 0 时按 book.file_path 从原文件重建内存章节缓存。
            # 故本用例改为对 *这两条职责* 做真实断言(含自愈路径真实触发)。
            r = su.new("pipeline._stage_parse: 章节校验 + 缓存重建 (PY-104 语义)")
            r.evidence_add(log)
            src = (H.PROJECT_ROOT / "modules/workflow/conversion_workflow.py").read_text(
                encoding="utf-8")
            body_src = src.split("def _stage_parse", 1)[1].split("def _stage_clean", 1)[0]
            H.kv("_stage_parse 函数体", body_src.strip().replace("\n", " | ")[:260])
            r.check("函数体非空壳: 同时含 DB 回填与 ParserFactory 重建两条职责",
                    "book_service" in body_src and "ParserFactory.parse" in body_src,
                    "退化回 logger.info+原样返回 时此项 FAIL")

            book, err = import_file(H.FIXTURES / "sample_small.txt")
            if not book:
                r.block("前置导入失败, 无法验证 _stage_parse", str(err)[:200])
            else:
                from modules.workflow.conversion_workflow import ConversionWorkflow
                bid = book["id"]
                chs = chapters_of(bid)

                # (a) 正常路径: 校验通过, 章数不丢, char_count 与实际一致
                out = ConversionWorkflow()._stage_parse(
                    {"task_id": "harness-p2-a", "book_id": bid,
                     "chapters": [dict(c) for c in chs]})
                got = out.get("chapters", [])
                H.kv("(a) 正常路径", f"入 {len(chs)} 章 -> 出 {len(got)} 章, "
                                    f"字数 {sum(len(c.get('content') or '') for c in got)}")
                r.check("(a) 正常路径返回章节非空且章数不丢",
                        len(got) > 0 and len(got) == len(chs), f"{len(chs)} -> {len(got)}")
                drift = [(c.get("title"), c.get("char_count"), len(c.get("content") or ""))
                         for c in got if c.get("char_count") != len(c.get("content") or "")]
                r.check("(a) char_count 与 content 实际长度一致", not drift, str(drift)[:200])

                # (a2) 校验职责: 人为注入 char_count 漂移 + 清空一章 content(只留 id),
                #      _stage_parse 应修正计数并按 id 从 DB 回填正文
                dirty = [dict(c) for c in chs]
                dirty[0]["char_count"] = len(dirty[0]["content"] or "") + 7
                blanked_title = dirty[1]["title"]
                dirty[1]["content"] = ""
                dirty[1]["char_count"] = 0
                out2 = ConversionWorkflow()._stage_parse(
                    {"task_id": "harness-p2-a2", "book_id": bid, "chapters": dirty})
                g2 = out2.get("chapters", [])
                drift2 = [(c.get("title"), c.get("char_count"), len(c.get("content") or ""))
                          for c in g2 if c.get("char_count") != len(c.get("content") or "")]
                backfilled = len(g2) > 1 and bool((g2[1].get("content") or "").strip())
                H.kv("(a2) 注错后", f"计数漂移 {len(drift2)} 章, "
                                   f"空章「{blanked_title}」回填后 {len(g2[1]['content'] or '')} 字")
                r.check("(a2) 计数漂移被修正 且 空 content 按 id 从 DB 回填",
                        not drift2 and backfilled,
                        f"残余漂移={drift2[:2]}, 回填={backfilled}")

                # (b) 自愈路径: context chapters 被清空 -> 从 book.file_path 重建内存章节
                out3 = ConversionWorkflow()._stage_parse(
                    {"task_id": "harness-p2-b", "book_id": bid, "chapters": []})
                rebuilt = out3.get("chapters", [])
                rb_chars = sum(len(c.get("content") or "") for c in rebuilt)
                H.kv("(b) 重建章数", len(rebuilt))
                H.kv("(b) 重建章标题", [c.get("title") for c in rebuilt])
                H.kv("(b) 重建总字数", rb_chars)
                r.check("(b) chapters 清空后按 book.file_path 重建(自愈路径真实触发)",
                        len(rebuilt) > 0,
                        f"重建 {len(rebuilt)} 章 / {rb_chars} 字 (DB 原 {len(chs)} 章)")
                r.check("(b) 重建结果为内存章节(不回写 DB: id 均为 None)",
                        all(c.get("id") is None for c in rebuilt),
                        f"ids={[c.get('id') for c in rebuilt][:5]}")
                r.check("(b) 重建章序/标题与导入期一致 且 字数不丢",
                        [c.get("title") for c in rebuilt] == [c["title"] for c in chs]
                        and rb_chars == sum(c["char_count"] or 0 for c in chs),
                        f"重建 {len(rebuilt)} 章 {rb_chars} 字 vs DB {len(chs)} 章 "
                        f"{sum(c['char_count'] or 0 for c in chs)} 字")
                r.notes.append("PY-104 裁定(智能体2 8cee561): 保留导入期解析为真实路径, "
                               "_stage_parse 不再二次全量解析; 仅在校验不可用时按 "
                               "book.file_path 重建内存缓存(本用例 (b) 即该兜底分支)。"
                               f"自愈重建实测 {len(rebuilt)} 章 / {rb_chars} 字。")

            # ---------- 1: TXT ----------
            r = su.new("TXT 解析: 章节切分与正文保真")
            book, err = import_file(H.FIXTURES / "sample_small.txt")
            if not book:
                r.fail("TXT 导入失败", str(err)[:200])
            else:
                chs = chapters_of(book["id"])
                titles = [c["title"] for c in chs]
                H.kv("标题", titles)
                H.kv("各章字数", [c["char_count"] for c in chs])
                r.check("按 '第N章' 切成 3 章", len(chs) == 3, f"{len(chs)} 章")
                all_text = "\n".join(c["content"] or "" for c in chs)
                must = ["林昭拖着行李走进客栈大堂", "三月十五，朱雀大街",
                        "下一个就是你", "He walked to the window"]
                lost = [m for m in must if m not in all_text]
                r.check("正文关键句无损保留", not lost, f"缺失: {lost}")
                # 章内是否串章: 第一章的内容不应混入第三章
                ch1 = chs[0]["content"] or ""
                r.check("章节边界正确(首章不含末章内容)",
                        "周主事" not in ch1, "第三章人物出现在第一章正文"
                        if "周主事" in ch1 else "ok")

            # ---------- 2: EPUB ----------
            r = su.new("EPUB 解析: TOC 分章 + 标题/作者元数据")
            book, err = import_file(H.FIXTURES / "sample.epub")
            if not book:
                r.fail("EPUB 导入失败", str(err)[:300])
            else:
                chs = chapters_of(book["id"])
                H.kv("title/author", f"{book['title']} / {book['author']}")
                H.kv("章节数", len(chs))
                H.kv("标题", [c["title"] for c in chs])
                r.check("EPUB 元数据 title 正确", "测试书" in (book["title"] or ""),
                        book["title"])
                r.check("EPUB 元数据 author 正确", (book["author"] or "") != "",
                        book["author"])
                r.check("EPUB 切成 3 章", len(chs) == 3, f"{len(chs)}")
                full = "\n".join(c["content"] or "" for c in chs)
                r.check("HTML 标签已被剥离", "<p>" not in full and "<html>" not in full,
                        full[:120].replace("\n", " "))
                r.check("正文无损", "豆浆铺" in full and "码头" in full, "")

            # ---------- 3: PDF ----------
            r = su.new("PDF 解析: 跨页章节合并 + 页眉页码处理")
            book, err = import_file(H.FIXTURES / "sample.pdf")
            if not book:
                r.fail("PDF 导入失败", str(err)[:300])
            else:
                chs = chapters_of(book["id"])
                H.kv("章节数", len(chs))
                for c in chs:
                    H.kv(f"  [{c['index']}] {c['title']}",
                         (c["content"] or "").replace("\n", " ")[:70])
                full = "\n".join(c["content"] or "" for c in chs)
                r.check("PDF 产生章节", len(chs) >= 1, f"{len(chs)} 章")
                r.check("PDF 正文抽取成功", "林晚推开窗" in full and "汽笛" in full,
                        f"len={len(full)}")
                pgmark = [c["title"] for c in chs if "测试书_三个清晨" in (c["content"] or "")]
                H.kv("含页眉'测试书_三个清晨'的章", pgmark)
                r.check("页眉/页码未被误当章节标题",
                        not any("页" == (c["title"] or "")[-1:] for c in chs),
                        str([c["title"] for c in chs])[:200])

            # ---------- 4: MOBI (真实书籍) ----------
            r = su.new("MOBI 解析: 真实书籍 (data/books 副本)")
            p = H.FIXTURES / "real" / "real_sample.mobi"
            book, err = import_file(p)
            if not book:
                r.fail("MOBI 导入失败", str(err)[:400])
            else:
                chs = chapters_of(book["id"])
                H.kv("title", book["title"])
                H.kv("章节数", len(chs))
                H.kv("total_chars", book["total_chars"])
                for c in chs[:5]:
                    H.kv(f"  [{c['index']}] {c['title']}",
                         (c["content"] or "").replace("\n", " ")[:60])
                r.check("MOBI 解析出多章", len(chs) > 1, f"{len(chs)} 章")
                full = "\n".join((c["content"] or "") for c in chs)
                bad = full.count("\ufffd")
                r.check("无解码乱码 U+FFFD", bad == 0, f"count={bad}")
                empty = [c["title"] for c in chs if not (c["content"] or "").strip()]
                r.check("无空章节", not empty, f"{len(empty)} 空章: {empty[:5]}")
                # 口径记账 (F-R4-7): 本样本 = 33 万字级"格式覆盖"真书, 非最大真书。
                from backend.config import Config
                top = H.db_rows("SELECT title,total_chars FROM books WHERE total_chars<=? "
                                "ORDER BY total_chars DESC LIMIT 1", (Config.MAX_BOOK_CHARS,))
                biggest = top[0] if top else None
                H.kv("本样本规模", f"{book['total_chars']} 字 (33 万字级《盐战》, 仅作格式覆盖)")
                H.kv("上界内最大真书(F-R4-7 口径)",
                     f"{biggest['total_chars']} 字 {biggest['title']!r}" if biggest else "DB 内无")
                r.notes.append("口径修订(F-R4-7): 大书一律写作 'MAX_BOOK_CHARS(%s) 上界内的最大真书' = %s;"
                               " 不再引用 '330 万字 MOBI'。盘点表: "
                               "qa/round-2/artifacts/result_R4_BIGBOOK.json data.inventory"
                               % (Config.MAX_BOOK_CHARS,
                                  (f"{biggest['total_chars']} 字《{biggest['title']}》" if biggest else "无")))
                su.set_meta("book_scale_wording", {
                    "口径": "MAX_BOOK_CHARS 上界内最大真书",
                    "max_book_chars": Config.MAX_BOOK_CHARS,
                    "fixture_mobi_chars": book["total_chars"],
                    "largest_importable_book_chars": biggest["total_chars"] if biggest else None,
                    "largest_importable_book_title": biggest["title"] if biggest else None,
                    "inventory_table": "qa/round-2/artifacts/result_R4_BIGBOOK.json",
                    "废弃口径": "330 万字 MOBI",
                })

            # ---------- 5: AZW3 ----------
            r = su.new("AZW3 解析 (复用 mobi 解析器)")
            book, err = import_file(H.FIXTURES / "real" / "real_sample.azw3")
            if not book:
                r.fail("AZW3 导入失败", str(err)[:400])
            else:
                chs = chapters_of(book["id"])
                H.kv("title", book["title"])
                H.kv("章节数", len(chs))
                H.kv("total_chars", book["total_chars"])
                r.check("AZW3 解析出章节", len(chs) >= 1, f"{len(chs)}")

            # ---------- 6: ChapterDetector 自动分章 ----------
            r = su.new("ChapterDetector: 无章节标记 TXT 的自动分章")
            from modules.book_parser.chapter_detector import ChapterDetector
            txt = (H.FIXTURES / "sample_nochapter.txt").read_text(encoding="utf-8")
            det = ChapterDetector(txt).detect(min_content_length=50)
            H.kv("无标记纯正文 -> 检出章节", [d["title"] for d in det])
            book, err = import_file(H.FIXTURES / "sample_nochapter.txt")
            if book:
                chs = chapters_of(book["id"])
                H.kv("导入后章节", [c["title"] for c in chs][:8])
                H.kv("导入后 total_chars", book["total_chars"])
                H.kv("原始文件字符数", len(txt))
                r.check("整书未丢失内容",
                        abs(book["total_chars"] - len(txt)) < len(txt) * 0.35,
                        f"入库 {book['total_chars']} vs 原文件 {len(txt)}")
                r.check("ChapterDetector 有可用输出", len(det) >= 1, f"{len(det)}")

            # ---------- 6b: TXT 书名来源 (UUID 污染) ----------
            r = su.new("TXT 书名/章名 来源: 长首行时是否退化为 UUID")
            long_first = ("这一段的开头是一行超过一百个汉字的连续正文，用来触发 txt_parser 中"
                          "lines[0] 长度大于 100 的分支，从而检验书名提取逻辑是否会退化到内部"
                          "存储文件名上，进而影响前端展示与导出文件的命名。\n\n"
                          + "他走在长街上，风把落叶卷起来。她站在门口，等了很久很久。" * 8)
            p = H.TMP / "probe_longfirst.txt"
            p.write_text(long_first, encoding="utf-8")
            book, err = import_file(p, fname="probe_longfirst.txt")
            if not book:
                r.block("导入失败", str(err)[:200])
            else:
                import re as _re
                chs = chapters_of(book["id"])
                H.kv("上传文件名", "probe_longfirst.txt")
                H.kv("入库 book.title", book["title"])
                H.kv("入库 chapter.title", [c["title"] for c in chs])
                looks_uuid = bool(_re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    book["title"] or ""))
                r.check("book.title 不是内部 UUID 文件名", not looks_uuid,
                        f"book.title={book['title']} "
                        f"(txt_parser: data.title=Path(stored).stem, 首行>=100字时不覆盖)")
                ch_uuid = [c["title"] for c in chs
                           if _re.fullmatch(r"[0-9a-f\-]{36}", c["title"] or "")]
                r.check("chapter.title 可读(非 UUID)", not ch_uuid, f"{ch_uuid}")

            # ---------- 7: 章节标题模式识别 ----------
            r = su.new("ChapterDetector: 多模式标题识别覆盖率")
            samples = {
                "第一章 风起": True, "第3节": True, "楔子": True, "Chapter 12 The Dawn": True,
                "卷二 江山": True, "前言": True, "番外 若梦": True, "尾声": True,
                "他走进了房间": False, "今天天气很好": False,
            }
            text = "\n\n".join(
                f"{t}\n\n" + ("这是内容正文，" * 30) for t in samples)
            found = {d["title"]: False for d in ChapterDetector(text).detect(min_content_length=20)}
            got = set(found.keys())
            H.kv("检出标题", sorted(got))
            missed = [t for t, expect in samples.items() if expect and t not in got]
            false_pos = [t for t, expect in samples.items() if not expect and t in got]
            r.check("标准标题全部命中", not missed, f"漏: {missed}")
            r.check("正文行未被误判为标题", not false_pos, f"误判: {false_pos}")
        finally:
            for bid in _created:
                H.http("DELETE", f"/books/{bid}")
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
