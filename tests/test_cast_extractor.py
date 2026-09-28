"""
WF-01 回归测试：角色抽取（假角色/漏主角/未归属句）

覆盖两处修复（缺陷报告 WF-REPORT.md ② WF-01）：
1. modules/text_processor/dialogue_detector.py `_find_role_hint` 引号**后窗**提示
   必须落在说话动词邻域，且 "……的是XX，" 结构里 的 之前须有说话动词；
   `_role_from_head` 的 "了/着" 兜底不再把谓语字吃进候选名。
2. modules/cast_analyzer/cast_extractor.py 归属链：
   - 后窗提示的"说话动词邻域"复核 + 非叙述句式门限（方位词/叙述动词尾巴）
   - "名字主语 + 同句代词"性别指代档案 -> "她/他 + 说话线索" 回落最近提及
   - 否定式提示（"某某没接话"）不作归属证据

fixture 全部为**自造文本**，姓名与措辞刻意与 qa/harness/fixtures/sample_cast_novel.txt
和被测样书不同，用于证明修复是通用规则而非针对某个样本字符串打补丁。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.cast_analyzer.cast_extractor import (  # noqa: E402
    NARRATOR, CastExtractor, LOCATIVE_HEADS, NARRATION_TAILS, PRONOUNS,
    is_person_shaped)
from modules.text_processor.dialogue_detector import DialogueDetector  # noqa: E402


# ---------------------------------------------------------------- fixtures
# 章一：对白后接叙述残片（"里头传来拖长的问话"式），主角只被代词指代
CHAPTER_1 = (
    "雨点砸在石板上，溅起一层白雾。纪云舟收了伞，站在檐下，她抬手叩了三扇门环。\n"
    "“这么晚了，谁啊？”里头传来拖长的问话。\n"
    "纪云舟又敲了一下门环，她屏着气等。\n"
    "“我姓纪，找我爹留下的方子。”她的声音很轻，却一字一顿。\n"
    "门轴响了半声，灯光从门缝里淌出来。沈长庚站在门后，他手里还捏着半张纸。\n"
    "“进来再说。”沈长庚道，“方子在里屋。”\n"
)

# 章二：只给代词归属线索（他说/她笑着），并有 "X队长" 别称形态
CHAPTER_2 = (
    "第二天一早，后院堆着的竹筐被翻得乱七八糟。沈队长站在井台边，他皱着眉数了两遍。\n"
    "“少了三个筐。”他说，“昨天收药时还在的。”\n"
    "阿福端着热汤跨过门槛，她笑着把汤塞进纪云舟手里，“姐姐先喝口热的。”\n"
    "纪云舟接过瓦罐，她低头抿了一口，眼睛却盯着那扇虚掩的木门。\n"
    "“昨夜你听见动静了吗？”\n"
    "阿福的笑容僵了一下，“能有什么动静。”\n"
)

# 章三：另一种叙述残片措辞（院中响起…），证明不依赖 "里头/传来" 二字
CHAPTER_3 = (
    "院中响起杂乱的脚步声，天没亮透。纪云舟蹲下身，她捡起一片撕碎的纸角。\n"
    "“这字……”她把纸角举到眼前，指节泛白。\n"
    "沈长庚从里屋出来，他敲了敲井台，“别查了。”\n"
)

CHAPTERS = [(0, CHAPTER_1), (1, CHAPTER_2), (2, CHAPTER_3)]


def build_cast(chapters=CHAPTERS, **kwargs):
    ex = CastExtractor(**kwargs)
    for idx, text in chapters:
        ex.process_chapter(text, idx)
    return ex, ex.finalize()


def names(cast):
    return [c.name for c in cast]


# ------------------------------------------------------- detector: 后窗提示
def test_post_hint_rejects_narration_fragment():
    """引号后紧跟 "里头传来拖长的问话" 不再被截成角色提示。"""
    det = DialogueDetector()
    segs = [s for s in det.detect_dialogues("“这么晚了，谁啊？”里头传来拖长的问话。")
            if s["type"] == "dialogue"]
    assert segs, "对白本身要被检出"
    assert segs[0]["role_hint"] in (None, ""), f"实得 {segs[0]['role_hint']!r}"


def test_post_hint_rejects_possessive_narration():
    """“……”她的声音很轻 —— 无说话动词的 "的XX，" 结构不算提示。"""
    det = DialogueDetector()
    segs = [s for s in det.detect_dialogues("“我姓纪。”她的声音很轻，却一字一顿。")
            if s["type"] == "dialogue"]
    hint = segs[0]["role_hint"]
    assert hint in (None, ""), f"实得 {hint!r}"


def test_post_hint_keeps_speech_verb_form():
    """高置信的 "……"XX说/道" 后窗提示必须保留（PY-109 能力不回退）。"""
    det = DialogueDetector()
    text = ("“药铺今晚不接单。”沈长庚道，“你先回去。”\n"
            "“姐姐先喝口热的。”阿福说道。")
    hints = [s["role_hint"] for s in det.detect_dialogues(text) if s["type"] == "dialogue"]
    assert hints[0] == "沈长庚", str(hints)
    assert "阿福" in hints, str(hints)


