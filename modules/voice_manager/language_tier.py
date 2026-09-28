"""音色参考音频的语种档（PY-323 a′）。

一处真值，两侧消费：克隆建行时按**声明语种**打标，离线工具按**声学复判**改标，
分发（整书合成 / 试听）只问一句"这行能不能交给 openvoice 说中文"。

为什么必须有这张表（实测，勿再猜）：`agent-ops/py323` 网格逐个排雷的结论是
openvoice 的字准瓶颈**不在噪声系数**（`noise_scale` 0.333~0.8 恒 1.000），而是
**跨语种 SE × converter 交叉项**——普通话参考 tpsim 0.917~1.000，英文参考
0.500~0.917（生产 6 枚复判 5 枚不合格，均值 0.819）。所以"英文参考的音色别说中文"
是**分发问题**，不是调参问题；而在线跑尺子（whisper）CPU 上每枚要 6~14s，不可承受，
故改成离线一次性打标 + 分发查表。

档位只描述**参考音频**，与待合成文本无关（文本语种由 `openvoice_adapter._pick_language`
按 CJK 判定，那是另一条链）。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

TIER_MANDARIN = "mandarin_ref"
TIER_NONMANDARIN = "nonmandarin_ref"
TIER_UNKNOWN = "unknown"
TIERS = (TIER_MANDARIN, TIER_NONMANDARIN, TIER_UNKNOWN)

OPENVOICE_ENGINE = "openvoice"
# 语种档不合格时改由谁发声：voxcpm 吃普通话 prompt / 语音设计，实测在 CPU 上可用，
# 且试听高档本来就把 `Config.TTS_ENGINE` 排在自己的引擎之后（同一套兜底次序）。
DEFAULT_FALLBACK_ENGINE = "voxcpm"

# 声学判种的置信地板：低于它一律 unknown（宁可不降级，也不拿一次猜测改路由）
MIN_LANG_PROB = 0.60

# 声明语种里"不用 zh 前缀表达普通话"的写法（下载器/数据集里都见过）
_MANDARIN_TAGS = {"cmn", "mandarin", "chinese"}


def _field(row: Any, key: str, default: Any = None) -> Any:
    """ORM 行 / to_dict() 字典两种形态统一取值（调用点两种都在传）。"""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def tier_from_language(language: Any) -> str:
    """按**声明**语种给档。空值给 unknown —— 没说过不等于说过普通话。

    写法历史上有 `zh`/`zh-CN`/`zh_CN`/`ZH_Hans` 四种（下载器与克隆表单各写各的），
    所以先归一分隔符再按 BCP-47 主标签判，别拿集合精确匹配（会把 `zh_CN` 误判成外文）。
    粤语例外：它是 `zh` 主标签下的另一种口语，不是 openvoice 该拿去说普通话的那档。
    """
    tag = str(language or "").strip().lower().replace("_", "-")
    if not tag:
        return TIER_UNKNOWN
    if tag.startswith("yue") or "cant" in tag:
        return TIER_NONMANDARIN
    if tag == "zh" or tag.startswith("zh-") or tag in _MANDARIN_TAGS:
        return TIER_MANDARIN
    return TIER_NONMANDARIN


def tier_from_detection(lang: Any, prob: Any) -> str:
    """按**声学**判种给档：置信不足或判不出都落 unknown（不改路由）。"""
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return TIER_UNKNOWN
    tag = str(lang or "").strip().lower()
    if not tag or p < MIN_LANG_PROB:
        return TIER_UNKNOWN
    return TIER_MANDARIN if tier_from_language(tag) == TIER_MANDARIN else TIER_NONMANDARIN


def tier_of(row: Any) -> Optional[str]:
    value = _field(row, "audio_language_tier")
    value = str(value).strip() if value is not None else ""
    return value or None


def openvoice_tier_gate(engine_name: Any, row: Any,
                        fallback: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """分发门：返回 `(该用哪个引擎, 降级原因|None)`。

    只拦"openvoice + 参考音频非普通话"这一格（实测不合格的那一格）；
    其余一律原样通过（含 NULL/unknown —— 没有档就没有证据改路由）。
    原因非空时调用方**必须**显式记进日志/产物元数据，不许静默换引擎
    （换了引擎=换了音色，用户听到的不是同一把嗓子）。
    """
    name = str(engine_name or "").strip().lower()
    if name != OPENVOICE_ENGINE:
        return name or None, None
    if tier_of(row) != TIER_NONMANDARIN:
        return name, None
    target = str(fallback or DEFAULT_FALLBACK_ENGINE).strip().lower() or DEFAULT_FALLBACK_ENGINE
    if target == OPENVOICE_ENGINE:
        # 调用方的兜底还是 openvoice（整书项目引擎就是它时会出现）：等于没兜底，
        # 回落到模块默认档，否则这格就白拦了。
        target = DEFAULT_FALLBACK_ENGINE
    reason = (f"参考音频语种档={TIER_NONMANDARIN}（openvoice 跨语种克隆实测字准 "
              f"0.500-0.917，不合格）-> 引擎改由 {target} 发声")
    return target, reason
