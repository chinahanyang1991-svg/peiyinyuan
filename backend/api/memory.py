"""记忆管理 API"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from backend.services.memory_service import memory_service

router = APIRouter()


class CreateMemoryRequest(BaseModel):
    project_id: Optional[str] = None
    memory_type: str = "general"
    title: str
    content: str = ""
    tags: list = []


@router.get("/")
async def list_memories(
    project_id: Optional[str] = None,
    memory_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    memories = memory_service.list_all(project_id, memory_type, limit)
    return {"status": "ok", "memories": memories, "total": len(memories)}


@router.post("/")
async def create_memory(req: CreateMemoryRequest):
    memory = memory_service.create(
        project_id=req.project_id, memory_type=req.memory_type,
        title=req.title, content=req.content, tags=req.tags,
    )
    return {"status": "ok", "memory": memory}


@router.get("/search")
async def search_memories(q: str = Query(..., min_length=1), limit: int = 20):
    results = memory_service.search(q, limit)
    return {"status": "ok", "results": results, "total": len(results)}


@router.get("/{memory_id}")
async def get_memory(memory_id: str):
    m = memory_service.get(memory_id)
    if not m:
        raise HTTPException(404, "记忆不存在")
    return {"status": "ok", "memory": m}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str):
    if not memory_service.delete(memory_id):
        raise HTTPException(404, "记忆不存在")
    return {"status": "ok"}