"""
音色匹配（多人有声书批次 A）

设计依据: specs/2026-09-20-multi-cast-audiobook-design.md 4.1

打分（满分 100，纯函数、无 IO、可离线单测）:
    性别 gender      40   完全一致 40 / 任一方 unknown|neutral 且另一方不冲突 15 / 冲突 0
    年龄段 age       30   完全一致 30 / 相邻段(child-young-adult-senior) 18 / 未知 8 / 冲突 0
    风格标签 style   20   20 * |角色 tags ∩ 音色 tags| / |角色 tags|（角色无 tags 时给 6 分基础分）
    语言 language    10   音色语言 == 角色语言（默认 zh）10 / 音色语言缺失 4 / 不同 0
旁白（narrator）不特殊加分，而是固定画像（沉稳/磁性/浑厚/庄重 + adult），
因此同一套打分函数天然把旁白推向沉稳成年音（spec 4.1）。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Union

GENDER_WEIGHT = 40
AGE_WEIGHT = 30
STYLE_WEIGHT = 20
LANGUAGE_WEIGHT = 10

GENDER_PARTIAL = 15      # 任一方 neutral/unknown 但与另一方不冲突
GENDER_UNKNOWN = 14      # 音色未标注性别
AGE_PARTIAL = 18         # 相邻年龄段
AGE_UNKNOWN = 8          # 任一方年龄段未知
STYLE_NO_TAG = 6         # 角色没有风格标签
LANGUAGE_UNKNOWN = 4     # 音色未标注语言

AGE_LADDER = ["child", "young", "adult", "senior"]

# 音色风格标签里的同义词（提高词表命中率，仅用于比较，不改写数据）
STYLE_SYNONYMS = {
    "沉稳": {"沉稳", "稳重", "专业", "平静", "内敛"},
    "磁性": {"磁性", "浑厚", "低沉", "醇厚"},
    "浑厚": {"浑厚", "磁性", "低沉"},
    "庄重": {"庄重", "严肃", "正经"},
    "温柔": {"温柔", "温暖", "柔和", "甜美"},
    "活泼": {"活泼", "可爱", "清脆", "明亮"},
    "少女": {"少女", "年轻", "活泼"},
    "童声": {"童声", "儿童", "小孩"},
    "老年": {"老年", "苍老", "年迈"},
    "男": {"male", "男", "低沉", "浑厚"},
    "女": {"female", "女", "甜美", "清脆"},
}

DEFAULT_LANGUAGE = "zh"
NARRATOR_NAME = "旁白"

VoiceLike = Union[Dict, object]          # dict 或 VoiceProfile ORM 实例


# ---------------------------------------------------------------- 取值助手
def _attr(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        val = obj.get(key, default)
    else:
        val = getattr(obj, key, default)
    if isinstance(val, str):
        val = val.strip()
        return val or default
    return val


def _tagset(tags: Optional[Iterable]) -> List[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,，、\s]+", tags)
    out = []
    for t in tags:
        t = str(t).strip()
        if t:
            out.append(t)
    return out


def _expanded(tags: Sequence[str]) -> set:
    """把标签集按同义词表展开（原标签始终保留）。"""
    out = set()
    for t in tags:
        out.add(t)
        for syn in STYLE_SYNONYMS.get(t, ()):  # 同义词命中也算重合
            out.add(syn)
    return out


# ---------------------------------------------------------------- 角色属性归一
def role_attrs(cand) -> Dict:
    """CastCandidate / dict -> {name, gender, age_group, identity, tags, language, is_narrator}"""
    name = _attr(cand, "name", "") or ""
    profile = _attr(cand, "profile", {}) or {}
    is_narrator = bool(_attr(cand, "is_narrator", False)) or name == NARRATOR_NAME
    gender = (profile.get("gender") or "unknown").lower()
    age_group = (profile.get("age_group") or "unknown").lower()
    identity = profile.get("identity") or "unknown"
    tags = _tagset(profile.get("personality_tags"))
    if is_narrator:
        gender = "neutral" if gender in ("", "unknown") else gender
        age_group = "adult" if age_group in ("", "unknown") else age_group
        tags = tags or ["沉稳", "磁性", "浑厚", "庄重"]
    return {
        "name": name,
        "gender": gender or "unknown",
        "age_group": age_group or "unknown",
        "identity": identity,
        "tags": tags,
        "language": (profile.get("language") or DEFAULT_LANGUAGE).lower(),
        "is_narrator": is_narrator,
    }


# ---------------------------------------------------------------- 打分
def gender_score(role_gender: str, voice_gender: Optional[str]) -> int:
    rg = (role_gender or "unknown").lower()
    vg = (voice_gender or "").lower()
    if not vg or vg == "unknown":
        return GENDER_UNKNOWN          # 音色未标注性别：给保守分，不惩罚也不奖励
    if rg == vg:
        return GENDER_WEIGHT
    if rg in ("unknown", "neutral") or vg == "neutral":
        return GENDER_PARTIAL
    return 0


def age_score(role_age: str, voice_age: Optional[str]) -> int:
    va = (voice_age or "").lower()
    ra = (role_age or "").lower()
    if not va or va == "unknown" or ra in ("", "unknown"):
        return AGE_UNKNOWN
    if ra == va:
        return AGE_WEIGHT
    if ra in AGE_LADDER and va in AGE_LADDER:
        if abs(AGE_LADDER.index(ra) - AGE_LADDER.index(va)) == 1:
            return AGE_PARTIAL
    return 0


def style_score(role_tags: Sequence[str], voice_tags: Optional[Iterable]) -> int:
    rt = _tagset(role_tags)
    vt = _expanded(_tagset(voice_tags))
    if not rt:
        return STYLE_NO_TAG            # 角色无风格标签：中性基础分
    hit = sum(1 for tag in rt if _expanded([tag]) & vt)
    return int(round(STYLE_WEIGHT * hit / len(rt)))


def language_score(role_language: str, voice_language: Optional[str]) -> int:
    vl = (voice_language or "").lower()
    if not vl or vl == "unknown":
        return LANGUAGE_UNKNOWN
    if vl == (role_language or DEFAULT_LANGUAGE).lower():
        return LANGUAGE_WEIGHT
    return 0


def score_voice(cand, voice: VoiceLike) -> Dict:
    """单个 (角色, 音色) 打分，返回 {voice_id, name, score, breakdown}。"""
    attrs = role_attrs(cand)
    breakdown = {
        "gender": gender_score(attrs["gender"], _attr(voice, "gender")),
        "age": age_score(attrs["age_group"], _attr(voice, "age_group")),
        "style": style_score(attrs["tags"], _attr(voice, "style_tags")),
        "language": language_score(attrs["language"], _attr(voice, "language")),
    }
    total = sum(breakdown.values())
    return {
        "voice_id": _attr(voice, "id"),
        "voice_name": _attr(voice, "name"),
        "engine": _attr(voice, "engine"),
        "source": _attr(voice, "source"),
        "gender": _attr(voice, "gender"),
        "age_group": _attr(voice, "age_group"),
        "score": total,
        "breakdown": breakdown,
    }


def rank_voices(cand, voices: Sequence[VoiceLike], top_n: int = 0) -> List[Dict]:
    """对一个角色返回按分数降序的音色推荐（分数同 -> voice_id 稳定序）。"""
    scored = [score_voice(cand, v) for v in voices]
    scored.sort(key=lambda s: (
        -s["score"],
        -s["breakdown"]["gender"],
        str(s["voice_id"] or ""),
    ))
    return scored[:top_n] if top_n and top_n > 0 else scored


def suggest(cast: Sequence, voice_profiles: Sequence[VoiceLike],
            top_n: int = 5) -> Dict[str, List[Dict]]:
    """
    cast -> {role_name: [Top-N 推荐音色]}
    每项含 voice_id / score / breakdown，前端直接把推荐置顶。
    """
    voices = list(voice_profiles or [])
    out: Dict[str, List[Dict]] = {}
    for cand in cast or []:
        name = _attr(cand, "name") or ""
        if not name:
            continue
        out[name] = rank_voices(cand, voices, top_n=top_n)
    return out


def auto_assign(cast: Sequence, voice_profiles: Sequence[VoiceLike],
                min_score: int = 40,
                taken: Optional[Iterable[str]] = None) -> Dict[str, Optional[str]]:
    """
    给每个角色挑一个可直接落库的 voice_id（分数 >= min_score），否则返回 None
    （UI 提示用户手选，不硬塞不合格音色）。

    跨角色去重: 打分是纯函数，同画像的角色会算出**完全相同**的排序，逐角色独立取
    Top1 于是把整本书压到一枚嗓子上（实测《谍影重重》60 个说话角色只剩 5 枚音色、
    48 枚共用同一个"台式男声"，上校与小孩同音）。这里按戏份多到少贪心占用，
    后来的角色退到"次优且未被占用"。角色数 > 合格音色数时退回最高分复用——
    多人有声书宁可两角同音，也不要把整张表交回用户手点。

    taken: 不许再分的 voice_id —— 重跑分析时传本项目里用户手工绑定的那些。
    耗尽回退只在"本轮自动分出的音色"里复用；taken 里的音色永不外借，宁可返回 None。
    """
    voices = list(voice_profiles or [])
    taken_ids = {t for t in (taken or ()) if t}
    used: set = set(taken_ids)
    # PY-333: 次级键必须是角色名。只按 lines_count 降序时，戏份数相同的角色保持
    # **传入顺序**，而贪心占用又对顺序敏感，于是同一本书换一次角色列表顺序就整体
    # 错配一格。实测（《谍影重重》60 个说话角色 / 563 枚绑定池，只读探针
    # agent-ops/cast-reset/order_invariance_probe.py）：
    #   同一批候选只换顺序 —— 旧口径漂 14 处(逆序) / 4 处(打乱 seed=333)；
    #   补名次级键后 —— 原序/逆序/打乱三种输入映射完全相同（0 处不同）。
    # 这也是"落库的映射"与"当日重算的映射"60 处只有 5 处一致（55 处错一格）的根因，
    # 并让 confirm 的回退分支与分析主路径第一次给出同一张表。
    # 注：漂的是"谁拿哪枚"，两条不变量（一人一枚、池内无回放档）始终成立。
    ordered = sorted((c for c in (cast or []) if _attr(c, "name")),
                     key=lambda c: (-int(_attr(c, "lines_count", 0) or 0),
                                    str(_attr(c, "name") or "")))
    result: Dict[str, Optional[str]] = {}
    for cand in ordered:
        name = _attr(cand, "name")
        eligible = [i for i in rank_voices(cand, voices) if i["score"] >= min_score]
        picked = next((i["voice_id"] for i in eligible if i["voice_id"] not in used), None)
        if picked is None:
            # 只复用本轮自动分出去的音色；用户手选的那枚永不外借，宁可 None 让 UI 提示手选
            reuse = next((i["voice_id"] for i in eligible if i["voice_id"] not in taken_ids), None)
            picked = reuse
        if picked:
            used.add(picked)
        result[name] = picked
    return result
