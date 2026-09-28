"""工作流模板数据模型"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, JSON, DateTime
from backend.models.base import Base


class WorkflowTemplate(Base):
    __tablename__ = "workflow_templates"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    description = Column(String, default="")
    stages = Column(JSON, default=list)
    engine = Column(String, default="voxcpm")
    output_format = Column(String, default="m4b")
    settings = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "stages": self.stages,
            "engine": self.engine,
            "output_format": self.output_format,
            "settings": self.settings,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }