"""New observation worlds and explicit synthetic reference controls, never human gold.

The provider receives only ``observations`` and quantity/channel metadata. Claims,
reference relations and key-observation IDs remain in the evaluator.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json

from api.auth import Principal
from providers.fixtures import FixtureProvider, ProviderError
from providers.registry import ServiceBinding

VERSION = 'closed-loop-source-controls-v1'
PRINCIPAL = Principal('closed_loop_synthetic', 'evaluation_reader')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class SourceCase:
    case_id: str
    observations: dict
    initial_checks: tuple
    review_claims: tuple = ()
    review_actions: tuple = ()
    key_observation_ids: tuple = ()
    acquisition_available: bool = True
    scenario_origin: str = 'new_author_constructed_world'

    def freeze(self):
        return {'case_id': self.case_id, 'world_sha256': fingerprint(self.observations),
                'reference_sha256': fingerprint({'claims': self.review_claims, 'actions': self.review_actions,
                    'key_observation_ids': self.key_observation_ids, 'available': self.acquisition_available}),
                'reference_kind': 'synthetic_source_constructed_provisional', 'human_review': 'pending',
                'scenario_origin': self.scenario_origin,
                'external_blind_test': False}


class SourceWorldProvider(FixtureProvider):
    name = 'closed_loop_queryable_synthetic_world'

    def __init__(self, case, principal=PRINCIPAL):
        self._data, self.principal = deepcopy(case.observations), principal
        scope = self.ticket().scope
        metrics = {r['metric']: {'unit': r['unit'], 'aggregation': r['aggregation'],
            'description': r.get('quantity_description', 'unknown'),
            'observed_object': r.get('observed_object', 'unknown'), 'source': VERSION}
            for r in self._data['metrics']}
        self.binding = ServiceBinding(principal.tenant_id, (principal.user_id,), scope.service,
            scope.environment, scope.service_version, scope.allowed_dependencies, observations={
                'prometheus': {'synthetic_world': True}, 'loki': {'synthetic_world': True},
                'metrics': metrics, 'log_queries': {name: 'synthetic_partition' for name in
                    ('dependency', 'configuration', 'resource')},
                'log_metadata': {'dependency': {'description': 'Outgoing request outcomes and route-specific probes.'},
                    'resource': {'description': 'Local worker, queue and resource acquisition events.'},
                    'configuration': {'description': 'Local configuration parsing and loading events.'}}})

    def query(self, name, args, scope, *, max_metric_points=60):
        self.authorize(self.principal, scope)
        if name in self._data.get('source_errors', {}):
            raise ProviderError(self._data['source_errors'][name])
        rows, truncated = super().query(name, args, scope)
        if name == 'get_service_metrics' and max_metric_points < 60:
            selected, counts = [], {}
            for row in rows:
                if counts.get(row['metric'], 0) < max_metric_points:
                    selected.append(row)
                    counts[row['metric']] = counts.get(row['metric'], 0) + 1
            return selected, truncated or len(selected) < len(rows)
        return rows, truncated


def _world(number, symptoms):
    start = datetime(2026, 10, 18, 6, tzinfo=timezone.utc)
    service = 'component_' + hashlib.sha256(str(number).encode()).hexdigest()[:8]
    return {'synthetic': True, 'ticket': {'incident_id': f'synthetic_{number:02}',
        'title': 'Unseen synthetic request failure', 'symptoms': symptoms,
        'scope': {'service': service, 'environment': 'staging', 'service_version': 'v7',
                  'start': start.isoformat(), 'end': (start + timedelta(minutes=20)).isoformat(),
                  'allowed_dependencies': ['peer_' + hashlib.sha256(service.encode()).hexdigest()[:8]]}},
        'metrics': [], 'logs': [], 'changes': [], 'owners': [], 'runbooks': [], 'incidents': []}


def _row(world, identifier, minute, **fields):
    start = datetime.fromisoformat(world['ticket']['scope']['start'])
    return {'id': identifier, 'timestamp': (start + timedelta(minutes=minute)).isoformat(),
        'service': world['ticket']['scope']['service'], 'environment': 'staging',
        'data_version': VERSION, 'visibility': 'public_synthetic', **fields}


def _log(world, identifier, minute, message, category='dependency', code='REQUEST_FAILURE', level='ERROR'):
    row = _row(world, identifier, minute, category=category, error_code=code, level=level, message=message)
    world['logs'].append(row)
    return identifier


def _metric(world, identifier, minute, metric, value, unit, description, observed_object):
    world['metrics'].append(_row(world, identifier, minute, metric=metric, value=value, unit=unit,
        aggregation='mean', granularity='1m', quantity_description=description, observed_object=observed_object))


def _checks(world, *pairs):
    window = {k: world['ticket']['scope'][k] for k in ('start', 'end')}
    return tuple({'tool': tool, 'args': {**window, **args}} for tool, args in pairs)


def _claim(statement, relation, *selectors, level='mechanism'):
    return {'statement': statement, 'relation': relation, 'selectors': selectors, 'level': level}


def build_cases():
    cases = []
    w = _world(41, 'Business /submit requests fail; a /ready probe sometimes succeeds.')
    _log(w, 'probe', 4, 'GET /ready returned HTTP 200; route=/ready; no /submit request was sent by this probe.',
         code='PROBE_OK', level='INFO')
    _log(w, 'business', 5, 'POST /submit to dependency exceeded the read deadline and returned HTTP 504.', code='READ_DEADLINE')
    cases.append(SourceCase('probe_scope', w, _checks(w, ('get_service_logs', {'category': 'dependency'})), (
        _claim('GET /ready返回200，因此整个依赖组件健康且可以排除其所有故障。', 'partial', ('probe', 'message')),
        _claim('观测到/ready探针成功以及/submit请求读取超时；探针不能代表所有业务路径。', 'supported',
               ('probe', 'message'), ('business', 'message'))), key_observation_ids=('business',)))

    w = _world(42, 'Requests wait for a local slot; determine observed boundaries without assuming CPU exhaustion.')
    _metric(w, 'wait', 5, 'signal_a', 800, 'ms', 'Time waiting for a worker slot.', 'local worker acquisition')
    _metric(w, 'cpu', 5, 'signal_b', 11, 'percent', 'Processor utilization at database host.', 'database processor')
    cases.append(SourceCase('compound_assertion', w, _checks(w, ('get_service_metrics', {'metrics': ['signal_a', 'signal_b']})), (
        _claim('06:05的工作槽等待为800ms，且数据库处理器利用率为100%。', 'partial', ('wait', 'value'), ('cpu', 'value')),
        _claim('06:05的数据库处理器利用率为100%。', 'contradicted', ('cpu', 'value')),
        _claim('06:05的工作槽等待为800ms。', 'supported', ('wait', 'value'))), key_observation_ids=('wait', 'cpu')))

    w = _world(43, 'Failures were observed before a later settings update; compare the recorded timestamps.')
    _log(w, 'early-failure', 4, 'Business request failed with AUTH_REJECTED; first failure recorded by this source at 06:04.', code='AUTH_REJECTED')
    w['changes'].append(_row(w, 'later-change', 8, category='configuration', version='v7',
        summary='Authentication audience setting updated at 06:08.', config_summary={'auth_audience': 'scope-b'}))
    cases.append(SourceCase('temporal_counterevidence', w, _checks(w,
        ('get_service_logs', {'category': 'dependency'}), ('get_recent_changes', {})), (
        _claim('06:08配置更新触发了06:04已经记录的首次拒绝。', 'contradicted',
               ('early-failure', 'message'), ('later-change', 'summary'), level='trigger'),
        _claim('拒绝记录早于配置更新，仅凭此次更新时间无法将它作为该记录的起因。', 'supported',
               ('early-failure', 'message'), ('later-change', 'summary'))), key_observation_ids=('early-failure', 'later-change')))

    w = _world(44, 'Some requests fail name lookup; a separate request also waits for a local worker.')
    _log(w, 'lookup', 5, 'request=r17 failed before connect because name lookup returned SERVFAIL; resolver internal state was not observed.', code='LOOKUP_SERVFAIL')
    _log(w, 'queue', 6, 'request=r18 exceeded local worker-acquisition deadline; request=r18 never attempted name lookup.',
         category='resource', code='WORKER_WAIT')
    cases.append(SourceCase('contribution_and_uniqueness', w, _checks(w,
        ('get_service_logs', {'category': 'dependency'}), ('get_service_logs', {'category': 'resource'})), (
        _claim('r17在连接之前因名称解析返回SERVFAIL而失败；尚未定位解析器内部起因。', 'supported', ('lookup', 'message')),
        _claim('所有失败请求的唯一原因均为名称解析器自身宕机。', 'contradicted', ('lookup', 'message'), ('queue', 'message'))),
        key_observation_ids=('lookup', 'queue')))

    w = _world(45, 'A remote route returns 401; remote internal configuration is outside the local observation scope.')
    _log(w, 'remote-reject', 5, 'POST remote /authorize returned 401 audience mismatch; remote configuration values were not observed.', code='AUDIENCE_REJECTED')
    action = {'action': '请值班负责人在获得对端访问授权后核对其认证配置与被拒请求，记录是否匹配，若不匹配再评估下一步。',
              'condition': '仅人工只读核查；由有权限的负责人执行，不预设结果。',
              'decision': 'appropriate', 'selectors': (('remote-reject', 'message'),)}
    cases.append(SourceCase('future_manual_check', w, _checks(w, ('get_service_logs', {'category': 'dependency'})),
        (_claim('对端认证配置已确认错误。', 'insufficient', ('remote-reject', 'message')),), (action,),
        key_observation_ids=('remote-reject',), acquisition_available=False))

    w = _world(46, 'Requests return 502 after payload validation; local validation records are available in another channel.')
    _log(w, 'generic-failure', 5, 'request=r21 returned HTTP 502; downstream response was not recorded by this channel.', code='GATEWAY_FAILURE')
    _log(w, 'local-detail', 5, 'request=r21 rejected locally: payload schema field mode was invalid; no outgoing connection was attempted.',
         category='configuration', code='LOCAL_SCHEMA_REJECTED')
    cases.append(SourceCase('followup_available', w, _checks(w, ('get_service_logs', {'category': 'dependency'})),
        (_claim('request=r21的502已经确认由对端数据库故障导致。', 'insufficient', ('generic-failure', 'message')),),
        key_observation_ids=('local-detail',)))

    w = _world(47, 'A request timeout is recorded locally; the needed remote records are not accessible through this service.')
    _log(w, 'local-timeout', 5, 'request=r29 exceeded the outbound read deadline; no remote internals or wire response were captured.', code='READ_DEADLINE')
    w['source_errors'] = {'get_recent_changes': 'SOURCE_NOT_CONFIGURED'}
    cases.append(SourceCase('followup_unavailable', w, _checks(w, ('get_service_logs', {'category': 'dependency'})),
        (_claim('远端进程已经确认崩溃。', 'insufficient', ('local-timeout', 'message')),),
        key_observation_ids=(), acquisition_available=False))

    # Explicitly a known regression, not an unseen holdout: the v2.2 failure
    # generalized a successful ping and attributed saturation solely to capacity.
    w = _world(48, 'Requests wait for pooled connections while traffic rises; a database ping succeeds.')
    _log(w, 'pool-visible-failure', 10,
         'Connection acquisition exceeded 1500ms deadline; in_use=20; pool_max=20; waiters=45.',
         category='resource', code='ACQUIRE_TIMEOUT')
    _log(w, 'db-ping-only', 10,
         'One database ping returned successfully in 8ms; this probe did not execute business SQL or inspect locks.',
         code='DB_PING_OK', level='INFO')
    quantity = (
        ('in-use', 'observed_1', 20, 'connections', 'Number of currently occupied pooled connections.', 'application connection pool'),
        ('waiters', 'observed_3', 45, 'requests', 'Requests currently waiting to acquire a pooled connection.', 'connection acquisition queue'),
        ('acquisition', 'observed_4', 1500, 'ms', 'Observed acquisition wait ending at the configured deadline.', 'connection acquisition attempt'),
        ('ping', 'observed_5', 8, 'ms', 'Duration of one successful database liveness probe.', 'single database ping'),
    )
    for identifier, metric, value, unit, description, observed_object in quantity:
        _metric(w, identifier, 10, metric, value, unit, description, observed_object)
    for minute, suffix, rate in ((4, 'before', 35), (10, 'after', 100)):
        _metric(w, 'pool-size-' + suffix, minute, 'observed_2', 20, 'connections',
                'Configured maximum count of pooled connections.', 'application connection pool configuration')
        _metric(w, 'traffic-' + suffix, minute, 'observed_6', rate, 'requests/s',
                'Number of incoming requests per second.', 'service incoming traffic')
    cases.append(SourceCase('pool_probe_ambiguity', w, _checks(w,
        ('get_service_logs', {'category': 'resource'}), ('get_service_logs', {'category': 'dependency'}),
        ('get_service_metrics', {'metrics': ['observed_2']}),
        ('get_service_metrics', {'metrics': ['observed_6']}),
        ('get_service_metrics', {'metrics': ['observed_1', 'observed_3', 'observed_4', 'observed_5']})), (
        _claim('当前可见失败环节是连接池已用20/20以及连接获取等待达到1500ms期限，仍有45个等待者。',
               'supported', ('pool-visible-failure', 'message')),
        _claim('数据库ping在8ms内成功，因此数据库整体健康，可以排除其所有故障。',
               'partial', ('db-ping-only', 'message')),
        _claim('流量增长已经证明pool_max=20配置不足是当前故障的唯一根因。',
               'insufficient', ('traffic-before', 'value'), ('traffic-after', 'value'),
               ('pool-size-after', 'value'), ('pool-visible-failure', 'message'))),
        key_observation_ids=('pool-visible-failure', 'db-ping-only', 'traffic-before', 'traffic-after'),
        scenario_origin='known_regression_from_v22_failure'))
    return {case.case_id: case for case in cases}


def fixed_draft(case, executor):
    """Resolve source selectors after actual reads; never expose reference labels."""
    def refs(selectors):
        result = []
        for row_id, field in selectors:
            evidence = next((item for item in executor.evidence.values() if item.locator.endswith('/' + row_id)), None)
            if evidence is None:
                raise ValueError('Fixed review source was not acquired')
            reference = next(option.reference_id for option in evidence.reference_options if option.field_path == field)
            result.append({'reference_id': reference})
        return result

    raw = {'claims': [{'statement': spec['statement'], 'refs': refs(spec['selectors']),
                       'counter_refs': [], 'level': spec['level']} for spec in case.review_claims],
           'next_steps': [{'action': spec['action'], 'condition': spec['condition'],
                           'refs': refs(spec['selectors']), 'kind': 'manual_check'} for spec in case.review_actions], 'gaps': []}
    return raw, reference_targets(case)


def reference_targets(case):
    expected = {f'C{index}': {'kind': 'factual_relation', 'value': spec['relation']}
                for index, spec in enumerate(case.review_claims, 1)}
    expected.update({f'A{index}': {'kind': 'check_appropriateness', 'value': spec['decision']}
                     for index, spec in enumerate(case.review_actions, 1)})
    return expected


def acquired_key_observations(case, report):
    acquired = {item.get('locator', '').rsplit('/', 1)[-1] for item in report.get('evidence', [])}
    required = set(case.key_observation_ids)
    return {'reference_kind': 'synthetic_key_observation_ids', 'required_count': len(required),
            'acquired_count': len(required & acquired), 'omitted_ids': sorted(required - acquired),
            'available': case.acquisition_available, 'human_verified': False}
