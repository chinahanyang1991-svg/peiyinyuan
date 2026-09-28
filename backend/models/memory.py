"""记忆数据模型"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, JSON, DateTime
from backend.models.base import Base


class Memory(Base):
    __tablename__ = "memories"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String, nullable=True, index=True)
    memory_type = Column(String, default="general")
    title = Column(String, nullable=False)
    content = Column(Text, default="")
    tags = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "memory_type": self.memory_type,
            "title": self.title,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }