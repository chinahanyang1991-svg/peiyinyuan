"""
音色条件单点解析（P0-1）

现状问题：一次合成需要的"条件"（用哪个引擎、什么语言、speed/pitch/emotion 三个
可直接喂给 ``engine.synthesize`` 的参数）此前分散在四处各自拼装（读代码后的实况）：

* ``backend/api/dubbing.py::preview_dubbing``        —— 只读全局 settings：不夹 speed、
  不兜非数值、而且**根本不传 pitch**
* ``backend/api/cast.py::preview_cast_role``         —— 借道下面的句级 helper：把
  role.mix 塞进 ``sent``、把 ``{"speed": None, "pitch": None}`` 塞进 ``settings``
* ``modules/workflow/cast_pipeline_hooks.py::synth_params_for_sentence`` —— 句级 > 全局
  settings，自己实现了一遍分层兜底 + 裁剪
* ``modules/workflow/conversion_workflow.py::_stage_synthesize``（PY-116）—— 自己
  ``getattr(prof, "engine")`` 读音色行做句级引擎路由

四套写法对"越界裁剪 / 非数值兜底 / emotion 从哪来"的处理并不一致 —— 改一次要动四处。
本模块把"从 VoiceProfile 行读出 engine/语言/性别/speed/pitch/风格标签/meta，拼成
synth 参数字典"收敛成**一个纯函数**（不查库、不碰引擎、不改入参）。

优先级（高 -> 低）：
    显式 overrides  >  layers（按传入顺序，先者优先；用于句级/角色级/全局级叠层）
    >  profile.meta 里的 speed/pitch/emotion  >  profile 行字段  >  defaults  >  内置默认

    "调用点给的层压过音色自带偏好"是刻意的：项目全局 settings / 角色 mix 是用户在
    这个项目上显式拧的旋钮，音色行 meta 里的 speed 只是这个音色自带的倾向。

与既有调用点的字段名/行为对齐（这是重构，不是改语义）：
    - 返回 dict 里 ``speed`` / ``pitch`` / ``emotion`` 三键与
      ``synth_params_for_sentence`` 原样同名同义；
    - ``speed`` 夹到 [0.5, 2.0]、``pitch`` 夹到 [-12, +12]（半音），越界只裁剪不抛错，
      非数值/NaN/inf 落回下一层 —— 与原实现同一条规矩；
    - ``emotion`` 兼容句级 ``tone`` 键（P5 ToneDetector 写在句上，不是 ``emotion``）；
      空串/None 视为"未指定"（原实现 ``sent.get("tone") or "neutral"`` 就是这个意思）。

两处刻意的收紧（唯一与旧行为不同的点，都在 ``preview_dubbing``，见模块评审记录）：
    1) speed 以前原样（未裁剪、可能是 None/字符串）丢给引擎，现在与 P7/角色试听一样
       夹进 [0.5, 2.0] 并对非数值兜底；
    2) pitch 以前根本不传，现在传（项目 global_settings 里本来就有 pitch，此前在
       配音试听这条路上是死值 —— 与角色试听/P7 主链路不一致）。
    引擎侧本来就会忽略越界值，这不是新增约束，而是"两个入口一套语义"的目的本身。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

# ---------- 内置默认（与 BaseTTSEngine.synthesize 的形参默认一致） ----------
DEFAULT_SPEED: float = 1.0
DEFAULT_PITCH: float = 0.0
DEFAULT_EMOTION: str = "neutral"
DEFAULT_LANGUAGE: str = "zh"
DEFAULT_CATEGORY: str = "human"

# ---------- 值域（与 cast_pipeline_hooks 原有常量一致） ----------
MIN_SPEED, MAX_SPEED = 0.5, 2.0
MIN_PITCH, MAX_PITCH = -12.0, 12.0

# 句级/角色级叠层里 emotion 的兼容键（P5 把语气写在 sent["tone"]）
_EMOTION_KEYS = ("emotion", "tone")
# VoiceProfile 上参与条件拼装的非数值字段
_PROFILE_STR_FIELDS = ("id", "name", "source", "category", "engine", "language",
                       "gender", "age_group", "description",
                       "reference_audio_path", "sample_audio_path")

CONDITIONING_KEYS = ("voice_id", "engine", "language", "gender", "age_group",
                     "category", "style_tags", "description",
                     "reference_audio_path", "sample_audio_path",
                     "speed", "pitch", "emotion")

__all__ = [
    "DEFAULT_SPEED", "DEFAULT_PITCH", "DEFAULT_EMOTION", "DEFAULT_LANGUAGE",
    "MIN_SPEED", "MAX_SPEED", "MIN_PITCH", "MAX_PITCH", "CONDITIONING_KEYS",
    "coerce_number", "clamp_number", "resolve_profile_conditioning",
]


def coerce_number(value: Any, fallback: float) -> float:
    """可选数值：None / 非数字 / NaN / inf 一律用 fallback（越界不在此处裁剪）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return v if np.isfinite(v) else float(fallback)


def clamp_number(value: float, lo: float, hi: float) -> float:
    """把 value 夹进 [lo, hi]；越界只裁剪不抛错。"""
    v = coerce_number(value, lo)
    return float(min(max(v, lo), hi))