def test_post_hint_keeps_alias_of_clause_subject():
    """叙述主语归因（前窗）沿用旧宽松口径："XX沉下脸，他从怀里…" 仍归 XX。"""
    det = DialogueDetector()
    text = "沈长庚沉下脸，他从怀里摸出一串钥匙，“今年也得开。”"
    hints = [s["role_hint"] for s in det.detect_dialogues(text) if s["type"] == "dialogue"]
    assert hints == ["沈长庚"], str(hints)


def test_clause_subject_not_swallow_predicate():
    """clause_subject 不再把谓语字吃进名字（林晚晴收 / 小满抱 式误截）。"""
    det = DialogueDetector()
    assert det.clause_subject("纪云舟收了伞") == "纪云舟"
    assert det.clause_subject("阿福抱着药篓") == "阿福"
    assert det.clause_subject("周主事的弟子来了") == "周主事"


# ------------------------------------------------------ extractor: 假角色
def test_narration_fragment_never_becomes_role():
    """里头传来/院中响起 这类叙述残片不得成为角色（WF-01 主诉）。"""
    _ex, cast = build_cast()
    got = names(cast)
    assert "里头传来" not in got and "院中响起" not in got, str(got)
    for role in got:
        if role == NARRATOR:
            continue
        assert not any(role.startswith(h) for h in LOCATIVE_HEADS), f"方位短语成角色: {role}"
        assert not any(role.endswith(t) for t in NARRATION_TAILS), f"叙述残片成角色: {role}"


def test_no_time_place_subject_roles():
    """时间/处所作小句主语时不得升格为角色（指代档案的人名形态门限）。"""
    _ex, cast = build_cast()
    got = names(cast)
    for junk in ("第二天", "后院堆", "雨点", "门轴响"):
        assert junk not in got, f"叙述主语被当成角色: {junk}"


def test_looks_like_name_gates_are_generic():
    """_looks_like_name 的叙述句式门限走通用词表，不是样本专用串。"""
    ex = CastExtractor()
    assert ex._looks_like_name("纪云舟") is True
    assert ex._looks_like_name("里头传来") is False      # 方位词开头
    assert ex._looks_like_name("院中响起") is False      # 方位词 + 叙述动词尾巴
    assert ex._looks_like_name("外头传来") is False
    assert ex._looks_like_name("飘来") is False          # 叙述动词尾巴本身
    assert ex._looks_like_name("声音很轻") is False      # 单字功能词
    assert ex._looks_like_name("沈长庚") is True


def test_each_veto_branch_has_an_exclusive_veto():
    """上面那些串**每一枚都被好几道门同时拦着**——PY-338 变异检验实测：把方位词头
    或叙述尾巴整条摘掉，88 枚测试一条都不红（`里头传来` 还被姓名形态门拦着）。
    所以这里补的是各道门的**独占否决**样本：摘掉那一道门，别的门一概拦不住。
    独占关系由 `qa/harness/probe_py338_gate_redundancy.py` 逐条门算出来（词表全集
    + 词表×姓氏/尾字组合 64,700 串），不是挑出来的巧台。

    """
    ex = CastExtractor()
    # 方位/处所词开头，但姓名形态门放行（`根`/`士` 落在职务通称尾字里）
    assert is_person_shaped("墙根") is True and ex._looks_like_name("墙根") is False
    assert is_person_shaped("堂中士") is True and ex._looks_like_name("堂中士") is False
    # 叙述趋向尾巴，姓氏开头 ⇒ 形态门放行，只剩尾巴门拦
    assert is_person_shaped("丁传来") is True and ex._looks_like_name("丁传来") is False
    # 虚词表成员，且不在代词表/方位头/尾巴表里 ⇒ 只剩 STOP_WORDS 拦
    for word in ("大声", "小声", "许多"):
        assert word not in PRONOUNS and word not in LOCATIVE_HEADS
        assert not any(word.endswith(t) for t in NARRATION_TAILS)
        assert ex._looks_like_name(word) is False



