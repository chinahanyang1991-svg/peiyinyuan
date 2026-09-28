"""
转换任务 API 路由

修复 (P0 #2):
- WebSocket 添加 receive 循环，响应浏览器 ping/pong
- 用 asyncio.Task 替代手动 while-True sleep，避免 ping 超时被浏览器断开
- 错误处理：捕获 WebSocketDisconnect、异常时尝试发送错误消息再断开
"""

import asyncio
import logging
import re
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, Query, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import os

from backend.services.task_service import (
    PAUSE_ALLOWED_FROM,
    RESUME_ALLOWED_FROM,
    task_service,
)
from modules.workflow.conversion_workflow import (
    SUPPORTED_OUTPUT_FORMATS,
    conversion_workflow,
)
from modules.tts_engine import ffmpeg_utils

logger = logging.getLogger(__name__)

router = APIRouter()

# WF-21: 任务状态的**唯一**可过滤集合。
# 取值来源 = `backend/models/task.py:19` 列注释 + 服务层实际写入的字面量
# （task_service.py:184/187/260/301/322 写 completed/running/cancelled/failed，
#   建行的列默认值是 queued，暂停由 pause_task 落 paused）。
TASK_STATUSES: frozenset = frozenset(
    {"queued", "running", "paused", "completed", "failed", "cancelled"})

# WF-21: GET /api/tasks/ 允许的查询参数白名单（越界一律 422，不再静默忽略）
_TASK_LIST_QUERY_KEYS: frozenset = frozenset({"limit", "offset", "status"})


def _reject_unknown_task_query_params(request: Request) -> None:
    """未知查询参数 → 422（WF-21）。

    FastAPI 默认把没声明的 query 参数**丢掉不报错**，这正是
    `?status=processing` 看起来"过滤失效"而垃圾值也 200 的根因：端点压根没有
    该形参。这里在依赖里显式拒绝，报错形态与 pydantic 校验错误一致。
    """
    unknown = sorted(set(request.query_params.keys()) - _TASK_LIST_QUERY_KEYS)
    if unknown:
        raise HTTPException(
            422,
            detail=[{
                "loc": ["query", key],
                "msg": f"未知查询参数 {key!r}，本端点仅接受: "
                       f"{', '.join(sorted(_TASK_LIST_QUERY_KEYS))}",
                "type": "value_error.extra",
            } for key in unknown])



class StartConversionRequest(BaseModel):
    project_id: Optional[str] = None
    book_id: Optional[str] = None
    output_format: str = "m4b"
    settings: dict = {}


def _reject_bad_engine_settings(settings: dict) -> None:
    """指令#1/#3：档位与引擎名在**请求边界**就判，不留到 P4/P7 才炸。

    为什么必须在这里：`settings.tier` / `settings.engine` 决定整本书用哪台引擎发声，
    写错一个字母过去会让任务跑到合成阶段才失败 —— 用户视角是"跑了 20 分钟白跑"。
    项目全局设置那条路已有白名单 (dubbing_service.WF-11)，转换入口这侧此前没人管。
    """
    from modules.tts_engine import tier as tts_tier
    from modules.tts_engine.engine_registry import engine_registry

    tier_val = (settings or {}).get("tier")
    if tier_val not in (None, ""):
        try:
            tts_tier.resolve_tier(tier_val)
        except ValueError as e:
            raise HTTPException(422, f"settings.tier 非法：{e}")
    engine_val = (settings or {}).get("engine")
    if engine_val not in (None, ""):
        name = str(engine_val).strip().lower()
        if not engine_registry.is_known_engine(name):
            raise HTTPException(
                422,
                f"settings.engine 非法：{engine_val!r} 不是已注册引擎，可选: "
                f"{', '.join(engine_registry.known_engine_names())}"
                f"（或删掉该键按档位默认引擎跑）")


