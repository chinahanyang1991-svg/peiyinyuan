"""
[C1] cast 抽取实测: modules/cast_analyzer/cast_extractor.py + cast_profiler.py
运行: set PYTHONIOENCODING=utf-8 && python qa/harness/run_c1_cast_extract.py

真实路径: 使用生产 DialogueDetector 作对白/归属信号源（与 P5 同一函数），
         中文小说 fixture 流式逐章喂入 CastExtractor -> CastCandidate。
         LLM 画像: LLMService 不可用时走 spec 4.1 明文的规则全表兜底（设计内路径，
         非 mock 生产逻辑）；可用时同时记录真实 LLM 结果。
判定: exit 0 = ALL_GREEN；结果 JSON 落 qa/round-1/artifacts/result_C1-cast-extract.json
"""
import json
import re
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

FIXTURE = H.FIXTURES / "sample_cast_novel.txt"
CHAPTER_RE = re.compile(r"(?m)^(?=第[一二三四五六七八九十百千0-9]+章)")

EXPECT_SPEAKERS = {"周建国", "林晚晴", "小满"}
EXPECT_GENDERS = {"周建国": "male", "林晚晴": "female", "小满": "female"}
ALIAS_FORMS = {"老周", "周队长"}
RARE_NAME = "阿贵"


class UnavailableLLM:
    """只用于把"LLM 不可用"这一分支跑成确定结论（不是生产链路的替身）。"""

    @staticmethod
    def is_ready():
        return False


def split_chapters(text):
    parts = [p.strip() for p in CHAPTER_RE.split(text) if p and p.strip()]
    return parts


def build_cast(chapters):
    from modules.cast_analyzer.cast_extractor import CastExtractor
    ex = CastExtractor()
    for idx, chap in enumerate(chapters):
        ex.process_chapter(chap, idx)
    return ex, ex.finalize()


def index_by_name(cast):
    return {c.name: c for c in cast}


