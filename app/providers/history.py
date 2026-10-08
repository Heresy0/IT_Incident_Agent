"""Owner-only confirmed experience shared by fixture and registered providers."""
import re
from providers.fixtures import ProviderError
from providers.http import safe_text


def private_history(store, principal, scope, args):
    terms = set(re.findall(r'\w+', args.symptoms.casefold()))
    result = []
    for row in store.confirmed_cases(principal):
        incident, confirmation = row['document']['incident'], row['document']['confirmation']
        if (incident['service'], incident['environment'], incident['service_version']) != (
                scope.service, scope.environment, scope.service_version):
            continue
        text = confirmation['confirmed_cause'] + ' ' + confirmation['resolution']
        if (not terms.intersection(re.findall(r'\w+', text.casefold()))
                or args.error_code and args.error_code not in confirmation['error_codes']
                or args.version and args.version != scope.service_version):
            continue
        result.append({'id': row['id'], 'timestamp': row['confirmed_at'], 'service': scope.service,
            'environment': scope.environment, 'data_version': f"manual-{row['revision']}", 'text': safe_text(text),
            'resolution': safe_text(confirmation['resolution']), 'confirmed': True, 'confirmed_at': row['confirmed_at'],
            'versions': [scope.service_version], 'validity': 'active'})
    return result


class ProviderWithHistory:
    def __init__(self, base, store, principal):
        self.base, self.store, self.principal = base, store, principal
        self.binding = base.binding
        self.name = base.name + '_with_private_history'

    def ticket(self):
        return self.base.ticket()

    def authorize(self, principal, scope):
        self.base.authorize(principal, scope)

    def query(self, name, args, scope):
        self.authorize(self.principal, scope)
        if name != 'search_incidents':
            return self.base.query(name, args, scope)
        try:
            rows, truncated = self.base.query(name, args, scope)
        except ProviderError as exc:
            if exc.code != 'SOURCE_NOT_CONFIGURED':
                raise
            rows, truncated = [], False
        rows = [*private_history(self.store, self.principal, scope, args), *rows]
        return rows[:4], truncated or len(rows) > 4
