"""Shared deterministic diagnosis expansion; no models or evaluation answers."""
import json
from evidence.contracts import Finding, InvestigationOutput
from agents.contracts import Hypothesis, DiagnosisDraft
from agents.investigation import resolve_output, validate_output
from evidence.ownership import resolve_owner


class ProtocolError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def expand_refs(refs, registry):
    selected, seen = [], set()
    for item in refs:
        if item.reference_id not in registry.references or item.reference_id in seen:
            raise ProtocolError("UNKNOWN_OR_DUPLICATE_REFERENCE")
        seen.add(item.reference_id)
        selected.append(registry.references[item.reference_id].model_copy(deep=True))
    if selected:
        validate_output(InvestigationOutput(findings=[Finding(statement="Reference validation", refs=selected)]),
                        registry.evidence, ("observation", "runbook", "past_incident"))
    return selected


def expand_diagnosis(selection, registry):
    capabilities = getattr(registry.provider, 'repair_capabilities', [])
    for action in selection.recommended_actions:
        if action.repair is not None:
            intent = action.repair
            capability = next((item for item in capabilities if item['action'] == intent.action
                               and item['target'] == intent.target), None)
            if not capability or not action.requires_approval:
                raise ProtocolError('REPAIR_CAPABILITY_DENIED')
            if any(eid not in registry.evidence or registry.evidence[eid].kind != 'observation'
                   for eid in intent.evidence_ids):
                raise ProtocolError('REPAIR_CURRENT_EVIDENCE_REQUIRED')
            if (intent.action == 'scale_service' and intent.parameters['replicas'] > capability['max_replicas']
                    or intent.action == 'rollback_release' and intent.parameters['version'] not in capability['versions']):
                raise ProtocolError('REPAIR_PARAMETERS_DENIED')
    facts, _ = resolve_output(json.dumps({"findings": [f.model_dump() for f in selection.findings]}), registry)
    hypotheses = [Hypothesis(hypothesis_id=f"H{n}", cause=h.cause,
        support_refs=expand_refs(h.support_refs, registry), counter_refs=expand_refs(h.counter_refs, registry),
        pending_checks=list(h.pending_checks)) for n, h in enumerate(selection.hypotheses, 1)]
    return DiagnosisDraft(findings=facts.findings, hypotheses=hypotheses, recommended_actions=selection.recommended_actions,
                          missing_information=selection.missing_information, escalation_team=resolve_owner(selection.escalation_ref, registry))