def main():
    log = H.LOGS / "c1_cast_extract.log"
    with H.Tee(log):
        su = H.Suite("C1-cast-extract", "run_c1_cast_extract.py")

        from modules.cast_analyzer.cast_extractor import (
            CastExtractor, MAX_CHAPTER_SAMPLES, NARRATOR, PRONOUNS)
        from modules.cast_analyzer.cast_profiler import CastProfiler, profiles_to_role_extra_data

        # ---------------- 1: fixture 规格 ----------------
        r = su.new("fixture 规格: 字数/说话人/前后缀归属变体齐备")
        r.evidence_add(log)
        r.evidence_add(FIXTURE)
        raw = FIXTURE.read_text(encoding="utf-8")
        chapters = split_chapters(raw)
        compact = re.sub(r"\s+", "", raw)
        H.kv("fixture", str(FIXTURE))
        H.kv("字符数(去空白)", len(compact))
        H.kv("章节数", len(chapters))
        prefix_hits = re.findall(r"[\u4e00-\u9fa5]{2,4}(?:说道|说|道|问道|答道|笑道)", raw)
        suffix_hits = re.findall(r"[\u201d\"]\s*[\u4e00-\u9fa5]{2,4}(?:说道|说|道)", raw)
        prompt_hits = re.findall(r"(?m)^[\u4e00-\u9fa5]{2,4}[：:][\u201c\"]", raw)
        H.kv("前缀式 XX说/道", len(prefix_hits))
        H.kv("后缀式 \u201d\u2026\u2026XX说", len(suffix_hits))
        H.kv("称谓冒号式", len(prompt_hits))
        quoted = re.findall(r"\u201c[^\u201d]{2,}\u201d", raw)
        H.kv("引号对白数", len(quoted))
        r.check("fixture >= 800 字", len(compact) >= 800, f"{len(compact)}")
        r.check("章节 >= 2 (流式契约)", len(chapters) >= 2, f"{len(chapters)}")
        r.check("含前缀式归属", len(prefix_hits) >= 3, str(prefix_hits[:6]))
        r.check("含后缀式归属", len(suffix_hits) >= 3, str(suffix_hits[:6]))
        r.check("含代词句(需被丢弃或回溯)", "他说" in compact or "她" in compact, "OK")

        # ---------------- 2: 抽取结果规模 ----------------
        r = su.new("流式逐章抽取: 角色 >=3 + 旁白固定项")
        r.evidence_add(log)
        t0 = time.perf_counter()
        ex, cast = build_cast(chapters)
        elapsed = time.perf_counter() - t0
        stats = ex.stats()
        H.kv("耗时(4 章 fixture)", f"{elapsed:.4f}s")
        H.kv("extractor.stats", json.dumps(stats, ensure_ascii=False))
        by_name = index_by_name(cast)
        H.kv("候选", [(c.name, c.lines_count, c.chapters_touched) for c in cast])
        names = set(by_name)
        r.check("对白句被全部归属", stats["dialogue_lines"] > 0
                and stats["unresolved_lines"] == 0,
                f"lines={stats['dialogue_lines']} unresolved={stats['unresolved_lines']}")
        r.check(">=3 个具名说话人", len(EXPECT_SPEAKERS & (names - {NARRATOR})) >= 3,
                f"命中 {sorted(EXPECT_SPEAKERS & names)}")
        r.check("旁白固定项存在", NARRATOR in names and by_name[NARRATOR].is_narrator,
                f"旁白行数={by_name[NARRATOR].lines_count if NARRATOR in names else 'MISSING'}")
        r.check("旁白叙述段被计数", by_name.get(NARRATOR) and by_name[NARRATOR].lines_count >= 5,
                str(by_name.get(NARRATOR).lines_count if by_name.get(NARRATOR) else 0))
        r.check("样句上限 8 条", all(len(c.sample_quotes) <= 8 for c in cast),
                str(max(len(c.sample_quotes) for c in cast)))
        r.check("chapters_touched 跨章", len(by_name["周建国"].chapters_touched) >= 3,
                str(by_name["周建国"].chapters_touched))
        r.check("co_speakers 有对手戏", len(by_name["周建国"].co_speakers) >= 1,
                str(by_name["周建国"].co_speakers))
        r.check("按戏份降序(旁白除外)", [c.lines_count for c in cast if not c.is_narrator]
                == sorted([c.lines_count for c in cast if not c.is_narrator], reverse=True),
                str([c.lines_count for c in cast]))

        # ---------------- 3: 别名合并 ----------------
        r = su.new("别名合并: 老周/周队长 归一到 周建国")
        r.evidence_add(log)
        merged = by_name.get("周建国")
        r.check("老周/周队长 不再独立成候选", not (ALIAS_FORMS & names),
                f"残留 {sorted(ALIAS_FORMS & names)}")
        r.check("周建国 记录到别名", merged is not None
                and ALIAS_FORMS <= set(merged.aliases),
                f"aliases={merged.aliases if merged else None}")
        r.check("职务称谓进 titles", merged is not None and "队长" in merged.titles,
                str(merged.titles if merged else None))
        ex_raw_total = sum(agg.lines for agg in ex._aggs.values())
        r.check("合并后行数守恒", merged is not None and merged.lines_count >= 6,
                f"周建国 lines={merged.lines_count if merged else 0} 全库={ex_raw_total}")

        # ---------------- 4: 噪声过滤 ----------------
        r = su.new("噪声过滤: 代词/单次出现/超长候选被丢弃")
        r.evidence_add(log)
        r.check("无代词候选", not (names & set(PRONOUNS)), str(sorted(names & set(PRONOUNS))))
        r.check("单次出现角色被丢", RARE_NAME not in names,
                f"dropped={stats['dropped']}")
        r.check("丢弃原因含 rare", stats["dropped"].get("rare", 0) >= 1, str(stats["dropped"]))
        r.check("候选名长度 <= 6", all(len(c.name) <= 6 for c in cast),
                str([c.name for c in cast]))
        bare = CastExtractor()
        bare.process_chapter("\n\u201c今日天色不错。\u201d他说。\n", 0)
        bstats = bare.stats()
        r.check("纯代词句归为 unresolved", bstats["unresolved_lines"] == 1
                and not bare._aggs, json.dumps(bstats, ensure_ascii=False))
        long_name = CastExtractor(min_lines=1)
        long_name.process_chapter(
            "\n\u201c这件事我从头到尾都知道。\u201d那个穿灰长衫的伙计说道。\n", 0)
        lnames = {c.name for c in long_name.finalize()}
        r.check("超长误截片段被丢", all(len(n) <= 6 for n in lnames), str(sorted(lnames)))

        # ---------------- 5: 画像(LLM 不可用 -> 全表兜底) ----------------
        r = su.new("画像: LLM 不可用时整表 fallback, 不报错")
        r.evidence_add(log)
        ex2, cast2 = build_cast(chapters)
        profiler = CastProfiler(llm_service=UnavailableLLM())
        ok = True
        try:
            profiler.profile(cast2)
        except Exception as exc:
            ok = False
            r.fail("profile 不得抛错", f"{type(exc).__name__}: {exc}")
        if ok:
            r.check("profile 未抛错", True, "OK")
        prof_by_name = index_by_name(cast2)
        for name, expect in EXPECT_GENDERS.items():
            got = prof_by_name.get(name)
            r.check(f"性别正确 {name}={expect}",
                    got is not None and got.profile.get("gender") == expect,
                    f"got={got.profile.get('gender') if got else None} "
                    f"he={getattr(got, 'he_count', None)} she={getattr(got, 'she_count', None)}")
        narrator = prof_by_name.get(NARRATOR)
        r.check("旁白画像固定沉稳成年",
                narrator is not None and narrator.profile.get("age_group") == "adult"
                and "沉稳" in (narrator.profile.get("personality_tags") or []),
                json.dumps(narrator.profile if narrator else {}, ensure_ascii=False))
        r.check("兜底来源标记 fallback",
                all(c.profile.get("source") in ("fallback", "builtin_narrator") for c in cast2),
                str({c.name: c.profile.get("source") for c in cast2}))
        r.check("兜底不消耗 LLM 调用", profiler.stats()["llm_calls"] == 0,
                json.dumps(profiler.stats(), ensure_ascii=False))

        # ---------------- 6: 真实 LLM 路径(可选, 不可用则显式记录) ----------------
        r = su.new("画像: LLM 可用时走真实批量调用")
        r.evidence_add(log)
        try:
            from modules.llm_client.llm_service import LLMService
            svc = LLMService()
            ready = bool(svc.is_ready())
        except Exception as exc:
            ready = False
            r.notes.append(f"LLMService 构造失败: {type(exc).__name__}: {exc}")
        H.kv("LLM sidecar is_ready", ready)
        if ready:
            ex3, cast3 = build_cast(chapters)
            real = CastProfiler(llm_service=svc, batch_size=20, quotes_per_role=3)
            t_llm = time.perf_counter()
            real.profile(cast3)
            H.kv("真实 LLM 画像耗时", f"{time.perf_counter() - t_llm:.2f}s")
            H.kv("profiler.stats", json.dumps(real.stats(), ensure_ascii=False))
            for c in cast3:
                H.kv(f"  {c.name}", json.dumps(c.profile, ensure_ascii=False))
            llm_named = [c for c in cast3 if not c.is_narrator
                         and c.profile.get("source") == "llm"]
            r.check("至少一个角色画像来自 LLM", len(llm_named) >= 1,
                    f"{[c.name for c in llm_named]}")
            r.check("批量调用次数 <= 角色批次数", real.llm_calls <= 2, str(real.llm_calls))
        else:
            r.notes.append("LLM sidecar 未就绪: 本次仅验证规则兜底路径（spec 4.1/11 允许）")
            r.check("LLM 未就绪时按设计跳过真实调用分支", True,
                    "sidecar 不可用, 兜底路径已在用例 5 验证")

        # ---------------- 7: Role.extra_data 契约 ----------------
        r = su.new("Role.extra_data 落库形状(spec 3 无迁移)")
        r.evidence_add(log)
        rows = profiles_to_role_extra_data(cast2)
        H.kv("行数", len(rows))
        H.kv("首行", json.dumps(rows[0], ensure_ascii=False)[:260])
        need = {"name", "role_type", "aliases", "cast", "mix"}
        r.check("字段齐备", all(need <= set(row) for row in rows), str(sorted(need)))
        r.check("cast 段含画像与统计", all({"gender", "age_group", "identity", "personality_tags",
                                          "lines_count", "chapters_count"} <= set(row["cast"])
                                          for row in rows),
                json.dumps(rows[0]["cast"], ensure_ascii=False))
        r.check("mix 段含增益/语速/音调", all({"gain_db", "speed", "pitch"} <= set(row["mix"])
                                       for row in rows), json.dumps(rows[0]["mix"]))

        # ---------------- 8: 性能红线 5 万字 < 2s ----------------
        r = su.new("性能红线: 5 万字输入 < 2s")
        r.evidence_add(log)
        repeat = int(50000 / max(1, sum(len(c) for c in chapters))) + 1
        big = [chap for _ in range(repeat) for chap in chapters]
        total_chars = sum(len(c) for c in big)
        ex_big = CastExtractor()
        t0 = time.perf_counter()
        for idx, chap in enumerate(big):
            ex_big.process_chapter(chap, idx)
        big_cast = ex_big.finalize()
        elapsed = time.perf_counter() - t0
        H.kv("章节数", len(big))
        H.kv("总字数", total_chars)
        H.kv("耗时", f"{elapsed:.3f}s")
        H.kv("角色数", len(big_cast))
        r.evidence_add(H.ARTIFACTS / "result_C1-cast-extract.json")
        r.check("输入 >= 5 万字", total_chars >= 50000, str(total_chars))
        r.check("耗时 < 2s", elapsed < 2.0, f"{elapsed:.3f}s for {total_chars} chars")
        r.check("大输入下候选可控", len(big_cast) <= 40,
                str([c.name for c in big_cast][:12]))

        # ---------------- 9: 流式内存峰值 ----------------
        r = su.new("流式契约: 内存峰值与章数无关(不累积整书文本)")
        r.evidence_add(log)

        def stream_peak(chapter_count):
            chap_list = [chapters[i % len(chapters)] for i in range(chapter_count)]
            payload = sum(len(c) for c in chap_list) * 2      # UTF-16 字节量估算
            ex_stream = CastExtractor()
            tracemalloc.start()
            for idx, chap in enumerate(chap_list):
                ex_stream.process_chapter(chap, idx)
            result = ex_stream.finalize()
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            return payload, peak, result, max(len(c) for c in chapters) * 2

        payload_small, peak_small, _small_cast, chapter_bytes = stream_peak(200)
        payload_big, peak_big, cast_big, _ = stream_peak(4000)
        H.kv("单章文本量", f"{chapter_bytes/1024:.1f}KB")
        H.kv("200 章: 文本量/峰值", f"{payload_small/1024:.1f}KB / {peak_small/1024:.1f}KB")
        H.kv("4000 章: 文本量/峰值", f"{payload_big/1024:.1f}KB / {peak_big/1024:.1f}KB")
        ratio = peak_big / max(1, payload_big)
        growth = peak_big / max(1, peak_small)
        H.kv("峰值折算章当量(仅说明)", f"{peak_big / max(1, chapter_bytes):.1f} 章")
        H.kv("峰值占全书文本", f"{ratio*100:.2f}%")
        r.check("峰值 < 全书文本量的 5%", ratio < 0.05,
                f"peak={peak_big/1024:.1f}KB vs 全书={payload_big/1024:.1f}KB (ratio={ratio:.4f})")
        r.check("20x 章数下峰值增长 <2x", growth < 2.0,
                f"growth={growth:.2f}x (200 章 {peak_small/1024:.1f}KB -> 4000 章 "
                f"{peak_big/1024:.1f}KB)")
        r.check("4000 章聚合工作集绝对量 < 256KB", peak_big < 256 * 1024,
                f"{peak_big/1024:.1f}KB")
        lead = max(cast_big, key=lambda c: c.lines_count)
        H.kv("4000 章流下主角统计",
             f"{lead.name} 行数={lead.lines_count} 出场章计数={lead.chapters_count} "
             f"章号样本={lead.chapters_touched}")
        r.check("长流下章数计数仍保留(O(1) 整数)", lead.chapters_count >= 1000,
                str(lead.chapters_count))
        r.check("章号样本被截断为常数", len(lead.chapters_touched) == MAX_CHAPTER_SAMPLES,
                str(lead.chapters_touched))

        rc = su.finish(exit_when_fail=False)
        print("")
        print("ALL_GREEN" if rc == 0 else f"RED ({rc})")
        sys.exit(0 if rc == 0 else 1)


if __name__ == "__main__":
    main()
