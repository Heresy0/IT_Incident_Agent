"""Expand observed owner selectors; keep exact legacy names out of model schemas."""
import json


class OwnerSelectionError(ValueError):
    def __init__(self, reason, path='escalation_ref'):
        self.reason, self.path = reason, path
        super().__init__(reason)


def observed_owner_team(item):
    if item is None or item.kind != 'observation' or not item.locator.startswith('get_service_owner/'):
        return None
    team = item.payload.get('team')
    return team if isinstance(team, str) and 1 <= len(team) <= 80 else None


def resolve_owner(selected, registry):
    if selected is None:
        return None
    ref = registry.references.get(selected.reference_id)
    item = registry.evidence.get(ref.evidence_id) if ref else None
    if ref is None or item is None:
        raise OwnerSelectionError('unknown_owner_reference')
    team = observed_owner_team(item)
    if team is None or ref.field_path != 'team' or ref.value != team:
        raise OwnerSelectionError('non_owner_reference')
    return team


def parse_owner_selection(schema, content, registry):
    data = json.loads(content)
    # Old scripted/model inputs remain readable, but must match an observed owner
    # exactly. Public reports still use their original expanded output contracts.
    if isinstance(data, dict) and 'escalation_team' in data:
        data = dict(data)
        team = data.pop('escalation_team')
        if team is not None:
            if not isinstance(team, str) or not 1 <= len(team) <= 80:
                raise OwnerSelectionError('unobserved_team', 'escalation_team')
            candidates = sorted(rid for rid, ref in registry.references.items()
                if ref.field_path == 'team' and ref.value == team and
                observed_owner_team(registry.evidence.get(ref.evidence_id)) == team)
            if not candidates:
                raise OwnerSelectionError('unobserved_team', 'escalation_team')
            if data.get('escalation_ref') is None:
                data['escalation_ref'] = {'reference_id': candidates[0]}
        selection = schema.model_validate_json(json.dumps(data))
        if resolve_owner(selection.escalation_ref, registry) != team:
            raise OwnerSelectionError('conflicting_owner_selection')
        return selection
    return schema.model_validate_json(content)
