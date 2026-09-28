"""
转换任务服务
管理有声书转换任务的生命周期
"""

import uuid
import os
import time
import logging
from typing import Optional
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import OperationalError

from backend.models.base import get_session
from backend.models.task import ConversionTask
from backend.config import Config

logger = logging.getLogger(__name__)

# SQLite 并发重试
_MAX_RETRIES = 3
_RETRY_DELAY = 0.1

# WF-07: 取消后 worker 仍上报进度的限频告警（同一任务 60s 最多一条，避免刷屏）
_CANCEL_IGNORED_LAST_LOG: dict = {}
_CANCEL_IGNORED_INTERVAL_S = 60.0


def _log_cancelled_ignored(task_id: str, progress: float, stage: str) -> None:
    now = time.time()
    if now - _CANCEL_IGNORED_LAST_LOG.get(task_id, 0.0) < _CANCEL_IGNORED_INTERVAL_S:
        return
    _CANCEL_IGNORED_LAST_LOG[task_id] = now
    logger.warning(f"[Task:{task_id}] 已是 cancelled 终态，忽略进度上报 "
                   f"(progress={progress:.3f}, stage={stage or '-'}) —— WF-07")
    if len(_CANCEL_IGNORED_LAST_LOG) > 500:
        _CANCEL_IGNORED_LAST_LOG.clear()

# ---- PY-311 / F-R4-2: 任务生命周期对账口径 ----
# 非终态 = "前端认为还在跑"的状态；重启后这类行不会再有人写它（僵尸行）。
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
NON_TERMINAL_STATUSES = ("running", "queued", "paused")
# 对账写入的失败原因（F-R4-1 要求的留痕文案）
RESTART_INTERRUPT_MESSAGE = "服务重启导致中断"
# PY-359: 人工生命周期迁移的合法起点。pause/resume 只对"当前真的在跑/真的被暂停"
# 的行有意义；从终态或 queued 改出来的是没人会再推进的僵尸行。
PAUSE_ALLOWED_FROM = ("running",)
RESUME_ALLOWED_FROM = ("paused",)


def _env_seconds(name: str, default: float) -> float:
    """读秒级环境变量（非法值回落默认，绝不让启动崩在配置上）。"""
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


def reconcile_grace_seconds() -> float:
    """PY-311: 距上次心跳小于该秒数的非终态行**不动**（可能被另一个常驻进程持有）。
    默认 300s；设 0 = 无条件对账（测试用）。"""
    return _env_seconds("PYY_RECONCILE_GRACE_SECONDS", 300.0)


def progress_stale_seconds() -> float:
    """F-R4-2: progress 读回判定"心跳超期"的阈值秒数，默认 1800s。"""
    return _env_seconds("PYY_PROGRESS_STALE_SECONDS", 1800.0)


def _format_of_path(path: str) -> str:
    """从产物文件名取真实格式后缀（小写、无点）；无后缀返回 ""。"""
    if not path:
        return ""
    return os.path.splitext(str(path))[1].lstrip(".").lower()


def _retry_on_lock(func):
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except OperationalError as e:
                last_error = e
                if "database is locked" in str(e).lower() and attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        raise last_error
    return wrapper


