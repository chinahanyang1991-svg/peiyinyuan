"""
配音员 - 下一代电子书转有声书整合工具
FastAPI 后端服务主入口

修复 (P1):
- CORS: 从 allow_origins=["*"] 改为可配置的白名单，避免浏览器拒绝凭证请求
- 日志: 使用 Python logging 统一配置，支持 DEBUG/INFO/WARNING 分级
- 启动事件: 使用 FastAPI 0.100+ lifespan 风格（保留旧版 @app.on_event 兼容性）
"""

import faulthandler
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend import runtime_guard

# -------- 日志（在任何模块被 import 之前配置） --------
_LOG_LEVEL = os.getenv("PYY_LOG_LEVEL", "INFO").upper()
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

# PY-348：旧行为是 `PYY_LOG_FILE` 不设就只写 stderr —— 实例死掉时 stderr 早已随控制台
# 消失，盘上连一行 traceback 都没有（PY-346 因此只能定罪到"不可判"）。现在默认落文件。
_GUARD_LOG_PATH = runtime_guard.resolve_log_file()
_handlers = [logging.StreamHandler(sys.stderr)]
if _GUARD_LOG_PATH:
    try:
        _GUARD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.FileHandler(str(_GUARD_LOG_PATH), encoding="utf-8")
        _handlers.append(_file_handler)
        faulthandler.enable(file=_file_handler.stream, all_threads=True)
        print(f"[配音员] 日志同时写入 {_GUARD_LOG_PATH}", file=sys.stderr)
    except Exception as e:
        print(f"[warning] cannot write to log file: {e}", file=sys.stderr)

logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format=_FORMAT,
    datefmt=_DATEFMT,
    handlers=_handlers,
    force=True,
)

