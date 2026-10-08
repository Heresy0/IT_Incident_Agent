from uuid import UUID
from fastapi import APIRouter, Depends
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


@router.post('/incidents/{incident_id}/repairs/{plan_id}/execute')
async def execute(incident_id: UUID, plan_id: UUID, payload: RepairExecution,
                  service: WorkflowService = Depends(get_workflow_service),
                  principal: Principal = Depends(get_current_principal)):
    # Blocking platform requests run off the ASGI loop; storage claim prevents duplicate execution.
    return await call(service.repairs.execute, str(incident_id), str(plan_id), payload.model_dump(), principal)
