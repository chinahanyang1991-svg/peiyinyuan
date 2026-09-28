"""
[P4] split 阶段实测: SentenceSplitter 中英混排分句正确性
运行: python qa/harness/run_p4_split.py

真实路径: 先经 /books/import 得到入库正文, 再用生产 SentenceSplitter 分句
          (与 pipeline._stage_split 调用的是同一个类、同一入参来源)。

断言修订 (round-2 续测1g, F-R4-6): 用例3 的 "分句不丢字 >= 98%" 单栏口径作废,
  改为 **双栏判定 + 空白单列**:
    栏1 非空白字符守恒 >= 99.5%      (判定)
    栏2 汉字多重集差 = 0            (判定, 丢失与凭空增多都算)
    栏3 空白/换行差                 (只 kv 输出, 不参与判定)
  理由与验算见 qa/harness/README-assertions.md §5。

真书规模口径 (F-R4-7): 本文件与 run_p2 一律按 **"MAX_BOOK_CHARS(=2,000,000) 上界内
  的最大真书"** 取值 —— 实测《寻秦记》(精排未删版) MOBI, DB total_chars=1,591,303
  (解析后 1,591,388 字 / 291 章)。不再引用"330 万字 MOBI"这一口径: 330 万字量级实为
  EPUB 3,870,692 / TXT 2,700,901, 而 >2,000,000 字被 PY-103 直接拒收
  (实测 2,700,901 字 TXT 在 import_book 3.99s 后抛 ValueError, 不入库)。
  盘点表引用: qa/round-2/artifacts/result_R4_BIGBOOK.json 的 data.inventory。
"""
import sys
import os
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

MIXED = (
    "Mr. Smith 说：\u201cThis is absolutely wonderful, isn't it?\u201d "
    "林昭愣了一下：\u201c博士，您认错人了。\u201d "
    "\u201c不可能，\u201d他盯着林昭的眼睛，"
    "\u201c你胸前这枚玉佩，是 Dr. Lee 留给我的。\u201d "
    "风吹过，烛火摇了摇。他缓缓坐下。\u201c你走吧。\u201d"
)
LONG = "她说了一句，然后停了停，又接着说，声音很低，像是在自言自语，谁也没有听见，" * 6