@router.post("/convert")
async def start_conversion(req: StartConversionRequest):
    """启动转换任务 (HTTP + 异步执行)。"""
    _reject_bad_engine_settings(req.settings)
    # PY-124/PY-127: 请求阶段就把"格式不合法"和"环境缺编码器"说清楚，
    # 而不是跑完 20 分钟后在打包阶段抛裸 FileNotFoundError 再悄悄降级。
    fmt = (req.output_format or "").strip().lower().lstrip(".")
    if fmt not in SUPPORTED_OUTPUT_FORMATS:
        raise HTTPException(
            422,
            f"不支持的输出格式 {req.output_format!r}，可选: "
            f"{', '.join(SUPPORTED_OUTPUT_FORMATS)}")
    if ffmpeg_utils.requires_ffmpeg(fmt) and not ffmpeg_utils.is_available():
        raise HTTPException(
            503,
            f"输出格式 {fmt} 需要 ffmpeg 编码器，当前环境未找到可用的 ffmpeg："
            f"{ffmpeg_utils.unavailable_reason()}")

    task = task_service.create_task(
        project_id=req.project_id,
        book_id=req.book_id,
        output_format=fmt,
        settings=req.settings,
    )

    # 触发后台任务（asyncio.create_task），不阻塞 HTTP 响应
    async def run_task():
        try:
            # run_conversion 内部已处理 complete_task / fail_task 的数据库更新
            result = await conversion_workflow.run_conversion(
                task_id=task["id"],
                project_id=req.project_id,
                book_id=req.book_id,
                output_format=fmt,
                settings=req.settings,
            )
            logger.info(
                f"[Task:{task['id']}] conversion finished: status={result.get('status')}"
            )
        except Exception as e:
            logger.exception(f"[Task:{task['id']}] run_task crashed: {e}")
            task_service.fail_task(task["id"], str(e))

    asyncio.create_task(run_task())
    return {"status": "ok", "task": task}


@router.get("/", dependencies=[Depends(_reject_unknown_task_query_params)])
async def list_tasks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, description="分页偏移 (WF-21)"),
    status: Optional[str] = Query(
        None,
        description="按状态过滤: " + " / ".join(sorted(TASK_STATUSES)) + " (WF-21)"),
):
    """获取任务列表 (WF-21: status 枚举过滤 + 未知查询参数 422)。

    旧实现 `list_tasks(limit)` 没有任何过滤形参：`?status=processing` 与
    `?status=随便什么` 都返回**全量 131 条**且状态码 200 —— 未知查询串被
    FastAPI 静默丢弃，调用方以为过滤生效了。现在：
      * `status` 必须落在 `TASK_STATUSES`（`backend/models/task.py:19` 注释的
        6 个取值 + 服务层实际写入的字面量），否则 **422**；
      * 白名单外的查询参数（拼错的 `stats=`、多传的 `page=`…）→ **422**，
        响应体沿用 FastAPI 校验错误的 `detail:[{loc/msg/type}]` 形态；
      * `total` 改为**同一过滤条件下的 SQL COUNT(*)**（旧值 `len(tasks)` 是本页
        条数，与已修的 WF-12/WF-23 同口径），并回显 `limit/offset/returned`。
    `status` 与 `tasks` 两个既有键的语义与类型不变。
    """
    if status is not None and status not in TASK_STATUSES:
        raise HTTPException(
            422,
            detail=[{
                "loc": ["query", "status"],
                "msg": f"未知任务状态 {status!r}，可选: "
                       f"{', '.join(sorted(TASK_STATUSES))}",
                "type": "value_error",
            }])
    tasks = task_service.list_tasks(limit, status=status, offset=offset)
    total = task_service.count_tasks(status=status)
    return {
        "status": "ok",
        "tasks": tasks,
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned": len(tasks),
    }


