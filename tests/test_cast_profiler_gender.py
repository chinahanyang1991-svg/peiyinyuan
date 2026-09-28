"""PY-334: 兜底画像不得把"角色自己台词里的他/她"当成这个角色自己的性别证据。

取证（agent-ops/cast-reset/gender_evidence_probe.py，09-23 04:0x 只读直采活库）：
    说话角色 61  落库 gender 分布 {'male': 58, 'female': 2, 'neutral': 1}
    玛莉  lines=851 他词={'他': 4} 她词={'她': 1}  -> male
    莎拉雅 lines=487 他词={'他': 6, '叔叔': 1} 她词={} -> male
    席娜  lines=117 他词={'他': 3} 她词={}          -> male
`_fallback_profile` 把 `sample_quotes`（角色**自己说的**句子）与名字/别名拼成一段文本
再数 MALE_WORDS/FEMALE_WORDS，而这两个词表**含裸代词"他""她"** —— 于是"她在谈他"
被判成"她是男的"。台词里的代词指的是谈话对象，不是说话人。

后果直接压在红线上：性别是音色打分最重的一项（40/100），女性角色判成 male 后
自动分配把她们绑到男声（`voice_matcher` 同口径）。

年龄/身份/性格**仍读台词**（那些确实是角色自己的属性），本文件第 5 例专门钉住这条边界。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.cast_analyzer.cast_extractor import CastExtractor          # noqa: E402
from modules.cast_analyzer.cast_profiler import (                       # noqa: E402
    CastProfiler, FEMALE_WORDS, MALE_WORDS)


class _NoLLM:
    """把画像器钉在兜底档：实盘 61 枚角色全部 profile_source=fallback 就是这个形状。"""

    def is_ready(self):
        return False


def fallback_one(**kw):
    view = {"name": "", "aliases": [], "quotes": [], "titles": [], "lines_count": 10,
            "he_count": 0, "she_count": 0, "it_count": 0, "is_narrator": False}
    view.update(kw)
    return CastProfiler(llm_service=_NoLLM())._fallback_profile(view)


MALE_HEAVY_QUOTES = ["把他关起来，谁也不许放出去。", "他手上那条疤，是我昨日亲眼看见的。",
                     "他知道的事，从来不肯多说一句。"] * 3


# ---------------------------------------------------------------- 边界本身
def test_own_dialogue_pronouns_are_not_gender_evidence():
    """台词里 他 出现 9 次、叙述零指代 -> 只能给 unknown，不许冒充当性别证据。"""
    got = fallback_one(name="玛莉", aliases=["玛莉·圣雅各"], quotes=MALE_HEAVY_QUOTES)
    assert got["gender"] == "unknown", got


def test_narration_pronoun_majority_still_decides():
    """真正的证据（抽取层按名字窗口算好的 她>他）照样判 female，即使台词满是"他"。"""
    got = fallback_one(name="沈若兰", quotes=MALE_HEAVY_QUOTES, he_count=1, she_count=4)
    assert got["gender"] == "female", got


def test_gendered_nouns_in_dialogue_are_not_evidence():
    """裸代词之外的另一半：台词里的**称谓名词**同样是指别人，不是说话人自己。

    实盘取证 莎拉雅 他词={'他': 6, '叔叔': 1} -> male，那个"叔叔"是她嘴里说的
    某个人。词表只允许在 名字/别名/称谓(titles) 上计数，砍掉 quotes。
    """
    got = fallback_one(name="莎拉雅", quotes=["我叔叔交代过，这事不必声张。"] * 4)
    assert got["gender"] == "unknown", got
    # 同一个词出现在别名里才是她自己的属性
    assert fallback_one(name="莎拉雅", aliases=["叔叔"])["gender"] == "male"


def test_name_and_alias_nouns_are_still_evidence():
    """名字/别名里的身份名词是自己的属性，仍算证据。"""
    assert fallback_one(name="王队长")["gender"] == "male"
    assert fallback_one(name="李婶子")["gender"] == "female"
    assert fallback_one(name="阿蓉", aliases=["少奶奶"])["gender"] == "female"


def test_word_lists_carry_no_bare_pronouns_and_no_duplicates():
    """裸代词必须留在抽取层（那里有指代窗口），词表里只放名词性称谓。

    旧词表里 叔叔/小姐 各写了两遍，`sum(text.count(w))` 因此把它们**双计**，
    在"他/她"之外又给男性称谓加了一次权重。
    """
    assert "他" not in MALE_WORDS and "她" not in FEMALE_WORDS, (MALE_WORDS, FEMALE_WORDS)
    assert len(set(MALE_WORDS)) == len(MALE_WORDS), MALE_WORDS
    assert len(set(FEMALE_WORDS)) == len(FEMALE_WORDS), FEMALE_WORDS


def test_age_and_personality_still_read_the_lines():
    """边界另一侧：年龄/性格是角色自己的属性，台词证据不许一起砍掉。"""
    got = fallback_one(name="小豆", quotes=["我还是个孩子，什么都不懂。", "我沉稳地坐下。"],
                       lines_count=3)
    assert got["age_group"] == "child", got
    assert "沉稳" in got["personality_tags"], got


# ---------------------------------------------------------------- 真路径
# 修前实测（本文件写完先跑过一遍）：苏晚 -> gender='male'，he_count=0/she_count=2。
# 三条台词各提一次"他"（共 3）压过了叙述里的两次"她"，于是她在谈的人取代了她自己。
GENDER_CHAPTERS = [
    (0, "“把他关起来，谁也不许放出去。”苏晚说，她转过身。\n"
        "“他手上的疤，是我昨日亲眼看见的。”苏晚说，她压低声音。\n"),
    (1, "“他一定会再来。”苏晚说，她抬手抹了把脸。\n"
        "“他不肯说实话，那就别怪我不客气。”周绍棠说，他盯着院门。\n"
        "“你先退下。”周绍棠说，他摆摆手。\n"),
]


def test_full_extraction_path_does_not_misgender_the_speaker():
    """CastExtractor -> CastProfiler 整条生产链路：她在谈"他"，画像必须是 female。

    只喂单测形状不算数——代词计数的正确窗口在抽取层，本例证明"去掉台词证据"
    之后仍能靠叙述窗口拿到正确性别（不是靠变 unknown 蒙对）。
    """
    ex = CastExtractor()
    for idx, text in GENDER_CHAPTERS:
        ex.process_chapter(text, idx)
    cast = ex.finalize()
    by_name = {c.name: c for c in cast}
    assert {"苏晚", "周绍棠"} <= set(by_name), sorted(by_name)
    assert by_name["苏晚"].she_count >= 1 and by_name["苏晚"].he_count == 0, by_name["苏晚"]
    profiles = CastProfiler(llm_service=_NoLLM()).profile(cast)
    got = {c.name: c.profile["gender"] for c in profiles}
    assert got["苏晚"] == "female", got
    assert got["周绍棠"] == "male", got


if __name__ == "__main__":                                              # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
