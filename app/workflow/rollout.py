"""Deterministic admission policy; it never reads a provider or starts a shadow run.

Acceptance evidence is injected as parsed data by the service operator. File
paths, credentials, queries and production write permissions are not policy inputs.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
import re


POLICY_VERSION = 'incident_rollout_v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def configuration_fingerprint(model_settings_sha256, variant, enable_challenge, limits, *, execution_mode='live'):
    """Bind acceptance to the actual model settings and admission budget envelope."""
    return _digest({'model_settings_sha256': model_settings_sha256, 'execution_variant': variant,
                    'enable_challenge': bool(enable_challenge), 'limits': limits,
                    'execution_mode': execution_mode})


@dataclass(frozen=True)
class AcceptanceGate:
    source_version: str
    review_policy_version: str
    evaluation_hash: str
    verdict: str = 'hold'
    configuration_sha256: str | None = None

    def __post_init__(self):
        if self.verdict not in {'pass', 'hold'}:
            raise ValueError('Unsupported acceptance verdict')
        if any(not isinstance(value, str) or not value.strip() or len(value) > 80
               for value in (self.source_version, self.review_policy_version)):
            raise ValueError('Acceptance versions must be bounded nonblank strings')
        if not isinstance(self.evaluation_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', self.evaluation_hash):
            raise ValueError('Acceptance evaluation_hash must be a SHA256 digest')
        if self.configuration_sha256 is not None and (not isinstance(self.configuration_sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', self.configuration_sha256)):
            raise ValueError('Acceptance configuration_sha256 must be a SHA256 digest')

    def status(self, source_version, review_policy_version, configuration_sha256=None):
        if self.verdict != 'pass':
            return 'hold', 'acceptance_hold'
        if self.source_version != source_version:
            return 'hold', 'acceptance_source_changed'
        if self.review_policy_version != review_policy_version:
            return 'hold', 'acceptance_review_policy_changed'
        if self.configuration_sha256 is None:
            return 'hold', 'acceptance_configuration_missing'
        if self.configuration_sha256 != configuration_sha256:
            return 'hold', 'acceptance_configuration_changed'
        return 'pass', 'acceptance_pass'


@dataclass(frozen=True)
class RolloutPolicy:
    mode: str = 'off'
    percent: int = 0
    salt: str = field(default='incident-canary-v1', repr=False)
    tenants: tuple[str, ...] = field(default=(), repr=False)
    services: tuple[str, ...] = field(default=(), repr=False)
    variant: str = 'single_pass'
    enable_challenge: bool = True
    shadow: bool = False
    gate: AcceptanceGate | None = None

    def __post_init__(self):
        if self.mode not in {'off', 'canary', 'full'}:
            raise ValueError('Unsupported rollout mode')
        if type(self.percent) is not int or not 0 <= self.percent <= 100:
            raise ValueError('Rollout percent must be an integer from 0 to 100')
        if self.variant not in {'single_pass', 'closed_loop'}:
            raise ValueError('Unsupported rollout variant')
        if type(self.shadow) is not bool or type(self.enable_challenge) is not bool:
            raise ValueError('Rollout shadow and challenge flags must be boolean')
        if not isinstance(self.salt, str) or not self.salt.strip() or len(self.salt) > 200:
            raise ValueError('Rollout salt must be bounded nonblank text')
        for name in ('tenants', 'services'):
            values = getattr(self, name)
            if (not isinstance(values, tuple) or len(values) > 1000
                    or any(not isinstance(value, str) or not value.strip() or len(value) > 80 for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError('Rollout audience must contain distinct bounded identifiers')
        if self.gate is not None and not isinstance(self.gate, AcceptanceGate):
            raise ValueError('Rollout gate must be an AcceptanceGate')

    @classmethod
    def parse(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError('Rollout configuration must be an object')
        data = dict(value)
        for name in ('tenants', 'services'):
            if isinstance(data.get(name), list):
                data[name] = tuple(data[name])
        if isinstance(data.get('gate'), dict):
            data['gate'] = AcceptanceGate(**data['gate'])
        return cls(**data)

    @classmethod
    def from_environment(cls):
        mode = os.getenv('INCIDENT_ROLLOUT_MODE')
        if mode is None:
            return None
        shadow = os.getenv('INCIDENT_ROLLOUT_SHADOW', 'false').lower()
        challenge = os.getenv('INCIDENT_ROLLOUT_CHALLENGE', 'true').lower()
        if any(flag not in {'true', 'false', '1', '0'} for flag in (shadow, challenge)):
            raise ValueError('Rollout boolean settings must be true or false')
        gate = os.getenv('INCIDENT_ROLLOUT_GATE')
        return cls.parse({'mode': mode, 'percent': int(os.getenv('INCIDENT_ROLLOUT_PERCENT', '0')),
            'salt': os.getenv('INCIDENT_ROLLOUT_SALT', 'incident-canary-v1'),
            'tenants': tuple(filter(None, (v.strip() for v in os.getenv('INCIDENT_ROLLOUT_TENANTS', '').split(',')))),
            'services': tuple(filter(None, (v.strip() for v in os.getenv('INCIDENT_ROLLOUT_SERVICES', '').split(',')))),
            'variant': os.getenv('INCIDENT_ROLLOUT_VARIANT', 'single_pass'),
            'enable_challenge': challenge in {'true', '1'},
            'shadow': shadow in {'true', '1'}, 'gate': json.loads(gate) if gate else None})

    def summary(self, source_version, review_policy_version, configuration_sha256=None):
        status, reason = self.gate.status(source_version, review_policy_version, configuration_sha256) if self.gate else (
            'hold', 'acceptance_missing')
        return {'policy_version': POLICY_VERSION, 'mode': self.mode, 'percent': self.percent,
            'effective_percent': 100 if self.mode == 'full' else self.percent if self.mode == 'canary' else 0,
            'variant': self.variant, 'enable_challenge': self.enable_challenge, 'policy_sha256': _digest(asdict(self)),
            'salt_sha256': hashlib.sha256(self.salt.encode()).hexdigest(),
            'tenant_allowlist_count': len(self.tenants), 'service_allowlist_count': len(self.services),
            'acceptance': {'verdict': status, 'reason': reason,
                           'evaluation_hash': self.gate.evaluation_hash if self.gate else None,
                           'configuration_sha256': self.gate.configuration_sha256 if self.gate else None,
                           'source_version': self.gate.source_version if self.gate else None,
                           'review_policy_version': self.gate.review_policy_version if self.gate else None},
            'shadow': {'requested': self.shadow, 'mode': 'offline_snapshot_only',
                       'production_executed': False, 'production_dual_reads': False,
                       'can_authorize_repair': False}}

    def select(self, incident, principal, registry, *, source_version, review_policy_version,
               configuration_sha256=None):
        summary = self.summary(source_version, review_policy_version, configuration_sha256)
        engine, reason, bucket = 'legacy', 'rollout_off', None
        if self.mode != 'off':
            if summary['acceptance']['verdict'] != 'pass':
                reason = summary['acceptance']['reason']
            elif not _authorized(incident, principal, registry):
                reason = 'audience_not_authorized'
            elif (self.tenants and principal.tenant_id not in self.tenants
                    or self.services and incident['service'] not in self.services):
                reason = 'audience_not_selected'
            else:
                key = {'policy': POLICY_VERSION, 'salt': self.salt,
                       'tenant': principal.tenant_id, 'incident': incident['id']}
                bucket = int(_digest(key)[:16], 16) % 10000
                if self.mode == 'full' or bucket < self.percent * 100:
                    engine, reason = 'autonomous_v2', 'full_selected' if self.mode == 'full' else 'canary_selected'
                else:
                    reason = 'canary_not_selected'
        summary.update(selected_engine=engine, reason=reason, bucket=bucket)
        return {'engine_id': engine, 'execution_variant': self.variant if engine == 'autonomous_v2' else 'legacy',
                'enable_rework': engine == 'autonomous_v2' and self.variant == 'closed_loop',
                'enable_challenge': engine == 'autonomous_v2' and self.variant == 'closed_loop' and self.enable_challenge,
                'rollout': summary}


def _authorized(incident, principal, registry):
    if not incident or principal is None or not incident.get('id'):
        return False
    if (incident.get('tenant_id', principal.tenant_id) != principal.tenant_id
            or incident.get('user_id', principal.user_id) != principal.user_id):
        return False
    from providers.fixtures import ProviderError
    try:
        if incident.get('demo_case_id'):
            from providers.bound import validate_demo
            validate_demo(incident, principal)
        else:
            registry.resolve(incident, principal)
        return True
    except (ProviderError, KeyError, TypeError):
        return False