class TaskService:
    """转换任务管理服务"""

    def create_task(self, project_id: str = None, book_id: str = None,
                    output_format: str = "m4b",
                    settings: dict = None) -> dict:
        """创建转换任务"""
        session = get_session()
        try:
            task = ConversionTask(
                id=str(uuid.uuid4()),
                project_id=project_id,
                book_id=book_id,
                output_format=output_format or Config.DEFAULT_AUDIO_FORMAT,
                settings=settings or {},
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.to_dict()
        finally:
            session.close()

    @_retry_on_lock
    def get_task(self, task_id: str) -> Optional[dict]:
        """获取任务详情"""
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            return task.to_dict() if task else None
        finally:
            session.close()

    @_retry_on_lock
    def list_tasks(self, limit: int = 50, status: Optional[str] = None,
                   offset: int = 0) -> list:
        """列出任务 (WF-21: 支持 status 过滤 + offset 翻页)。

        `status=None` 时行为与旧实现逐字一致（按创建时间倒序取 limit 条）；
        传值则先 `filter_by(status=...)` 再排序 —— 枚举合法性由 API 层把关。
        """
        session = get_session()
        try:
            query = session.query(ConversionTask)
            if status:
                query = query.filter(ConversionTask.status == status)
            tasks = (
                query.order_by(ConversionTask.created_at.desc())
                .offset(max(0, int(offset)))
                .limit(limit)
                .all()
            )
            return [t.to_dict() for t in tasks]
        finally:
            session.close()

    @_retry_on_lock
    def count_tasks(self, status: Optional[str] = None) -> int:
        """满足过滤条件的任务总数 (WF-21)。

        口径照抄 WF-12 的 `voice_store.count()`：SQL `COUNT(*)`，
        **不是**本页条数 —— 旧 `total = len(tasks)` 在 limit=50 时谎报总数。
        """
        session = get_session()
        try:
            query = session.query(func.count(ConversionTask.id))
            if status:
                query = query.filter(ConversionTask.status == status)
            return int(query.scalar() or 0)
        finally:
            session.close()

    @_retry_on_lock
    def update_progress(self, task_id: str, progress: float,
                        current_stage: str = "",
                        processed_chars: int = 0,
                        total_chars: Optional[int] = None,
                        message: str = "", error: str = "",
                        status: str = ""):
        """更新任务进度。

        PY-331: `status` 显式给出终态时以它为准，不再靠"progress 到 1.0 就是完成"
        去猜。旧行为下 cast 分析失败会先被这条进度写标成 completed（终态那一跳
        progress 也是 1.0），再由 fail_task 改回 failed，中间那段前端读到的是
        "失败的任务显示已完成"。不传 status 的调用方（转换主流程）行为不变。

        PY-121: 旧实现从不写 total_chars → 任务结束 tracker 被 pop、进度回落 DB 时
        出现 {processed_chars: 60, total_chars: 0}，分母为 0，前端百分比/剩余时间不可算。
        PY-120: 每次阶段推进追加一条阶段日志到 ConversionTask.logs（JSON），
        瞬时完成的阶段（parse/clean/split/analyze/assign）不再被 1~2s 轮询漏掉。
        WF-07: `cancelled` 是终态 —— 取消之后 worker 若还在推进度，前端会看到
        "已取消 + 进度继续涨"（实测 0.667→0.683→0.778）。这里直接冻结写入。
        """
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            if task:
                if task.status == "cancelled":
                    _log_cancelled_ignored(task_id, progress, current_stage)
                    return
                task.progress = min(progress, 1.0)
                if current_stage:
                    task.current_stage = current_stage
                if processed_chars is not None:
                    # PY-121: processed 单调，避免阶段间回落造成百分比重退
                    if processed_chars >= (task.processed_chars or 0):
                        task.processed_chars = processed_chars
                if total_chars is not None and total_chars > (task.total_chars or 0):
                    task.total_chars = total_chars

                if current_stage:
                    logs = list(task.logs or [])
                    last = logs[-1] if logs else None
                    if not isinstance(last, dict) or last.get("stage") != current_stage:
                        entry = {"stage": current_stage,
                                 "progress": round(min(progress, 1.0), 4)}
                        if message:
                            entry["message"] = message[:200]
                        if error:
                            entry["error"] = error[:200]
                        logs.append(entry)
                        task.logs = logs[-128:]

                if status and status != "running":
                    # PY-331: 调用方已经知道终态，别再猜。
                    task.status = status
                    task.completed_at = datetime.utcnow()
                elif progress >= 1.0:
                    task.status = "completed"
                    task.completed_at = datetime.utcnow()
                elif task.status == "queued":
                    task.status = "running"
                    task.started_at = datetime.utcnow()

                session.commit()
        finally:
            session.close()

    def pause_task(self, task_id: str) -> bool:
        """暂停任务。PY-359: 只有 running 能被暂停（旧实现无任何状态门）。"""
        return self._cas_status(task_id, PAUSE_ALLOWED_FROM, "paused")

    def resume_task(self, task_id: str) -> bool:
        """恢复任务。PY-359: 只有 paused 能被恢复 —— 旧实现走无门的
        `_update_status()`，任何一条存在的行（含 failed/cancelled/completed）
        都会被改成 running，而进程里并没有流水线在跑它，于是留下一行永远不动的僵尸。
        """
        return self._cas_status(task_id, RESUME_ALLOWED_FROM, "running")

    def cancel_task(self, task_id: str) -> bool:
        """取消任务。

        WF-07: 走 finalize_cancelled —— 旧实现只翻 status，既不写 completed_at
        也不留痕，于是取消中的任务在语义上仍是"未完成"，后续 fail_task /
        update_progress 都会把它改回 failed/running。返回值语义不变（行不存在
        返回 False）。
        """
        return self.finalize_cancelled(task_id) is not None

    @_retry_on_lock
    def append_log(self, task_id: str, stage: str, message: str = "",
                   extra: Optional[dict] = None, progress: Optional[float] = None,
                   error: str = "") -> bool:
        """WF-13: 把"运行期事实"追加进 ConversionTask.logs，随 GET /tasks/{id} 透出。

        与 update_progress 的区别：本方法只写日志，不推进度、不改状态，
        因此可以在任何阶段（含收尾）安全调用，也不会被 cancelled 冻结门挡住。
        `extra` 逐键并入日志条目（如 mix_applied / mix_stats / dropped_*_segments），
        排障时不必再翻服务端 logger。条目上限 128 条，与 PY-120 口径一致。
        """
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            if not task:
                return False
            entry = {"stage": stage or "info",
                     "progress": round(min(max(float(task.progress or 0.0), 0.0), 1.0), 4)
                     if progress is None else round(min(float(progress), 1.0), 4)}
            if message:
                entry["message"] = str(message)[:500]
            if error:
                entry["error"] = str(error)[:500]
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if k not in entry:
                        entry[k] = v
            logs = list(task.logs or [])
            logs.append(entry)
            task.logs = logs[-128:]
            session.commit()
            return True
        finally:
            session.close()

    @_retry_on_lock
    def finalize_cancelled(self, task_id: str, message: str = "任务已取消") -> Optional[dict]:
        """WF-07: 取消收尾 —— cancelled 是**终态**，写 completed_at 并留一条 logs。

        幂等：已是 cancelled 的行只补 completed_at / logs，不会被别的状态覆盖。
        返回落库后的任务 dict（任务不存在返回 None）。
        """
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            if not task:
                return None
            was_cancelled = task.status == "cancelled"
            task.status = "cancelled"
            if not task.completed_at:
                task.completed_at = datetime.utcnow()
            logs = list(task.logs or [])
            if not (was_cancelled and logs
                    and isinstance(logs[-1], dict)
                    and logs[-1].get("stage") == "cancelled"):
                logs.append({"stage": "cancelled",
                             "progress": round(min(float(task.progress or 0.0), 1.0), 4),
                             "message": (str(message)[:500] if message else "任务已取消")})
                task.logs = logs[-128:]
            session.commit()
            return task.to_dict()
        finally:
            session.close()

    @_retry_on_lock
    def fail_task(self, task_id: str, error_message: str):
        """标记任务失败。

        WF-07: 旧实现无条件覆写 status=failed —— 用户已经取消的任务，worker 在
        收尾时把手上的失败（例如取消前最后一次合成报错）写回终态，前端"已取消"
        变成"失败"。现按终态优先级处理：cancelled 行只留痕、不改状态。
        """
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            if task:
                if task.status == "cancelled":
                    logs = list(task.logs or [])
                    logs.append({
                        "stage": task.current_stage or "unknown",
                        "progress": round(min(float(task.progress or 0.0), 1.0), 4),
                        "error": str(error_message)[:500],
                        "message": "取消后收到的失败，保留 cancelled 终态 (WF-07)",
                    })
                    task.logs = logs[-128:]
                    session.commit()
                    logger.info(f"[Task:{task_id}] 已 cancelled，fail_task 不覆写终态 "
                                f"(原因: {str(error_message)[:120]})")
                    return
                task.status = "failed"
                task.error_message = error_message
                task.completed_at = datetime.utcnow()
                session.commit()
        finally:
            session.close()

    @_retry_on_lock
    def complete_task(self, task_id: str, output_path: str,
                      format_used: str = "", requested_format: str = ""):
        """标记任务完成。

        PY-124: 打包阶段可能因编码器/格式策略产出与请求不同的格式。旧实现只写
        output_path，output_format 永远停留在"请求值"，于是任务表里出现
        "output_format=m4b 而 output_path=....wav" 的行，前端/播放器直接放不动。
        现在把实际格式回写，并把请求值保留在 settings.requested_output_format。
        """
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            if task:
                task.status = "completed"
                task.progress = 1.0
                task.output_path = output_path
                task.completed_at = datetime.utcnow()

                real = _format_of_path(output_path) or (format_used or "").lower()
                if real and (task.output_format or "").lower() != real:
                    settings = dict(task.settings or {})
                    settings["requested_output_format"] = (
                        requested_format or settings.get("requested_output_format")
                        or task.output_format)
                    settings["format_used"] = real
                    task.settings = settings
                    task.output_format = real

                    logs = list(task.logs or [])
                    logs.append({"stage": "package", "progress": 1.0,
                                 "message": (f"实际输出格式 {real}"
                                             f"（请求 {settings['requested_output_format']}）")})
                    task.logs = logs[-128:]
                session.commit()
        finally:
            session.close()

    @_retry_on_lock
    def reconcile_format_used(self, dry_run: bool = False) -> dict:
        """校正历史任务行的 output_format 与真实产物后缀不一致的问题 (PY-124)。

        修复前落库的行是"请求值 + 静默降级后的真实文件"，这里按文件真实后缀回写，
        并把原请求值挪进 settings.requested_output_format。幂等，可 dry_run 预览。
        """
        session = get_session()
        scanned = mismatched = fixed = 0
        details = []
        try:
            tasks = (session.query(ConversionTask)
                     .filter(ConversionTask.status == "completed")
                     .filter(ConversionTask.output_path.isnot(None))
                     .all())
            for task in tasks:
                scanned += 1
                real = _format_of_path(task.output_path or "")
                if not real or (task.output_format or "").lower() == real:
                    continue
                mismatched += 1
                details.append({"id": task.id[:8], "stored": task.output_format,
                                "actual": real,
                                "output_path": os.path.basename(task.output_path or "")})
                if not dry_run:
                    settings = dict(task.settings or {})
                    settings.setdefault("requested_output_format", task.output_format)
                    settings["format_used"] = real
                    settings["reconciled_by"] = "PY-124"
                    task.settings = settings
                    task.output_format = real
                fixed += 1
            if not dry_run:
                session.commit()
        finally:
            session.close()
        return {"scanned": scanned, "mismatched": mismatched,
                "fixed": fixed if not dry_run else 0,
                "would_fix": fixed, "dry_run": dry_run, "details": details[:20]}

    @_retry_on_lock
    def reconcile_interrupted_tasks(self, active_ids=None,
                                    grace_seconds: Optional[float] = None,
                                    reason: str = RESTART_INTERRUPT_MESSAGE) -> dict:
        """PY-311: 启动对账 —— 把"上一个进程遗留的非终态任务"落成终态。

        旧行为（verify-round4 §1 PY-311 VERIFY_FAIL / §9 F-R4-1）: 后端被杀/重启后，
        `conversion_tasks` 里 status=running 的行永远停在 running、error_message=None，
        `/api/tasks/{id}/progress` 永久返回旧值 0.44 → 前端进度条一直"进行中"
        （实测全表 10 条僵尸行）。产品码里连 `aborted` 关键字都不存在。

        判定"僵尸"的三条门（缺一不动，避免误伤别的进程正在跑的任务）:
          1. status ∈ (running, queued, paused)  —— 非终态；
          2. id 不在本进程活跃集合里 —— 刚启动时该集合必为空集 ⇒ 全部对账；
          3. 距上次心跳(updated_at) > grace_seconds（默认 300s）—— 刚写过 DB 的行按
             "另有进程正在跑"放过。P7 长句合成可能几十分钟才回写一次，
             所以时间门只用于"防误伤"，不作为发现僵尸的手段。

        处置: status=failed + error_message="服务重启导致中断" + completed_at + logs 追加
        一条留痕（不改 progress，保留中断前的真实进度供事后查看）。
        """
        active = {str(x) for x in (active_ids or set())}
        grace = reconcile_grace_seconds() if grace_seconds is None else float(grace_seconds)
        now = datetime.utcnow()
        summary = {"scanned": 0, "reconciled": 0, "skipped_active": 0,
                   "skipped_fresh": 0, "ids": [], "reason": reason,
                   "grace_seconds": grace}
        session = get_session()
        try:
            rows = (session.query(ConversionTask)
                    .filter(ConversionTask.status.in_(NON_TERMINAL_STATUSES))
                    .all())
            for task in rows:
                summary["scanned"] += 1
                if task.id in active:
                    summary["skipped_active"] += 1
                    continue
                heartbeat = task.updated_at or task.started_at or task.created_at
                if grace > 0 and heartbeat and \
                        (now - heartbeat).total_seconds() < grace:
                    summary["skipped_fresh"] += 1
                    continue

                previous_status = task.status
                task.status = "failed"
                task.error_message = reason
                task.completed_at = now
                logs = list(task.logs or [])
                logs.append({
                    "stage": task.current_stage or "unknown",
                    "progress": round(min(float(task.progress or 0.0), 1.0), 4),
                    "message": (f"{reason}（启动对账 PY-311，原状态 {previous_status}"
                                f"，最后心跳 {heartbeat.isoformat() if heartbeat else '无'}）"),
                    "reconciled_by": "PY-311",
                })
                task.logs = logs[-128:]
                summary["reconciled"] += 1
                summary["ids"].append(task.id)
            if summary["reconciled"]:
                session.commit()
        finally:
            session.close()
        return summary

    @staticmethod
    def heartbeat_age_seconds(task: dict) -> Optional[float]:
        """F-R4-2: 距该行最后一次写库过了多少秒（无 updated_at 时回落 created_at）。"""
        if not isinstance(task, dict):
            return None
        raw = task.get("updated_at") or task.get("created_at")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        return (datetime.utcnow() - ts).total_seconds()

    def progress_staleness(self, task: dict) -> dict:
        """F-R4-2: 给"读回来的进度"打心跳标签。

        只加字段、**不改写 status**（DB 里是什么状态就报什么状态，不造假）:
          stale=False + age_seconds=...          : 终态行 / 心跳新鲜
          stale=True  + age_seconds + threshold   : 非终态但心跳远超阈值 —— 这行不会再
                                                   自己往前走，前端据此可以把进度条收尾
        （僵尸行的正解是 PY-311 启动对账把它落成 failed；本字段是"没重启也看门"的兜底信号。）
        """
        status = (task or {}).get("status") or ""
        threshold = progress_stale_seconds()
        if status in TERMINAL_STATUSES:
            return {"stale": False, "age_seconds": None,
                    "stale_after_seconds": threshold, "status": status}
        age = self.heartbeat_age_seconds(task)
        stale = age is not None and age > threshold
        return {"stale": bool(stale), "age_seconds": round(age, 1) if age is not None else None,
                "stale_after_seconds": threshold, "status": status}

    def delete_task(self, task_id: str) -> bool:
        """删除任务（PY-365 行为锁：只删库行，**绝不碰 task.output_path 的产物文件**）。

        红线：数据不丢 —— 用户可能顺手删单就毁了成品；孤儿文件由
        `backend/services/output_reconcile.py` 的盘点 + 显式 prune 处理。
        """
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            if task:
                session.delete(task)
                session.commit()
                return True
            return False
        finally:
            session.close()

    def _update_status(self, task_id: str, status: str) -> bool:
        """更新任务状态"""
        session = get_session()
        try:
            task = session.query(ConversionTask).filter_by(id=task_id).first()
            if task:
                task.status = status
                session.commit()
                return True
            return False
        finally:
            session.close()

    @_retry_on_lock
    def _cas_status(self, task_id: str, allowed_from: tuple, to_status: str) -> bool:
        """带状态门的状态迁移：`WHERE id=? AND status IN (allowed_from)` 的单条 UPDATE。

        判定写进 WHERE 而不是先读后写 —— 读与写之间 worker 可能已经把行收尾成
        终态（`update_progress` 就在干这件事），两次查询的写法会把那次收尾覆盖掉。
        返回 True 表示确实改动了这一行（rowcount>0）。
        """
        session = get_session()
        try:
            changed = session.query(ConversionTask).filter(
                ConversionTask.id == task_id,
                ConversionTask.status.in_(allowed_from),
            ).update({ConversionTask.status: to_status}, synchronize_session=False)
            session.commit()
            return bool(changed)
        finally:
            session.close()


# 全局单例
task_service = TaskService()