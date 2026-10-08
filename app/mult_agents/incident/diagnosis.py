"""Shared deterministic diagnosis expansion; no models or evaluation answers."""
import json
from .contracts import Finding, InvestigationOutput
from .collaboration_contracts import Hypothesis, DiagnosisDraft
from .investigation import resolve_output, validate_output
from .ownership import resolve_owner


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
    facts, _ = resolve_output(json.dumps({"findings": [f.model_dump() for f in selection.findings]}), registry)
    hypotheses = [Hypothesis(hypothesis_id=f"H{n}", cause=h.cause,
        support_refs=expand_refs(h.support_refs, registry), counter_refs=expand_refs(h.counter_refs, registry),
        pending_checks=list(h.pending_checks)) for n, h in enumerate(selection.hypotheses, 1)]
    return DiagnosisDraft(findings=facts.findings, hypotheses=hypotheses, recommended_actions=selection.recommended_actions,
                          missing_information=selection.missing_information, escalation_team=resolve_owner(selection.escalation_ref, registry))
