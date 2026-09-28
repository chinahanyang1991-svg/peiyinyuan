"""
转换任务数据模型
"""

import os
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, Text, Index
from backend.models.base import Base


class ConversionTask(Base):
    """转换任务模型"""
    __tablename__ = "conversion_tasks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, comment="关联配音项目ID")
    book_id = Column(String, comment="关联书籍ID")
    status = Column(String, default="queued", comment="状态: queued/running/paused/completed/failed/cancelled")
    progress = Column(Float, default=0.0, comment="进度 0.0-1.0")
    current_stage = Column(String, comment="当前阶段: import/parse/clean/split/analyze/assign/synthesize/postprocess/package")
    output_format = Column(String, default="m4b", comment="输出格式: m4b/mp3/wav/flac")
    output_path = Column(String, comment="输出文件路径")
    total_chars = Column(Integer, default=0, comment="总字符数")
    processed_chars = Column(Integer, default=0, comment="已处理字符数")
    error_message = Column(Text, comment="错误信息")
    settings = Column(JSON, default=dict, comment="转换设置快照")
    logs = Column(JSON, default=list, comment="阶段日志")
    started_at = Column(DateTime, comment="开始时间")
    completed_at = Column(DateTime, comment="完成时间")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # PERF-IDX: 任务列表固定是 `WHERE status=? ORDER BY created_at`，两列都缺索引时
    # 计划是 SCAN + TEMP B-TREE。
    __table_args__ = (
        Index("ix_conversion_tasks_status_created_at", status, created_at),
    )

    def to_dict(self):
        # PY-124: output_format 语义为"实际产物格式"；请求值单独放 settings 里，
        # 前端/播放器一律读 format_used，不再被静默降级产物骗到。
        real_format = ""
        if self.output_path:
            real_format = os.path.splitext(str(self.output_path))[1].lstrip(".").lower()
        # 产物真实字节数：作品库此前只有"字数×系数"的估算，而且展示时不标是估算，
        # 用户把它当成品体积读。这里给实时 stat（与下载端点 `api/tasks.py` 的
        # `os.path.exists(output_path)` 同一口径），文件未生成/已被移走时为 None，
        # 由前端明确标"约"或留空 —— 不再拿估算冒充事实。
        size_bytes = None
        if self.output_path:
            try:
                size_bytes = os.path.getsize(str(self.output_path))
            except OSError:
                size_bytes = None
        settings = self.settings or {}
        return {
            "id": self.id,
            "project_id": self.project_id,
            "book_id": self.book_id,
            "status": self.status,
            "progress": self.progress,
            "current_stage": self.current_stage,
            "output_format": self.output_format,
            "output_path": self.output_path,
            "output_size_bytes": size_bytes,
            "format_used": real_format or (self.output_format or ""),
            "requested_output_format": (settings.get("requested_output_format")
                                        or self.output_format),
            "total_chars": self.total_chars,
            "processed_chars": self.processed_chars,
            "error_message": self.error_message,
            "settings": self.settings,
            # PY-120: 阶段推进轨迹（update_progress 逐段写入），前端据此画 9 段进度
            "logs": self.logs or [],
            "elapsed_seconds": (
                (self.completed_at - self.started_at).total_seconds()
                if self.started_at and self.completed_at else None
            ),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            # PY-311/F-R4-2: 最后一次写库时间 = 心跳。verify-round4 §1 记录僵尸行时
            # 列表接口拿不到该字段（探针输出 "updated": null），前端无从判断停滞。
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<ConversionTask(id={self.id}, status={self.status}, progress={self.progress:.0%})>"