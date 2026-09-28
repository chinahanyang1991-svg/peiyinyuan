"""
cast_pipeline_hooks: 多人有声书模式的 pipeline 接线函数（批次 B 纯新增，批次 C 才接）

设计依据: docs/superpowers/specs/2026-09-20-multi-cast-audiobook-design.md 5/6
批次 A 依赖: modules/cast_analyzer（画像/别名表）、modules/mixer（混音）
批次 B 产物: backend/services/cast_service.py（写 Role.extra_data + project.extra_data.cast）

本模块的定位（为什么存在）:
    批次 B 禁止编辑 modules/workflow/conversion_workflow.py / text_processor / pipeline.py
    （智能体 2b 正在修 PY-115..128）。所以 P5/P6/P7/P8 需要的 cast 逻辑先做成
    这里的**纯函数**：不 import backend、不开 DB、不碰全局状态，只吃 dict + ndarray。
    批次 C 验证通过后，按下表把调用点插进 conversion_workflow 即可，本文件本身不用改。

============================================================================
批次 C 接线表（锚点 = conversion_workflow.ConversionWorkflow 的方法/语句；
行号取自 HEAD 50aed94 + 智能体2b 在途改动，行号若漂移请按"函数名 + 引号内语句"定位）
============================================================================

| 位置 | 锚点 | 动作 |
|------|------|------|
| P5 | `_stage_analyze` 末尾 `dialogue_count = sum(...)`（现 :497）之前 | **插入** `normalize_speakers(context.get("sentences"), cast)`：把 DialogueDetector 给的 role_hint 按别名表归一到规范角色名。不改检测逻辑。 |
| P6 | `_stage_assign` 内 `for sent in sentences:` 整个循环体（现 :579-611，从 `if mode == "single":` 到 `break`） | **替换**为 `assign_with_cast(context, cast)["sentences"]`（cast = cast_service.get_cast(project_id)）。函数内部保留了原有"无 project / mode=single / 分段区间"三条路径，所以整体替换等价于升级而非改语义。`context["voice_map"] = voice_map`（:613）与 `assigned = sum(...)`（:615）两行保持原样。 |
| P6 前置 | `_stage_assign` 开头 `project = dubbing_service.get_project(project_id)`（现 :513） | **插入** `cast = cast_for_assign(project, project_id)`：Role.to_dict() 不带 extra_data，必须补一次带画像的读取（见该函数 docstring）。 |
| P7 | `_stage_synthesize` 的 `engine.synthesize(text=..., speed=settings.get("speed", 1.0), pitch=settings.get("pitch", 0.0), emotion=tone)`（现 :711-720） | **替换实参**：`p = synth_params_for_sentence(sent, settings)` 后传 `speed=p["speed"], pitch=p["pitch"], emotion=p["emotion"]`。现状只吃全局 speed/pitch，句级（角色级）参数被丢弃 —— 这是本函数存在的唯一理由。 |
| P8 | `_stage_postprocess` 末尾 `context["audio_arrays"] = processed`（现 :825）之前 | **插入** `processed = postprocess_with_mix(context, mix_settings, processed)["audio_arrays"]`。mix 未配置时函数原样返回入参列表，逐样本行为与现状一致（modules.mixer 的直通保证）。 |
| P9 | `_stage_package` 的 `all_audio = all_audio / peak * 0.99`（现 :884） | **加守卫**：`if not context.get("mix_applied"):`。混音后 P8 已做过 -1dBFS 软限幅 + LUFS 归一，P9 再按峰值拉升会把绝对响度重新拉平（相对关系不变，但 LUFS 目标失效）。 |

`context` 即 pipeline 的 context dict；`mix_settings` 传
`dubbing_service.get_project(id)["global_settings"]`（含 "mix" 键）或其 "mix" 子字典均可，
两种形状本模块都认。

安全红线（所有函数共同遵守）:
1. cast 数据缺失/项目未确认 -> 一律退回现状行为，绝不因"多人模式没配好"让任务失败。
2. 不新增异常路径：句级参数越界只裁剪不抛错；混音异常由批次 C 的调用点决定（建议 raise，
   与 PY-117 "不以静音兜底" 的裁定一致）。
3. 纯函数：同一输入必得同一输出，便于批次 C 用单测钉住（qa/harness/run_c5_*）。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 与 backend/services/cast_service.py 的常量保持一致（不 import backend，避免循环依赖）
NARRATOR_NAMES = ("旁白", "narrator")
_QUOTE_CHARS = "\"'“”‘’『』「」〈〉《》 \t\r\n\u3000-—–_·.。:：,，;；!！?？"
_SENT_MIN_SPEED, _SENT_MAX_SPEED = 0.5, 2.0
_SENT_MIN_PITCH, _SENT_MAX_PITCH = -12.0, 12.0


# ============================================================ 小工具（纯函数）
def norm_name(name) -> str:
    """角色名/speaker 归一：去空白、引号残留、大小写（与 cast_service.norm_name 同形）。"""
    s = str(name or "").strip()
    for ch in "\"'“”『「』』 \t\u3000":
        s = s.replace(ch, "")
    return s.lower()


def is_narrator_name(name) -> bool:
    """是否旁白名（归一后比较）。"""
    return norm_name(name) in NARRATOR_NAMES


def mix_settings_of(project_or_mix: Optional[Dict]) -> Dict:
    """
    兼容两种形状的混音配置取值:
        project["global_settings"]（含 "mix" 键）  或  global_settings["mix"] 本体
    批次 C 在 P8 直接传 `dubbing_service.get_project(id)["global_settings"]` 即可。
    """
    if not isinstance(project_or_mix, dict):
        return {}
    inner = project_or_mix.get("mix")
    if isinstance(inner, dict):
        return dict(inner)
    # 已经是 mix 本体（含 bgm/sfx/role_gains/master/gaps 任一 key）
    if any(k in project_or_mix for k in ("bgm", "sfx", "role_gains", "master", "gaps")):
        return dict(project_or_mix)
    return {}


def cast_is_active(cast: Optional[Dict]) -> bool:
    """cast 是否可用于接线：有角色表且旁白有音色（否则接了也只能全旁白，等于现状）。"""
    if not isinstance(cast, dict):
        return False
    roles = [r for r in (cast.get("roles") or []) if isinstance(r, dict)]
    if not roles:
        return False
    return any(r.get("voice_id") for r in roles)


# ============================================================ P5: speaker 归一
def normalize_speakers(sentences: Sequence[Dict], cast: Optional[Dict]) -> Dict:
    """
    P5 speaker 归一化（批次 C 插在 `_stage_analyze` 的
    `dialogue_count = sum(1 for s in sentences if s.get("is_dialogue"))` 之前）。

    DialogueDetector 只给局部线索（role_hint 可能是"老周""周队长""建国同志"），
    这里用 cast_service 写好的别名表把它们统一成 Role.name，P6 才有稳定 key。

    Args:
        sentences: context["sentences"]（原地修改）
        cast: cast_service.get_cast(project_id) 的返回；None/空 -> 直接不动

    Returns:
        {"changed": int, "resolved": int, "unresolved": int}
        （给 logger.info 用，批次 C 决定是否打印）
    """
    stats = {"changed": 0, "resolved": 0, "unresolved": 0}
    if not cast or not sentences:
        return stats
    alias = build_alias_index(cast)
    if not alias:
        return stats
    for sent in sentences:
        if not isinstance(sent, dict):
            continue
        raw = sent.get("speaker") or ""
        if not str(raw).strip():
            continue
        canonical = alias.get(norm_name(raw))
        if canonical:
            stats["resolved"] += 1
            if canonical != raw:
                sent["speaker"] = canonical
                sent["speaker_raw"] = raw
                stats["changed"] += 1
        else:
            # 认不出的 speaker 不要留着：P6 会把它当"名表未命中"归旁白，
            # 但显式清空更诚实（speaker_raw 仍可回溯）
            stats["unresolved"] += 1
            sent["speaker_raw"] = raw
            sent["speaker"] = ""
    return stats


def build_alias_index(cast: Dict) -> Dict[str, str]:
    """归一化后的 别名/规范名 -> 规范角色名 表（cast_service 写的别名表 + 角色自身别名）。"""
    table: Dict[str, str] = {}
    meta = (cast.get("cast") or {}) if isinstance(cast, dict) else {}
    for key, canonical in (meta.get("aliases") or {}).items():
        if canonical:
            table[norm_name(key)] = str(canonical)
    for role in cast.get("roles") or []:
        if not isinstance(role, dict):
            continue
        name = str(role.get("name") or "").strip()
        if not name:
            continue
        table.setdefault(norm_name(name), name)
        for alias in role.get("aliases") or []:
            a = norm_name(alias)
            if a:
                table.setdefault(a, name)
    return table


# ============================================================ P6: 角色 -> 音色
def cast_for_assign(project: Optional[Dict], project_id: Optional[str] = None) -> Optional[Dict]:
    """
    P6 前置：给 `_stage_assign` 用的 cast 视图。

    为什么需要它：`dubbing_service.get_project()` 走的是
    `Role.to_dict()`（backend/models/project.py:60），**不带 extra_data**，
    而 cast 的别名/角色级 mix 全在 extra_data 里 —— 直接拿 project dict 接
    assign_with_cast 会静默退化成"只有 name->voice_id"。

    批次 C 的调用（替换 conversion_workflow 现 :513 那行为两行）:
        project = dubbing_service.get_project(project_id) if project_id else None
        cast = cast_for_assign(project, project_id)          # <- 新增
    本函数只在 project 已带 extra_data（例如调用方自己用 cast_service.get_cast 拿的）
    时零开销返回，否则懒加载 cast_service（函数内 import，模块级不依赖 backend，
    这样纯单测不启服务也能跑）。
    """
    if not project:
        return None
    roles = project.get("roles") or []
    if roles and all("extra_data" in (r or {}) for r in roles):
        return {"roles": roles, "cast": (project.get("extra_data") or {}).get("cast") or {},
                "mode": project.get("mode")}
    pid = project_id or project.get("id")
    if not pid:
        return None
    try:
        from backend.services.cast_service import cast_service
        return cast_service.get_cast(pid)
    except Exception as exc:                              # pragma: no cover - 防御
        logger.warning(f"[cast_hooks] cast_service.get_cast({pid}) 失败，按无 cast 处理: {exc}")
        return None


def build_role_index(cast: Dict) -> Tuple[Dict[str, Dict], Optional[str], Optional[str]]:
    """
    -> (name -> {voice_id, role_id, kind, gain_db, speed, pitch}, narrator_voice, any_voice)

    kind 决定 P8 用哪种段间隙（dialogue/narration）。
    旁白判定沿用 cast_service.is_narrator_role 的两条规则（role_type 或 名字）。
    """
    index: Dict[str, Dict] = {}
    narrator_voice: Optional[str] = None
    any_voice: Optional[str] = None
    for role in (cast or {}).get("roles") or []:
        if not isinstance(role, dict):
            continue
        name = str(role.get("name") or "").strip()
        voice = role.get("voice_id") or None
        if voice and any_voice is None:
            any_voice = voice
        mix = role.get("mix") or (role.get("extra_data") or {}).get("mix") or {}
        narrator = (str(role.get("role_type") or "").lower() == "narrator") or is_narrator_name(name)
        if narrator and voice:
            narrator_voice = narrator_voice or voice
        if not name:
            continue
        entry = {
            "role_id": role.get("id") or "",
            "name": name,
            "voice_id": voice,
            "kind": "narration" if narrator else "dialogue",
            "gain_db": float(mix.get("gain_db", 0.0) or 0.0),
            "speed": float(mix.get("speed", 1.0) or 1.0),
            "pitch": float(mix.get("pitch", 0.0) or 0.0),
        }
        index[norm_name(name)] = entry
    return index, narrator_voice, any_voice


def assign_with_cast(ctx: Dict, cast: Optional[Dict]) -> Dict:
    """
    P6 多人分配（批次 C 替换 `_stage_assign` 中 `for sent in sentences:` 的整个循环体，
    现 conversion_workflow.py:579-611；`context["voice_map"] = voice_map`(:613) 与
    `assigned = sum(...)`(:615) 保留原样）。

    相对现状的增量:
      1. speaker 先过别名表归一（"老周" -> "周建国"），现状只做 speaker.lower() 精确查名表，
         一个角色的每个别名都要在 Role 表里重复建人才认得出来。
      2. 命中角色后写入**句级** speed/pitch/gain_db（来自 Role.extra_data.mix），
         P7 靠 synth_params_for_sentence 消费 —— 现状这两个字段全程没人写、也没人读。
      3. 写 role_id / role_kind，P8 混音按角色加增益、按旁白/对白选段间隙。
      4. 未命中一律归旁白音色（与 PY-113 的裁定一致），旁白缺音色时退回第一个可用音色。

    与现状等价的路径（保证不回归）:
      - cast 为空 / 没有任何角色绑了音色 -> 原样返回 ctx["sentences"]，不改字段。
      - project.mode == "single" -> 全部句子用 default_voice（与现 :580-582 同）。

    Args:
        ctx: pipeline context（读 "sentences"，原地改句字段）
        cast: cast_service.get_cast() 的返回（或 cast_for_assign 的形状）

    Returns:
        {"sentences": [...], "cast_stats": {...}} —— sentences 与现状同名，
        批次 C 的 return 语句保持 `return {"sentences": sentences}` 即可（cast_stats 另做日志）。
    """
    sentences = ctx.get("sentences") if isinstance(ctx, dict) else None
    sentences = sentences or []
    stats = {"total": len(sentences), "cast": False, "by_name": 0, "by_alias": 0,
             "to_narrator": 0, "unassigned": 0, "roles": 0}
    if not sentences:
        return {"sentences": sentences, "cast_stats": stats}

    alias = build_alias_index(cast or {})
    roles, narrator_voice, any_voice = build_role_index(cast or {})
    default_voice = any_voice
    fallback_narrator = narrator_voice or any_voice
    # 旁白条目：未命中的句子要带上它的 role_id/增益/语速（批次C 接线时发现
    # 只写 voice_id 会让旁白的角色级 mix 在 P7/P8 全程丢失）
    narrator_entry = next((e for e in roles.values() if e.get("kind") == "narration"), None)

    if not cast_is_active(cast):
        # 没有可用 cast（老项目/未确认）：保持现状 P6 的名表结果，不写任何新字段
        return {"sentences": sentences, "cast_stats": stats}

    stats["cast"] = True
    stats["roles"] = len(roles)

    # VoiceAssignment 区间覆盖优先级保持不变：cast 只接管"名表/别名"这一路，
    # 命中的句子若随后被区间覆盖，由批次 C 决定顺序（建议区间覆盖在后，保持 PY-114 语义）。
    for sent in sentences:
        if not isinstance(sent, dict):
            continue
        raw = str(sent.get("speaker") or "")
        key = norm_name(raw)
        canonical = alias.get(key)
        entry = roles.get(norm_name(canonical)) if canonical else None
        if entry is None:
            entry = roles.get(key)
            canonical = None

        if entry and entry.get("voice_id"):
            sent["voice_id"] = entry["voice_id"]
            sent["role_name"] = entry["name"]
            sent["role_id"] = entry["role_id"]
            sent["role_kind"] = entry["kind"]
            sent["speed"] = entry["speed"]
            sent["pitch"] = entry["pitch"]
            sent["role_gain_db"] = entry["gain_db"]
            if canonical and norm_name(canonical) != key:
                stats["by_alias"] += 1
            else:
                stats["by_name"] += 1
            continue

        # 未命中：归旁白（旁白没音色时退回第一个可用音色，与现 :549 同）
        sent["voice_id"] = fallback_narrator or default_voice
        sent["role_name"] = "narrator"
        sent["role_kind"] = "narration"
        if narrator_entry:
            # 旁白也是 Role 表里的一等公民：增益/语速/音高要跟着走，
            # 否则 P8 的 role_gains 与 P7 的句级参数对旁白永久失效。
            sent["role_id"] = narrator_entry["role_id"]
            sent["speed"] = narrator_entry["speed"]
            sent["pitch"] = narrator_entry["pitch"]
            sent["role_gain_db"] = narrator_entry["gain_db"]
        if sent["voice_id"]:
            stats["to_narrator"] += 1
        else:
            stats["unassigned"] += 1

    ctx["sentences"] = sentences
    logger.info(f"[cast_hooks] assign_with_cast: {stats}")
    return {"sentences": sentences, "cast_stats": stats}


# ============================================================ P7: 句级合成参数
def synth_params_for_sentence(sent: Optional[Dict],
                              settings: Optional[Dict] = None,
                              default_speed: float = 1.0,
                              default_pitch: float = 0.0) -> Dict:
    """
    P7 句级参数（批次 C 用在 `_stage_synthesize` 的 engine.synthesize 调用处，
    现 conversion_workflow.py:711-720，替换
    `speed=settings.get("speed", 1.0), pitch=settings.get("pitch", 0.0), emotion=tone`
    三个实参）。

    现状问题（本函数要解决的）：`_stage_assign` 已经会把分段分配的 speed/pitch 写到
    `sent["speed"]/sent["pitch"]`（PY-114，现 :607-609），但 `_stage_synthesize` 只读
    全局 settings，句级值被静默丢弃 —— 于是"每个角色不同语速/音高"整条链路断在 P7。

    优先级（高 -> 低）:
        句级 sent["speed"]/sent["pitch"]  （cast 角色参数 或 VoiceAssignment 区间参数）
        > 全局 settings["speed"]/settings["pitch"]
        > 引擎默认 default_speed/default_pitch
    emotion 仍取 tone（P5 ToneDetector 的句级语气），保持现状语义不变。

    越界只裁剪不抛错：speed 夹到 [0.5, 2.0]，pitch 夹到 [-12, +12]（半音）。
    非数字/NaN -> 落回全局值，绝不把 None 传给引擎。

    P0-1 收敛：拼参数的规则（分层优先级 + 越界裁剪 + 非数值兜底）已搬到唯一真源
    `modules/voice_manager/conditioning.py::resolve_profile_conditioning`，本函数只是
    它在"句级 > 全局"两层上的一个薄封装（对外签名、返回键、数值全部不变）。
    conditioning 只依赖 numpy，不 import backend，本模块"纯函数、不碰全局状态"的
    纪律不变。

    Returns:
        {"speed": float, "pitch": float, "emotion": str}
    """
    from modules.voice_manager.conditioning import resolve_profile_conditioning

    sent = sent if isinstance(sent, dict) else {}
    cond = resolve_profile_conditioning(
        None,
        # 现状语义：emotion 只取句级 tone（P5 ToneDetector），空值落回 "neutral"
        overrides={"emotion": sent.get("tone") or "neutral"},
        layers=[sent, settings],                      # 句级 > 全局 settings
        defaults={"speed": default_speed, "pitch": default_pitch},
    )
    return {"speed": cond["speed"], "pitch": cond["pitch"], "emotion": cond["emotion"]}


def _num(value, fallback: float) -> float:
    """可选数值：非法/None/NaN/inf 一律用 fallback。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return v if np.isfinite(v) else float(fallback)