def _as_row(profile: Any) -> Dict[str, Any]:
    """把 VoiceProfile ORM 行 / to_dict() 结果 / 任意映射统一成普通 dict。"""
    if profile is None:
        return {}
    if isinstance(profile, Mapping):
        return dict(profile)
    row: Dict[str, Any] = {}
    for field in _PROFILE_STR_FIELDS:
        row[field] = getattr(profile, field, None)
    for field in ("style_tags", "meta"):
        row[field] = getattr(profile, field, None)
    return row


def _first_present(layers: Sequence[Optional[Mapping[str, Any]]],
                   keys: Sequence[str]) -> Optional[Any]:
    """按层顺序找第一个"显式给了值"的键；None / 空串都算"未指定"。"""
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        for key in keys:
            if key not in layer:
                continue
            value = layer[key]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None


_UNSET = object()


def _first_number(layers: Sequence[Optional[Mapping[str, Any]]],
                  keys: Sequence[str]) -> Any:
    """按层顺序找第一个**真能当数值用**的值（``_UNSET`` = 一层都没给）。

    与旧实现 ``_num(sent.get("speed"), _num(settings.get("speed"), default))`` 逐层
    下探的语义一致：非数值 / NaN / inf 也算"这层没给"，继续往下探，而不是直接跳到
    内置默认。
    """
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        for key in keys:
            if key not in layer:
                continue
            value = layer[key]
            if isinstance(value, str) and not value.strip():
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if np.isfinite(v):
                return v
    return _UNSET


def resolve_profile_conditioning(profile: Any = None,
                                 overrides: Optional[Mapping[str, Any]] = None,
                                 *,
                                 layers: Optional[Sequence[Optional[Mapping[str, Any]]]] = None,
                                 defaults: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """把一个音色行 + 各层覆盖解析成"可直接用"的条件字典。

    Args:
        profile: ``VoiceProfile`` ORM 行、它的 ``to_dict()`` / 任意映射，或 ``None``
            （调用方今日不读音色行时传 None，行为与旧实现严格一致）。
        overrides: 最高优先级显式值（只填它给的键，其余向下探）。
        layers: 中间叠层，**顺序即优先级**（句级 > 角色级 > 全局级）。
        defaults: 本调用点自己的兜底值（例如 P7 的 ``settings["speed"]``）。

    Returns:
        固定键集 ``CONDITIONING_KEYS`` 的 dict；其中 ``speed`` / ``pitch`` 已裁剪进
        值域，``engine`` 在音色行没声明且调用方没给兜底时是 ``None``
        （**绝不替调用方猜引擎** —— 路由兜底属于调用点的业务知识）。
    """
    row = _as_row(profile)
    row_meta = row.get("meta") if isinstance(row.get("meta"), Mapping) else {}
    row_style = row.get("style_tags")

    # 优先级从高到低的取值层：overrides -> layers -> profile 数值 -> profile 列 -> defaults
    num_layers: List[Optional[Mapping[str, Any]]] = []
    if isinstance(overrides, Mapping):
        num_layers.append(overrides)
    for layer in (layers or []):
        if isinstance(layer, Mapping):
            num_layers.append(layer)
    meta_layer = {"speed": row_meta.get("speed"), "pitch": row_meta.get("pitch"),
                  "emotion": row_meta.get("emotion")}
    # 音色行本身作为最低的"音色层"：既提供 engine/language/gender/category/路径等文本
    # 字段，也提供它自己可能带的 speed/pitch/emotion（dict 形态的调用方会给）。
    num_layers.extend([meta_layer, row])
    tail: Dict[str, Any] = {}
    if isinstance(defaults, Mapping):
        tail.update(defaults)

    def _number(keys: Sequence[str], builtin: float, lo: float, hi: float) -> float:
        picked = _first_number(num_layers, keys)
        if picked is _UNSET:
            picked = _first_number([tail], keys)
        if picked is _UNSET:
            picked = builtin
        return clamp_number(picked, lo, hi)

    def _text(keys: Sequence[str], builtin: Optional[str]) -> Optional[str]:
        picked = _first_present(num_layers, keys)
        if picked is None:
            picked = _first_present([tail], keys)
        return builtin if picked is None else picked

    # 风格标签：音色行是 CSV 字符串时（voice_service.analyze_and_save_voice 就这么写）
    # 切成列表，其余原样给出，调用方爱怎么用怎么用。
    if isinstance(row_style, str):
        style_tags: List[str] = [s.strip() for s in row_style.split(",") if s.strip()]
    elif isinstance(row_style, (list, tuple, set)):
        style_tags = [str(s) for s in row_style]
    else:
        style_tags = []

    voice_id = row.get("id") or row.get("voice_id")
    if isinstance(overrides, Mapping):
        voice_id = overrides.get("voice_id") or overrides.get("id") or voice_id

    return {
        "voice_id": voice_id,
        "engine": _text(("engine",), None),
        "language": _text(("language",), DEFAULT_LANGUAGE),
        "gender": _text(("gender",), None),
        "age_group": _text(("age_group",), None),
        "category": _text(("category",), DEFAULT_CATEGORY),
        "style_tags": style_tags,
        "description": _text(("description",), ""),
        "reference_audio_path": _text(("reference_audio_path",), None),
        "sample_audio_path": _text(("sample_audio_path",), None),
        "speed": _number(("speed",), DEFAULT_SPEED, MIN_SPEED, MAX_SPEED),
        "pitch": _number(("pitch",), DEFAULT_PITCH, MIN_PITCH, MAX_PITCH),
        "emotion": str(_text(_EMOTION_KEYS, DEFAULT_EMOTION)),
    }
