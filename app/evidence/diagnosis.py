"""Shared deterministic diagnosis expansion; no models or evaluation answers."""
import json
from evidence.contracts import Finding, InvestigationOutput
from agents.contracts import Hypothesis, DiagnosisDraft
from agents.investigation import resolve_output, validate_output
from evidence.ownership import resolve_owner
from evidence.model_view import evidence_view


class ProtocolError(ValueError):
    def __init__(self, code, details=None):
        self.code = code
        self.details = details or []
        super().__init__(code)


def diagnosis_reference_options(registry):
    """Only source fields exposed to the model, never hidden snapshot metadata."""
    if not registry.references:
        return []
    return [{'reference_id': option['reference_id'], 'evidence_id': evidence.evidence_id,
             'field_path': option['field_path']}
            for evidence in registry.evidence.values()
            for option in evidence_view(evidence)['reference_options']
            if option['reference_id'] in registry.references]


def validate_selected_refs(refs, registry, path, allowed_ids):
    seen = set()
    for index, item in enumerate(refs):
        location = f'{path}.{index}.reference_id'
        if item.reference_id not in allowed_ids:
            raise ProtocolError('UNKNOWN_REFERENCE', [{'path': location, 'type': 'unknown_reference',
                'instruction': '只能选择本次提供的 reference_options 中真实的 REF_ 编号，不能填写 EV_ 编号或自行构造。'}])
        if item.reference_id in seen:
            raise ProtocolError('DUPLICATE_REFERENCE', [{'path': location, 'type': 'duplicate_reference',
                'instruction': '同一个引用列表内每个 reference_id 只能出现一次；删除该重复项，保留原有有效引用。'}])
        seen.add(item.reference_id)


def expand_refs(refs, registry, path='refs', *, allowed_ids=None):
    if allowed_ids is None:
        allowed_ids = {option['reference_id'] for option in diagnosis_reference_options(registry)}
    validate_selected_refs(refs, registry, path, allowed_ids)
    selected = [registry.references[item.reference_id].model_copy(deep=True) for item in refs]
    if selected:
        validate_output(InvestigationOutput(findings=[Finding(statement="Reference validation", refs=selected)]),
                        registry.evidence, ("observation", "runbook", "past_incident"))
    return selected


def expand_diagnosis(selection, registry):
    allowed_ids = {option['reference_id'] for option in diagnosis_reference_options(registry)}
    for index, finding in enumerate(selection.findings):
        validate_selected_refs(finding.refs, registry, f'findings.{index}.refs', allowed_ids)
    capabilities = getattr(getattr(registry, 'provider', None), 'repair_capabilities', [])
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
        support_refs=expand_refs(h.support_refs, registry, f'hypotheses.{n - 1}.support_refs', allowed_ids=allowed_ids),
        counter_refs=expand_refs(h.counter_refs, registry, f'hypotheses.{n - 1}.counter_refs', allowed_ids=allowed_ids),
        pending_checks=list(h.pending_checks)) for n, h in enumerate(selection.hypotheses, 1)]
    return DiagnosisDraft(findings=facts.findings, hypotheses=hypotheses, recommended_actions=selection.recommended_actions,
                          missing_information=selection.missing_information, escalation_team=resolve_owner(selection.escalation_ref, registry))
