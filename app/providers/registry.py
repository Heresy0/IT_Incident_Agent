"""Server-owned service bindings; callers never select credentials or endpoints."""
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from evidence.contracts import Ticket
from providers.fixtures import ProviderError


class ObservationProvider(Protocol):
    name: str
    binding: 'ServiceBinding'
    def ticket(self): ...
    def authorize(self, principal, scope): ...
    def query(self, name, args, scope, *, max_metric_points=60): ...


@dataclass(frozen=True)
class ServiceBinding:
    tenant_id: str
    users: tuple[str, ...]
    service: str
    environment: str
    version: str
    dependencies: tuple[str, ...] = ()
    observations: dict = field(default_factory=dict, repr=False)
    repairs: dict = field(default_factory=dict, repr=False)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()


class ServiceRegistry:
    def __init__(self, bindings=(), provider_factory=None):
        from copy import deepcopy
        self._bindings = deepcopy(tuple(bindings))
        self.provider_factory = provider_factory
        keys = [(b.tenant_id, b.service, b.environment) for b in self._bindings]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate service binding')

    def catalog(self, principal):
        return [{'service': binding.service, 'environment': binding.environment, 'service_version': binding.version,
                 'allowed_dependencies': list(binding.dependencies),
                 'metrics': [{'name': name, 'unit': item['unit']} for name, item in binding.observations.get('metrics', {}).items()]}
                for binding in self._bindings if binding.tenant_id == principal.tenant_id and principal.user_id in binding.users]

    def resolve(self, snapshot, principal):
        from copy import deepcopy
        for binding in self._bindings:
            if (binding.tenant_id, binding.service, binding.environment, binding.version) == (
                    principal.tenant_id, snapshot['service'], snapshot['environment'], snapshot['service_version']):
                if principal.user_id in binding.users:
                    return deepcopy(binding)
        raise ProviderError('SERVICE_NOT_REGISTERED')

    def ticket(self, snapshot, principal):
        binding = self.resolve(snapshot, principal)
        return Ticket.model_validate({
            'incident_id': snapshot['id'], 'title': snapshot['title'], 'symptoms': snapshot['symptoms'],
            'purpose': snapshot.get('purpose', 'diagnosis'),
            'scope': {'tenant_id': principal.tenant_id, 'user_id': principal.user_id,
                      'service': binding.service, 'environment': binding.environment,
                      'service_version': binding.version, 'allowed_dependencies': binding.dependencies,
                      'start': datetime.fromisoformat(snapshot['start']),
                      'end': datetime.fromisoformat(snapshot['end'])}})

    def provider(self, snapshot, principal):
        from providers.http import HTTPObservationProvider
        return (self.provider_factory or HTTPObservationProvider)(
            self.resolve(snapshot, principal), self.ticket(snapshot, principal), principal)

    @classmethod
    def from_file(cls, path):
        from providers.configuration import read_configuration, validate_configuration, ServiceConfigurationError
        data = read_configuration(path)
        issues = validate_configuration(data)
        errors = [issue for issue in issues if issue['level'] == 'error']
        if errors:
            raise ServiceConfigurationError(errors)
        bindings = []
        # Validate without normalizing stored values: existing binding fingerprints stay stable.
        for row in data['services']:
            row = dict(row)
            for key in ('users', 'dependencies'):
                row[key] = tuple(row.get(key, ()))
            bindings.append(ServiceBinding(**row))
        return cls(bindings)

    @classmethod
    def from_environment(cls):
        path = os.getenv('INCIDENT_SERVICES_FILE')
        return cls.from_file(path) if path else cls()
