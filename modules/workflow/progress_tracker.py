"""
Progress tracker for real-time conversion progress updates.
"""

import asyncio
import time
from typing import Optional, Callable

class ProgressTracker:
    """Tracks and broadcasts conversion progress."""
    
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.progress = 0.0  # 0.0 to 1.0
        self.current_stage = ""
        self.message = ""  # PY-115: 阶段内人类可读状态（如"模型加载中，已用 45s"）
        self.processed_chars = 0
        self.total_chars = 0
        self.status = "queued"
        self.error_message = ""
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.stage_history: list[dict] = []
        self._subscribers: list[Callable] = []
        self._lock = asyncio.Lock()
    
    def subscribe(self, callback: Callable):
        """Subscribe to progress updates: callback(progress_dict)."""
        self._subscribers.append(callback)
    
    async def update(self, progress: float, stage: str = "",
                     processed_chars: int = 0, total_chars: int = 0,
                     status: str = "", error: str = "", message: str = ""):
        """Update progress and notify all subscribers."""
        async with self._lock:
            self.progress = min(progress, 1.0)
            if stage:
                self.current_stage = stage
                self.stage_history.append({
                    "stage": stage,
                    "progress": progress,
                    "timestamp": time.time(),
                })
                if len(self.stage_history) > 256:  # PY-120: 轨迹上限保护
                    del self.stage_history[:len(self.stage_history) - 256]
            if message:
                self.message = message
            # 使用 is not None 而非真值检查，允许 0 值正确更新
            if processed_chars is not None:
                self.processed_chars = processed_chars
            if total_chars is not None:
                self.total_chars = total_chars
            if status:
                self.status = status
                if status == "running" and not self.started_at:
                    self.started_at = time.time()
                if status in ("completed", "failed", "cancelled"):
                    self.completed_at = time.time()
            if error:
                self.error_message = error

        await self._broadcast()
    
    async def _broadcast(self):
        """Notify all subscribers of current progress."""
        data = self.to_dict()
        for callback in self._subscribers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception:
                pass  # Silently ignore subscriber errors
    
    def _visited_stages(self) -> list:
        """PY-120: 去重后的阶段推进序列（轮询漏掉的瞬时阶段由此补齐）。"""
        seen = []
        for e in self.stage_history:
            s = e.get("stage")
            if s and s not in seen:
                seen.append(s)
        return seen

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "progress": self.progress,
            "current_stage": self.current_stage,
            "message": self.message,
            "processed_chars": self.processed_chars,
            "total_chars": self.total_chars,
            "status": self.status,
            "error_message": self.error_message,
            "stages_visited": self._visited_stages(),
            "stage_history": self.stage_history[-32:],
            "elapsed_seconds": (time.time() - self.started_at) if self.started_at else 0,
        }