"""
[P5] analyze 阶段实测: DialogueDetector + ToneDetector 对真实对白的输出
运行: python qa/harness/run_p5_analyze.py

真实路径: 书籍经 /books/import 入库 -> split 成句 -> 送入 pipeline._stage_analyze
          (与生产完全同一函数)
"""
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

DIALOG_TEXT = (
    "林昭推开房门，看见窗边坐着一个人。\n"
    "\u201c你来了。\u201d那人没有回头。\n"
    "周主事放下茶盏，压低声音：\u201c昨夜宫里出了事。\u201d\n"
    "\u201c该变的都变了。\u201d林昭给自己倒了杯茶。\n"
    "阿贵蹲在门槛上，头也不抬，\u201c你当真要走？\u201d\n"
    "她叹了口气：\u201c罢了，时候到了总要走的。\u201d\n"
)


def main():
    log = H.LOGS / "p5_analyze.log"
    with H.Tee(log):
        su = H.Suite("P5-analyze", "run_p5_analyze.py")

        # ---------- 1: 检测器单元测试级(真实类真实输入) ----------
        r = su.new("DialogueDetector 对整段对白的角色识别")
        r.evidence_add(log)
        from modules.text_processor.dialogue_detector import DialogueDetector
        from modules.text_processor.tone_detector import ToneDetector
        from modules.cast_analyzer.cast_extractor import looks_like_name
        dd = DialogueDetector(name_gate=looks_like_name)
        segs = dd.detect_dialogues(DIALOG_TEXT)
        dlg = [s for s in segs if s["type"] == "dialogue"]
        H.kv("片段总数", len(segs))
        for s in segs:
            H.kv(f"  {s['type']:9s}", f"role={s.get('role_hint')!r} "
                                      f"content={s['content'][:24]!r}")
        r.check("检出 5 段对话", len(dlg) == 5, f"{len(dlg)}")
        expect_roles = {"林昭", "周主事", "阿贵", "那人"}
        got_roles = {s.get("role_hint") for s in dlg}
        H.kv("角色提示命中", sorted(x for x in got_roles if x))
        H.kv("未命中角色", sorted(expect_roles - got_roles))
        # 口径变更史（双向棘轮，两半都必须逐条对上）:
        #   PRE-WF-01 = 4/4，但夹带两枚假角色 里头传来 / 声音很轻（下游凭空造角色绑音色）
        #   WF-01     = 2/4 零假角色，代价是整条**动作后窗**归因被关掉（旧账单，见下）
        #   PY-338    = 3/4 零假角色：后窗恢复"动作主语"归因，但候选必须过 cast 的判名门。
        # 上限之所以是 3/4 而不是 4/4：`那人` 以指示代词开头，判名门（正向姓名形态）不认，
        # 这是设计结论，不是漏修——为把它凑成 4/4 而放宽判名门，会同时放回那两枚假角色。
        # 命中集钉成精确值 = 双向棘轮: 再放宽后窗 → 多出角色翻红；再收紧扣回 → 少角色翻红。
        # 每半步都要连着改本断言 + issues.md PY-338 的账，不许让召回率静默变化。
        r.check("关键角色命中 == PY-338 口径下的 {周主事, 林昭, 阿贵}（3/4，那人按设计不认）",
                (expect_roles & got_roles) == {"周主事", "林昭", "阿贵"},
                f"命中 {sorted(expect_roles & got_roles)} / 未命中 {sorted(expect_roles - got_roles)}"
                f" / 全集 {sorted(got_roles - {None})} —— 未命中只允许 那人(判名门否)，"
                f"其余任何一枚掉队都是召回回退")
        r.check("零假角色（WF-01 那两枚必须继续被拦）",
                not ({s.get("role_hint") for s in dlg} & {"里头传来", "声音很轻"}),
                f"全集 {sorted(x for x in got_roles if x)}")

        roles = dd.extract_roles(segs)
        H.kv("extract_roles", roles)
        r.check("extract_roles 输出角色统计", bool(roles), str(roles)[:150])

        # ---------- 2: ToneDetector ----------
        r = su.new("ToneDetector 情感判定")
        cases = {
            "你当真要走？": "question",
            "太好了！我们成功了！": "excited",
            "她流着泪说：再也见不到了……": "sad",
            "混蛋，你给我滚！": "angry",
            "请问，这里发生什么事了": "question",
        }
        td = ToneDetector()
        for text, want in cases.items():
            got = td.detect(text)
            H.kv(f"  {text[:16]!r}", f"primary={got['primary']} conf={got['confidence']} "
                                    f"scores={got['scores']}")
            r.check(f"情感={want}: {text[:12]}...", got["primary"] == want,
                    f"实际 {got['primary']}")

        # ---------- 3: 真实 pipeline._stage_analyze (split 之后的片段) ----------
        r = su.new("pipeline._stage_analyze 在真实 split 片段上的产出")
        bid = None
        be = H.start_backend(H.LOGS / "p5_backend.log")
        try:
            code, body = H.http("POST", "/books/import",
                                files={"file": ("p5_sample.txt",
                                                (H.FIXTURES / "sample_small.txt").read_bytes(),
                                                "text/plain")}, timeout=300)
            if code != 200:
                r.block("导入失败", str(body)[:200])
            else:
                bid = body["book"]["id"]
                _, cb = H.http("GET", f"/books/{bid}/chapters", timeout=120)
                # WF-09 起列表只下发预览，正文必须走懒加载出口补（见 fill_chapter_content）
                chs = H.fill_chapter_content(cb.get("chapters", []))
                from modules.workflow.conversion_workflow import ConversionWorkflow
                from modules.text_processor.sentence_splitter import SentenceSplitter
                sp = SentenceSplitter()
                sentences = []
                for ch in chs:
                    for s in sp.split(ch["content"] or "", "zh"):
                        sentences.append({"chapter_id": ch["id"], "chapter_title": ch["title"],
                                          "text": s, "char_count": len(s)})
                ctx = {"task_id": "harness-p5", "sentences": sentences}
                cw = ConversionWorkflow()
                out = cw._stage_analyze(ctx)
                sents = out["sentences"]
                ndlg = sum(1 for s in sents if s.get("is_dialogue"))
                with_speaker = sum(1 for s in sents if s.get("speaker"))
                tones = {}
                for s in sents:
                    tones[s.get("tone")] = tones.get(s.get("tone"), 0) + 1
                H.kv("句子总数", len(sents))
                H.kv("标记为对话的句子", f"{ndlg} ({ndlg/max(len(sents),1):.1%})")
                H.kv("带 speaker 的句子", f"{with_speaker} ({with_speaker/max(len(sents),1):.1%})")
                H.kv("tone 分布", tones)
                for s in sents:
                    if s.get("is_dialogue"):
                        H.kv("  对话例句", f"{s['text'][:34]!r} speaker={s['speaker']!r} "
                                          f"tone={s['tone']}")
                # ---------- 对白/语气断言修订 (round-2 裁定, 智能体1c) ----------
                # 旧断言 r.check("split 之后仍能完整识别跨片段对白", ndlg >= raw_dialogues*0.6)
                # 不可达: raw_dialogues 对 ASCII 直引号左右各计一次 -> 46 = 23 处对白 x2,
                # 阈值 46*0.6=27.6 高于整书真实对白处数(23), 任何实现都无法满足。
                # 期望值全部改为按 *成对白处数* 计, 并补三条可判定行为断言
                # (以下数字先用 python -c 对当前实现离线验算, 见 README-assertions.md):
                #   整书对白处数 23 / split 片段 33 / analyze 判为对白 16 -> 16/23=69.6% >= 60%
                #   片段内引号成对可见 16, 其中判为对白 16 -> 子集召回 100% >= 90%
                #   判为对白却不含任何引号的片段 0
                #   含疑问信号(？/? 或句末 吗/呢/么)的片段 7, primary 全部 == question
                LQ, RQ, DQ = "\u201c", "\u201d", '"'

                def quote_pairs(t):
                    """一处对白 = 一对引号; ASCII 直引号左右同符, 需除以 2"""
                    return t.count(DQ) // 2 + t.count(LQ)

                def pair_visible(t):
                    return t.count(DQ) >= 2 or (t.count(LQ) >= 1 and t.count(LQ) == t.count(RQ))

                raw_dialogues = sum(quote_pairs(c["content"] or "") for c in chs)
                H.kv("正文引号字符数(左+右, 旧口径翻倍值)",
                     sum((c["content"] or "").count(DQ) + (c["content"] or "").count(LQ) for c in chs))
                H.kv("整书对白处数(按成对引号计)", raw_dialogues)
                r.check("前置: 正文确有引号对白（否则召回门不可判定）", raw_dialogues > 0,
                        f"raw_dialogues={raw_dialogues} —— 为 0 通常意味着正文没读到手")
                r.check("对白句子被识别出来", ndlg > 0,
                        f"{ndlg}/{len(sents)}; 整书正文实际含 {raw_dialogues} 处引号对白")
                r.check("整书对白召回 >= 处数*0.6 (分母=成对白处数, 非引号字符数)",
                        ndlg >= raw_dialogues * 0.6,
                        f"识别 {ndlg} / 对白处数 {raw_dialogues} = "
                        f"{ndlg/max(raw_dialogues,1):.1%} (旧口径 46 使阈值 27.6 不可能达到)")
                visible = [s for s in sents if pair_visible(s["text"])]
                vis_hit = [s for s in visible if s.get("is_dialogue")]
                vis_recall = len(vis_hit) / max(len(visible), 1)
                H.kv("分句后引号仍成对可见的片段", f"{len(visible)} / {len(sents)}")
                r.check("可判定子集召回: 引号成对可见的片段 >=90% 判为对白",
                        len(visible) > 0 and vis_recall >= 0.9,
                        f"{len(vis_hit)}/{len(visible)} = {vis_recall:.1%}")
                halluc = [s["text"] for s in sents
                          if s.get("is_dialogue")
                          and not any(q in s["text"] for q in (DQ, LQ, RQ))]
                H.kv("幻觉对白(判为对白却无引号)", len(halluc), )
                H.kv("示例", halluc[:5])
                r.check("无幻觉对白: 不含任何引号字符却判为对白的片段 = 0", not halluc,
                        f"{len(halluc)} 段: {halluc[:3]}")

                # ---------- PY-110 可判定行为: 疑问信号 -> question ----------
                # ToneDetector 现行实现(PUNCT_MAP ？=0.8 硬信号 + QUESTION_PREFIXES +
                # 句末 吗/呢/么 0.6)下, "含疑问信号的片段 primary 必为 question" 可判定;
                # 反向(无信号却判 question)不作约束—— 什么/谁 等词库同样命中。
                def q_signal(t):
                    t = (t or "").strip()
                    return ("？" in t or "?" in t
                            or bool(re.search(r'(吗|呢|么)[\u201d"\u300d]?\s*$', t)))

                qsents = [s for s in sents if q_signal(s["text"])]
                qbad = [(s["text"][:40], s.get("tone")) for s in qsents if s.get("tone") != "question"]
                H.kv("含疑问信号的片段", len(qsents))
                q_tones = {s.get("tone"): 0 for s in qsents}
                for s in qsents:
                    q_tones[s.get("tone")] = q_tones.get(s.get("tone"), 0) + 1
                H.kv("其中 tone 分布", q_tones)
                for snippet, tn in qbad[:5]:
                    H.kv("  疑问误判", f"{tn!r} <- {snippet!r}")
                r.check("PY-110 可判定行为: 含 ？/? 或句末 吗/呢/么 的片段 primary == question",
                        len(qsents) > 0 and not qbad,
                        f"{len(qsents) - len(qbad)}/{len(qsents)} 一致 (子集非空, 否则视为不可判定)")
                r.notes.append("能力边界(round1b 跟踪, 非 PY-110 声明范围): split->analyze 顺序使 "
                               f"{raw_dialogues} 处整书对白中仅 {len(visible)} 处在分句后引号仍成对可见, "
                               f"故片段级识别上限 = {len(visible)} (实测 {ndlg})。")

                # speaker 是否可作为角色名匹配
                junk = [s["speaker"] for s in sents
                        if s.get("speaker") and len(s["speaker"]) < 2]
                H.kv("可疑 speaker(长度<2)", junk[:10])

                # ---------- 4: 交叉验证: 未 split 的整段正文对白召回 ----------
                r2 = su.new("对照: 未分句时 DialogueDetector 召回率")
                full = "\n".join(c["content"] or "" for c in chs)
                segs_full = dd.detect_dialogues(full)
                dlg_full = [s for s in segs_full if s["type"] == "dialogue"]
                H.kv("整段正文 -> 对话片段", f"{len(dlg_full)} (引号对白 {raw_dialogues} 处)")
                r2.check("整段检测召回 >= 片段检测召回",
                         len(dlg_full) >= ndlg,
                         f"整段 {len(dlg_full)} vs 分句后 {ndlg} -> "
                         f"pipeline 的顺序(split->analyze) 损失对白识别")

                # ---------- 5: 真实电子书(弯引号 U+201C/U+201D)链路 ----------
                r3 = su.new("真实 EPUB(中文弯引号) split->analyze 召回")
                ecode, ebody = H.http("POST", "/books/import",
                                      files={"file": ("p5_sample_curly.epub",
                                                      (H.FIXTURES / "sample_curly.epub").read_bytes(),
                                                      "application/epub+zip")}, timeout=300)
                if ecode != 200:
                    r3.block("EPUB 导入失败", str(ebody)[:200])
                else:
                    evid = ebody["book"]["id"]
                    try:
                        _, ech = H.http("GET", f"/books/{evid}/chapters", timeout=120)
                        echs = H.fill_chapter_content(ech.get("chapters", []))
                        etext = "\n".join(c["content"] or "" for c in echs)
                        e_quotes = etext.count("\u201c")
                        H.kv("EPUB 书名", ebody["book"].get("title"))
                        H.kv("EPUB 章数", len(echs))
                        H.kv("正文弯引号(左)数 = 实际对白处数", e_quotes)
                        e_sents = []
                        for c in echs:
                            for s in sp.split(c["content"] or "", "zh"):
                                e_sents.append({"chapter_id": c["id"],
                                                "chapter_title": c["title"],
                                                "text": s, "char_count": len(s)})
                        # 根因诊断: split 是否在弯引号内部切断 (导致引号不成对)
                        LQ, RQ = "\u201c", "\u201d"
                        unpaired = [s["text"] for s in e_sents
                                    if s["text"].count(LQ) != s["text"].count(RQ)]
                        H.kv("引号不成对的 split 片段", f"{len(unpaired)} / {len(e_sents)}")
                        for t in e_sents[:12]:
                            H.kv("  split 片段", repr(t["text"]))
                        for t in unpaired[:8]:
                            H.kv("  破引号片段", repr(t))
                        r3.check("split 片段内弯引号成对率 >= 80%",
                                 len(e_sents) - len(unpaired) >= len(e_sents) * 0.8,
                                 f"{len(unpaired)} 个片段引号破对 -> "
                                 f"sentence_splitter.QUOTE_CONTENT 未包含 U+201C/U+201D, "
                                 f"在对白内部按句号强切, analyze 拿不到成对引号")
                        eout = cw._stage_analyze({"task_id": "harness-p5e",
                                                  "sentences": list(e_sents)})
                        es = eout["sentences"]
                        endlg = sum(1 for x in es if x.get("is_dialogue"))
                        espk = sum(1 for x in es if x.get("speaker"))
                        etones = {}
                        for x in es:
                            etones[x.get("tone")] = etones.get(x.get("tone"), 0) + 1
                        H.kv("split 片段数", len(e_sents))
                        H.kv("analyze 判为对白", f"{endlg} / {len(es)}")
                        H.kv("analyze 带 speaker", f"{espk} / {len(es)}")
                        H.kv("tone 分布", etones)
                        for x in es:
                            if x.get("is_dialogue"):
                                H.kv("  对白片段", f"{x['text'][:36]!r} speaker={x['speaker']!r}")
                        r3.check("前置: EPUB 正文确有弯引号对白（否则召回门不可判定）",
                                 e_quotes > 0,
                                 f"e_quotes={e_quotes} —— 为 0 通常意味着正文没读到手")
                        r3.check("弯引号对白召回 >= 60%",
                                 endlg >= e_quotes * 0.6,
                                 f"实际对白 {e_quotes} 处 / 判为对白 {endlg} 句")
                        r3.check("speaker 形状合格（凡有值即 >=2 字可当角色名）",
                                 all(len(x["speaker"]) >= 2 for x in es if x.get("speaker")),
                                 f"带 speaker 的句数 {espk}/{len(es)}; "
                                 f"样例 {[x['speaker'] for x in es if x.get('speaker')][:6]}")
                        # 双向棘轮（PY-338 后半）: 本 EPUB 夹具唯一的归因线索是动作后窗
                        # "“你当真要走？”阿贵蹲在门槛上，头也不抬。"。WF-01 把这一档整条关掉
                        # 后实测 0/11（当时的账单快照）；PY-338 恢复"动作主语+判名门"后实测
                        # 1/11，且归到的必须是 阿贵。钉精确值 + 指名角色：收紧回去会红，
                        # 放宽到把 "身后有人喊了一声" 这类叙述残片也归因同样会红。
                        eact = [x["speaker"] for x in es if x.get("speaker")]
                        r3.check("EPUB 动作后窗归因 == PY-338 口径下的 1/11 且角色为 阿贵",
                                 espk == 1 and eact == ["阿贵"],
                                 f"{espk}/{len(es)} 归因，角色 {eact}")
                    finally:
                        H.http("DELETE", f"/books/{evid}")
        finally:
            if bid:
                H.http("DELETE", f"/books/{bid}")
            H.stop_backend(be)
        su.finish(exit_when_fail=False)


if __name__ == "__main__":
    main()
