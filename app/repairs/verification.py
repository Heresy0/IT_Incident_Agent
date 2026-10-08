"""Verify registered symptom metrics now, independently of the old incident window."""
from datetime import datetime, timezone
import math
import time
from providers.fixtures import ProviderError
from providers.http import request_json


def validate_checks(binding, target):
    checks = target.get('symptom_checks', [])
    if not isinstance(checks, list) or len(checks) > 4:
        raise ProviderError('SYMPTOM_CHECK_CONFIG_INVALID')
    definitions = binding.observations.get('metrics', {})
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {'metric', 'operator', 'threshold', 'max_age_seconds'}:
            raise ProviderError('SYMPTOM_CHECK_CONFIG_INVALID')
        metric = check.get('metric')
        threshold, age = check.get('threshold'), check.get('max_age_seconds', 60)
        if (not isinstance(metric, str) or metric not in definitions or metric in seen
                or check.get('operator') not in {'lte', 'gte', 'eq'}
                or type(threshold) not in {int, float} or not math.isfinite(threshold)
                or type(age) is not int or not 5 <= age <= 300):
            raise ProviderError('SYMPTOM_CHECK_CONFIG_INVALID')
        seen.add(metric)
        if not isinstance(definitions[metric].get('freshness_query'), str) or not definitions[metric]['freshness_query']:
            raise ProviderError('SYMPTOM_FRESHNESS_QUERY_REQUIRED')
    if checks and not binding.observations.get('prometheus'):
        raise ProviderError('SYMPTOM_SOURCE_NOT_CONFIGURED')
    return [{**check, 'unit': definitions[check['metric']]['unit'],
             'max_age_seconds': check.get('max_age_seconds', 60)} for check in checks]


class SymptomVerifier:
    def __init__(self, *, transport=None):
        self.transport = transport

    def verify(self, binding, target, *, deadline=None):
        checks = validate_checks(binding, target)
        if not checks:
            return {'passed': False, 'checks': [], 'code': 'SYMPTOM_CHECK_REQUIRED'}
        source = binding.observations['prometheus']
        now = datetime.now(timezone.utc)
        observations = []
        for check in checks:
            if deadline is not None and time.monotonic() >= deadline:
                raise ProviderError('REPAIR_TIME_BUDGET_EXCEEDED')
            data = request_json(source['url'], 'GET', '/api/v1/query', params={
                'query': binding.observations['metrics'][check['metric']]['query'], 'time': now.timestamp()},
                authorization_env=source.get('authorization_env'), transport=self.transport)
            if data.get('status') != 'success' or data.get('data', {}).get('resultType') != 'vector':
                raise ProviderError('SYMPTOM_RESPONSE_INVALID')
            samples = data['data']['result']
            observation = {**check, 'passed': False, 'evaluated_at': now.isoformat()}
            if len(samples) != 1:
                observation['code'] = 'SYMPTOM_SAMPLE_MISSING' if not samples else 'SYMPTOM_NOT_AGGREGATED'
            else:
                try:
                    stamp, raw = samples[0]['value']
                    value, timestamp = float(raw), float(stamp)
                    if not math.isfinite(value) or not math.isfinite(timestamp):
                        raise ValueError()
                except (ValueError, TypeError, KeyError, IndexError):
                    raise ProviderError('SYMPTOM_RESPONSE_INVALID') from None
                observation.update(value=value, observed_at=datetime.fromtimestamp(timestamp, timezone.utc).isoformat())
                if not now.timestamp() - check['max_age_seconds'] <= timestamp <= now.timestamp() + 5:
                    observation['code'] = 'SYMPTOM_SAMPLE_STALE'
                else:
                    observation['passed'] = {'lte': value <= check['threshold'], 'gte': value >= check['threshold'],
                                              'eq': value == check['threshold']}[check['operator']]
                    if deadline is not None and time.monotonic() >= deadline:
                        raise ProviderError('REPAIR_TIME_BUDGET_EXCEEDED')
                    freshness = request_json(source['url'], 'GET', '/api/v1/query', params={
                        'query': binding.observations['metrics'][check['metric']]['freshness_query'],
                        'time': now.timestamp()}, authorization_env=source.get('authorization_env'), transport=self.transport)
                    try:
                        fresh_rows = freshness['data']['result']
                        if freshness['status'] != 'success' or freshness['data']['resultType'] != 'vector' or len(fresh_rows) != 1:
                            raise ValueError()
                        age = float(fresh_rows[0]['value'][1])
                        evaluated = float(fresh_rows[0]['value'][0])
                        if not math.isfinite(age) or not math.isfinite(evaluated):
                            raise ValueError()
                    except (ValueError, TypeError, KeyError, IndexError):
                        raise ProviderError('SYMPTOM_FRESHNESS_INVALID') from None
                    observation['source_age_seconds'] = age
                    if not 0 <= age <= check['max_age_seconds'] or not now.timestamp() - 60 <= evaluated <= now.timestamp() + 5:
                        observation.update(passed=False, code='SYMPTOM_SOURCE_STALE')
            observations.append(observation)
        return {'passed': all(item['passed'] for item in observations), 'checks': observations}