def main():
    log = H.LOGS / "p4_split.log"
    with H.Tee(log):
        su = H.Suite("P4-split", "run_p4_split.py")
        from modules.text_processor.sentence_splitter import SentenceSplitter
        sp = SentenceSplitter()

        # ---------- 1: 中英混排 + 引号 + 缩写 ----------
        r = su.new("中英混排/引号/英文缩写 分句正确性")
        r.evidence_add(log)
        sents = sp.split(MIXED, "zh")
        H.kv("输入", MIXED)
        H.kv("句数", len(sents))
        for i, s in enumerate(sents):
            H.kv(f"  [{i}]", s)
        lost = [p for p in ("Mr.", "Dr.") if p not in "".join(sents)]
        r.check("英文缩写未被错误切分 (Mr./Dr.)", not lost, f"丢失: {lost}")
        r.check("字符无丢失", len("".join(sents)) >= len(MIXED) - 6,
                f"原 {len(MIXED)} -> 分句后合计 {len(''.join(sents))}")
        broken_quote = [s for s in sents if s.count("\u201c") != s.count("\u201d")
                        and ("\u201c" in s or "\u201d" in s)]
        H.kv("引号不成对的句", broken_quote)
        r.check("引号内内容不被切开", not broken_quote, f"{len(broken_quote)} 句引号破损")
        eng = [s for s in sents if re.search(r"[A-Za-z]{4,}", s)]
        H.kv("含英文的句", eng)
        r.check("英文整句保留 (This is absolutely wonderful)",
                any("This is absolutely wonderful" in s for s in sents), str(eng)[:200])

        # ---------- 2: 逗号碎片化 (TTS 韵律/调用次数) ----------
        # 断言修订(round-2 裁定, 智能体1c): 旧 ratio = 句数 / max(输入句末标点数,1) < 2.0
        # 在本 fixture 上数学不可满足 —— 手算:
        #   LONG = 37 字单元 x6 = 222 字; 句末标点 [。！？…] 出现 0 次 -> 分母保底 = 1
        #   ratio < 2.0 且分母=1  =>  句数 <= 1  =>  单句长度 = 222 字
        #   但 MAX_SENTENCE_LENGTH = 150, 无损切分 222 字必然 >= 2 段,
        #   与下一行 "无超长句残留" 互斥 -> 两条断言不可能同时为真。
        # 改为直接度量 PY-107 的真实语义("韵律单元是否停在逗号上 / 是否各有句末终结符"),
        # 并对 fixture 先验算可达性:
        #   现实现实测: 句数=3, 逗号结尾=0, 句末标点结尾=3
        #     -> 逗号结尾占比 = 0/3 = 0.0% < 5%            [可达]
        #     -> 句数/句末标点结尾数 = 3/3 = 1.0 ∈ [0.8,2.5] [可达]
        #   变异检验(PY-107 修复前的无条件逗号强拆): 句数=36, 逗号结尾=36, 句末标点结尾=0
        #     -> 100% >= 5% FAIL, 36/max(0,1)=36.0 ∉ [0.8,2.5] FAIL  => 断言仍能抓住回归
        r = su.new("分句粒度: 是否在每个逗号处断裂")
        sents2 = sp.split(LONG, "zh")
        strong_in = len(re.findall(r"[。！？…]", LONG))
        commas = LONG.count("，")
        H.kv("长文本", f"{len(LONG)} 字, 输入句末标点数 {strong_in}, 逗号数 {commas}")
        H.kv("分句结果句数", len(sents2))
        H.kv("平均句长", round(sum(len(x) for x in sents2) / max(len(sents2), 1), 1))
        H.kv("前 6 句", sents2[:6])
        ends_with_comma = sum(1 for s in sents2 if s.endswith(("，", ",", "、", "；")))
        ends_with_strong = sum(1 for s in sents2 if s and s[-1] in sp.CN_SENTENCE_END)
        H.kv("以逗号结尾的句数", ends_with_comma)
        H.kv("以句末标点结尾的句数", ends_with_strong)
        comma_share = ends_with_comma / max(len(sents2), 1)
        r.check("逗号结尾句占比 < 5% (韵律单元不再停在逗号上)", comma_share < 0.05,
                f"{ends_with_comma}/{len(sents2)} = {comma_share:.1%} "
                f"(输入含 {commas} 个逗号, 若逐个强拆将得 ~{commas} 句并以逗号结尾) -> "
                f"旧实现 CN_WEAK_BREAKS 无条件二次拆分, TTS 调用放大 {commas/max(len(sents2),1):.0f} 倍")
        gran = len(sents2) / max(ends_with_strong, 1)
        r.check("句数/句末标点 ∈ [0.8, 2.5] (每个 TTS 单元恰好消费一个句末终结符)",
                0.8 <= gran <= 2.5,
                f"句数 {len(sents2)} / 句末标点结尾句数 {ends_with_strong} = {gran:.2f} "
                f"(输入句末标点数 {strong_in}, 无句末标点的退化长句由 _normalize_tail 补 。)")
        over = [s for s in sents2 if len(s) > sp.MAX_SENTENCE_LENGTH]
        H.kv(f"超过 MAX_SENTENCE_LENGTH({sp.MAX_SENTENCE_LENGTH}) 的句", len(over))
        r.check("无超长句残留", not over, f"{len(over)} 句超长")

        # ---------- 2b: US-B2 SplitPolicy 注入（纯离线，不依赖后端/DB）----------
        r2b = su.new("US-B2: SplitPolicy 注入 — 默认零变化 + gsv 档双栏字符守恒")
        from modules.text_processor.sentence_splitter import (
            SentenceSplitter as _SS, DEFAULT_SPLIT_POLICY as _DEF,
            GPTSOVITS_SPLIT_POLICY as _GSV)
        # 默认 policy 与不注入逐字等价（单引擎项目零变化的护栏）
        same = all(_SS().split(t, "zh") == _SS(policy=_DEF).split(t, "zh")
                   for t in (MIXED, LONG))
        r2b.check("默认 SplitPolicy 分句结果 == 改造前(SentenceSplitter())", same)
        H.kv("gsv policy", f"max{_GSV.max_len}/min{_GSV.min_len}/"
                           f"tail={_GSV.tail_merge}/upper={_GSV.upper_bound}")
        gsv = _SS(policy=_GSV)
        ADVERSARIAL = "旁白起。" + "".join(
            "他说道：\u201c第%d句很长的中文引号内容用来测试占位符原子性，这里还有逗号，继续。\u201d她点头。" % i
            for i in range(9))
        for nm, txt in (("MIXED", MIXED), ("MIXEDx3", MIXED * 3),
                        ("LONG", LONG), ("QUOTE-DENSE", ADVERSARIAL)):
            outs = gsv.split(txt, "zh")
            rep = H.char_conservation_report(txt, outs)
            H.kv(f"gsv/{nm} 句数/非空白守恒/汉字丢失",
                 f"{len(outs)} / {rep['nonws_ratio']:.4%} / {rep['han_lost']}")
            r2b.check(f"gsv/{nm} [栏1] 非空白守恒 ∈ [99.5%,100.05%] (F-R4-6)",
                      0.995 <= rep["nonws_ratio"] <= 1.0005,
                      f"{rep['nonws_out']}/{rep['nonws_in']}={rep['nonws_ratio']:.4%}")
            r2b.check(f"gsv/{nm} [栏2] 汉字多重集差 = 0 (F-R4-6)",
                      rep["han_lost"] == 0 and rep["han_gained"] == 0,
                      f"丢失={rep['han_lost']} 凭空={rep['han_gained']} "
                      f"丢失样本={rep['han_lost_sample']!r}")
            leak = [o for o in outs if "__QUOTE_" in o]
            r2b.check(f"gsv/{nm} 引号保护占位符未泄漏进成品", not leak, str(leak)[:160])
            quoted = [s for s in outs if "\u201c" in s or '"' in s]
            nonquoted = [s for s in outs if s not in quoted]
            H.kv(f"gsv/{nm} 片段长度 max/p50/超45数(引号保护|无引号)",
                 f"{max(len(s) for s in outs)}/{sorted(len(s) for s in outs)[len(outs)//2]}/"
                 f"{sum(1 for s in quoted if len(s) > _GSV.max_len)}|"
                 f"{sum(1 for s in nonquoted if len(s) > _GSV.max_len)}")
            # 无引号片段: max_len 是目标, min_len=6 回并允许至多 2x 余量(碎片句代价换 45 字粒度)
            r2b.check(f"gsv/{nm} 无引号片段不超过 2*max_len({_GSV.max_len * 2})",
                      all(len(s) <= _GSV.max_len * 2 for s in nonquoted),
                      f"非引号片段最长 {max((len(s) for s in nonquoted), default=0)}")
            if nonquoted:
                r2b.check(f"gsv/{nm} 无引号片段 <= max_len 占比 >= 80%",
                          (sum(1 for s in nonquoted if len(s) <= _GSV.max_len)
                           / len(nonquoted)) >= 0.8,
                          f"{sum(1 for s in nonquoted if len(s) <= _GSV.max_len)}/{len(nonquoted)}")
            else:
                H.kv(f"gsv/{nm} 无引号片段占比护栏", "N/A（本语料全部片段含引号，跳过该比例判定）")

        # gsv 档在真书语料规模上的守恒（缺字红线）：只读 DB, 不启后端
        n_gsv = int(os.environ.get("PYY_P4_GSV_CHAPTERS", "20"))
        try:
            rb = H.db_rows("SELECT id,title,total_chars FROM books WHERE total_chars<=2000000 "
                           "AND (SELECT COUNT(*) FROM chapters c WHERE c.book_id=books.id)>0 "
                           "ORDER BY total_chars DESC LIMIT 1")
        except Exception as e:                                    # pragma: no cover
            rb = []
            H.kv("gsv/真书语料", f"DB 不可读: {e}")
        if rb and n_gsv > 0:
            chs2 = H.db_rows(f'SELECT content FROM chapters WHERE book_id=? '
                             f'ORDER BY "index" LIMIT {int(n_gsv)}', (rb[0]["id"],))
            body2 = "".join(c["content"] or "" for c in chs2)
            outs2 = []
            for c in chs2:
                outs2.extend(gsv.split(c["content"] or "", "zh"))
            rep2 = H.char_conservation_report(body2, outs2)
            L2 = sorted(len(s) for s in outs2)
            H.kv("gsv/真书语料", f"{rb[0]['title']!r} 前 {len(chs2)} 章 {len(body2)} 字")
            H.kv("gsv/真书 句数/非空白守恒/汉字丢失",
                 f"{len(outs2)} / {rep2['nonws_ratio']:.4%} / {rep2['han_lost']}")
            H.kv("gsv/真书 片段长度 p50/p95/max", f"{L2[len(L2)//2]}/{L2[int(len(L2)*.95)]}/{L2[-1]}")
            r2b.check("gsv/真书 [栏1] 非空白字符守恒 ∈ [99.5%,100.05%] (F-R4-6)",
                      0.995 <= rep2["nonws_ratio"] <= 1.0005,
                      f"{rep2['nonws_out']}/{rep2['nonws_in']}={rep2['nonws_ratio']:.4%}")
            r2b.check("gsv/真书 [栏2] 汉字多重集差 = 0 (F-R4-6)",
                      rep2["han_lost"] == 0 and rep2["han_gained"] == 0,
                      f"丢失={rep2['han_lost']} 凭空={rep2['han_gained']} "
                      f"丢失样本={rep2['han_lost_sample']!r}")
            r2b.check("gsv/真书 无占位符泄漏", not any("__QUOTE_" in s for s in outs2))
            r2b.check("gsv/真书 片段不超 upper_bound(500)",
                      all(len(s) <= _GSV.upper_bound for s in outs2), f"max={L2[-1]}")
        else:
            H.kv("gsv/真书语料", "N/A（DB 无在容量上界内的书 或 PYY_P4_GSV_CHAPTERS=0）")

        # ---------- 2c: US-B5 音色分组调度 index 序列等价（纯离线）----------
        r2c = su.new("US-B5: gsv 音色分组执行 index 序列等价 + 换组次数不增")
        from modules.workflow.conversion_workflow import (
            group_gptsovits_execution_order as _grp,
            count_gsv_reference_switches as _cnt)
        _pattern = "ABABABCBC"   # 同章 A/B 交替 + 次章 B/C 交替
        seq = [{"index": i, "chapter_id": f"c{1 + i // _pattern.__len__()}",
                "voice_id": _pattern[i % len(_pattern)], "gsv": True}
               for i in range(len(_pattern))]
        ordered = _grp(seq)
        doc_seq = [x["index"] for x in seq]
        grp_seq = [x["index"] for x in ordered]
        H.kv("文档序 index", doc_seq)
        H.kv("分组后执行序 index", grp_seq)
        H.kv("ref 装载次数 文档/分组",
             f"{_cnt(seq)} / {_cnt(ordered)}")
        r2c.check("分组执行是输入的排列（每 index 恰一次）",
                  sorted(grp_seq) == sorted(doc_seq) and len(grp_seq) == len(doc_seq),
                  f"{len(grp_seq)} vs {len(doc_seq)}")
        r2c.check("index 集合等价（乱序执行不改成品落账 index）",
                  set(grp_seq) == set(doc_seq), f"差 {set(doc_seq) ^ set(grp_seq)}")
        r2c.check("分组后 ref 装载次数 <= 文档序（换组次数不增）",
                  _cnt(ordered) <= _cnt(seq),
                  f"{_cnt(seq)} -> {_cnt(ordered)}")

        # ---------- 3: 真实入库正文的分句统计 ----------
        r = su.new("真实书籍正文分句: 句数/字数守恒")
        bid = None
        be = H.start_backend(H.LOGS / "p4_backend.log")
        try:
            code, body = H.http("POST", "/books/import",
                                files={"file": ("p4_sample.txt",
                                                (H.FIXTURES / "sample_small.txt").read_bytes(),
                                                "text/plain")}, timeout=300)
            if code != 200:
                r.block("导入失败", str(body)[:200])
            else:
                bid = body["book"]["id"]
                _, cb = H.http("GET", f"/books/{bid}/chapters", timeout=120)
                chs = cb.get("chapters", [])
                # L14⑫ / WAVE1-B F-B-X1（观测位归因，不改产品码）：WF-09 起章节列表
                # 只下发 content_preview，`content` 恒为 null —— 旧 harness 直读
                # ch["content"] 于是拿到空串，[栏1] 非空白守恒 / [栏2] 汉字多重集差
                # 两条断言因此"假红"（红的是取数口径，不是分句器）。
                # 修法 = 按 WF-09 的懒加载出口逐章补正文（与前端 EditPage 同一口径），
                # 产品侧 GET /books/{id}/chapters 的预览语义保持不变。
                for ch in chs:
                    if not ch.get("content"):
                        _, cc = H.http("GET", f"/books/chapters/{ch['id']}/content",
                                       timeout=120)
                        ch["content"] = (cc or {}).get("content") or ""
                H.kv("整本正文合计字符", sum(len(ch["content"] or "") for ch in chs))
                total_sent = 0
                per_chap = []
                out_sents = []
                for ch in chs:
                    content = ch["content"] or ""
                    ss = sp.split(content, "zh")
                    total_sent += len(ss)
                    out_sents.extend(ss)
                    per_chap.append((ch["title"], len(content), len(ss),
                                     round(sum(len(x) for x in ss) / max(len(ss), 1), 1)))
                joined_in = "".join((ch["content"] or "") for ch in chs)
                rep = H.char_conservation_report(joined_in, out_sents)
                H.kv("章节 -> (字数, 句数, 平均句长)", per_chap)
                H.kv("总句数", total_sent)
                H.kv("[栏1] 非空白字符 in/out/守恒",
                     f"{rep['nonws_in']} / {rep['nonws_out']} / {rep['nonws_ratio']:.4%}")
                H.kv("[栏2] 汉字多重集 in/out/丢失/凭空增多",
                     f"{rep['han_in']} / {rep['han_out']} / {rep['han_lost']} / {rep['han_gained']}")
                H.kv("[栏3] 空白差(单列输出, 不参与判定)",
                     f"in={rep['ws_in']} out={rep['ws_out']} delta={rep['ws_delta']}")
                old = H.old_conservation_gate(rep)
                H.kv("(对照) 修订前旧门限", f"{old['formula']} -> "
                                      f"{rep['raw_out']}/{rep['nonws_in']} = {old['ratio']:.4f} "
                                      f"{'PASS(绿)' if old['passes'] else 'FAIL(红)'}; "
                                      f"静默丢失余量 {old['loss_headroom_chars']} 字符 "
                                      f"({old['loss_headroom_pct_of_body']:.2%} 正文) 仍判绿")
                # 栏1: 非空白守恒 (上界 1.0005 防"凭空造字/重复正文"蒙过下界断言)
                r.check("[栏1] 非空白字符守恒 ∈ [99.5%, 100.05%] (F-R4-6)",
                        0.995 <= rep["nonws_ratio"] <= 1.0005,
                        f"{rep['nonws_out']}/{rep['nonws_in']} = {rep['nonws_ratio']:.4%}")
                # 栏2: 汉字多重集差 = 0 —— 正文一个字都不能丢, 也不能多
                r.check("[栏2] 汉字多重集差 = 0 (丢失=0 且 凭空增多=0, F-R4-6)",
                        rep["han_lost"] == 0 and rep["han_gained"] == 0,
                        f"丢失={rep['han_lost']} 凭空={rep['han_gained']} "
                        f"丢失样本={rep['han_lost_sample']!r} (汉字以外的标点/字母差由栏1 覆盖)")
                short = [s for c in chs for s in sp.split(c["content"] or "", "zh")
                         if len(s) <= 3]
                H.kv("长度<=3的碎片句", len(short), )
                H.kv("碎片示例", short[:12])
                r.check("无大量 3 字以下碎片", len(short) < total_sent * 0.05,
                        f"{len(short)}/{total_sent} 句为碎片")

                # ---------- 3b: 真书语料上的双栏守恒 (F-R4-6 可达性实证) ----------
                # fixture 仅 2.6KB、空白稀疏, 单靠它无法证明新口径在"空白密集真书"上可达,
                # 也无法证明旧 98% 门限会误判。语料口径(F-R4-7)= DB 内
                # total_chars <= Config.MAX_BOOK_CHARS 的**最大真书**(上界内最大), 取前 N 章;
                # 盘点表引用 qa/round-2/artifacts/result_R4_BIGBOOK.json data.inventory。
                # 只读已入库章节 -> 不重导入、不写库、秒级完成。
                n_real = int(os.environ.get("PYY_P4_REAL_CHAPTERS", "40"))
                r3b = su.new("真书语料分句守恒: 双栏口径在 10^5 字量级成立 (F-R4-6)")
                from backend.config import Config
                rb = H.db_rows("SELECT id,title,total_chars,format FROM books "
                               "WHERE total_chars<=? AND "
                               "(SELECT COUNT(*) FROM chapters c WHERE c.book_id=books.id)>0 "
                               "ORDER BY total_chars DESC LIMIT 1", (Config.MAX_BOOK_CHARS,))
                if not rb or n_real <= 0:
                    r3b.block("无可用真书语料 (data/books 未入库大书)",
                              f"SQL 命中 {len(rb)} 行 / PYY_P4_REAL_CHAPTERS={n_real} "
                              "-> 数据可用性问题, 按 ENV 记 BLOCKED, 不记产品 FAIL")
                else:
                    rch = H.db_rows(f'SELECT title,content FROM chapters WHERE book_id=? '
                                    f'ORDER BY "index" LIMIT {int(n_real)}', (rb[0]["id"],))
                    rtext = "".join(c["content"] or "" for c in rch)
                    rsents = []
                    for c in rch:
                        rsents.extend(sp.split(c["content"] or "", "zh"))
                    rrep = H.char_conservation_report(rtext, rsents)
                    H.kv("语料(F-R4-7 口径)", f"{rb[0]['title']!r} {rb[0]['format']} "
                                     f"全书 {rb[0]['total_chars']} 字 / 上界 {Config.MAX_BOOK_CHARS} "
                                     f"-> 取前 {len(rch)} 章 {rrep['raw_in']} 字")
                    su.set_meta("realbook_corpus", {"title": rb[0]["title"], "format": rb[0]["format"],
                                                    "book_chars": rb[0]["total_chars"],
                                                    "max_book_chars": Config.MAX_BOOK_CHARS,
                                                    "chapters": len(rch), "raw_chars": rrep["raw_in"]})
                    H.kv("句数", len(rsents))
                    H.kv("[栏1] 非空白守恒",
                         f"{rrep['nonws_out']}/{rrep['nonws_in']} = {rrep['nonws_ratio']:.4%}")
                    H.kv("[栏2] 汉字多重集 丢失/凭空增多",
                         f"{rrep['han_lost']} / {rrep['han_gained']} (in={rrep['han_in']} out={rrep['han_out']})")
                    H.kv("[栏3] 空白差(单列, 不判)",
                         f"{rrep['ws_delta']} = 原始字符的 {rrep['ws_delta']/max(rrep['raw_in'],1):.2%}")
                    old = H.old_conservation_gate(rrep)
                    H.kv("(对照A) 修订前旧门限", f"{old['formula']} -> "
                                          f"{rrep['raw_out']}/{rrep['nonws_in']} = {old['ratio']:.4f} "
                                          f"{'PASS(绿)' if old['passes'] else 'FAIL(红)'}; "
                                          f"静默丢失余量 {old['loss_headroom_chars']} 字符 "
                                          f"({old['loss_headroom_pct_of_body']:.2%} 正文) 仍判绿")
                    H.kv("(对照B) 空白进分母的同型门限",
                         f"raw_out/raw_in = {rrep['raw_ratio']:.4f} -> "
                         f"98% 门限 {'PASS(绿)' if rrep['raw_ratio'] >= 0.98 else 'FAIL(红)'}; "
                         f"差值 {rrep['raw_in'] - rrep['raw_out']} 全部落在栏3 空白")
                    su.set_meta("old_gate_recompute", {
                        "scope": f"{rb[0]['title']} 前 {len(rch)} 章",
                        "old_form_A_rawout_vs_nonwsin": old,
                        "old_form_B_rawout_vs_rawin": {
                            "ratio": rrep["raw_ratio"],
                            "passes_98": rrep["raw_ratio"] >= 0.98,
                            "ws_only_gap_chars": rrep["raw_in"] - rrep["raw_out"]},
                        "new_form_nonws_ratio": rrep["nonws_ratio"],
                        "new_form_han_lost": rrep["han_lost"],
                        "note": "对照B 即 3c BIGBOOK 探针用的分母口径: 全书实测 "
                                "1,558,199/1,591,388 = 0.9791 (见 result_R4_BIGBOOK.json "
                                "checks['分句不丢字(>=98%)'] 与 kv['分句字符守恒'], 38,148 句), "
                                "差值纯为排版空白 -> 旧写法双向都不安全: A 漏判真丢字, B 误判丢正文。"})
                    r3b.check("[栏1] 真书非空白字符守恒 ∈ [99.5%, 100.05%] (F-R4-6)",
                              0.995 <= rrep["nonws_ratio"] <= 1.0005,
                              f"{rrep['nonws_out']}/{rrep['nonws_in']} = {rrep['nonws_ratio']:.4%}")
                    r3b.check("[栏2] 真书汉字多重集差 = 0 (F-R4-6)",
                              rrep["han_lost"] == 0 and rrep["han_gained"] == 0,
                              f"丢失={rrep['han_lost']} 凭空={rrep['han_gained']} "
                              f"丢失样本={rrep['han_lost_sample']!r}")
                    r3b.check("[栏3 反向护栏] 空白差确实存在且被单列剥离(证明本用例不是 vacuous)",
                              rrep["ws_delta"] > 0 and rrep["han_lost"] == 0,
                              f"空白差={rrep['ws_delta']} 汉字丢失={rrep['han_lost']} "
                              f"-> 旧口径会把这 {rrep['ws_delta']} 个排版空白计入'丢字'")
                    r3b.notes.append(
                        f"口径(F-R4-7): 真书规模 = MAX_BOOK_CHARS({Config.MAX_BOOK_CHARS}) 上界内的最大真书 "
                        f"= {rb[0]['title']!r} {rb[0]['total_chars']} 字; 本用例取前 {len(rch)} 章 "
                        f"{rrep['raw_in']} 字; 盘点表见 qa/round-2/artifacts/result_R4_BIGBOOK.json")

                # ---------- 4: 与 pipeline._stage_split 行为一致性 ----------
                r2 = su.new("pipeline._stage_split 输出的句子结构")
                from modules.workflow.conversion_workflow import ConversionWorkflow
                cw = ConversionWorkflow()
                ctx = {"task_id": "harness-p4", "book_language": "zh", "settings": {},
                       "chapters": [dict(c) for c in chs]}
                out = cw._stage_split(ctx)
                sents = out["sentences"]
                H.kv("pipeline 句数", len(sents))
                H.kv("首句结构", sents[0] if sents else None)
                need = {"chapter_id", "chapter_title", "text", "char_count"}
                r2.check("句子字段完整(chapter_id/title/text/char_count)",
                         all(need <= set(s.keys()) for s in sents), str(need))
                r2.check("pipeline 句数与直接分句一致", len(sents) == total_sent,
                         f"{len(sents)} vs {total_sent}")
                lang_bug = [s["text"] for s in sents[:3]]
                H.kv("前 3 句", lang_bug)
        finally:
            if bid:
                H.http("DELETE", f"/books/{bid}")
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
