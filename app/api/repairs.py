from uuid import UUID
from fastapi import APIRouter, Depends, Query
from api.auth import Principal, get_current_principal
from api.incidents import call
from repairs.contracts import RepairProposal, RepairApproval, RepairExecution, RepairFromRecommendation, ACTIONS
from workflow.dependencies import WorkflowService, get_workflow_service

router = APIRouter(prefix='/api/v1', tags=['repairs'])


@router.get('/repair-actions')
def actions(principal: Principal = Depends(get_current_principal)):
    return {'items': [{'action': action, **definition, 'requires_approval': True} for action, definition in ACTIONS.items()]}


@router.get('/incidents/{incident_id}/repairs')
async def listing(incident_id: UUID, service: WorkflowService = Depends(get_workflow_service),
                  principal: Principal = Depends(get_current_principal)):
    return {'items': await call(service.repairs.list, str(incident_id), principal)}


@router.get('/incidents/{incident_id}/repair-capabilities')
async def capabilities(incident_id: UUID, service: WorkflowService = Depends(get_workflow_service),
                       principal: Principal = Depends(get_current_principal)):
    return {'items': await call(service.repairs.incident_capabilities, str(incident_id), principal)}


@router.post('/incidents/{incident_id}/repairs', status_code=201)
async def propose(incident_id: UUID, payload: RepairProposal, service: WorkflowService = Depends(get_workflow_service),
                  principal: Principal = Depends(get_current_principal)):
    return await call(service.repairs.propose, str(incident_id), payload.model_dump(), principal)


@router.post('/incidents/{incident_id}/repairs/from-recommendation', status_code=201)
async def from_recommendation(incident_id: UUID, payload: RepairFromRecommendation,
                  service: WorkflowService = Depends(get_workflow_service),
                  principal: Principal = Depends(get_current_principal)):
    return await call(service.repairs.from_recommendation, str(incident_id), payload.model_dump(), principal)


@router.post('/incidents/{incident_id}/repairs/{plan_id}/approval')
async def approval(incident_id: UUID, plan_id: UUID, payload: RepairApproval,
                   service: WorkflowService = Depends(get_workflow_service),
                   principal: Principal = Depends(get_current_principal)):
    return await call(service.repairs.approve, str(incident_id), str(plan_id), payload.model_dump(), principal)


@router.get('/incidents/{incident_id}/repairs/{plan_id}')
async def detail(incident_id: UUID, plan_id: UUID, service: WorkflowService = Depends(get_workflow_service),
                 principal: Principal = Depends(get_current_principal)):
    return await call(service.repairs.get, str(incident_id), str(plan_id), principal)


@router.get('/incidents/{incident_id}/repairs/{plan_id}/events')
async def events(incident_id: UUID, plan_id: UUID, after_seq: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                 service: WorkflowService = Depends(get_workflow_service),
                 principal: Principal = Depends(get_current_principal)):
    plan = await call(service.repairs.get, str(incident_id), str(plan_id), principal)
    batch = [event for event in plan['events'] if event['seq'] > after_seq][:limit]
    return {'items': batch, 'status': plan['status'], 'effective_status': plan['effective_status'],
            'next_seq': batch[-1]['seq'] if batch else after_seq,
            'has_more': bool(batch and batch[-1]['seq'] < len(plan['events']))}


@router.post('/incidents/{incident_id}/repairs/{plan_id}/execute')
async def execute(incident_id: UUID, plan_id: UUID, payload: RepairExecution,
                  service: WorkflowService = Depends(get_workflow_service),
                  principal: Principal = Depends(get_current_principal)):
    # Blocking platform requests run off the ASGI loop; storage claim prevents duplicate execution.
    return await call(service.repairs.execute, str(incident_id), str(plan_id), payload.model_dump(), principal)
