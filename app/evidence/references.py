"""Shared source selectors and expansion, independent of legacy agents."""
from evidence.contracts import Finding, InvestigationOutput
from evidence.model_view import evidence_view
from evidence.validation import validate_output


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
