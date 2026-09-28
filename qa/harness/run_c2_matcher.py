"""
[C2] 音色匹配实测: modules/cast_analyzer/voice_matcher.py（纯函数离线）
运行: set PYTHONIOENCODING=utf-8 && python qa/harness/run_c2_matcher.py

真实路径: 生产函数 score_voice / rank_voices / suggest / auto_assign 直调；
         角色输入用 c1 同款 fixture 的真实抽取+兜底画像结果，
         音色输入用与 VoiceProfile 同字段名的 dict 与对象两种形态（生产里是 ORM 行）。
判定: exit 0 = ALL_GREEN；结果 JSON 落 qa/round-1/artifacts/result_C2-matcher.json
"""
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402

FIXTURE = H.FIXTURES / "sample_cast_novel.txt"
CHAPTER_RE = re.compile(r"(?m)^(?=第[一二三四五六七八九十百千0-9]+章)")

# 与 backend/models/voice.py 的字段一一对应（gender/age_group/style_tags/language/source/engine）
VOICES = [
    {"id": "v_male_deep", "name": "男-沉稳磁性", "source": "builtin", "engine": "voxcpm",
     "language": "zh", "gender": "male", "age_group": "adult",
     "style_tags": ["沉稳", "磁性", "浑厚"]},
    {"id": "v_male_old", "name": "男-苍老旁白", "source": "builtin", "engine": "kokoro",
     "language": "zh", "gender": "male", "age_group": "senior",
     "style_tags": ["沉稳", "庄重"]},
    {"id": "v_female_soft", "name": "女-温柔青年", "source": "cloned", "engine": "indextts",
     "language": "zh", "gender": "female", "age_group": "young",
     "style_tags": ["温柔", "活泼"]},
    {"id": "v_female_girl", "name": "女-活泼少女", "source": "builtin", "engine": "voxcpm",
     "language": "zh", "gender": "female", "age_group": "child",
     "style_tags": ["活泼", "清脆"]},
    {"id": "v_female_elder", "name": "女-年长", "source": "builtin", "engine": "kokoro",
     "language": "zh", "gender": "female", "age_group": "senior",
     "style_tags": ["庄重"]},
    {"id": "v_en_male", "name": "EN-Adult Male", "source": "downloaded", "engine": "kokoro",
     "language": "en", "gender": "male", "age_group": "adult",
     "style_tags": ["沉稳"]},
    {"id": "v_unknown", "name": "未标注音色", "source": "designed", "engine": "mosstts",
     "language": None, "gender": None, "age_group": None, "style_tags": []},
]


class UnavailableLLM:
    @staticmethod
    def is_ready():
        return False


def real_cast_from_fixture():
    """走真实链路: fixture -> CastExtractor -> CastProfiler(兜底) -> 角色画像。"""
    from modules.cast_analyzer.cast_extractor import CastExtractor
    from modules.cast_analyzer.cast_profiler import CastProfiler
    raw = FIXTURE.read_text(encoding="utf-8")
    chapters = [p.strip() for p in CHAPTER_RE.split(raw) if p and p.strip()]
    ex = CastExtractor()
    for idx, chap in enumerate(chapters):
        ex.process_chapter(chap, idx)
    cast = ex.finalize()
    CastProfiler(llm_service=UnavailableLLM()).profile(cast)
    return cast


def role(name, gender="unknown", age="unknown", tags=None, narrator=False, lines=3):
    from modules.cast_analyzer.cast_extractor import CastCandidate
    return CastCandidate(
        name=name, is_narrator=narrator, lines_count=lines,
        profile={"gender": gender, "age_group": age,
                 "personality_tags": list(tags or []), "source": "fallback"})