# 静默第三方噪日志
for _name in ("uvicorn", "uvicorn.access", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

logger = logging.getLogger("peiyinyuan")

# 创建 FastAPI 应用实例
app = FastAPI(
    title="配音员 API",
    description="下一代电子书转有声书整合工具 - 后端服务",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)


@app.on_event("startup")
async def startup_event():
    """应用启动时初始化数据库和配置。"""
    from backend.config import Config
    from backend.models.base import init_db

    # 确保数据目录存在
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化数据库
    added_columns = init_db() or []
    if added_columns:
        # F-R4-3: 轻量列迁移留痕（老库文件上缺的列在这里被补齐）
        logger.info(
            f"[配音员][DB] 补齐缺失列 {len(added_columns)} 个: {', '.join(added_columns)}"
        )

    logger.info(
        f"[配音员] 启动完成 | DB={Config.DB_PATH} | TTS_ENGINE={Config.TTS_ENGINE}"
    )

    # ---- PY-345: 实例身份留痕 ----
    # 定罪现场（2026-09-24 02:2x 本机实测）：默认端口 :8000 上挂着一台 cwd 落在 git
    # 工作树 wt_u11b 的后端。BASE_DIR 跟着 __file__ 走，于是 ① 权重目录指到工作树里
    # 那份不存在的 data/models/voxcpm2（引擎 state=failed），② 用的是工作树自己那本
    # 空库（GET /api/books -> total=0，主库同刻 90 本）。全程 /api/health 只说 "ok"。
    _log_instance_identity()

    # ---- PY-348: 死亡留痕（心跳行 + atexit 的 exit 行）----
    # 有 exit = 自己退的（原因去同批日志里读）；无 exit + 末条心跳 free 很低 = 容量打死；
    # 无 exit + free 正常 = 被外部强杀（进程没机会写任何东西）。
    _heartbeat = runtime_guard.start(base_dir=str(Config.BASE_DIR))
    logger.info(
        f"[配音员][PY-348] 死亡留痕 | 日志={_GUARD_LOG_PATH or '仅 stderr'} | "
        f"心跳={'在跑' if _heartbeat else '关闭(测试进程)'} | "
        f"文件={runtime_guard.liveness_path()}")

    # ---- PY-311: 启动对账（上一进程遗留的非终态任务落终态） ----
    # 旧行为：后端重启后 conversion_tasks 里的 running/queued/paused 行永远留在原地
    # （没有进程会再写它们），前端进度条一直"进行中"、/progress 永久返回旧值。
    # verify-round4 §1 实测 10 条僵尸行、产品码连 aborted 都不存在 —— 见 PY-311。
    # 现在：进程刚起时本进程活跃集必为空 ⇒ 表内全部非终态行都是僵尸；
    # 用进程内活跃集当白名单，保证"绝不误伤本进程正在跑的任务"。
    if os.getenv("PYY_STARTUP_RECONCILE", "1").strip().lower() not in ("0", "false", "no"):
        try:
            from backend.services.task_service import task_service
            from modules.workflow.conversion_workflow import conversion_workflow
            rec = task_service.reconcile_interrupted_tasks(
                active_ids=conversion_workflow.active_task_ids())
            logger.info(
                f"[配音员][PY-311] 启动对账: 扫描非终态 {rec['scanned']} 行 -> "
                f"置 failed {rec['reconciled']} 行"
                f"（跳过活跃 {rec['skipped_active']}/心跳未过期 {rec['skipped_fresh']}）"
                f"| 原因='{rec['reason']}' | 心跳宽限 {rec['grace_seconds']:.0f}s")
            if rec["reconciled"]:
                logger.info(
                    f"[配音员][PY-311] 被对账任务: "
                    f"{[str(i)[:8] for i in rec['ids'][:20]]}")
        except Exception as e:
            # 对账失败不得阻塞服务启动（只降级为"僵尸行仍在"，与修复前同形）
            logger.warning(f"[配音员][PY-311] 启动对账失败(不阻塞启动): {e}")

    # ---- PY-115: 模型启动预热（后台线程，不阻塞启动） ----
    # 旧行为：220.6s 的 CPU 冷启动被压在首个任务的 synthesize 阶段里，
    # 期间前端停在 import 0% 且无任何提示。现在启动即预热，
    # 并由 /api/system/model_status + 任务进度事件（loading_model）反馈状态。
    # ENG-PRELOAD（#86）：**本地档启动即无条件触发装载** —— 内存门不再作为默认跳过理由，
    # 只有用户显式设置 PYY_PRELOAD_MIN_FREE_GB（或 PYY_PRELOAD_MODEL=0）时才拦；
    # 装载失败会带上限重试并把真实异常冒到 model_status（state=failed + error）。
    # 指令#1（双模式）：默认档位是在线档，此时**不**装载 voxcpm 那 5GB 级权重 ——
    # 死亡窗取证（PY-346/§31）里常驻 5.2GB 的正是它，而在线档一辈子用不到。
    # 真要"开机就备好本地模型"设 TTS_PRELOAD_LOCAL=true；本地档（TTS_TIER=local）自动预热。
    try:
        from modules.tts_engine import tier as tts_tier
        from modules.tts_engine.model_preloader import model_preloader
        if tts_tier.preload_local_at_start():
            preload_engine = tts_tier.default_engine(tts_tier.TIER_LOCAL)
            st = model_preloader.start(preload_engine, respect_memory_gate=True)
            snap = model_preloader.snapshot(preload_engine)
            logger.info(
                f"[配音员] 模型预热状态: {st} | engine={preload_engine} | "
                f"tier={tts_tier.resolve_tier()} | gate={snap.get('gate')} | {snap['message']}"
            )
        else:
            logger.info(
                f"[配音员] 档位=在线({tts_tier.default_engine(tts_tier.TIER_ONLINE)})，"
                f"启动不装载本地模型；任务真用本地引擎时由预热线程按 PY-115 装载")
    except Exception as e:
        logger.warning(f"[配音员] 模型预热启动失败(不影响服务): {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时清理资源。"""
    logger.info("[配音员] 服务已关闭")


# -------- 健康检查 --------
def _voxcpm_weights_dir() -> Path:
    """voxcpm 权重目录：认适配器那份权威定义，这里不再另立路径口径。"""
    from modules.tts_engine.engines.voxcpm_adapter import _DEFAULT_MODEL_DIR
    return Path(_DEFAULT_MODEL_DIR)


def _instance_identity() -> dict:
    """本实例"到底在哪台代码/哪个库上跑"的身份（PY-345）。

    只做字符串拼接 + 一次目录 stat，**不碰数据库**：`/api/health` 是轮询端点，
    导入大书期间它必须保持轻量（见 backend/api/books.py 里 PY-340 那条约束）。
    """
    from backend.config import Config

    weights = _voxcpm_weights_dir()
    return {
        "base_dir": str(Config.BASE_DIR),
        "cwd": os.getcwd(),
        "db_path": str(Config.DB_PATH),
        "weights_dir": str(weights),
        "weights_present": weights.is_dir(),
    }


def _log_instance_identity() -> dict:
    """启动时把实例身份写进日志；权重目录缺失必须点名（PY-117：不许只留一个 failed）。"""
    ident = _instance_identity()
    logger.info(
        f"[配音员][PY-345] 实例身份 | BASE_DIR={ident['base_dir']} | cwd={ident['cwd']} | "
        f"DB={ident['db_path']} | 权重目录={ident['weights_dir']}"
        f"(存在={ident['weights_present']})"
    )
    if not ident["weights_present"]:
        logger.warning(
            f"[配音员][PY-345] 权重目录不存在: {ident['weights_dir']} -> voxcpm 装载必然失败"
            f"（红线④：人声不可应用）。这通常意味着本实例是从 git 工作树/未同步 data/ 的"
            f"检出里起来的，请回到主检出目录启动。"
        )
    return ident


@app.get("/api/health")
async def health_check():
    """健康检查端点。"""
    from backend.config import Config

    return {
        "status": "ok",
        "version": "1.0.0",
        "tts_engine": Config.TTS_ENGINE,
        # 指令#1: 档位是"这套配音是在线还是本地"的单一答案，前端与启动器都读它，
        # 不再从引擎名反推（引擎名会随默认档变化）。
        "tts_tier": Config.TTS_TIER,
        "device": Config.TTS_DEVICE,
        "gpu_available": Config.is_gpu_available(),
        "instance": _instance_identity(),
    }


@app.get("/api/system/info")
async def system_info():
    """系统信息端点。"""
    import platform
    from backend.config import Config

    return {
        "platform": platform.system(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "gpu_available": Config.is_gpu_available(),
        "data_dir": str(Config.DATA_DIR),
        "model_status": _model_status(),
    }


def _model_status() -> dict:
    """PY-115: 模型加载状态摘要（idle/loading/ready/failed + 已耗时）。"""
    try:
        from modules.tts_engine.model_preloader import model_preloader
        return model_preloader.snapshot()
    except Exception as e:  # 绝不因状态探测拖垮健康检查
        return {"state": "unknown", "message": f"状态不可用: {e}"}


@app.get("/api/system/liveness")
async def system_liveness(count: int = 5):
    """PY-348: 把"上次它是什么时候没的"做成可查的。

    读 `logs/liveness.log` 的最后几条。判读口径（写在这里，不让人猜）：
    最后一条是 `exit` = 进程自己退的（原因去同批 backend-*.log 里读）；
    最后一条是 `heartbeat` 且 `free_gb` 很低 = 容量被打死；
    最后一条是 `heartbeat` 且 `free_gb` 正常 = 被外部强杀（进程没机会写任何东西）。
    """
    from backend import runtime_guard
    return {
        "log_file": str(runtime_guard.liveness_path()),
        "exists": runtime_guard.liveness_path().exists(),
        "last_events": runtime_guard.read_last_events(count=max(1, min(count, 50))),
    }


@app.get("/api/system/model_status")
async def model_status():
    """TTS 模型预热/加载状态（前端在 loading 期显示"模型加载中"）。"""
    return {"status": "ok", "model": _model_status()}


@app.get("/api/system/engines")
async def system_engines():
    """两套配音档 + 全部引擎端口（指令#1 模式开关 / 指令#3 付费档的数据源）。

    只读注册表元数据与 env：**不发一次网络请求、不加载一份权重**。缺 Key 的付费档
    也必须能被列出来 —— "端口在、未配置" 是一种产品状态，不是错误（红线：不编造
    "这个引擎现在能用"）。
    """
    from modules.tts_engine import tier as tts_tier
    from modules.tts_engine.engine_registry import engine_registry
    try:
        engines = engine_registry.list_engines()
        engines_error = None
    except Exception as e:                                  # pragma: no cover
        engines, engines_error = [], f"{type(e).__name__}: {e}"
    try:
        modes = tts_tier.describe()
        modes_error = None
    except Exception as e:                                  # pragma: no cover
        modes, modes_error = {}, str(e)
    err = engines_error or modes_error
    # U-6/M-4 口径：出错时不给"看着像成功"的空载荷 —— status 要跟着变，
    # 前端据此区分"没有付费档"与"这次根本没问到"。
    return {"status": "degraded" if err else "ok",
            "tiers": modes.get("tiers"), "tier": modes.get("tier"),
            "modes": modes.get("modes"), "engines": engines, "error": err}


# -------- CORS 中间件（安全默认值） --------
# 读取允许的 origins 列表（逗号分隔）。
# 为空字符串时允许所有来源（仅用于开发环境）。
_CORS_ORIGINS_RAW = os.getenv("PYY_CORS_ORIGINS", "*")
if _CORS_ORIGINS_RAW.strip() == "*":
    _CORS_ORIGINS = ["*"]
    _ALLOW_CREDENTIALS = False  # 浏览器不允许 "*" + allow_credentials=True
else:
    _CORS_ORIGINS = [o.strip() for o in _CORS_ORIGINS_RAW.split(",") if o.strip()]
    _ALLOW_CREDENTIALS = True

logger.info(f"[CORS] allow origins: {_CORS_ORIGINS} (credentials={_ALLOW_CREDENTIALS})")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------- 路由注册（延迟导入，避免循环引用） --------
def register_routes():
    """注册所有 API 路由。"""
    from backend.api.books import router as books_router
    from backend.api.voices import router as voices_router
    from backend.api.dubbing import router as dubbing_router
    from backend.api.tasks import router as tasks_router
    from backend.api.workflows import router as workflows_router
    from backend.api.llm import router as llm_router
    from backend.api.mineru_parse import router as mineru_router
    from backend.api.memory import router as memory_router
    from backend.api.cast import router as cast_router

    app.include_router(books_router, prefix="/api/books", tags=["电子书"])
    app.include_router(voices_router, prefix="/api/voices", tags=["音色"])
    app.include_router(dubbing_router, prefix="/api/dubbing", tags=["配音"])
    app.include_router(tasks_router, prefix="/api/tasks", tags=["任务"])
    app.include_router(workflows_router, prefix="/api/workflows", tags=["工作流"])
    app.include_router(llm_router, prefix="/api/llm", tags=["LLM 智能分析"])
    app.include_router(mineru_router, prefix="/api/mineru", tags=["MinerU 文档解析"])
    app.include_router(memory_router, prefix="/api/memory", tags=["记忆"])
    app.include_router(cast_router, prefix="/api", tags=["多人有声书"])


register_routes()
