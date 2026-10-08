import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from .run_router import _response, get_run, run_events
from backend.auth import Principal, get_current_principal
from backend.schemas import ResearchRequest, ResearchResponse
from backend.service import WorkflowService, get_workflow_service
from backend.service.workflow_service import CapacityExceeded

router = APIRouter(prefix="/api/v1/research", tags=["research"])


async def _start(payload, service, principal):
    request = principal.bind(payload.model_dump())
    try:
        return await asyncio.to_thread(service.start_run, request, principal)
    except CapacityExceeded as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
    except Exception as exc:
        logging.getLogger("backend.api").error("run_start_failed | exception_type=%s", type(exc).__name__)
        raise HTTPException(503, "研究服务或运行存储暂不可用。") from exc


@router.post("/run", response_model=ResearchResponse)
async def run_research(payload: ResearchRequest, workflow_service: WorkflowService = Depends(get_workflow_service),
                       principal: Principal = Depends(get_current_principal)):
    run_id = await _start(payload, workflow_service, principal)
    return await workflow_service.wait_result(run_id, principal)


@router.post("/stream")
async def stream_research(payload: ResearchRequest, workflow_service: WorkflowService = Depends(get_workflow_service),
                       principal: Principal = Depends(get_current_principal)):
    run_id = await _start(payload, workflow_service, principal)
    return _response(workflow_service, run_id, principal)



# Optional research compatibility routes share the IT run reader.
router.add_api_route("/runs/{run_id}", get_run, methods=["GET"])
router.add_api_route("/runs/{run_id}/events", run_events, methods=["GET"])