def main():
    log = H.LOGS / "c2_matcher.log"
    with H.Tee(log):
        su = H.Suite("C2-matcher", "run_c2_matcher.py")

        from modules.cast_analyzer import voice_matcher as VM

        # ---------------- 1: 权重口径 ----------------
        r = su.new("打分口径: 性别40 + 年龄段30 + 风格20 + 语言10")
        r.evidence_add(log)
        H.kv("WEIGHTS", f"gender={VM.GENDER_WEIGHT} age={VM.AGE_WEIGHT} "
                        f"style={VM.STYLE_WEIGHT} language={VM.LANGUAGE_WEIGHT}")
        r.check("权重常量与 spec 一致",
                (VM.GENDER_WEIGHT, VM.AGE_WEIGHT, VM.STYLE_WEIGHT, VM.LANGUAGE_WEIGHT)
                == (40, 30, 20, 10), "40/30/20/10")
        perfect = role("周建国", "male", "adult", ["沉稳", "磁性"])
        got = VM.score_voice(perfect, VOICES[0])
        H.kv("满分项", json.dumps(got, ensure_ascii=False))
        r.check("全命中 = 100", got["score"] == 100, str(got["breakdown"]))
        r.check("分项拆分正确", got["breakdown"] == {"gender": 40, "age": 30,
                                              "style": 20, "language": 10},
                str(got["breakdown"]))
        gender_only = VM.score_voice(role("甲", "male", "child", ["冷淡"]), VOICES[0])
        H.kv("性别对/年龄错/风格错", json.dumps(gender_only["breakdown"], ensure_ascii=False))
        r.check("性别对但年龄风格错 -> 只得分性别+语言",
                gender_only["breakdown"]["gender"] == 40
                and gender_only["breakdown"]["age"] == 0
                and gender_only["breakdown"]["style"] == 0,
                str(gender_only["breakdown"]))
        mismatch = VM.score_voice(role("乙", "female", "adult", ["温柔"]), VOICES[0])
        r.check("性别冲突项 = 0 分", mismatch["breakdown"]["gender"] == 0,
                str(mismatch["breakdown"]))
        lang = VM.score_voice(role("丙", "male", "adult", []), VOICES[5])
        r.check("语言不符 -> 语言分 0", lang["breakdown"]["language"] == 0,
                str(lang["breakdown"]))
        adjacent = VM.age_score("young", "adult")
        r.check("相邻年龄段给部分分", adjacent == VM.AGE_PARTIAL, str(adjacent))
        unknown = VM.score_voice(role("丁"), VOICES[6])
        r.check("未标注音色得保守分", 0 < unknown["score"] < 100,
                json.dumps(unknown["breakdown"], ensure_ascii=False))

        # ---------------- 2: 排序正确性 ----------------
        r = su.new("rank_voices 排序: 分值降序 + 稳定序")
        r.evidence_add(log)
        ranked = VM.rank_voices(role("林晚晴", "female", "young", ["温柔", "活泼"]), VOICES)
        for item in ranked:
            H.kv(f"  {item['voice_id']}", f"score={item['score']} "
                                          f"breakdown={item['breakdown']}")
        scores = [i["score"] for i in ranked]
        r.check("按分数降序", scores == sorted(scores, reverse=True), str(scores))
        r.check("女声青年排首位", ranked[0]["voice_id"] == "v_female_soft",
                f"{ranked[0]['voice_id']} score={ranked[0]['score']}")
        top3 = VM.rank_voices(role("小满", "female", "child", ["活泼"]), VOICES, top_n=3)
        r.check("top_n 截断生效", len(top3) == 3, str([i["voice_id"] for i in top3]))
        tie_a = VM.rank_voices(role("戊", "male", "adult", ["沉稳"]), VOICES)
        tie_b = VM.rank_voices(role("戊", "male", "adult", ["沉稳"]), VOICES)
        r.check("同输入结果可复现", [i["voice_id"] for i in tie_a] == [i["voice_id"] for i in tie_b],
                str([i["voice_id"] for i in tie_a][:4]))

        # ---------------- 3: 旁白偏好 ----------------
        r = su.new("旁白: 沉稳/磁性与 adult 音优先(spec 4.1)")
        r.evidence_add(log)
        narrator = role("旁白", "neutral", "adult", ["沉稳", "磁性", "浑厚", "庄重"],
                        narrator=True, lines=99)
        nr = VM.rank_voices(narrator, VOICES)
        for item in nr[:4]:
            H.kv(f"  {item['voice_id']}", f"score={item['score']}")
        r.check("旁白首选为沉稳磁性男声", nr[0]["voice_id"] == "v_male_deep",
                f"{nr[0]['voice_id']} score={nr[0]['score']}")
        r.check("旁白不选儿童音色", "v_female_girl" not in [i["voice_id"] for i in nr[:2]],
                str([i["voice_id"] for i in nr[:2]]))

        # ---------------- 4: dict 与 ORM 对象同形 ----------------
        r = su.new("音色入参兼容 dict 与 VoiceProfile 对象")
        r.evidence_add(log)
        obj = SimpleNamespace(id="v_obj", name="对象音色", source="builtin", engine="voxcpm",
                              language="zh", gender="female", age_group="adult",
                              style_tags=["温柔", "沉稳"])
        as_dict = {"id": "v_obj", "name": "对象音色", "source": "builtin", "engine": "voxcpm",
                   "language": "zh", "gender": "female", "age_group": "adult",
                   "style_tags": ["温柔", "沉稳"]}
        s_obj = VM.score_voice(role("己", "female", "adult", ["温柔"]), obj)
        s_dict = VM.score_voice(role("己", "female", "adult", ["温柔"]), as_dict)
        H.kv("对象打分", json.dumps(s_obj["breakdown"], ensure_ascii=False))
        r.check("对象与 dict 打分一致", s_obj["score"] == s_dict["score"]
                and s_obj["breakdown"] == s_dict["breakdown"], f"{s_obj['score']}")
        r.check("voice_id 取自对象", s_obj["voice_id"] == "v_obj", str(s_obj["voice_id"]))

        # ---------------- 5: suggest / auto_assign ----------------
        r = su.new("suggest 与 auto_assign 输出契约")
        r.evidence_add(log)
        cast = real_cast_from_fixture()
        H.kv("真实角色画像", [(c.name, c.profile.get("gender"), c.profile.get("age_group"))
                             for c in cast])
        t0 = time.perf_counter()
        sug = VM.suggest(cast, VOICES, top_n=5)
        elapsed = time.perf_counter() - t0
        for name, items in sug.items():
            H.kv(f"  {name}", f"Top5={[(i['voice_id'], i['score']) for i in items]}")
        r.check("每个角色都有推荐", set(sug) == {c.name for c in cast},
                f"{sorted(sug)}")
        r.check("Top-N 长度 <=5", all(len(v) <= 5 for v in sug.values()),
                str({k: len(v) for k, v in sug.items()}))
        r.check("真实角色性别不被异性音色占首",
                sug["林晚晴"][0]["voice_id"] in {"v_female_soft", "v_female_girl", "v_female_elder"}
                and sug["周建国"][0]["voice_id"] in {"v_male_deep", "v_male_old"},
                f"林晚晴={sug['林晚晴'][0]['voice_id']} 周建国={sug['周建国'][0]['voice_id']}")
        assign = VM.auto_assign(cast, VOICES, min_score=40)
        H.kv("auto_assign", json.dumps(assign, ensure_ascii=False))
        r.check("低分不硬塞(None)", all(v in {x["id"] for x in VOICES} | {None}
                                for v in assign.values()), str(assign))
        assign_strict = VM.auto_assign(cast, [VOICES[5]], min_score=90)
        r.check("不合格音色集合 -> 全 None", all(v is None for v in assign_strict.values()),
                str(assign_strict))
        r.check("空输入不崩", VM.suggest([], VOICES) == {} and VM.suggest(cast, []) == {
            c.name: [] for c in cast}, "OK")

        # ---------------- 6: 规模与耗时 ----------------
        r = su.new("规模打分耗时(1000 音色 x 200 角色)")
        r.evidence_add(log)
        many_voices = [dict(VOICES[i % len(VOICES)], id=f"v{i}") for i in range(1000)]
        many_roles = [role(f"角色{i}", ["male", "female"][i % 2],
                           ["child", "young", "adult", "senior"][i % 4], ["沉稳", "温柔"])
                      for i in range(200)]
        t0 = time.perf_counter()
        big = VM.suggest(many_roles, many_voices, top_n=5)
        elapsed = time.perf_counter() - t0
        H.kv("耗时", f"{elapsed:.3f}s")
        H.kv("输出角色数", len(big))
        r.check("20 万次打分 < 5s", elapsed < 5.0, f"{elapsed:.3f}s")
        r.check("每角色返回 Top5", all(len(v) == 5 for v in big.values()),
                str({k: len(v) for k, v in list(big.items())[:3]}))

        rc = su.finish(exit_when_fail=False)
        print("")
        print("ALL_GREEN" if rc == 0 else f"RED ({rc})")
        sys.exit(0 if rc == 0 else 1)


if __name__ == "__main__":
    main()