@router.get("/orphan-files")
async def list_orphan_files():
    """PY-365·甲：产物目录只读盘点（零副作用，绝不删东西）。

    路径说明：必须注册在 `GET /{task_id}` **之前** —— FastAPI 按注册顺序匹配，
    字面量 `/orphan-files` 若排在 `/{task_id}` 之后会被其吞掉（task_id 收到
    "orphan-files" 后 404）。未与 `/{id}` 冲突，故保留 `/api/tasks/orphan-files`
    的仓库既有风格路径，不需要改挂 `/api/system/...`。
    """
    from backend.services import output_reconcile

    return {"status": "ok", **output_reconcile.build_inventory()}


class PruneOrphanFilesRequest(BaseModel):
    # confirm 默认 False：不显式点名就什么都不删（红线：数据不丢）。
    confirm: bool = False
    paths: list[str] = []


@router.post("/orphan-files/prune")
async def prune_orphan_files(req: PruneOrphanFilesRequest):
    """PY-365·甲：显式清理孤儿产物。三重门与"删前落盘删单"在 output_reconcile。"""
    from backend.services import output_reconcile

    try:
        result = output_reconcile.prune_orphans(paths=req.paths, confirm=req.confirm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", **result}


@router.get("/{task_id}")
async def get_task_status(task_id: str):
    """获取任务详情。"""
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"status": "ok", "task": task}


@router.get("/{task_id}/output-state")
async def get_task_output_state(task_id: str):
    """PY-365·丙：单条任务的产物对账标记（新端点，不动既有详情契约）。

    `output_missing` 与 `output_size_bytes is None` 同源判定（models 的 to_dict
    口径，`backend/models/task.py:44-49`），不造第二套真值：
    output_path 非空而 stat 失败 = 行在、文件不在。
    """
    from backend.services.output_reconcile import output_state

    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"status": "ok", "task_id": task_id, **output_state(task)}


def _state_gate_reason(action: str, current: str) -> str:
    if action == "pause":
        return f"任务当前是 {current}，只有正在转换（running）的任务才能暂停。"
    return f"任务当前是 {current}，只有已暂停（paused）的任务才能继续。"


def _no_pipeline_reason(action: str, current: str) -> str:
    verb = "暂停" if action == "pause" else "继续"
    return (f"本进程里没有这个任务的转换流水线（服务重启过，任务行还停在 {current}），"
            f"无法{verb}；请刷新页面后重新发起一次转换。")


def _move_task_state(task_id: str, action: str) -> dict:
    """PY-359: pause / resume 共用的一条链路 —— 状态门 → 流水线门 → 落库 → 落库不成回滚。

    旧实现只有"落库"这一步，而且把流水线的返回值丢掉：后端重启之后
    `conversion_workflow._active_pipelines` 必为空（它的 pause/resume 据此返回
    False，`modules/workflow/conversion_workflow.py:657-671`），于是"继续"等于
    把数据库行改成 running、什么都不跑；而僵尸行的清理只在下次启动做一次
    （`backend/main.py:104`），进程内没有任何地方会把这一行改回终态 —— 用户看到
    一个永远不动的"正在转换"。

    两道门的分工：状态门在这里先问一次（为了不去惊动流水线、并且把"现在是什么态"
    说给用户），真正的原子判定在服务层的带状态条件 UPDATE 里；端点这侧读到写完
    之间状态若被 worker 收尾，服务层的 CAS 会返回 False，走下面的回滚分支。
    """
    if action == "pause":
        allowed_from, write = PAUSE_ALLOWED_FROM, task_service.pause_task
        command, rollback = conversion_workflow.pause, conversion_workflow.resume
        key, verb = "paused", "暂停"
    else:
        allowed_from, write = RESUME_ALLOWED_FROM, task_service.resume_task
        command, rollback = conversion_workflow.resume, conversion_workflow.pause
        key, verb = "resumed", "继续"

    existing = task_service.get_task(task_id)
    if not existing:
        raise HTTPException(404, "任务不存在")
    current = existing.get("status")
    if current not in allowed_from:
        raise HTTPException(409, _state_gate_reason(action, current))

    if not command(task_id):
        raise HTTPException(409, _no_pipeline_reason(action, current))
    if not write(task_id):
        rollback(task_id)
        raise HTTPException(
            409, f"{verb}没有生效：任务状态刚刚被别的操作改过。"
                 f"已把转换流水线放回原状，请刷新后重试。")
    return {"status": "ok", key: True, "task": task_service.get_task(task_id)}


