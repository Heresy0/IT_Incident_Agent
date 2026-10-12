"""Same scripted planner, real graph/executor, synthetic snapshots; no model API."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from langchain_core.messages import AIMessage
from api.auth import Principal
from providers.fixtures import FixtureProvider, ProviderError
from evidence.contracts import TOOL_ARGS
from runtime.context import Limits
from workflow.coordinator import Collaboration
from workflow.diagnostic_pack import DiagnosticPack
from workflow.task_checks import completion
from tools.query_coverage import covers

ROOT = Path(__file__).resolve().parents[2]
P = Principal('synthetic_demo', 'cli_reader')
METHOD = 'mechanical_collection_control_v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class SnapshotProvider(FixtureProvider):
    name = 'diagnostic_pack_synthetic_control'

    def __init__(self, snapshot):
        self._data, self.principal = deepcopy(snapshot), P

    def query(self, name, args, scope):
        code = self._data.get('source_errors', {}).get(name)
        if code:
            raise ProviderError(code)
        return super().query(name, args, scope)


class NeverCalled:
    def invoke(self, messages):
        raise AssertionError('This experiment stops before Diagnosis/Reviewer; no model API')


class CollectionOnlyDraft:
    """Neutral placeholder if the graph routes directly to diagnosis after reuse/recovery."""
    def invoke(self, messages):
        return AIMessage(content=json.dumps({'findings': [], 'hypotheses': [], 'recommended_actions': [],
            'missing_information': ['取证控制实验未执行语义分析，请人工评估实际观测。']}))


class ReplayPlanner:
    def __init__(self, baseline):
        self.baseline, self.calls = baseline, 0

    def invoke(self, messages):
        self.calls += 1
        payload = json.loads(messages[1].content)['input']
        window = {k: payload['ticket']['scope'][k] for k in ('start', 'end')}
        required = [{'tool': c['tool'], 'args': TOOL_ARGS[c['tool']].model_validate_json(
            json.dumps({**window, **c['args']})).model_dump(mode='json')} for c in self.baseline]
        already_covered = all(any(r['name'] == check['tool'] and r['status'] in {'ok', 'empty'}
            and not r['truncated'] and covers(check['tool'], r['query_profile'], check['args'])
            for r in payload.get('collection', [])) for check in required)
        if self.calls == 1 and not already_covered:
            value = {'action': 'dispatch', 'reason': '固定的基线查询回放，不代表自主查询选择。',
                     'tasks': [{'role': 'investigation', 'goal': '取得基线症状与变更记录',
                         'need_ids': ['N1'], 'checks': [{'tool': c['tool'], 'args': {**window, **c['args']}}
                                                      for c in self.baseline],
                         'expected_value': '记录当前观测，供后续人工评估'}]}
        else:
            value = {'action': 'request_info', 'reason': '机械取证实验在解释前停止。',
                     'missing_information': ['未运行自主诊断或复核，不能据此评价原因判断质量。']}
        return AIMessage(content=json.dumps(value, ensure_ascii=False))


def select_pack(packs, snapshot):
    scope = snapshot['ticket']['scope']
    matches = [pack for pack in packs if (pack.service, pack.environment, pack.service_version) == (
        scope['service'], scope['environment'], scope['service_version'])]
    if len(matches) > 1:
        raise ValueError('Duplicate diagnostic pack registration')
    return matches[0] if matches else None


def cases():
    """Known development fixtures plus declared perturbations, never held-out answers."""
    packets = [DiagnosticPack.model_validate_json(json.dumps(row)) for row in json.loads(
        (ROOT / 'examples/diagnostic-packs.example.json').read_text(encoding='utf-8'))['packs']]
    data = {cid: json.loads((ROOT / 'fixtures/incident_freeze_review' / f'{cid}.json').read_text(encoding='utf-8'))
            for cid in ('fr_001', 'fr_002', 'fr_003')}
    resource_baseline = [
        {'tool': 'get_service_metrics', 'args': {'metrics': ['pool_wait', 'request_error_rate', 'request_latency_ms']}},
        {'tool': 'get_recent_changes', 'args': {}}]
    definitions = [
        {'case_id': 'connection_wait', 'snapshot': data['fr_001'], 'baseline': resource_baseline},
        {'case_id': 'authentication', 'snapshot': data['fr_003'], 'baseline': [
            {'tool': 'get_service_logs', 'args': {'category': 'dependency'}},
            {'tool': 'get_recent_changes', 'args': {}}]},
        {'case_id': 'source_unavailable', 'snapshot': data['fr_002'], 'baseline': [
            {'tool': 'get_service_metrics', 'args': {'metrics': ['request_error_rate', 'request_latency_ms']}},
            {'tool': 'get_recent_changes', 'args': {}}]},
    ]
    def variant(name, source='connection_wait'):
        item = deepcopy(next(r for r in definitions if r['case_id'] == source))
        item['case_id'] = name
        definitions.append(item)
        return item
    item = variant('different_mechanism')
    item['snapshot']['logs'][0].update(error_code='QUERY_LOCK_WAIT',
        message='业务查询等待锁；未确认连接泄漏、容量不足或发布因果。')
    for row in item['snapshot']['metrics']:
        if row['metric'] == 'db_cpu':
            row['value'] = 90
    item = variant('renamed_registration')
    mapping = {'pool_wait': 'conn_waiters', 'pool_usage': 'conn_pressure', 'db_cpu': 'storage_cpu'}
    scope = item['snapshot']['ticket']['scope']
    original_service = scope['service']
    scope['service'] = 'renamed-renderer'
    for rows in (item['snapshot'][key] for key in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents')):
        for row in rows:
            if row['service'] == original_service:
                row['service'] = scope['service']
            if row.get('metric') in mapping:
                row['metric'] = mapping[row['metric']]
    item['baseline'][0]['args']['metrics'][0] = 'conn_waiters'
    renamed = packets[0].model_dump()
    renamed['service'] = scope['service']
    renamed['checks'][1]['args']['metrics'] = list(mapping.values())
    packets.append(DiagnosticPack.model_validate_json(json.dumps(renamed)))
    item = variant('status_only')
    item['snapshot']['ticket']['purpose'] = 'status_check'
    item['baseline'] = [{'tool': 'get_service_metrics', 'args': {'metrics': ['request_error_rate', 'request_latency_ms']}}]
    item = variant('small_budget')
    item['tool_budget'] = 1
    item = variant('empty_business_logs')
    item['snapshot']['logs'] = []
    item = variant('truncated_business_logs')
    item['snapshot']['logs'][0]['message'] = 'oversized source line ' * 300
    item = variant('dense_business_logs')
    seed = deepcopy(item['snapshot']['logs'][0])
    item['snapshot']['logs'] = [{**deepcopy(seed), 'id': f'dense-{i:03d}',
        'timestamp': f'2026-10-12T09:10:{i:02d}+00:00',
        'message': ('业务请求等待连接；此日志不证明触发原因。' * 25) + str(i)} for i in range(50)]
    item = variant('unregistered_service')
    scope = item['snapshot']['ticket']['scope']
    scope['service'] = 'new-service-without-pack'
    for rows in (item['snapshot'][key] for key in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents')):
        for row in rows:
            row['service'] = scope['service']
    for item in definitions:
        item['pack'] = select_pack(packets, item['snapshot'])
    return definitions


def run_case(case, enabled):
    provider = SnapshotProvider(case['snapshot'])
    models = {role: NeverCalled() for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')}
    models['supervisor'] = ReplayPlanner(case['baseline'])
    models['diagnosis'] = CollectionOnlyDraft()
    limits = Limits(model_calls=12, tool_calls=case.get('tool_budget', 8), reserve_model_calls=3, reserve_seconds=20)
    c = Collaboration(models, provider, P, limits=limits, parallel_collection=False,
                      diagnostic_pack=case['pack'] if enabled else None)
    report = c.run()
    # Score query coverage after execution. No required result/answer enters prompts.
    window = provider.ticket().scope.model_dump(mode='json', include={'start', 'end'})
    required = []
    if provider.ticket().purpose != 'status_check' and case['pack'] is not None:
        required = [{'tool': row.tool, 'args': c.executors['investigation'].validate_args(
            row.tool, {**window, **row.args}).model_dump(mode='json')} for row in case['pack'].checks]
    coverage = completion(required, c.executors['investigation'])
    metrics = sorted({e.payload['metric'] for e in c.evidence.values() if 'metric' in e.payload})
    report.update(workflow='multi', execution_mode='scripted_control_only', model='scripted-replay',
                  experiment_arm='pack' if enabled else 'baseline', experiment_case=case['case_id'],
                  fixture_snapshot_sha256=digest(case['snapshot']))
    row = {'case_id': case['case_id'], 'arm': report['experiment_arm'], 'run_id': report['run_id'],
           'status': report['status'], 'termination_reason': report['run_summary']['termination_reason'],
           'tool_calls': report['run_summary']['tool_calls'], 'scripted_model_steps': report['run_summary']['model_calls'],
           'paid_model_calls': 0, 'query_statuses': [r['status'] for r in coverage['checks']],
           'input_chars_max': max((e.get('input_chars', 0) for e in report['events'] if e['type'] == 'model_input'), default=0),
           'input_budget_rejections': sum(e.get('dimension') == 'input_chars' for e in report['events'] if e['type'] == 'budget_rejected'),
           'required_checks_not_executed': sum(r['status'] == 'not_executed' for r in coverage['checks']),
           'failed_queries': sum(e.get('status') == 'error' for e in report['events'] if e['type'] == 'tool_result'),
           'observed_metrics': metrics, 'pack_status': report.get('diagnostic_pack', {}).get('status', 'disabled_or_unregistered'),
           'observations_count': len(report['evidence']),
           'pack_handoff': report.get('diagnostic_pack', {}).get('handoff', []),
           'goal_verified': False, 'semantic_quality': 'excluded_scripted'}
    return report, row


def run_controls(selected, on_result=None):
    reports, rows = [], []
    for case in selected:
        for enabled in (False, True):
            report, row = run_case(case, enabled)
            if on_result:
                on_result(report, row)
            reports.append(report)
            rows.append(row)
    pairs = []
    for case in selected:
        pair = {r['arm']: r for r in rows if r['case_id'] == case['case_id']}
        pairs.append({'case_id': case['case_id'], 'baseline': pair['baseline'], 'pack': pair['pack'],
                      'extra_tool_calls': pair['pack']['tool_calls'] - pair['baseline']['tool_calls']})
    return {'method': METHOD, 'cases': len(selected), 'runs': len(rows), 'paid_model_calls': 0,
            'semantic_quality': 'excluded_scripted', 'pairs': pairs,
            'limitations': ['使用相同脚本规划策略并复用已覆盖查询，仅比较程序取证和开销；没有运行自主诊断或复核。',
                '基于已知合成开发案例及衍生扰动，不是外部盲测或独立保留集成绩。',
                '诊断包由已知服务配置设计，跨服务转移需要重新登记名称；不证明自动发现新故障的能力。',
                '固定空诊断仅用于复用/恢复路径的图收尾，不比较completed比例；空结果、失败和输入门禁均保留。',
                '整体推迟基础包会在小预算下失去原流程可取得的部分观测；这是待评估的策略代价。',
                '补齐证据增加上下文；密集日志接近原输入上限，尚未验证真实Diagnosis/Reviewer能稳定接收。']}, reports
