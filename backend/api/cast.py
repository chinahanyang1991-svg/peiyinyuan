"""
多人有声书 cast / BGM / 混音 API 路由（批次 B）

设计依据: docs/superpowers/specs/2026-09-20-multi-cast-audiobook-design.md 6

挂载方式（backend/main.py 只加一行 include_router，prefix="/api"）:
    /api/dubbing/projects/{id}/analyze-cast|cast|cast/suggestions|cast/confirm|mix-settings
    /api/bgm | /api/bgm/upload | /api/bgm/{bgm_id}/preview | /api/bgm/{bgm_id} (DELETE, WF-06)

路径自带 /dubbing/... 与 /bgm 前缀，因此一个 router 即可覆盖两组端点，
不与既有 dubbing router 冲突（同前缀但路径互不重复）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import Config
from backend.services.cast_service import cast_service

logger = logging.getLogger(__name__)

router = APIRouter()

# 上传素材白名单（与 BgmStore 能力一致：ffmpeg 可转的一切格式 + soundfile 直读格式）
BGM_UPLOAD_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aiff", ".aac", ".mp4"}
# 无 ffmpeg 时 soundfile/libsndfile 仍可直读的格式
SOUNDFILE_NATIVE_SUFFIXES = {".wav", ".flac", ".aiff", ".ogg", ".au"}

_FFMPEG_PROBE: Dict[str, Any] = {"checked_at": 0.0, "path": None, "info": None}
_FFMPEG_PROBE_TTL_S = 60.0


# ============================================================ 请求模型
class AnalyzeCastRequest(BaseModel):
    book_id: Optional[str] = None
    use_llm: Optional[bool] = None


class ConfirmRoleItem(BaseModel):
    role_id: Optional[str] = None
    name: Optional[str] = None
    role_type: Optional[str] = None
    voice_id: Optional[str] = None
    aliases: Optional[List[str]] = None
    gender: Optional[str] = None
    age_group: Optional[str] = None
    identity: Optional[str] = None
    personality_tags: Optional[List[str]] = None
    mix: Optional[Dict[str, Any]] = None
    cast: Optional[Dict[str, Any]] = None


class ConfirmCastRequest(BaseModel):
    roles: List[ConfirmRoleItem] = Body(default_factory=list)
    auto_assign_rest: bool = Body(default=True)


class RolePatchRequest(BaseModel):
    name: Optional[str] = None
    role_type: Optional[str] = None
    voice_id: Optional[str] = None
    aliases: Optional[List[str]] = None
    gender: Optional[str] = None
    age_group: Optional[str] = None
    identity: Optional[str] = None
    personality_tags: Optional[List[str]] = None
    mix: Optional[Dict[str, Any]] = None


class MixSettingsRequest(BaseModel):
    mix: Optional[Dict[str, Any]] = None


class CastPreviewRequest(BaseModel):
    role_id: Optional[str] = None
    role_name: Optional[str] = None
    text: Optional[str] = None
    max_sentences: int = 2
    # L14⑧: 复听同一条采样轨迹 —— 前端把上次试听响应头 X-Last-Successful-Seed
    # (US-C4 出口) 原样回传。只有 supports_seed 的引擎能收这个字段，其他引擎收到
    # 非空 seed 一律 400（与 base_tts._reject_seed 同一口径），所以前端对不兑现
    # seed 的引擎连键都不发（services/api.ts seedForRequest）。
    seed: Optional[int] = None


# ============================================================ 小工具
def _require_project(project_id: str) -> Dict:
    cast = cast_service.get_cast(project_id)
    if not cast:
        raise HTTPException(404, "项目不存在")
    return cast


def ffmpeg_probe_info() -> Dict[str, Any]:
    """ffmpeg 探测信息 {path, source, version, error}，缓存 60s。

    WF-15 修复：旧实现 `shutil.which(Config.FFMPEG_PATH or "ffmpeg")` 只看 PATH，
    而本仓库的 ffmpeg 常态由 pip 包 imageio-ffmpeg 自带二进制提供（P8/P9、
    M4BBuilder 都走 modules/tts_engine/ffmpeg_utils 的三级兜底）。两套探测并存
    导致 /api/bgm 报 ffmpeg_available:false —— 假阴性，实测环境必现。
    现统一委派 ffmpeg_utils.probe()，并把"来源"一并上报。
    """
    now = time.time()
    cached = _FFMPEG_PROBE.get("info")
    if cached is not None and now - _FFMPEG_PROBE["checked_at"] < _FFMPEG_PROBE_TTL_S:
        return dict(cached)
    from modules.tts_engine import ffmpeg_utils

    info = ffmpeg_utils.probe()
    _FFMPEG_PROBE["info"] = dict(info)
    _FFMPEG_PROBE["path"] = info.get("path")
    _FFMPEG_PROBE["checked_at"] = now
    return dict(info)


def ffmpeg_path() -> Optional[str]:
    """可用 ffmpeg 可执行文件路径；不可用 None（与引擎侧/打包侧同一结论）。"""
    return ffmpeg_probe_info().get("path")


# ============================================================ cast 任务
@router.post("/dubbing/projects/{project_id}/analyze-cast")
async def analyze_cast(project_id: str, req: Optional[AnalyzeCastRequest] = None):
    """
    启动 cast 分析后台任务（spec 6）。返回 task.id，进度走既有
    GET/WS /api/tasks/{task_id}/progress（stage: cast_extract/cast_profile/cast_persist）。
    """
    req = req or AnalyzeCastRequest()
    try:
        started = cast_service.start_analysis(project_id, book_id=req.book_id,
                                              use_llm=req.use_llm)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    task = started["task"]
    project = started["project"]
    use_llm = bool((task.get("settings") or {}).get("use_llm", True))

    async def runner():
        try:
            result = await cast_service.run_analysis(
                task["id"], project_id, project["book_id"], use_llm=use_llm)
            logger.info(f"[Cast:{task['id']}] finished status={result.get('status')}")
        except Exception as exc:                      # pragma: no cover - 兜底日志
            logger.exception(f"[Cast:{task['id']}] runner crashed: {exc}")

    asyncio.create_task(runner())
    return {"status": "ok", "task": task, "project_id": project_id}


@router.get("/dubbing/projects/{project_id}/cast")
async def get_cast(project_id: str):
    """角色表（属性/别名/样句/戏份计数 + 分析元信息）。"""
    data = cast_service.get_cast(project_id)
    if data is None:
        raise HTTPException(404, "项目不存在")
    return {"status": "ok", **data}


@router.get("/dubbing/projects/{project_id}/cast/suggestions")
async def cast_suggestions(project_id: str, top_n: int = Query(5, ge=1, le=50),
                          language: Optional[str] = Query(None)):
    """每角色推荐音色 Top-N（打分拆解见 modules/cast_analyzer/voice_matcher.py）。"""
    _require_project(project_id)
    try:
        data = cast_service.get_suggestions(project_id, top_n=top_n, language=language)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"status": "ok", **data}


@router.put("/dubbing/projects/{project_id}/cast/confirm")
async def confirm_cast(project_id: str, req: ConfirmCastRequest):
    """落库确认：Role 属性/别名/音色 + 别名表写进 project.extra_data.cast。"""
    _require_project(project_id)
    payload = {"roles": [r.model_dump(exclude_none=True) if hasattr(r, "model_dump")
                         else r.dict(exclude_none=True) for r in (req.roles or [])],
               "auto_assign_rest": req.auto_assign_rest}
    try:
        result = cast_service.confirm_cast(project_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok", **result}


@router.put("/dubbing/projects/{project_id}/cast/roles/{role_id}")
async def patch_cast_role(project_id: str, role_id: str, req: RolePatchRequest):
    """手工修正单个角色的属性/别名/局部混音（unknown -> 用户补全）。"""
    patch = req.model_dump(exclude_none=True) if hasattr(req, "model_dump") else req.dict(
        exclude_none=True)
    try:
        role = cast_service.update_role(project_id, role_id, patch)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    if role is None:
        raise HTTPException(404, "角色不存在")
    return {"status": "ok", "role": role}


@router.delete("/dubbing/projects/{project_id}/roles/{role_id}")
async def delete_cast_role(project_id: str, role_id: str):
    """删除角色（旁白不可删；既有前端已在调这个路径，后端补齐）。"""
    _require_project(project_id)
    try:
        ok = cast_service.delete_role(project_id, role_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404, "角色不存在")
    return {"status": "ok"}


@router.post("/dubbing/projects/{project_id}/cast/preview")
async def preview_cast_role(project_id: str, req: CastPreviewRequest):
    """
    按角色试听（spec 6: 同一引擎小样本合成 <=2 句）。
    引擎未加载时给 503 + 明确指引，绝不返回静音冒充成品。
    """
    cast = _require_project(project_id)
    roles = cast.get("roles") or []
    role = None
    if req.role_id:
        role = next((r for r in roles if r.get("id") == req.role_id), None)
    if role is None and req.role_name:
        role = next((r for r in roles
                     if (r.get("name") or "").strip() == req.role_name.strip()), None)
    if role is None:
        role = next((r for r in roles if r.get("voice_id")), None)
    if role is None:
        raise HTTPException(400, "未找到角色，请先分析角色或为该角色分配音色")
    voice_id = role.get("voice_id")
    if not voice_id:
        raise HTTPException(400, f"角色 '{role.get('name')}' 尚未分配音色，无法试听")

    text = (req.text or "").strip()
    if not text:
        quotes = ((role.get("cast") or {}).get("sample_quotes")
                  or (role.get("extra_data") or {}).get("sample_quotes") or [])
        text = " ".join(list(quotes)[:max(1, req.max_sentences)]) or \
            "这是一段角色试听样本，用于确认音色是否贴合人物。"

    from modules.tts_engine.engine_registry import EngineLoadError, engine_registry
    from modules.voice_manager.language_tier import openvoice_tier_gate
    from modules.voice_manager.voice_store import voice_store

    engine_name = Config.TTS_ENGINE or "voxcpm"
    # PY-323(a′): 本接口把 voice_id 的**参考音频**喂给引擎，所以"全局引擎恰好是
    # openvoice + 该行是非普通话参考"这一格，失效模式与整书 P5 完全相同（英文参考
    # 说中文，字准 0.500-0.917）。降级必须显式记日志：换引擎=换嗓子。
    engine_name, tier_reason = openvoice_tier_gate(
        engine_name, voice_store.get(voice_id))
    if tier_reason:
        logger.warning(f"[cast] 试听语种档降级 {voice_id[:8]}: {tier_reason}")
    # C4-5: 这里必须是非阻塞的状态判断。旧代码用 engine_registry.get_loaded()，
    # 会在请求线程里内联加载模型（本机实测 203.0s），前端/脚本一律超时；
    # 且加载失败抛的 EngineLoadError 会冒成 500。预热由 model_preloader(PY-115)
    # 与转换任务负责，试听只读状态、绝不触发装载。
    if not engine_registry.is_loaded(engine_name):
        # 指令#1: 上面那条"绝不在请求内等加载"的门是为**本地权重**立的（本机实测
        # 203.0s 内联装载）。在线引擎（edgetts 等）的"加载"只是一次 import，毫秒级，
        # 所以不能因为它没被提前"预热"过就把在线档的角色试听判死 —— 那是切换档位
        # 后新出现的假故障。C4-5 的原意（不在请求里烧模型冷启动）保持不变。
        from modules.tts_engine import tier as tts_tier
        if not tts_tier.is_online_engine(engine_name):
            try:
                from modules.tts_engine.model_preloader import model_preloader
                model_state = model_preloader.snapshot()
            except Exception as exc:                          # pragma: no cover - 防御
                model_state = {"state": "unknown", "message": str(exc)}
            raise HTTPException(503, {
                "reason": f"TTS 模型 '{engine_name}' 尚未装载，试听不在请求内等待加载",
                "model_status": model_state,
                "next": "内存低于预热门限时启动预热会自动跳过：可查询 "
                        "GET /api/system/model_status，或先发起一次转换任务触发加载后重试",
            })
    try:
        engine = engine_registry.get_loaded(engine_name)
    except EngineLoadError as exc:
        raise HTTPException(503, f"模型装载失败，无法试听: {exc}")
    if engine is None:
        raise HTTPException(503, "TTS 引擎不可用（依赖未安装），无法试听")

    from modules.voice_manager.audio_quality import (AudioQualityError,
                                                     synthesize_with_gate)
    from modules.voice_manager.conditioning import resolve_profile_conditioning
    from backend.services.voice_service import (make_preview_seed,
                                                record_last_successful_seed)

    role_mix = role.get("mix") or {}
    # P0-1: 拼参数只在 conditioning.py 一处实现。此处只有"角色 mix"一层有效
    # （旧写法把 role.mix 塞进句级 sent、把 None 塞进全局 settings 来借道 P7
    # helper，等价于下面这一句），speed/pitch 值域与裁剪行为完全不变，
    # emotion 仍旧固定 neutral —— 角色试听不参与 P5 语气检测。
    cond = resolve_profile_conditioning(
        None,
        overrides={"emotion": "neutral"},
        layers=[role_mix],
        defaults={"speed": 1.0, "pitch": 0.0},
    )
    # US-C4/L14⑤: 与 dubbing 试听同一口径的 seed 生产者 —— 引擎兑现 seed 才播种
    # （不兑现的引擎收到 seed 会显式 400），产物过了质量门就把实际生效的 seed
    # 落到 VoiceProfile.seed，并由响应头 X-Last-Successful-Seed 回传给调用方。
    if req.seed is not None and not getattr(engine, "supports_seed", False):
        # L14⑧: 与 base_tts._reject_seed 同一口径 —— 不兑现 seed 的引擎收到 seed
        # 是"调用方写错了"，必须响亮拒绝，而不是静默忽略后给一条无法复现的产物。
        raise HTTPException(400, {
            "reason": f"引擎 '{engine_name}' 不支持 seed 参数"
                      f"（supports_seed=False，合成结果与 seed 无关）",
            "next": "请去掉请求体里的 seed 字段；seed 仅对 voxcpm / openvoice 有效",
        })
    seed_base = req.seed if req.seed is not None else make_preview_seed(engine)
    try:
        # P0-4: 合成 -> 双门（-50 dBFS 静音地板 + 谱平坦度退化音）
        #      -> 未过门换 seed 再抽一次 -> 两次都不过才报错（不返回坏音频）
        outcome = await asyncio.to_thread(
            lambda: synthesize_with_gate(
                engine,
                text=text[:600],
                voice_id=voice_id,
                speed=cond["speed"],
                pitch=cond["pitch"],
                emotion=cond["emotion"],
                seed=seed_base,
            ))
    except AudioQualityError as exc:
        logger.warning(f"[cast] 试听产物未过质量门: {exc.report}")
        raise HTTPException(502, f"试听音频未通过质量门: {exc.report['reason']}")
    except Exception as exc:
        logger.warning(f"[cast] 试听失败: {exc}")
        raise HTTPException(500, f"试听合成失败: {exc}")
    audio = outcome.audio
    if outcome.seed is not None and voice_id:
        record_last_successful_seed(voice_id, outcome.seed)

    import numpy as np
    import soundfile as sf
    from starlette.background import BackgroundTask

    arr = np.asarray(audio, dtype=np.float32)
    tmp = tempfile.NamedTemporaryFile(prefix="cast_preview_", suffix=".wav", delete=False)
    tmp.close()
    headers = {"Content-Disposition": "inline"}
    if outcome.seed is not None:
        headers["X-Last-Successful-Seed"] = str(outcome.seed)
    if tier_reason:
        # 与 /api/voices/{id}/preview 同一词汇（voices.py 从 meta.voice_binding 取值），
        # 前端据此标注"这一耳朵听到的不是该音色原引擎的嗓子"。只发 ASCII 令牌，
        # 中文原因留在日志与 meta 里，HTTP 头塞中文会炸。
        headers["X-Preview-Voice-Binding"] = "tier_downgrade"
    try:
        sf.write(tmp.name, arr, int(getattr(engine, "sample_rate", 48000)))
        return FileResponse(tmp.name, media_type="audio/wav",
                            headers=headers,
                            background=BackgroundTask(_unlink_quiet, tmp.name))
    except Exception as exc:
        _unlink_quiet(tmp.name)
        raise HTTPException(500, f"试听音频写入失败: {exc}")


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ============================================================ mix settings
@router.get("/dubbing/projects/{project_id}/mix-settings")
async def get_mix_settings(project_id: str):
    """读取 mix 配置（project.global_settings.mix）+ 章节生效视图 + 是否直通。"""
    data = cast_service.get_mix_settings(project_id)
    if data is None:
        raise HTTPException(404, "项目不存在")
    return {"status": "ok", **data}


@router.put("/dubbing/projects/{project_id}/mix-settings")
async def put_mix_settings(project_id: str, req: MixSettingsRequest):
    """写入 mix 配置；schema 校验失败给 400 + 逐条原因（modules/mixer 的 validate_settings）。"""
    _require_project(project_id)
    try:
        result = cast_service.update_mix_settings(project_id, req.mix)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok", **result}


# ============================================================ BGM 库
@router.get("/bgm")
async def list_bgm(mood: Optional[str] = Query(None)):
    """BGM/音效素材列表（内置占位素材首访时生成；含智能体4 resource.db 外部音效）。"""
    from modules.mixer import BgmStore, LIBRARY_SR

    store = BgmStore()
    ensured = store.ensure_builtin()
    bgm = store.list_bgm(mood=mood)
    sfx = store.list_sfx()
    external = store.external_sfx()
    ffmpeg = ffmpeg_path()
    ffprobe = ffmpeg_probe_info()
    return {
        "status": "ok",
        "bgm": bgm,
        "sfx": sfx,
        "external_sfx": external,
        "total": len(bgm) + len(sfx),
        "library_sr": int(LIBRARY_SR),
        "ffmpeg_available": bool(ffmpeg),
        "ffmpeg_path": ffmpeg,
        # WF-15: 上报"哪来的 ffmpeg"，避免只留一个 bool 又被质疑是假阴性
        "ffmpeg_source": ffprobe.get("source"),
        "ffmpeg_version": ffprobe.get("version"),
        "ffmpeg_error": ffprobe.get("error"),
        "builtin_created": ensured.get("created", []),
        "bgm_dir": ensured.get("bgm_dir"),
    }


@router.post("/bgm/upload")
async def upload_bgm(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    kind: str = Form("bgm"),
    moods: Optional[str] = Form(None),
    loopable: Optional[str] = Form(None),
):
    """
    用户上传背景音乐/音效：入库 data/bgm/（或 data/bgm/sfx/）。
    统一 ffmpeg 转 wav 48k 立体声（spec 4.2）；ffmpeg 不可用且格式非 libsndfile
    直读 -> 明确 4xx，不静默降质。
    """
    from modules.mixer import BgmStore

    if kind not in ("bgm", "sfx"):
        raise HTTPException(400, "kind 只能是 bgm 或 sfx")
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if not filename:
        raise HTTPException(400, "缺少文件名，无法判断音频格式")
    if suffix not in BGM_UPLOAD_SUFFIXES:
        raise HTTPException(
            415, f"不支持的音频格式 {suffix or '(无扩展名)'}，可上传: "
                 + ", ".join(sorted(BGM_UPLOAD_SUFFIXES)))

    ffmpeg = ffmpeg_path()
    if not ffmpeg and suffix not in SOUNDFILE_NATIVE_SUFFIXES:
        raise HTTPException(
            415, f"服务器未安装 ffmpeg，无法把 {suffix} 转成 48kHz wav。"
                 f"请安装 ffmpeg（https://ffmpeg.org/download.html）或先转成 wav/flac 再上传")

    max_bytes = Config.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    tmp_dir = Path(tempfile.mkdtemp(prefix="bgm_upload_"))
    tmp_file = tmp_dir / f"upload{suffix}"
    written = 0
    try:
        with open(tmp_file, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        413, f"素材过大: 已读 {written} 字节，超过上限 {max_bytes} 字节 "
                             f"(MAX_UPLOAD_SIZE_MB={Config.MAX_UPLOAD_SIZE_MB})")
                fh.write(chunk)
        if written == 0:
            raise HTTPException(400, "上传文件为空")

        mood_list = [m.strip() for m in (moods or "").replace("，", ",").split(",") if m.strip()]
        store = BgmStore()
        loop = None
        if loopable is not None:
            loop = str(loopable).strip().lower() in ("1", "true", "yes", "on")
        try:
            record = await asyncio.to_thread(
                store.add_audio_file, tmp_file, name or Path(filename).stem,
                mood_list, kind, loop)
        except (RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(422, f"素材入库失败: {exc}")
        if not record.get("id"):
            raise HTTPException(500, "素材入库返回异常：缺少 id")
        return {"status": "ok", "bgm": record,
                "converter": record.get("source"),
                "ffmpeg_available": bool(ffmpeg),
                "bgm_dir": str(store.bgm_dir)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.delete("/bgm/{bgm_id}")
async def delete_bgm(bgm_id: str):
    """删除库内素材（删记录 + 删文件）—— 补齐 WF-06「曲库只增不减」。

    - 找不到 id -> 404
    - 内置占位素材 -> 403 拒删：`GET /api/bgm` 每次都跑 `ensure_builtin()`，
      文件缺了就按 numpy 规格重新生成，删记录只是把同一份占位素材藏一次，
      因此这类素材必须不可删（前端据此隐藏删除按钮）。
    - 用户上传素材 -> 从 meta.json 的 bgm/sfx 桶移除，并删掉转码后的 wav。

    BgmStore 没有 delete 方法且 `modules/mixer/` 不在本次可改清单内，
    这里用它公开的 load_meta()/save_meta() 做读-改-写（原子替换由 save_meta 内部
    的 tmp+replace 保证）。
    """
    from modules.mixer import BgmStore
    from modules.mixer.bgm_store import BUILTIN_BGM, BUILTIN_SFX

    builtin_ids = {s["id"] for s in BUILTIN_BGM} | {s["id"] for s in BUILTIN_SFX}
    store = BgmStore()
    meta = store.load_meta()

    bucket, record = None, None
    for name in ("bgm", "sfx"):
        for row in meta.get(name) or []:
            if row.get("id") == bgm_id:
                bucket, record = name, row
                break
        if record:
            break
    if record is None:
        raise HTTPException(404, f"素材不存在: {bgm_id}")
    if bgm_id in builtin_ids or record.get("placeholder"):
        raise HTTPException(
            403, f"内置占位素材不可删除（曲库自带，删除后会在下次列表时重建）: {bgm_id}")

    # 文件路径来自 meta 记录，仍校验必须落在 bgm_dir 内（防被改坏的索引指向任意文件）
    removed_file: Optional[str] = None
    file_failed = False
    rel = record.get("file")
    root = store.bgm_dir.resolve()
    if rel:
        target = (store.bgm_dir / str(rel)).resolve()
        if str(target).startswith(str(root)):
            if target.exists():
                try:
                    target.unlink()
                    removed_file = str(target)
                except OSError as exc:
                    file_failed = True
                    logger.warning("[WF-06] 素材文件删除失败(记录仍移除): %s %s", target, exc)
        else:
            logger.warning("[WF-06] 素材文件路径越出库目录，跳过删除: %s", rel)

    meta[bucket] = [r for r in (meta.get(bucket) or []) if r.get("id") != bgm_id]
    store.save_meta(meta)
    logger.info("[WF-06] 已删除素材 %s (桶=%s, 文件=%s)", bgm_id, bucket, removed_file)

    return {
        "status": "ok",
        "deleted": bgm_id,
        "kind": bucket,
        "name": record.get("name"),
        "file_removed": bool(removed_file),
        "file_path": removed_file,
        "file_remove_failed": file_failed,
        "remaining": {k: len(meta.get(k) or []) for k in ("bgm", "sfx")},
    }


@router.get("/bgm/{bgm_id}/preview")
async def preview_bgm(bgm_id: str):
    """试听库内素材（wav 直发）。"""
    from modules.mixer import BgmStore

    store = BgmStore()
    path = store.resolve(bgm_id)
    if path is None or not Path(path).exists():
        raise HTTPException(404, f"素材不存在: {bgm_id}")
    return FileResponse(str(path), media_type="audio/wav")
