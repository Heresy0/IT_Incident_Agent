import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict

from backend.auth import Principal, get_current_principal
from backend.service import get_workflow_service

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])
THREAD_PATTERN = r"^[A-Za-z0-9_-]{1,80}$"


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(default="", max_length=40)
    language: Literal["zh-CN", "en"] = "zh-CN"
    response_style: Literal["通俗", "专业", "简洁", "详细"] = "简洁"


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=1600)
    thread_id: str = Field(default="default_thread", pattern=THREAD_PATTERN)


async def manager(service=Depends(get_workflow_service)):
    try:
        return await asyncio.to_thread(service.get_memory_manager)
    except Exception:
        raise HTTPException(503, "记忆服务未启用或暂不可用；不会回退到未隔离的存储。") from None


@router.get("")
async def read_memory(thread_id: str = Query(default="default_thread", pattern=THREAD_PATTERN),
                      principal: Principal = Depends(get_current_principal), memory=Depends(manager)):
    def read():
        return {"profile": memory.get_profile(principal.tenant_id, principal.user_id),
                "session": memory.session(principal.tenant_id, principal.user_id, thread_id),
                "memories": memory.list_memories(principal.tenant_id, principal.user_id, thread_id if memory.scope == "thread" else None)}
    return await asyncio.to_thread(read)


@router.put("/profile")
async def save_profile(payload: Profile, principal: Principal = Depends(get_current_principal), memory=Depends(manager)):
    return await asyncio.to_thread(memory.update_profile, principal.tenant_id, principal.user_id, payload.model_dump())


@router.post("/notes", status_code=201)
async def add_note(payload: Note, principal: Principal = Depends(get_current_principal), memory=Depends(manager)):
    try:
        memory_id = await asyncio.to_thread(memory.add_note, principal.tenant_id, principal.user_id, payload.content, payload.thread_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": memory_id}


@router.delete("")
async def clear_memory(target: Literal["all", "session", "long_term", "entry"] = "session",
                       thread_id: str | None = Query(default=None, pattern=THREAD_PATTERN),
                       memory_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
                       principal: Principal = Depends(get_current_principal), memory=Depends(manager)):
    try:
        return await asyncio.to_thread(memory.clear, principal.tenant_id, principal.user_id,
                                       target=target, thread_id=thread_id, memory_id=memory_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
