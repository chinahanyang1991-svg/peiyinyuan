"""
工作流 API 路由
处理工作流模板 CRUD、自定义转换流程
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from modules.workflow.workflow_service import workflow_template_service

router = APIRouter()

# CPU 增量口径需要一个起点：在模块装载时 priming 一次，
# 这样第一枚 /system/status 请求报的是"自进程启动以来"的占用，而不是 0.0。
try:
    import psutil as _psutil

    _psutil.cpu_percent(interval=None)
    _CPU_PRIMED = True
except Exception:  # psutil 缺失时不拖垮路由装载，交给端点里的 unavailable 出口
    _CPU_PRIMED = False


class CreateTemplateRequest(BaseModel):
    name: str
    description: str = ""
    stages: Optional[list] = None
    # 指令#1：空串 = 模板不声明引擎，运行时由当前档位定默认嗓。
    # 这里原来写死 "voxcpm"，于是"建模板"这条路会把用户钉在本地档（5GB + 51×耗时）。
    engine: str = ""
    output_format: str = "m4b"
    settings: dict = {}


class UpdateTemplateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    stages: Optional[list] = None
    engine: Optional[str] = None
    output_format: Optional[str] = None
    settings: Optional[dict] = None


@router.get("/templates")
async def list_templates(limit: int = Query(50)):
    templates = workflow_template_service.list_all(limit)
    return {"status": "ok", "templates": templates, "total": len(templates)}


@router.post("/templates")
async def create_template(req: CreateTemplateRequest):
    template = workflow_template_service.create(
        name=req.name, description=req.description,
        stages=req.stages, engine=req.engine,
        output_format=req.output_format, settings=req.settings,
    )
    return {"status": "ok", "template": template}


class RunTemplateRequest(BaseModel):
    book_id: str
    project_id: Optional[str] = None
    output_format: Optional[str] = None
    settings: dict = {}


@router.post("/templates/{template_id}/run")
async def run_template(template_id: str, req: RunTemplateRequest):
    """用模板起一枚真实转换任务 (U-5)。

    刻意**复用** `POST /api/tasks/convert` 那条生产路径（格式白名单、ffmpeg 可用性
    预检、create_task、后台 asyncio 执行都在里面），而不是抄一份——两份实现会漂，
    漂了以后"模板跑出来的书"和"手动跑出来的书"行为不一致，排查成本全给用户。
    模板的 settings 是底座，调用方 settings 覆盖其上；engine 同样按此优先级。
    """
    from backend.api.tasks import StartConversionRequest, start_conversion

    template = workflow_template_service.get(template_id)
    if not template:
        raise HTTPException(404, f"模板不存在: {template_id}")
    book_id = (req.book_id or "").strip()
    if not book_id:
        # U-6 同一条线：不能"200 + 什么都没发生"，缺输入就在状态码层面可见。
        raise HTTPException(422, "缺少 book_id —— 模板只是参数组合，书必须显式给")

    settings = {**(template.get("settings") or {}), **(req.settings or {})}
    # 指令#1: 这里原来补的是字面量 "voxcpm" —— 自建模板（engine 列留空）因此永远被
    # 判成本地档，前端的模式切换在这条路上根本到不了。现在模板没声明引擎就**不补**，
    # 让 `settings.engine` 缺位、由档位真值 (modules/tts_engine/tier.py) 决定。
    # 三枚官方模板各自显式声明引擎，行为一字不变。
    tpl_engine = (template.get("engine") or "").strip().lower()
    if tpl_engine:
        settings.setdefault("engine", tpl_engine)
    resp = await start_conversion(StartConversionRequest(
        project_id=req.project_id,
        book_id=book_id,
        output_format=(req.output_format or template.get("output_format") or "m4b"),
        settings=settings,
    ))
    # `engine: null` 不是"没有引擎"，是"由档位定"（P7 与这里同源：tier.default_engine）。
    # 直接把生效值报出来，省得前端再猜一遍。
    from modules.tts_engine import tier as tts_tier
    effective_engine = (settings.get("engine")
                        or tts_tier.default_engine(tts_tier.resolve_tier(
                            settings.get("tier"))))
    return {
        "status": "ok",
        "task": resp.get("task"),
        "template": {"id": template["id"], "name": template["name"]},
        "used": {"engine": effective_engine,
                 "engine_from_template": bool(tpl_engine or settings.get("engine")),
                 "tier": tts_tier.resolve_tier(settings.get("tier")),
                 "output_format": resp.get("task", {}).get("output_format"),
                 "settings": settings},
    }


@router.get("/system/status")
async def system_status():
    """系统状态端点。返回CPU/GPU状态、模型加载状态、ffmpeg可用性等。

    PY-340：本端点原先在 `async def` 里同步跑 `psutil.cpu_percent(interval=1)`，
    每次调用把**整个事件循环**冻 1s（实测 /api/health 从 8ms 被拖到 1748ms，
    三枚并发串行成 1028/2049/3069ms）。现两处改动：
      ① 一切可能阻塞的采集搬进线程池，CPU 改用非阻塞增量口径；
      ② 采集不到不再写成 0/False（那会把"没测到"冒充成"测到了"），
         而是给 `unavailable` + 原因——与 PY-117「不得用静默冒充成功」同一条线。
    """
    return await run_in_threadpool(_collect_system_status)


# psutil 的 interval=None 口径是"距上一次调用的增量"，故必须先 priming 一次，
# 否则进程启动后第一枚请求只能拿到 0.0 —— 又是一个假值。
_CPU_PRIMED = False


def _cpu_percent_nonblocking(psutil) -> float:
    global _CPU_PRIMED
    if not _CPU_PRIMED:
        psutil.cpu_percent(interval=None)
        _CPU_PRIMED = True
    return float(psutil.cpu_percent(interval=None))


def _collect_system_status() -> dict:
    import platform
    import sys
    from backend.config import Config

    hardware: dict = {}
    try:
        import psutil
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Config.DATA_DIR))
        hardware = {
            "cpu_usage": _cpu_percent_nonblocking(psutil),
            "memory_usage": memory.percent,
            "memory_total_gb": memory.total / (1024 ** 3),
            "disk_usage": disk.percent,
            "disk_total_gb": disk.total / (1024 ** 3),
            "unavailable": None,
        }
    except Exception as e:
        hardware = {"unavailable": f"{type(e).__name__}: {e}"}

    gpu_available = Config.is_gpu_available()
    gpu_usage = 0
    if gpu_available:
        try:
            import torch
            gpu_usage = torch.cuda.utilization()
        except Exception:
            gpu_usage = 0
    hardware["gpu_available"] = gpu_available
    hardware["gpu_usage"] = gpu_usage

    # ffmpeg：复用 cast 那份 60s 缓存的三级兜底探测（PATH / 配置路径 / imageio-ffmpeg
    # 自带二进制）。自跑 `ffmpeg -version` 只看 PATH 是 WF-15 已经定性的假阴性，不再重蹈。
    try:
        from backend.api.cast import ffmpeg_probe_info
        ffmpeg_info = ffmpeg_probe_info()
    except Exception as e:
        ffmpeg_info = {"error": f"{type(e).__name__}: {e}", "version": None}

    # 模型加载状态：model_preloader 的槽位快照才是真值。旧实现 import 了一个
    # 不存在的 `modules.tts_engine.engines`，必然抛错并被裸 except 吞掉，
    # 于是 tts_model_loaded 永远报 False。
    try:
        from modules.tts_engine.model_preloader import model_preloader
        model_snapshot = model_preloader.snapshot(Config.TTS_ENGINE)
        tts_model_loaded = model_snapshot.get("state") == "ready"
        model_state = model_snapshot.get("state")
        model_reason = model_snapshot.get("error") or model_snapshot.get("skipped_reason")
    except Exception as e:
        tts_model_loaded = False
        model_state = "unavailable"
        model_reason = f"{type(e).__name__}: {e}"

    # 任务计数：status 的真实枚举里没有 "waiting"，旧值恒等于 0。
    try:
        from backend.services.task_service import task_service
        running_tasks = task_service.count_tasks(status="running")
        waiting_tasks = task_service.count_tasks(status="queued")
        task_error = None
    except Exception as e:
        running_tasks = 0
        waiting_tasks = 0
        task_error = f"{type(e).__name__}: {e}"

    return {
        "status": "ok",
        "hardware": hardware,
        "services": {
            "tts_model_loaded": tts_model_loaded,
            "tts_model_state": model_state,
            "tts_model_reason": model_reason,
            "ffmpeg_available": bool(ffmpeg_info.get("version")),
            "ffmpeg_source": ffmpeg_info.get("source"),
            "ffmpeg_error": ffmpeg_info.get("error"),
            "running_tasks": running_tasks,
            "waiting_tasks": waiting_tasks,
            "task_count_error": task_error,
        },
        "version": {
            "system": platform.system(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            # 旧值硬编码 "voxcpm"，换引擎后依旧那么报。取 Config 真值。
            "tts_engine": Config.TTS_ENGINE,
            # 指令#1: 档位决定 `tts_model_loaded` 该怎么读 —— 在线档根本没有本地权重，
            # False 是"不需要"，不是"没装好"。少了这个键，两者在响应里长得一模一样。
            "tts_tier": getattr(Config, "TTS_TIER", None),
        }
    }


@router.get("/templates/{template_id}")
async def get_template(template_id: str):
    t = workflow_template_service.get(template_id)
    if not t:
        raise HTTPException(404, "模板不存在")
    return {"status": "ok", "template": t}


@router.put("/templates/{template_id}")
async def update_template(template_id: str, req: UpdateTemplateRequest):
    data = {k: v for k, v in req.dict().items() if v is not None}
    if not data:
        raise HTTPException(400, "无更新字段")
    ok = workflow_template_service.update(template_id, **data)
    if not ok:
        raise HTTPException(404, "模板不存在")
    return {"status": "ok"}


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    ok = workflow_template_service.delete(template_id)
    if not ok:
        raise HTTPException(404, "模板不存在")
    return {"status": "ok"}


@router.get("/")
async def list_workflows(limit: int = Query(50)):
    """获取工作流列表（兼容旧API 路径）。

    WF-17 修复：旧实现 `return {"workflows": []}` 是恒空桩 —— 前端/脚本拿到
    200 + 空表，无法区分"没有模板"和"这个端点没实现"。本仓库的工作流实体就是
    WorkflowTemplate（无第二套对象），故直接返回真实模板列表，
    并同时以 `templates` 键给出（与 GET /templates 同构）。
    """
    templates = workflow_template_service.list_all(limit)
    return {
        "status": "ok",
        "workflows": templates,
        "templates": templates,
        "total": len(templates),
        "canonical": "/api/workflows/templates",
        "create_via": "POST /api/workflows/templates",
    }


@router.post("/create")
async def create_workflow():
    """历史别名：本端点从未落库过任何数据，故明确拒绝 (WF-17)。

    旧实现固定返回 `{"status": "ok", "message": "请使用 POST ..."}` ——
    调用方以为创建成功（HTTP 200），实际什么都没写。改成 405 + Allow，
    让"此路不通"在状态码层面就是可发现的。
    已核对前端：`frontend/src` 只调用 `/workflows/templates`，无消费方。
    """
    raise HTTPException(
        status_code=405,
        detail="工作流即模板：请使用 POST /api/workflows/templates 创建"
               "（/api/workflows/create 为历史别名，从未落库）",
        headers={"Allow": "POST /api/workflows/templates"})