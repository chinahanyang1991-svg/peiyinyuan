"""PY-335: 虚词字做**名字内部语素**时放行，做**叙述残片**时仍拦。

取证（agent-ops/cast-reset/stopchar_probe.py + stopchar_adversarial.py，09-23 05:17
只读直采《谍影重重》173 章，把 60 枚候选的判名路径逐条打出来）：
`is_person_shaped()` 逐字扫 `BANNED_NAME_CHARS` 时只在"首字且是姓氏"这一格豁免，
于是 沈若**兰** / 王**羲之** / 李**从**文 / 周**之**才 这类三字的正牌人名被当成
"他向着""之所以"的残片丢掉。汉字人名里 若/之/才/和/从/亦/其/然 是常用字。

修法只有一条门：豁免给"**首字是常见姓氏 + 全长>=3 + 该字属 NAME_INTERNAL_STOP_CHARS**"，
两字残片走不到这条门（温和/从容/这才/之前 全是两字）。
外加一处让位：`_looks_like_name()` 里那条更严的 `any(ch in STOP_CHARS …)` 重复否决
必须去掉，否则它会把 `is_person_shaped` 刚给的豁免顶掉。

期望值取自 agent-ops/cast-reset/py335_gate_*.txt 的现采（同段文本、同一次运行）：
    修前 A 名单 = ['旁白', '管家']            ← 4 枚中文名全丢
    修后 B 名单 = ['旁白', '李从文', '沈若兰', '王羲之', '周之才', '管家']
    真书《谍影重重》名单 61 枚**逐枚不变**（新增=[] 丢失=[] -> BOOK_UNCHANGED）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from modules.cast_analyzer.cast_extractor import (  # noqa: E402
    COMMON_SURNAMES, NAME_INTERNAL_STOP_CHARS, STOP_CHARS, CastExtractor,
    is_person_shaped)
from modules.cast_analyzer.cast_profiler import CastProfiler                  # noqa: E402


class _NoLLM:
    def is_ready(self):
        return False


# 首字是姓氏、内部语素是虚词字的真名形态（自造，不取自任何样书）
SHAPED_OK = [
    "沈若兰", "王羲之", "李从文", "周之才",
    "李若兰", "张和安", "顾念之", "王守之",
    "林晚晴", "卡洛斯", "玛莉·圣雅各",
]
# 两字残片 / 非姓氏开头 / 窗口误截 —— 一条都不许放进来
SHAPED_NO = [
    "沈若", "若兰", "陈若",                     # 两字：豁免门要求 >=3
    "温和", "和顺", "从容", "这才", "之前",
    "如若", "但见", "若干", "跟随",             # 两字叙述残片
    "他向着", "向着他",                         # 代词字，任何位置都不行
    "若兰姑娘", "土行之", "和仲", "从权",       # 非姓氏开头 / 姓氏但内部字不在白名单
    "老之才", "小从然", "阿其然",               # 称谓前缀开头：姓氏头这一条腿专拦它们
]

CHAPTERS = [
    (0, "“账本子呢？”沈若兰说，她皱着眉。\n"
        "“在你手上。”沈若兰说，她指了指桌面。\n"
        "“我来拿。”沈若兰说，她翻开抽屉。\n"
        "“这块玉是谁的？”王羲之说，他举起来对着光。\n"
        "“是我母亲的。”王羲之说，他放回了锦盒。\n"
        "“收好罢。”王羲之说，他转过身去。\n"),
    (1, "“路上小心。”李从文说，他抱起了书箱。\n"
        "“我认得路。”李从文说，他笑了笑。\n"
        "“那我就不送了。”李从文说，他站住了脚。\n"
        "“先生怎么看？”周之才说，他斟了茶。\n"
        "“不如何。”周之才说，他把杯子推过去。\n"
        "“你且留下。”周之才说，他按住了椅背。\n"),
    (2, "“他走了吗？”管家问，这才跟上前来。\n"
        "“还没有。”管家说，和顺地退到门边。\n"
        "“如若不差钱，便多买些。”管家说，他数着铜板。\n"
        "“事情从容办才好。”管家说，他压低了声音。\n"
        "“他为人温和。”管家说，他添了炭。\n"
        "“之前的账要清。”管家说，他翻开了册子。\n"
        "“但见新人笑。”管家说，他叹了一声。\n"
        "“若干担米。”管家说，他记在纸上。\n"
        "“他跟随队伍许久。”管家说，他揉了揉腿。\n"),
]
TARGETS = ["沈若兰", "王羲之", "李从文", "周之才"]
TRAPS = ["这才", "和顺", "如若", "从容", "温和", "之前", "但见", "若干", "跟随"]


def roster(chapters=CHAPTERS):
    ex = CastExtractor(min_line_share=0.0)
    for idx, text in chapters:
        ex.process_chapter(text, idx)
    return {c.name: c for c in ex.finalize()}


@pytest.mark.parametrize("name", SHAPED_OK)
def test_stop_chars_are_legal_inside_a_full_name(name):
    assert is_person_shaped(name), name


@pytest.mark.parametrize("name", SHAPED_NO)
def test_two_char_fragments_and_non_surname_heads_stay_rejected(name):
    assert not is_person_shaped(name), name


def test_exemption_needs_all_three_conditions():
    """豁免是三门与运算：长度、姓氏头、白名单字，缺一个就仍然拦。

    每条腿都配了"单独拆掉它就翻绿"的反例（变异 MUT-3 / MUT-4 实测）。前一版用
    "若兰X / X然" 当姓氏腿与长度腿的反例，那些串被别的门拦下，删掉哪条腿都不红，
    等于没护栏。
    """
    assert NAME_INTERNAL_STOP_CHARS < STOP_CHARS      # 白名单须是虚词字真子集（死成员由这条抓）
    for ch in sorted(NAME_INTERNAL_STOP_CHARS):
        assert not is_person_shaped("沈" + ch), ch     # 长度 <3：两字残片不进
    for name in ("老之才", "小从然", "阿其然"):         # 称谓前缀开头，不是常见姓氏
        assert name[0] not in COMMON_SURNAMES
        assert not is_person_shaped(name), name
    for name in ("沈地兰", "沈向兰"):                  # 姓氏+长度够，但内部字不在白名单
        assert not is_person_shaped(name), name


def test_chinese_names_reach_the_roster_on_the_real_path():
    """整链路（process_chapter -> finalize）：修前 4 枚全丢，修后 4 枚全进且无夹带。"""
    got = roster()
    assert set(got) == {"旁白", "管家"} | set(TARGETS), sorted(got)
    for trap in TRAPS:
        assert trap not in got, trap


def test_exempted_names_do_not_break_gender_evidence():
    """放进来的人名照样吃 PY-336 的窗口口径：沈若兰 只拿到她，三位只拿到他。"""
    got = roster()
    assert (got["沈若兰"].he_count, got["沈若兰"].she_count) == (0, 2)
    assert (got["王羲之"].he_count, got["王羲之"].she_count) == (2, 0)
    assert (got["周之才"].he_count, got["周之才"].she_count) == (2, 0)
    profiles = CastProfiler(llm_service=_NoLLM()).profile(list(got.values()))
    gender = {c.name: c.profile["gender"] for c in profiles}
    assert gender["沈若兰"] == "female", gender
    assert gender["王羲之"] == "male" and gender["李从文"] == "male", gender


if __name__ == "__main__":                                              # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
