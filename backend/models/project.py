"""
配音项目数据模型
包含项目、角色、音色分配
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from backend.models.base import Base


# 项目状态取值表。book_missing 由 WF-10 引入：关联书籍被删除后，
# 项目仍可读（保留角色/分配等历史），但状态明确标失效，前端据此禁用"开始转换"。
PROJECT_STATUSES = ("draft", "ready", "converting", "done", "book_missing")
STATUS_BOOK_MISSING = "book_missing"


class DubbingProject(Base):
    """配音项目模型"""
    __tablename__ = "dubbing_projects"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, comment="项目名称")
    # 关联书籍 ID。注意：`book_id` 只有外键约束、没有 ORM relationship，
    # 且 SQLite 默认不开 PRAGMA foreign_keys，所以删书不会级联删项目、也不会报错
    # ——失效标记由 backend/services/book_service.delete_book 在同一事务里把
    # 引用该书的项目置为 status='book_missing'（WF-10）。
    book_id = Column(String, ForeignKey("books.id"), comment="关联书籍ID")
    mode = Column(String, default="single", comment="配音模式: single/multi/mixed")
    status = Column(
        String, default="draft",
        comment="状态: " + "/".join(PROJECT_STATUSES),
    )
    global_settings = Column(JSON, default=dict, comment="全局设置: speed/pitch/emotion")
    extra_data = Column(JSON, default=dict, comment="扩展元数据")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    roles = relationship("Role", back_populates="project", cascade="all, delete-orphan")
    assignments = relationship("VoiceAssignment", back_populates="project", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "book_id": self.book_id,
            "mode": self.mode,
            "status": self.status,
            "global_settings": self.global_settings,
            "role_count": len(self.roles) if self.roles else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<DubbingProject(id={self.id}, name={self.name}, mode={self.mode})>"


class Role(Base):
    """角色模型（用于多人配音）"""
    __tablename__ = "roles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("dubbing_projects.id"), nullable=False)
    name = Column(String, nullable=False, comment="角色名称: 旁白/主角/配角")
    role_type = Column(String, default="narrator", comment="角色类型: narrator/hero/supporting/custom")
    voice_id = Column(String, ForeignKey("voice_profiles.id"), comment="分配的音色ID")
    extra_data = Column(JSON, default=dict)

    project = relationship("DubbingProject", back_populates="roles")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "role_type": self.role_type,
            "voice_id": self.voice_id,
        }

    def __repr__(self):
        return f"<Role(id={self.id}, name={self.name}, type={self.role_type})>"


class VoiceAssignment(Base):
    """音色分配模型（文本片段级）"""
    __tablename__ = "voice_assignments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, ForeignKey("dubbing_projects.id"), nullable=False)
    chapter_id = Column(String, ForeignKey("chapters.id"), comment="关联章节ID")
    voice_id = Column(String, ForeignKey("voice_profiles.id"), comment="音色ID")
    role_id = Column(String, ForeignKey("roles.id"), comment="角色ID")
    start_char = Column(Integer, default=0, comment="起始字符位置")
    end_char = Column(Integer, default=0, comment="结束字符位置")
    text_snippet = Column(JSON, default=dict, comment="文本片段预览")
    settings = Column(JSON, default=dict, comment="局部设置: speed/pitch/emotion")
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("DubbingProject", back_populates="assignments")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "chapter_id": self.chapter_id,
            "voice_id": self.voice_id,
            "role_id": self.role_id,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "settings": self.settings,
        }

    def __repr__(self):
        return f"<VoiceAssignment(chapter={self.chapter_id}, voice={self.voice_id})>"