# ============================================================ P8: 混音接线
def postprocess_with_mix(ctx: Dict, mix_settings: Optional[Dict],
                         processed: Optional[List[Dict]] = None) -> Dict:
    """
    P8 混音接线（批次 C 插在 `_stage_postprocess` 末尾
    `context["audio_arrays"] = processed` 之前，现 conversion_workflow.py:825）。

    调用形状:
        mixed = postprocess_with_mix(context, project_global_settings)
        context["audio_arrays"] = mixed["audio_arrays"]
    （不传 processed 时函数自取 context["audio_arrays"]。）

    行为:
      1. mix 未配置（无 BGM/SFX、增益全 0、LUFS 未设）-> **原样返回**入参段列表，
         applied=False。modules.mixer.AudioMix 的直通保证逐样本与现状一致，
         所以现有 P8 的每段 去静音/归一/淡入淡出 结果不受影响。
      2. 配置了 mix -> 按章分桶（章内按 index 保序），每章调 AudioMix.mix 得一条成品轨，
         返回**每章一段**的 audio_arrays（保持 P9 需要的
         index/chapter_id/chapter_title/audio/duration_ms/peak/text 字段），
         并置 context["mix_applied"]=True + context["mix_stats"]（限幅/LUFS 实况）。
      3. 角色级增益：段上带 role_gain_db（P6 写的）与 settings.role_gains（面板配的）叠加，
         由 AudioMix.assemble 内部完成，本函数只负责把 VocalSegment 喂对。
      4. 句级字段从 context["sentences"] 按 audio_arrays[i]["index"] 回捞
         （P7 的 index 就是句子下标，:747-755），所以 P8 不需要 sentences 也认得角色。

    不吞异常：混音失败让批次 C 的调用点 raise（与 PY-117 "不得用静音冒充成品" 一致）。
    """
    segments = processed if processed is not None else (ctx.get("audio_arrays") or [])
    out = {"audio_arrays": segments, "applied": False, "passthrough": True, "chapters": 0}
    settings = mix_settings_of(mix_settings)

    from modules.mixer import AudioMix, VocalSegment

    if not segments or AudioMix.is_passthrough(settings):
        return out

    sr = int(ctx.get("sample_rate") or 24000)
    sentences = ctx.get("sentences") or []
    # audio_arrays 的 "index" 是句子在原列表中的下标（P7 的 enumerate），
    # 但空文本句会被 continue 跳过，所以只能按下标查、不能按位置查。
    by_index: Dict[int, Dict] = {}
    for i, s in enumerate(sentences):
        if isinstance(s, dict):
            by_index[i] = s

    mixer = AudioMix(sample_rate=sr)
    buckets: Dict[str, List] = {}
    order: List[str] = []
    for seg in segments:
        audio = seg.get("audio")
        if audio is None or len(audio) == 0:
            continue
        sent = by_index.get(int(seg.get("index", -1))) or {}
        role = str(sent.get("role_name") or seg.get("role_name") or "").strip()
        narrator = is_narrator_name(role) or str(
            sent.get("role_kind") or seg.get("role_kind") or "") == "narration"
        if not role:
            role = "旁白" if narrator else ""
        vs = VocalSegment(
            audio=np.asarray(audio, dtype=np.float32),
            role=role,
            role_id=str(sent.get("role_id") or seg.get("role_id") or ""),
            kind="narration" if narrator else "dialogue",
            gain_db=float(sent.get("role_gain_db") or seg.get("role_gain_db") or 0.0),
        )
        cid = str(seg.get("chapter_id") or "")
        if cid not in buckets:
            buckets[cid] = []
            order.append(cid)
        buckets[cid].append((seg, vs))

    mixed: List[Dict] = []
    chapter_stats: List[Dict] = []
    for i, cid in enumerate(order):
        chapter_settings = AudioMix.settings_from_project({"mix": settings}, i)
        items = buckets[cid]
        track = mixer.mix([vs for _seg, vs in items], chapter_settings)
        first = items[0][0]
        mixed.append({
            "index": int(first.get("index", len(mixed))),
            "chapter_id": first.get("chapter_id"),
            "chapter_title": first.get("chapter_title", ""),
            "audio": track,
            "duration_ms": (len(track) / float(sr)) * 1000.0 if sr else 0.0,
            "peak": float(np.max(np.abs(track))) if track.size else 0.0,
            "text": first.get("text", ""),
        })
        chapter_stats.append({"chapter_id": cid, "segments": len(items),
                              **dict(mixer.last_stats or {})})

    ctx["mix_applied"] = True
    ctx["mix_stats"] = {"chapters": len(mixed), "detail": chapter_stats}
    out.update({"audio_arrays": mixed, "applied": True, "passthrough": False,
                "chapters": len(mixed)})
    logger.info(f"[cast_hooks] postprocess_with_mix: {len(segments)} 段 -> {len(mixed)} 章成品轨")
    return out


__all__ = [
    "NARRATOR_NAMES",
    "norm_name",
    "is_narrator_name",
    "mix_settings_of",
    "cast_is_active",
    "build_alias_index",
    "normalize_speakers",
    "cast_for_assign",
    "build_role_index",
    "assign_with_cast",
    "synth_params_for_sentence",
    "postprocess_with_mix",
]
