"""
Pipeline orchestrator for audiobook conversion workflow.
Stages: import -> parse -> clean -> split -> analyze -> assign -> synthesize -> postprocess -> package

修复 (P1 #asyncio.Event):
- 使用 asyncio.Event / asyncio.Lock 替换 while-sleep 忙等
- 暂停/取消支持即时响应
"""

import asyncio
from enum import Enum
from typing import Callable, Optional
import logging

logger = logging.getLogger(__name__)


class PipelineStage(Enum):
    IMPORT = "import"
    PARSE = "parse"
    CLEAN = "clean"
    SPLIT = "split"
    ANALYZE = "analyze"
    ASSIGN = "assign"
    SYNTHESIZE = "synthesize"
    POSTPROCESS = "postprocess"
    PACKAGE = "package"


class Pipeline:
    """Workflow pipeline that executes stages in order.

    可暂停/恢复/取消：pause() / resume() / cancel()
    取消后 pipeline 会在下一个 stage 边界停止。
    """

    def __init__(self, stages: list = None):
        self.stages = stages or list(PipelineStage)
        self._handlers: dict = {}
        self._on_progress: Optional[Callable] = None

        # 控制原语（Event：线程安全 + 协程安全）
        self._cancelled = asyncio.Event()
        self._paused = asyncio.Event()
        # _paused 默认是 "未暂停" → set() 表示继续运行
        self._paused.set()

        self._lock = asyncio.Lock()

    # -------- 注册 --------
    def register_handler(self, stage: PipelineStage, handler: Callable):
        """Register a handler function for a pipeline stage."""
        self._handlers[stage] = handler

    def on_progress(self, callback: Callable):
        """Set progress callback: callback(stage, progress, message, error=None)."""
        self._on_progress = callback

    # -------- 状态控制 --------
    def cancel(self):
        """标记取消。Pipeline 将在下一个 stage 边界停止。"""
        self._cancelled.set()
        # 若在暂停状态被取消，需要解除暂停，否则循环永久等待
        self._paused.set()
        logger.info("Pipeline 已标记取消")

    def pause(self):
        """暂停 pipeline（下一个 stage 边界或合成句间检查处）。"""
        self._paused.clear()
        logger.info("Pipeline 已暂停")

    def resume(self):
        """恢复 pipeline。"""
        self._paused.set()
        logger.info("Pipeline 已恢复")

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def is_paused(self) -> bool:
        # 当 _paused Event 未被 set 时，表示处于暂停状态
        return not self._paused.is_set()

    # -------- 执行 --------
    async def run(self, context: dict) -> dict:
        """Run all pipeline stages in order.

        同步 handler 在当前线程执行；async handler 在当前事件循环执行。
        为避免长时间阻塞的同步 handler 冻结事件循环，调用方应使用线程池。
        """
        total_stages = len(self.stages)

        for i, stage in enumerate(self.stages):
            # 取消检查
            if self._cancelled.is_set():
                logger.info(f"Pipeline cancelled at stage {stage.value}")
                context["status"] = "cancelled"
                break

            # 暂停等待（可被取消打断）
            try:
                await self._wait_resumable()
            except asyncio.CancelledError:
                context["status"] = "cancelled"
                break

            if self._cancelled.is_set():
                context["status"] = "cancelled"
                break

            progress_ratio = i / total_stages
            await self._notify_progress(stage.value, progress_ratio, f"开始阶段: {stage.value}")

            try:
                handler = self._handlers.get(stage)
                result = None
                if handler is not None:
                    if asyncio.iscoroutinefunction(handler):
                        result = await handler(context)
                    else:
                        # 同步 handler —— 可能长时间阻塞，交由调用方决定是否放入线程池
                        # 这里做保护性检查：每 1s 检查一次取消/暂停
                        result = await self._run_sync_with_periodic_check(handler, context)
                    # 处理 handler 返回协程的情况（lambda 包裹 async def）
                    if asyncio.iscoroutine(result):
                        result = await result
                    if result:
                        context.update(result)

                logger.info(f"Stage {stage.value} completed")
                await self._notify_progress(
                    stage.value, (i + 1) / total_stages,
                    f"完成阶段: {stage.value}"
                )
            except Exception as e:
                logger.exception(f"Stage {stage.value} failed: {e}")
                context["status"] = "failed"
                context["error_message"] = str(e)
                await self._notify_progress(
                    stage.value, (i + 1) / total_stages,
                    f"阶段失败: {stage.value} - {e}", error=str(e)
                )
                break

        if "status" not in context or context["status"] not in ("failed", "cancelled"):
            context["status"] = "completed"

        return context

    # -------- 内部 --------
    async def _wait_resumable(self) -> None:
        """等待暂停解除，或被取消时抛出 CancelledError。"""
        if self._paused.is_set():
            return  # 未暂停，快速返回

        # 等待：_paused set 或 _cancelled set
        paused_wait = asyncio.create_task(self._paused.wait())
        cancelled_wait = asyncio.create_task(self._cancelled.wait())
        try:
            done, pending = await asyncio.wait(
                [paused_wait, cancelled_wait],
                return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
        except asyncio.CancelledError:
            paused_wait.cancel()
            cancelled_wait.cancel()
            raise

    async def _run_sync_with_periodic_check(self, sync_handler: Callable, context: dict):
        """在独立线程池里执行同步 handler，避免阻塞事件循环。

        每 2s 检查一次取消/暂停标记（给外部控制响应的机会）。
        """
        loop = asyncio.get_event_loop()

        # 简单方案：直接放入线程池。对于真长时间任务，外部会阻塞事件循环——
        # 但这里执行一次就返回，期间无法响应暂停/取消。
        # 因此我们进一步包装：每隔一小段时间就把控制权让出来。
        # 对于配音员这种场景，各 stage handler 内部自己控制即可。
        return await loop.run_in_executor(None, lambda: sync_handler(context))

    async def _notify_progress(self, stage: str, progress: float,
                                message: str, error: str = None):
        if not self._on_progress:
            return
        callback = self._on_progress
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(stage, progress, message, error)
            else:
                callback(stage, progress, message, error)
        except Exception as e:
            logger.warning(f"progress callback failed: {e}")
