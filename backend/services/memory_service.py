"""记忆管理服务"""

import uuid
from datetime import datetime
from typing import Optional
from backend.models.base import get_session
from backend.models.memory import Memory


class MemoryService:
    def create(self, project_id: str = None, memory_type: str = "general",
               title: str = "", content: str = "", tags: list = None) -> dict:
        session = get_session()
        try:
            m = Memory(
                id=str(uuid.uuid4()),
                project_id=project_id,
                memory_type=memory_type,
                title=title,
                content=content,
                tags=tags or [],
            )
            session.add(m)
            session.commit()
            session.refresh(m)
            return m.to_dict()
        finally:
            session.close()

    def get(self, memory_id: str) -> Optional[dict]:
        session = get_session()
        try:
            m = session.query(Memory).filter_by(id=memory_id).first()
            return m.to_dict() if m else None
        finally:
            session.close()

    def list_all(self, project_id: str = None, memory_type: str = None,
                 limit: int = 50) -> list:
        session = get_session()
        try:
            q = session.query(Memory)
            if project_id:
                q = q.filter_by(project_id=project_id)
            if memory_type:
                q = q.filter_by(memory_type=memory_type)
            memories = q.order_by(Memory.updated_at.desc()).limit(limit).all()
            return [m.to_dict() for m in memories]
        finally:
            session.close()

    def search(self, query: str, limit: int = 20) -> list:
        session = get_session()
        try:
            q = session.query(Memory).filter(
                Memory.title.contains(query) | Memory.content.contains(query)
            ).order_by(Memory.updated_at.desc()).limit(limit)
            return [m.to_dict() for m in q.all()]
        finally:
            session.close()

    def delete(self, memory_id: str) -> bool:
        session = get_session()
        try:
            m = session.query(Memory).filter_by(id=memory_id).first()
            if not m:
                return False
            session.delete(m)
            session.commit()
            return True
        finally:
            session.close()


memory_service = MemoryService()