"""Shared authenticated run results and replay, independent of workflow type."""
import asyncio
import json
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from api.auth import Principal, get_current_principal
from workflow.dependencies import WorkflowService, get_workflow_service

router = APIRouter(prefix="/api/v1", tags=["runs"])

def _response(service, run_id, principal, after=0):
    async def stream():
        async for event in service.events(run_id, after, principal):
            yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": run_id, "X-Research-Run-ID": run_id})


@router.get("/runs/{run_id}")
async def get_run(run_id: UUID, workflow_service: WorkflowService = Depends(get_workflow_service),
                       principal: Principal = Depends(get_current_principal)):
    run = await asyncio.to_thread(workflow_service.get_run, str(run_id), principal)
    if not run:
        raise HTTPException(404, "运行记录不存在。")
    return run


@router.get("/runs/{run_id}/events")
async def run_events(run_id: UUID, after_seq: int = Query(default=0, ge=0),
                     last_event_id: str | None = Header(default=None),
                     workflow_service: WorkflowService = Depends(get_workflow_service),
                       principal: Principal = Depends(get_current_principal)):
    run_id = str(run_id)
    if not await asyncio.to_thread(workflow_service.get_run, run_id, principal):
        raise HTTPException(404, "运行记录不存在。")
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
            if cursor < 0:
                raise ValueError()
            after_seq = max(after_seq, cursor)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID 必须是非负事件序号。") from exc
    return _response(workflow_service, run_id, principal, after_seq)
