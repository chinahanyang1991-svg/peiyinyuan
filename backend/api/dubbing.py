"""
配音编排 API 路由
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from backend.services.dubbing_service import dubbing_service

router = APIRouter()


class CreateProjectRequest(BaseModel):
    name: str
    book_id: str = None
    mode: str = "single"  # single / multi / mixed


class AddRoleRequest(BaseModel):
    name: str
    role_type: str = "custom"


class AssignVoiceRequest(BaseModel):
    role_id: str
    voice_id: str


class UpdateAssignmentsRequest(BaseModel):
    assignments: list


class UpdateSettingsRequest(BaseModel):
    settings: dict


@router.post("/projects")
async def create_project(req: CreateProjectRequest):
    """创建配音项目"""
    project = dubbing_service.create_project(
        name=req.name, book_id=req.book_id, mode=req.mode,
    )
    return {"status": "ok", "project": project}


@router.get("/projects")
async def list_projects():
    """获取配音项目列表"""
    projects = dubbing_service.list_projects()
    return {"status": "ok", "projects": projects, "total": len(projects)}


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    """获取配音项目详情"""
    project = dubbing_service.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    return {"status": "ok", "project": project}


@router.get("/projects/{project_id}/roles")
async def list_roles(project_id: str):
    """获取项目角色列表"""
    project = dubbing_service.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"status": "ok", "roles": project.get("roles", [])}


@router.post("/projects/{project_id}/roles")
async def add_role(project_id: str, req: AddRoleRequest):
    """添加角色"""
    role = dubbing_service.add_role(project_id, req.name, req.role_type)
    return {"status": "ok", "role": role}


@router.put("/projects/{project_id}/roles/assign-voice")
async def assign_voice_to_role(project_id: str, req: AssignVoiceRequest):
    """为角色分配音色"""
    success = dubbing_service.assign_voice_to_role(
        project_id, req.role_id, req.voice_id,
    )
    if not success:
        raise HTTPException(404, "角色不存在")
    return {"status": "ok"}


@router.put("/projects/{project_id}/assignments")
async def update_voice_assignments(project_id: str, req: UpdateAssignmentsRequest):
    """更新音色分配"""
    dubbing_service.update_voice_assignments(project_id, req.assignments)
    return {"status": "ok"}


@router.put("/projects/{project_id}/settings")
async def update_global_settings(project_id: str, req: UpdateSettingsRequest):
    """更新全局配音设置（语速、语调、情感）"""
    success = dubbing_service.update_global_settings(project_id, req.settings)
    if not success:
        raise HTTPException(404, "项目不存在")
    return {"status": "ok"}


def _render_preview(project_id: str, voice_id: str, settings: dict) -> tuple:
    """同步渲染一枚试听 wav：取引擎 -> 解析条件 -> 合成过质量门 -> 记 seed -> 写盘。

    U-8: 这一整段是请求路径上最长的一次 CPU/IO 阻塞（模型推理 + 双门测量 +
    soundfile 写盘 + 一次 SQLite 写），旧实现直接在 `async def` 里跑完，于是
    试听期间 `/api/health` 这类轻量端点跟着卡住（红线①）。整体搬到线程里执行，
    抛出的 HTTPException 由路由原样透传，503/502/500 三档口径一字未改。

    返回 `(wav 路径, 实际生效的 seed)`。
    """
    import tempfile

    import soundfile as sf

    from modules.tts_engine.engine_registry import EngineLoadError, engine_registry
    from modules.voice_manager.audio_quality import (AudioQualityError,
                                                     synthesize_with_gate)
    from modules.voice_manager.conditioning import \
        resolve_profile_conditioning
    from modules.voice_manager.voice_store import voice_store

    # PY-118: get_loaded 加载失败会上抛 EngineLoadError(不再返回半死引擎)，
    # 这里翻译成面向用户的 503，避免裸异常变成 500。
    # 指令#1: 用哪台引擎发声跟着项目的**档位/引擎**走。原来这里写死 voxcpm ——
    # 于是"切到在线档"的项目点一次试听仍会把 5GB 本地模型装起来，切换在这条路径上
    # 等于没生效。503 的两行文案一字不动（`qa/harness/run_u8_mutation.py` M13/M14 认它）。
    from modules.tts_engine import tier as tts_tier
    want_engine = ((settings or {}).get("engine") or "").strip().lower()
    if not want_engine:
        want_engine = tts_tier.default_engine(
            tts_tier.resolve_tier((settings or {}).get("tier")))
    try:
        engine = engine_registry.get_loaded(want_engine)
    except EngineLoadError as e:
        raise HTTPException(503, f"TTS 引擎模型加载失败：{e}")
    if engine is None:
        raise HTTPException(503, "TTS引擎未加载，请先启动模型服务。可在系统中点击「开始转换」触发模型加载")

    from backend.services.voice_service import (make_preview_seed,
                                                record_last_successful_seed)

    test_text = "这是一段配音测试音频，用于试听音色效果。"

    # P0-1: "读 profile 拼合成参数"只在 conditioning.py 一处实现。
    # 优先级 = 音色行（含 meta.speed/meta.pitch/meta.emotion） > 项目全局
    # settings > 内置默认；speed/pitch 越界只裁剪不抛错。
    # 与旧写法的两处刻意差异（见 conditioning 模块 docstring 评审记录）：
    #   1) speed 现在夹进 [0.5, 2.0]（旧代码原样透传，None/字符串也照传）；
    #   2) pitch 不再被无视（旧代码连传都不传，与 cast 试听/P7 主链路不一致）。
    cond = resolve_profile_conditioning(
        voice_store.get(voice_id),
        layers=[settings],
        defaults={"speed": 1.0, "pitch": 0.0, "emotion": "neutral"},
    )

    # P0-4: 合成 -> 双门（-50 dBFS 静音地板 + 分帧谱平坦度退化音）
    #      -> 未过门换 seed 再抽一次 -> 两次都不过才报错。
    #
    # US-C4/L14⑤ seed 生产者：试听**过了质量门**就是"该音色最近一次成功合成"，
    # 于是把实际生效的 seed 落到 VoiceProfile.seed —— 此前全链路没人调
    # `record_last_successful_seed()`，生产库 seed 列非空 0 行，
    # `GET /api/voices/{id}` 的 seed 键与试听响应头 X-Last-Successful-Seed
    # 恒为"无值"（无值不发是对的，但一直无值就是没接线）。
    # 只对 `supports_seed=True` 的引擎播种：kokoro/edgetts/sample/gptsovits
    # 收到 seed 会**显式 400**（base_tts._reject_seed），所以这里连字段都不发。
    seed_base = make_preview_seed(engine)
    try:
        outcome = synthesize_with_gate(
            engine,
            text=test_text,
            voice_id=voice_id,
            speed=cond["speed"],
            pitch=cond["pitch"],
            emotion=cond["emotion"],
            seed=seed_base,
        )
    except AudioQualityError as qe:
        raise HTTPException(502, f"试听音频未通过质量门: {qe.report['reason']}")

    # 落库是 best-effort（record_last_successful_seed 内部吞异常返回 False）：
    # 记不上 seed 不该让一条已经过门的试听失败。
    record_last_successful_seed(voice_id, outcome.seed)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()   # 句柄不关：Windows 上写盘失败时 `os.unlink` 先抛「文件被占用」，把真因整个吞掉
    try:
        sf.write(tmp_path, outcome.audio, engine.sample_rate)
    except BaseException:
        # 旧写法是裸 `except:` —— 语义上等价于 BaseException，但读起来像漏写；
        # 显式写出来，同时保住"写盘失败不留半截 wav"这条原行为。
        os.unlink(tmp_path)
        raise
    return tmp_path, outcome.seed


@router.post("/projects/{project_id}/preview")
async def preview_dubbing(project_id: str):
    """配音预览（快速合成片段试听）

    U-8: 模型推理与写盘整体 `asyncio.to_thread`，不再按住事件循环。
    """
    project = dubbing_service.get_project(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    # 获取第一个分配的音色
    voice_id = None
    settings = project.get("global_settings", {})

    if project.get("assignments"):
        voice_id = project["assignments"][0].get("voice_id")
    elif project.get("roles"):
        for role in project["roles"]:
            if role.get("voice_id"):
                voice_id = role["voice_id"]
                break

    if not voice_id:
        raise HTTPException(400, "请先为角色分配音色后再试听")

    try:
        wav_path, seed_used = await asyncio.to_thread(
            _render_preview, project_id, voice_id, settings)
        headers = {"Content-Disposition": "inline"}
        if seed_used is not None:
            headers["X-Last-Successful-Seed"] = str(seed_used)
        return FileResponse(wav_path, media_type="audio/wav", headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"预览生成失败: {str(e)}")


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """删除配音项目"""
    if dubbing_service.delete_project(project_id):
        return {"status": "ok"}
    raise HTTPException(404, "项目不存在")