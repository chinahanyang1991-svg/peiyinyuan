"""auto_assign 的跨角色去重（PY-329 的回归护栏）。

存在的理由: 打分是纯函数，同画像角色算出的排序**完全相同**，而旧版 auto_assign 逐角色
独立取 Top1 —— 于是《谍影重重》60 个说话角色只剩 5 枚音色、48 枚共用同一"台式男声"
（上校和小孩同音）。这段行为此前没有任何单测覆盖（voice_matcher 单测数 = 0）。
"""
from modules.cast_analyzer.voice_matcher import auto_assign, rank_voices


def role(name, gender="male", age="adult", lines=10):
    return {"name": name, "lines_count": lines,
            "profile": {"gender": gender, "age_group": age, "personality_tags": []}}


def voice(vid, gender="male", age="adult", language="zh"):
    return {"id": vid, "name": vid, "gender": gender, "age_group": age,
            "language": language, "style_tags": [], "engine": "voxcpm", "source": "downloaded"}


MALES = [role("甲", lines=100), role("乙", lines=90), role("丙", lines=80)]
POOL = [voice("v1"), voice("v2"), voice("v3")]


def test_same_profile_roles_no_longer_share_one_voice():
    got = auto_assign(MALES, POOL, min_score=40)
    assigned = [v for v in got.values() if v]
    assert len(assigned) == 3, got
    assert len(set(assigned)) == 3, f"塌缩回一枚嗓子: {got}"


def test_heaviest_role_gets_the_best_match():
    got = auto_assign(MALES, POOL, min_score=40)
    best = rank_voices(MALES[0], POOL)[0]["voice_id"]
    assert got["甲"] == best, f"戏份最重的角色没拿到最优: {got} 最优={best}"


def test_pool_exhausted_reuses_instead_of_leaving_blank():
    got = auto_assign(MALES, [voice("only")], min_score=40)
    assert got == {"甲": "only", "乙": "only", "丙": "only"}, got


def test_manual_voices_are_not_handed_out():
    got = auto_assign(MALES, POOL, min_score=40, taken={"v1"})
    assert "v1" not in got.values(), got
    assert got["甲"] in {"v2", "v3"} and got["乙"] in {"v2", "v3"}, got
    # 第 3 个角色: 未占用的都满了 -> 复用本轮自动分出的，仍不给 v1
    assert got["丙"] in {"v2", "v3"}, got


def test_manual_voice_is_never_reused_even_when_only_eligible_one():
    # v2 与角色性别冲突(46 分) 不够格，唯一够格的 v1 是用户手选的 -> 宁可 None，不静默借用
    got = auto_assign([role("甲")], [voice("v1"), voice("v2", gender="female")],
                      min_score=80, taken={"v1"})
    assert got == {"甲": None}, got


def test_below_threshold_still_returns_none():
    got = auto_assign([role("甲", gender="male")], [voice("f1", gender="female")],
                      min_score=90)
    assert got == {"甲": None}, got


def test_same_lines_count_roles_are_order_invariant():
    """PY-333: 戏份数相同的角色，分配结果不得随"角色列表传入顺序"变。

    旧排序只有 lines_count 一个键，同戏份角色保持传入顺序，而贪心占用对顺序敏感
    —— 实盘《谍影重重》重算一次就有 14 处（逆序）/4 处（打乱）配对跟着漂，
    这也是"落库映射"与"当日重算映射"对不上的原因（见 order_invariance_probe.py）。
    期望值写死，不让它退化成"自己和自己相等"的恒真式。
    同戏份时按角色名码点序定序（丙 U+4E19 < 乙 U+4E59 < 甲 U+7532），所以"甲拿 v1"
    恰恰是错的 —— 变的是顺序，不变的是这套码点序。
    """
    tied = [role("甲", lines=10), role("乙", lines=10), role("丙", lines=10)]
    base = auto_assign(tied, POOL, min_score=40)
    assert base == {"丙": "v1", "乙": "v2", "甲": "v3"}, base
    for perm in (list(reversed(tied)), [tied[1], tied[2], tied[0]],
                 [tied[2], tied[0], tied[1]]):
        assert auto_assign(perm, POOL, min_score=40) == base, perm