# --------------------------------------------------- extractor: 主角不漏
def test_pronoun_only_protagonist_is_extracted():
    """只有代词线索（她的声音/她说/她把…）的主角必须被抽出（WF-01 主诉）。"""
    _ex, cast = build_cast()
    got = names(cast)
    assert "纪云舟" in got, f"主角缺席: {got}"
    hero = next(c for c in cast if c.name == "纪云舟")
    assert hero.lines_count >= 2
    assert "我姓纪" in "".join(hero.sample_quotes)


def test_unresolved_lines_decrease():
    """未归属句数须显著下降（同一样本旧实现为 3 句被丢弃）。"""
    ex, _cast = build_cast()
    stats = ex.stats()
    assert stats["dialogue_lines"] >= 8, str(stats)
    assert stats["unresolved_lines"] <= 1, f"未归属句仍偏多: {stats}"
    assert stats["mention_resolved"] >= 2, f"代词归属未生效: {stats}"


def test_alias_form_merges_into_full_name():
    """只以 "沈队长" 形态被提及的行，归并到同姓全名 沈长庚。"""
    ex, cast = build_cast()
    got = names(cast)
    assert "沈队长" not in got, f"别称未归并: {got}"
    shen = next(c for c in cast if c.name == "沈长庚")
    assert "沈队长" in shen.aliases, str(shen.aliases)
    assert ex.stats()["raw_candidates"] >= 3, "别称应作为独立候选出现后再归并"


def test_negated_hint_is_not_attribution_evidence():
    """"纪云舟没接话" 式否定提示不得把该句算作她的引号归属证据。"""
    text = ("“我姓纪，找我爹留下的方子。”纪云舟没接话，她抬手又敲了一下。\n"
            "“谁啊？”外头有人应了一声。\n")
    ex = CastExtractor()
    ex.process_chapter(text, 0)
    cast = {c.name: c for c in ex.finalize()}
    if "纪云舟" in cast:
        assert cast["纪云舟"].lines_count == 0 or cast["纪云舟"].pronoun_resolved > 0


# ---------------------------------------------------------- 契约/回归护栏
def test_stats_has_mention_resolved_and_shape_stable():
    """stats() 新增 mention_resolved，其余键与旧契约保持一致。"""
    ex, _cast = build_cast()
    stats = ex.stats()
    for key in ("chapters", "chars", "raw_candidates", "dialogue_lines",
                "narration_lines", "unresolved_lines", "short_text_relaxed",
                "dropped", "mention_resolved"):
        assert key in stats, f"stats 缺字段 {key}: {sorted(stats)}"


def test_direct_hint_cast_still_wins_and_narrator_last():
    """直连归属（XX说 / 老周：）仍是首选信号，旁白固定在末尾。"""
    chapters = [(0, "老周：“你们谁都别想蒙我。”\n老周：“我今早亲眼看见的。”\n"),
                (1, "“这块纸我昨夜见过。”纪云舟道，“在窗台的灯影里。”\n")]
    ex, cast = build_cast(chapters)
    assert names(cast)[-1] == NARRATOR, str(names(cast))
    by_name = {c.name: c for c in cast}
    assert "纪云舟" in by_name and "老周" in by_name, str(names(cast))
    assert ex.stats()["unresolved_lines"] == 0, str(ex.stats())


def test_pure_narration_text_yields_only_narrator():
    """纯叙述（无引号对白）不凭空造角色。"""
    ex, cast = build_cast([(0, "天亮了，巷子里的水从石板缝往下渗，没有人说话。\n")])
    assert names(cast) == [NARRATOR], str(names(cast))
    assert ex.stats()["dialogue_lines"] == 0


def test_colon_prompt_roles_unaffected():
    """冒号称谓式（"阿贵：…"）与旧实现一致，不受 WF-01 收紧影响。"""
    chapters = [(0, "阿贵：“我没别的意思，就是躲雨。”\n"),
                (1, "阿贵：“明早我去报官。”\n")]
    _ex, cast = build_cast(chapters)
    assert "阿贵" in names(cast), str(names(cast))


if __name__ == "__main__":                                          # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
