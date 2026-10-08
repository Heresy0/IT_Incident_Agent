"""Owner-bound incident CRUD; subscriptions reuse the existing run endpoints."""
import asyncio
import logging
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from api.auth import Principal, get_current_principal
from incidents.contracts import IncidentCreate, IncidentPatch, DiagnosisRequest, ResolutionRequest
from workflow.dependencies import WorkflowService, get_workflow_service
from incidents.support import IncidentError
from providers.bound import catalog
from workflow.service import CapacityExceeded
from providers.fixtures import ProviderError

router = APIRouter(prefix="/api/v1", tags=["incidents"])


async def call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except IncidentError as exc:
        raise HTTPException(exc.status, exc.code) from None
    except ProviderError as exc:
        raise HTTPException(422, exc.code) from None
    except CapacityExceeded as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from None
    except Exception as exc:
        logging.getLogger("incident.api").error("incident_api_failed | exception_type=%s", type(exc).__name__)
        raise HTTPException(503, "工单服务或存储暂不可用。") from None


@router.get("/incident-demo-cases")
async def demos(principal: Principal = Depends(get_current_principal)):
    return {"items": await call(catalog, principal)}


@router.get('/incident-services')
async def registered_services(service: WorkflowService = Depends(get_workflow_service),
                              principal: Principal = Depends(get_current_principal)):
    return {'items': await call(service.services.catalog, principal)}


@router.post("/incidents", status_code=201)
async def create(payload: IncidentCreate, service: WorkflowService = Depends(get_workflow_service),
                 principal: Principal = Depends(get_current_principal)):
    return await call(service.create_incident, payload.model_dump(mode="json"), principal)


@router.get("/incidents")
async def listing(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                  service: WorkflowService = Depends(get_workflow_service), principal: Principal = Depends(get_current_principal)):
    return {"items": await call(service.list_incidents, principal, offset, limit), "offset": offset, "limit": limit}


@router.get("/incidents/{incident_id}")
async def detail(incident_id: UUID, service: WorkflowService = Depends(get_workflow_service),
                 principal: Principal = Depends(get_current_principal)):
    return await call(service.get_incident, str(incident_id), principal)


@router.patch("/incidents/{incident_id}")
async def patch(incident_id: UUID, payload: IncidentPatch, service: WorkflowService = Depends(get_workflow_service),
                principal: Principal = Depends(get_current_principal)):
    return await call(service.patch_incident, str(incident_id), payload.changes.model_dump(mode="json"), payload.revision, principal)


@router.post("/incidents/{incident_id}/diagnoses", status_code=202)
async def diagnose(incident_id: UUID, payload: DiagnosisRequest, service: WorkflowService = Depends(get_workflow_service),
                   principal: Principal = Depends(get_current_principal)):
    run_id, replayed = await call(service.start_diagnosis, str(incident_id), payload.model_dump(), principal)
    return {"run_id": run_id, "incident_id": str(incident_id), "replayed": replayed,
        "result_url": f"/api/v1/runs/{run_id}", "event_url": f"/api/v1/runs/{run_id}/events"}


@router.post("/incidents/{incident_id}/resolution")
async def resolve(incident_id: UUID, payload: ResolutionRequest, service: WorkflowService = Depends(get_workflow_service),
                  principal: Principal = Depends(get_current_principal)):
    return await call(service.confirm_incident, str(incident_id), payload.model_dump(), principal)
