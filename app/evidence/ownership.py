"""Read only owner teams present in registered observation evidence."""


def observed_owner_team(item):
    if item is None or item.kind != 'observation' or not item.locator.startswith('get_service_owner/'):
        return None
    team = item.payload.get('team')
    return team if isinstance(team, str) and 1 <= len(team) <= 80 else None
