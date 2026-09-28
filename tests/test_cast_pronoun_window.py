"""PY-336: `_count_pronouns` 的代词窗口把**别人的**他/她记到了这个名字上。

取证（agent-ops/cast-reset/gender_window_probe.py，09-23 04:28 只读直采《谍影重重》
155 万字 / 173 章 / 60 枚说话角色，逐角色把"实际被数进去的窗口原文"打出来核）：

    玛莉   he=400 she=316 -> male（女主被判男声）
    席娜   he=92  she=75  -> male
    帕诺夫 he=161 she=35  -> male   ← 记录数只有 97/14：同一段窗口被后续对白重复覆盖
    霍兰   he=70  she=0
    被数进去的窗口原文（玛莉，前 15 段里就有多少个"他"指的是伯恩）：
        「把护照袋拿给他，」
        「也没睡觉，因此吓了一跳。她正看着他，或者应该说，凝视着他。」
        「看着他的手问。」
        「手上的杯子突然掉了下去，她瞪大眼睛看着他，脸色惨白，仿佛头部的血瞬间流干了。」
        「说：“他大概以为你是从哪个乡下来的教会执事，…」

旧口径 = 名字最后一次出现之后、引号之前那一段里**所有** 他/她/它，三条系统性偏置：
  1) 宾语位代词照计（"拿给他""看着他"= 谈的是别人）；
  2) 角色**自己的台词**里的代词照计（引号没被屏蔽，PY-334 的同一条口子）；
  3) 同一个人连续多句对白会重复覆盖同一段叙述窗口，窗口里的代词按句数**平方级**膨胀。
于是全书跟男主角对手戏最多的女性角色，男声代词计数必然压过她自己。

修法是"只数小句主语的代词 + 屏蔽引号内文本 + 每个代词位置只计一次"，
不是把窗口调大或调小 —— 窗口大小调不出性别，只会调出新的错挂。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.cast_analyzer.cast_extractor import CastExtractor, _Agg   # noqa: E402
from modules.cast_analyzer.cast_profiler import CastProfiler           # noqa: E402


class _NoLLM:
    def is_ready(self):
        return False


def counted(text, name="玛莉"):
    """直接喂 _count_pronouns：返回 (he, she, it)。窗口 = 引号起始位置之前。"""
    ex = CastExtractor()
    agg = _Agg(name)
    ex._count_pronouns(agg, text, {"start": len(text)}, name)
    return agg.he, agg.she, agg.it


# ---------------------------------------------------------------- 偏置 1: 宾语位
def test_object_pronoun_after_a_verb_is_not_subject_gender():
    """「把护照袋拿给他」里的 他 是收东西的人，不是玛莉。"""
    assert counted("玛莉把护照袋拿给他，") == (0, 0, 0)
    assert counted("玛莉抬起眼看着他，或者说，凝视着他。") == (0, 0, 0)


def test_clause_subject_pronoun_still_counts():
    """「她转过身」这种小句主语的代词仍是本人证据（这正是本函数该数的东西）。"""
    assert counted("玛莉说，她转过身。") == (0, 1, 0)
    assert counted("玛莉皱了皱眉。他推门进来。") == (1, 0, 0)


def test_plural_and_demonstrative_pronouns_are_not_single_person_evidence():
    assert counted("玛莉说，他们先走。") == (0, 0, 0)
    assert counted("玛莉说，她俩一起出门。") == (0, 0, 0)
    assert counted("玛莉说，其他人都散了。") == (0, 0, 0)


# ---------------------------------------------------------------- 偏置 2: 台词内
def test_pronouns_inside_the_characters_own_lines_are_masked():
    """台词里的"他"是她谈的对象（PY-334 已经砍掉画像层这条路，抽取层不能再放进来）。"""
    assert counted("玛莉说：“他一定还会再来，把他关起来。”") == (0, 0, 0)
    # 引号外的叙述主语代词照常计
    assert counted("玛莉说：“他还会来。”她按下通话键。") == (0, 1, 0)


# ---------------------------------------------------------------- 偏置 3: 重复覆盖
def test_shared_window_is_not_counted_once_per_line():
    """同一人连着几句无署名对白共用一段叙述窗口 -> 窗口里的代词只计一次。

    修前探针里 玛莉 的窗口原文就出现过这种套娃（同一段前缀被逐句重复计入）：
        「看着他的手问。⏎」
        「看着他的手问。⏎“好多了。手指已经灵活多了。”⏎」
        「看着他的手问。⏎“好多了。手指已经灵活多了。”⏎“我知道。”⏎」
    """
    ex = CastExtractor()
    agg = _Agg("玛莉")
    text = ("玛莉站在窗前。她等了很久，他始终没有回头。\n"
            "“第一句。”\n"
            "“第二句。”\n"
            "“第三句。”\n")
    starts = [i for i, ch in enumerate(text) if ch == "“"]
    assert len(starts) == 3, starts
    for pos in starts:
        ex._count_pronouns(agg, text, {"start": pos}, "玛莉")
    assert (agg.he, agg.she) == (1, 1), (agg.he, agg.she)


def test_floor_resets_between_chapters():
    """跨章时段的 offset 会重来，上一章的"已计位置"不许把新章全部挡掉。"""
    ex = CastExtractor()
    agg = _Agg("玛莉")
    chapter = "玛莉说，她转过身。“你走吧。”\n"
    for idx in range(3):
        ex.process_chapter(chapter, idx)
    got = {c.name: (c.he_count, c.she_count) for c in ex.finalize()}
    assert got.get("玛莉") == (0, 3), got


# ---------------------------------------------------------------- 真路径
MARIE_CHAPTERS = [
    (0, "“把手给我。”玛莉说，她看着他，或者说，凝视着他。\n"
        "“你受伤了。”玛莉说，她把药箱推到他面前。\n"
        "“别动。”玛莉说，她按住他的肩膀。\n"),
    (1, "“档案在哪？”伯恩说，他皱着眉。\n"
        "“在这里。”玛莉说，她翻开抽屉，把护照袋拿给他。\n"
        "“先看看伤口。”伯恩说，他卷起袖子。\n"
        "“外面有人在等。”伯恩说，他把枪塞回腰后。\n"),
]


def test_female_lead_comes_out_female_on_the_real_path():
    """CastExtractor -> CastProfiler 整链路：女主 玛莉 必须是 female，男主 伯恩 仍 male。

    期望值取自并排实测（`agent-ops/cast-reset/ce_old_2f56e6c.py` = 修前抽取器，与本仓
    HEAD 同段夹具各跑一遍）：
        修前  玛莉 he=3 / she=2 -> male（"看着他""推到他面前""拿给他"三个宾语位的他
              全记进她的账），伯恩 he=3 / she=1 -> male
        修后  玛莉 he=0 / she=2 -> female，伯恩 he=2 / she=1 -> male
    """
    ex = CastExtractor()
    for idx, text in MARIE_CHAPTERS:
        ex.process_chapter(text, idx)
    cast = ex.finalize()
    by_name = {c.name: c for c in cast}
    assert {"玛莉", "伯恩"} <= set(by_name), sorted(by_name)
    assert (by_name["玛莉"].he_count, by_name["玛莉"].she_count) == (0, 2), by_name["玛莉"]
    assert (by_name["伯恩"].he_count, by_name["伯恩"].she_count) == (2, 1), by_name["伯恩"]
    profiles = CastProfiler(llm_service=_NoLLM()).profile(cast)
    got = {c.name: c.profile["gender"] for c in profiles}
    assert got["玛莉"] == "female", got
    assert got["伯恩"] == "male", got


def test_genderless_narration_stays_unknown_instead_of_guessing():
    """没有主语位代词、也没有称谓 -> unknown（宁可空，不再从宾语位硬凑一个性别）。"""
    ex = CastExtractor()
    ex.process_chapter("“明天再见。”陈默说。\n“好。”陈默说。\n", 0)
    cast = ex.finalize()
    by_name = {c.name: c for c in cast}
    assert by_name["陈默"].he_count == 0 and by_name["陈默"].she_count == 0
    profiles = CastProfiler(llm_service=_NoLLM()).profile(cast)
    assert {c.name: c.profile["gender"] for c in profiles}["陈默"] == "unknown"


if __name__ == "__main__":                                              # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