@router.post("/{task_id}/pause")
async def pause_task(task_id: str):
    """暂停任务。PY-359: 状态门 + 流水线门，链路见 `_move_task_state`。"""
    return _move_task_state(task_id, "pause")


@router.post("/{task_id}/resume")
async def resume_task(task_id: str):
    """恢复任务。PY-359: 状态门 + 流水线门，链路见 `_move_task_state`。"""
    return _move_task_state(task_id, "resume")


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务。

    WF-07 修复：旧实现先 `task_service.cancel_task()` 写库、后
    `conversion_workflow.cancel()`，而 P7 的句循环当时看不到任何取消信号
    （旧 cancel 只在 pipeline 已注册时才生效），于是"取消"只改了 DB 里的字，
    整本书继续合成，最终 fail_task 把 status 覆成 failed。
    现在顺序为：①记录取消意图并通知 pipeline（pipeline 未注册也被记住）
    ②落 cancelled 终态（completed_at + logs 留痕，幂等）。
    """
    existing = task_service.get_task(task_id)
    if not existing:
        raise HTTPException(404, "任务不存在")
    notified = conversion_workflow.cancel(task_id)
    task = task_service.finalize_cancelled(task_id, "用户请求取消任务") \
        or task_service.get_task(task_id)
    return {"status": "ok", "cancelled": True, "notified_pipeline": bool(notified),
            "task": task}


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """删除任务。PY-365 行为锁：只删库行，**不级联删产物文件**（红线：数据不丢）；
    孤儿产物走 GET /orphan-files 盘点 + POST /orphan-files/prune 显式清理。"""
    if task_service.delete_task(task_id):
        return {"status": "ok"}
    raise HTTPException(404, "任务不存在")


@router.get("/{task_id}/progress")
async def get_progress(task_id: str):
    """获取任务进度 (HTTP 轮询)。

    F-R4-2: 回落 DB 时同样带心跳字段（stale/age_seconds/stale_after_seconds），
    前端据此判断"这一行不会再前进了"，不再被永久 0.44 的旧值骗住。
    """
    progress = conversion_workflow.get_progress(task_id)
    if progress:
        return {"status": "ok", "progress": progress}
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    payload = {
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "current_stage": task["current_stage"],
        "processed_chars": task["processed_chars"],
        "total_chars": task["total_chars"],
        "updated_at": task.get("updated_at"),
    }
    payload.update(task_service.progress_staleness(task))
    return {"status": "ok", "progress": payload}


@router.websocket("/{task_id}/progress")
async def task_progress_ws(websocket: WebSocket, task_id: str):
    """WebSocket 实时推送转换进度。

    修复点：
    - 启动 receive 循环，响应浏览器 ping/pong，避免 ping-interval 超时断开
    - 发送端独立，周期性读取进度；一旦任务结束，主动断开连接
    - 异常时记录日志并关闭连接
    """
    await websocket.accept()

    # --------- 1. 接收循环（处理客户端消息 + ping/pong） ---------
    async def recv_loop():
        try:
            while True:
                data = await websocket.receive_text()
                # 客户端消息（可选）：例如 {"action":"cancel"}
                if data and data.strip() == "ping":
                    try:
                        await websocket.send_text("pong")
                    except Exception:
                        break
        except WebSocketDisconnect:
            logger.info(f"[WS:{task_id}] client disconnected (recv)")
        except Exception as e:
            logger.warning(f"[WS:{task_id}] recv_loop error: {e}")

    # --------- 2. 发送循环（周期性推送进度） ---------
    async def send_loop():
        try:
            while True:
                progress = conversion_workflow.get_progress(task_id)
                if progress is None:
                    task = task_service.get_task(task_id)
                    if not task:
                        await websocket.send_json(
                            {"error": "任务不存在", "task_id": task_id}
                        )
                        return
                    progress = {
                        "task_id": task_id,
                        "status": task["status"],
                        "progress": task["progress"],
                        "current_stage": task["current_stage"],
                        "processed_chars": task["processed_chars"],
                        "total_chars": task["total_chars"],
                        "updated_at": task.get("updated_at"),
                    }
                    # F-R4-2: 与 HTTP 轮询同一口径的心跳标签
                    progress.update(task_service.progress_staleness(task))

                await websocket.send_json(progress)

                status = progress.get("status")
                if status in ("completed", "failed", "cancelled"):
                    logger.info(
                        f"[WS:{task_id}] task reached terminal state={status}, closing"
                    )
                    return
                if progress.get("stale") and not conversion_workflow.is_active(task_id):
                    # PY-311/F-R4-2: 非本进程在跑 + 心跳远超阈值 => 这行不会再前进，
                    # 主动结束推送，前端进度条据此停止（旧行为: 永久推 0.44/running）
                    logger.warning(
                        f"[WS:{task_id}] 心跳超期({progress.get('age_seconds')}s > "
                        f"{progress.get('stale_after_seconds')}s) 且本进程未持有该任务, "
                        f"关闭推送 (status={status})")
                    return

                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            logger.info(f"[WS:{task_id}] client disconnected (send)")
        except Exception as e:
            logger.warning(f"[WS:{task_id}] send_loop error: {e}")

    # --------- 3. 并发运行，任意循环结束即断开连接 ---------
    recv_task = asyncio.create_task(recv_loop())
    send_task = asyncio.create_task(send_loop())
    try:
        done, pending = await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        for t in done:
            # 传播异常（除了 WebSocketDisconnect 这种预期异常）
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.warning(f"[WS:{task_id}] task exception: {exc}")
    except Exception as e:
        logger.warning(f"[WS:{task_id}] ws orchestrator error: {e}")
    finally:
        try:
            await websocket.close(code=1000)
        except Exception:
            pass


def _ascii_fallback_name(filename: str) -> str:
    """ASCII 回退名 (RFC 6266): 非 ASCII / 危险字符统一替换, 保证 header 可 latin-1 编码。"""
    fallback = "".join(
        c if (0x20 < ord(c) < 0x7F and c not in '"\\') else "_"
        for c in (filename or "")
    )
    fallback = re.sub(r"[^\w.\-]+", "_", fallback).strip("._")
    return fallback or "download"


def content_disposition(filename: str) -> str:
    """构造 RFC 5987 安全的 Content-Disposition (PY-119)。

    之前直接写 f'attachment; filename="{中文书名}"' 会让 starlette 以 latin-1
    编 header 时抛 UnicodeEncodeError -> GET /tasks/{id}/download 返回 500
    (实测 11 个已完成任务中 9 个中文书名产物全部不可下载)。
    现按 RFC 5987/6266: ASCII 回退名 + filename*=UTF-8''<percent-encoded>。
    """
    fallback = _ascii_fallback_name(filename)
    encoded = quote(filename or "", safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


@router.get("/{task_id}/download")
async def download_task(task_id: str):
    """下载转换完成的有声书文件。"""
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "completed":
        raise HTTPException(400, "任务尚未完成，无法下载")

    output_path = task.get("output_path")
    if not output_path or not os.path.exists(output_path):
        raise HTTPException(404, "输出文件不存在")

    filename = os.path.basename(output_path)
    return FileResponse(
        path=output_path,
        filename=_ascii_fallback_name(filename),
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition(filename)},
    )
