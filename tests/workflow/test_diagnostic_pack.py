"""Real executor/graph boundaries for optional baseline collection; no model API."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from pydantic import ValidationError
from agents.scripted import scripted_models
from agents.contracts import TaskRequest, ReviewDecision
from providers.fixtures import FixtureProvider, ProviderError
from providers.http import HTTPObservationProvider
from providers.registry import ServiceBinding
from runtime.context import Limits, ExecutionError
from tests.tools.test_incident_tools import P
from tests.workflow.test_planned_default import planned_models
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import Collaboration, collaborate
from workflow.diagnostic_pack import DiagnosticPack, collect, refresh, check_exit, POLICY

ROOT = Path(__file__).resolve().parents[2]


class SnapshotProvider(FixtureProvider):
    name = 'diagnostic_pack_synthetic_control'

    def __init__(self, data, principal=P):
        self._data, self.principal = copy.deepcopy(data), principal

    def query(self, name, args, scope):
        code = self._data.get('source_errors', {}).get(name)
        if code:
            raise ProviderError(code)
        return super().query(name, args, scope)


class StopAfterCollection:
    """Intentionally ends the mechanical experiment without claiming a diagnosis."""
    def invoke(self, messages):
        return AIMessage(content=json.dumps({'action': 'request_info',
            'reason': '机械取证实验在解释前停止，不评价模型诊断质量。',
            'missing_information': ['当前仅验证查询覆盖，未运行自主诊断。']}))


def packet(provider, checks=None):
    scope = provider.ticket().scope
    return DiagnosticPack.model_validate_json(json.dumps({
        'pack_id': 'baseline', 'service': scope.service, 'environment': scope.environment,
        'service_version': scope.service_version,
        'checks': checks or [
            {'check_id': 'business-log', 'tool': 'get_service_logs', 'args': {'category': 'resource'},
             'human_check': '取得同窗口业务失败记录'},
            {'check_id': 'controls', 'tool': 'get_service_metrics',
             'args': {'metrics': ['pool_wait', 'pool_usage', 'db_cpu']},
             'human_check': '核查压力与负载对照'}]}))


def coordinator(data=None, **kwargs):
    data = data or json.loads((ROOT / 'fixtures/incident_freeze_review/fr_001.json').read_text(encoding='utf-8'))
    provider = SnapshotProvider(data)
    models = scripted_models()
    models['supervisor'] = StopAfterCollection()
    return Collaboration(models, provider, P, diagnostic_pack=packet(provider), **kwargs)


def collect_remaining_metrics_models():
    """Explicitly collect the scripted diagnosis's metric before interpretation."""
    models = planned_models()
    def plan(response, messages, calls):
        if calls != 1:
            return response
        scope = json.loads(messages[1].content)['input']['ticket']['scope']
        return json_response({'action': 'dispatch', 'reason': '核查剩余指标', 'tasks': [{
            'role': 'investigation', 'goal': '取得依赖错误指标', 'need_ids': ['N1'],
            'checks': [{'tool': 'get_service_metrics', 'args': {
                'start': scope['start'], 'end': scope['end'], 'metrics': ['dependency_error_rate']}}],
            'expected_value': '取得症状对照'}]})
    models['supervisor'] = AlterOutput(models['supervisor'], plan)
    return models


class DiagnosticPackTests(unittest.TestCase):
    def read_task(self, c, checks):
        checks = [{'tool': row['tool'], 'args': c.executors['investigation'].validate_args(
            row['tool'], row['args']).model_dump(mode='json')} for row in checks]
        task = TaskRequest.model_validate_json(json.dumps({'role': 'investigation', 'goal': '补齐登记对照',
            'checks': checks, 'need_ids': [], 'expected_value': '核实缺少的观测'}))
        objective = c.task_objective(task, False)
        result = c.collect_task(task, objective, c.executors['investigation'], c.context, c.events, 0)
        c.record_task(task, objective, result)

    def test_collects_missing_log_and_controls_without_semantic_promotion(self):
        c = coordinator()
        with patch.object(c.models['investigation'], 'invoke', side_effect=AssertionError('No leaf model')):
            collect(c)
        self.assertEqual(c.context.counts, {'model_calls': 0, 'web_calls': 0, 'tool_calls': 2})
        self.assertEqual(c.diagnostic_pack_result['status'], 'collected')
        self.assertIn('POOL_ACQUIRE_TIMEOUT', {e.payload.get('error_code') for e in c.evidence.values()})
        self.assertIn('db_cpu', {e.payload.get('metric') for e in c.evidence.values()})
        self.assertTrue(all(n.status == 'pending' for n in c.evidence_needs.values()))
        self.assertFalse(c.diagnostic_pack_result['mechanism_confirmed'])
        self.assertEqual(len(c.tasks), 1)
        self.assertTrue(all(e['execution_source'] == POLICY for e in c.events if e['type'] == 'tool_result'))

    def test_optional_node_runs_before_supervisor_and_reports_identity(self):
        c = coordinator()
        report = c.run()
        phases = [e['node'] for e in report['events'] if e['type'] == 'phase']
        self.assertEqual(phases[:2], ['diagnostic_pack', 'supervisor'])
        self.assertEqual(report['diagnostic_pack']['status'], 'collected')
        self.assertEqual(report['collection_execution']['diagnostic_pack_fingerprint'], c.diagnostic_pack.fingerprint)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['run_summary']['model_calls'], 1)

    def test_default_graph_and_full_scripted_review_stay_available(self):
        provider = FixtureProvider('case_001', P)
        checks = [
            {'check_id': 'business-log', 'tool': 'get_service_logs', 'args': {'category': 'dependency'}, 'human_check': '核查业务失败记录'},
            {'check_id': 'controls', 'tool': 'get_service_metrics', 'args': {'metrics': ['dependency_error_rate']}, 'human_check': '核查指标对照'}]
        baseline = collaborate(planned_models(), provider, P, limits=Limits(model_calls=12, tool_calls=4))
        enabled = collaborate(planned_models(), provider, P, limits=Limits(model_calls=12, tool_calls=4),
                              diagnostic_pack=packet(provider, checks))
        self.assertNotIn('diagnostic_pack', baseline)
        self.assertNotIn('diagnostic_pack_exit', baseline)
        self.assertFalse(any(e.get('node') == 'diagnostic_pack' for e in baseline['events']))
        self.assertEqual(baseline['status'], 'completed')
        self.assertEqual(enabled['status'], 'completed')
        self.assertTrue(enabled['reviews'])
        self.assertEqual(enabled['run_summary']['tool_calls'], 2)
        self.assertTrue(all(n['status'] == 'supported' for n in enabled['evidence_needs']))

    def test_unrelated_authentication_service_has_no_pool_queries(self):
        data = json.loads((ROOT / 'fixtures/incident_freeze_review/fr_003.json').read_text(encoding='utf-8'))
        provider = SnapshotProvider(data)
        raw = json.loads((ROOT / 'examples/diagnostic-packs.example.json').read_text(encoding='utf-8'))['packs'][1]
        c = Collaboration(scripted_models(), provider, P, diagnostic_pack=raw)
        collect(c)
        self.assertEqual([e['name'] for e in c.events if e['type'] == 'tool_result'],
                         ['get_service_logs', 'get_recent_changes'])
        self.assertFalse(any('metric' in e.payload for e in c.evidence.values()))

    def test_same_pack_keeps_distinct_evidence_for_a_different_failure(self):
        c = coordinator()
        c.provider._data['logs'][0]['error_code'] = 'QUERY_LOCK_WAIT'
        c.provider._data['logs'][0]['message'] = '业务查询等待锁；尚未确认连接泄漏或容量不足。'
        collect(c)
        self.assertIn('QUERY_LOCK_WAIT', {e.payload.get('error_code') for e in c.evidence.values()})
        self.assertFalse(c.diagnostic_pack_result['mechanism_confirmed'])
        self.assertEqual(c.tasks[0]['output']['tentative_hypotheses'], [])

    def test_renamed_service_and_metric_registration_needs_no_new_algorithm(self):
        c = coordinator()
        data = copy.deepcopy(c.provider._data)
        mapping = {'pool_wait': 'conn_waiters', 'pool_usage': 'conn_pressure', 'db_cpu': 'storage_cpu'}
        data['ticket']['scope']['service'] = 'renamed-api'
        for name in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
            for row in data[name]:
                row['service'] = 'renamed-api'
                if row.get('metric') in mapping:
                    row['metric'] = mapping[row['metric']]
        provider = SnapshotProvider(data)
        raw = packet(provider).model_dump()
        raw['checks'][1]['args']['metrics'] = list(mapping.values())
        c = Collaboration(scripted_models(), provider, P, diagnostic_pack=raw)
        collect(c)
        self.assertEqual({e.payload['metric'] for e in c.evidence.values() if 'metric' in e.payload}, set(mapping.values()))

    def test_failures_are_preserved_and_not_replayed(self):
        c = coordinator()
        c.provider._data['source_errors'] = {'get_service_logs': 'SOURCE_UNAVAILABLE'}
        with patch.object(c.provider, 'query', wraps=c.provider.query) as query:
            collect(c)
            collect(c)
        self.assertEqual(query.call_count, 2)  # One log failure and one metric read.
        self.assertEqual(c.diagnostic_pack_result['checks'][0]['status'], 'failed')
        self.assertEqual(c.diagnostic_pack_result['status'], 'collected_with_gaps')
        self.assertIn('查询失败', c.diagnostic_pack_result['handoff'][0])
        self.assertEqual(c.context.counts['tool_calls'], 2)

    def test_existing_failed_and_successful_reads_are_not_replayed(self):
        c = coordinator()
        ex = c.executors['investigation']
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        with patch.object(c.provider, 'query', side_effect=ProviderError('SOURCE_UNAVAILABLE')):
            ex.execute('get_service_logs', {**window, 'category': 'resource'})
        collect(c)
        self.assertEqual(c.diagnostic_pack_result['tool_calls'], 1)
        self.assertEqual(c.context.counts['tool_calls'], 2)

    def test_small_budget_defers_the_whole_pack_with_concrete_handoff(self):
        c = coordinator(limits=Limits(model_calls=6, tool_calls=1))
        report = c.run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['run_summary']['termination_reason'], 'BUDGET_EXCEEDED')
        self.assertEqual(report['run_summary']['tool_calls'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 0)
        self.assertEqual(report['diagnostic_pack']['status'], 'budget_deferred')
        self.assertEqual(len(report['diagnostic_pack']['handoff']), 2)

    def test_time_and_model_finalization_reserve_are_respected(self):
        for dimension in ('time', 'models'):
            c = coordinator(limits=Limits(model_calls=6, tool_calls=4))
            if dimension == 'time':
                c.context.started -= c.context.limits.seconds
            else:
                c.context.counts['model_calls'] = 4
            with patch.object(c.provider, 'query', side_effect=AssertionError('No admission')):
                collect(c)
            self.assertEqual(c.context.counts['tool_calls'], 0)
            self.assertEqual(c.diagnostic_pack_result['status'], 'budget_deferred')

    def test_exact_model_reserve_does_not_admit_reads_that_executor_will_deny(self):
        c = coordinator(limits=Limits(model_calls=12, tool_calls=4, reserve_model_calls=3))
        c.context.counts['model_calls'] = 9
        collect(c)
        self.assertEqual(c.diagnostic_pack_result['status'], 'budget_deferred')
        self.assertFalse(c.tasks)
        self.assertEqual(c.context.counts['tool_calls'], 0)

    def test_status_check_does_not_expand_into_mechanism_collection(self):
        c = coordinator()
        c.provider._data['ticket']['purpose'] = 'status_check'
        c.ticket = c.provider.ticket()
        collect(c)
        self.assertEqual(c.diagnostic_pack_result['status'], 'skipped_status_check')
        self.assertEqual(c.context.counts['tool_calls'], 0)
        self.assertFalse(c.tasks)

    def test_truncated_and_empty_results_are_limitations_not_health(self):
        for state in ('truncated', 'empty'):
            c = coordinator()
            if state == 'truncated':
                c.provider._data['logs'][0]['message'] = 'long record ' * 500
            else:
                c.provider._data['logs'] = []
            collect(c)
            self.assertEqual(c.diagnostic_pack_result['checks'][0]['status'], state)
            self.assertTrue(c.diagnostic_pack_result['handoff'])
            self.assertFalse(c.diagnostic_pack_result['goal_verified'])

    def test_wrong_service_environment_version_denied_before_reads(self):
        c = coordinator()
        for key in ('service', 'environment', 'service_version'):
            raw = c.diagnostic_pack.model_dump()
            raw[key] = 'different'
            with self.assertRaises(ValueError):
                Collaboration(scripted_models(), c.provider, P, diagnostic_pack=raw)

    def test_unsafe_or_answer_bearing_packages_rejected(self):
        c = coordinator()
        for mutate in (lambda p: p.update(expected_cause='pool leak'),
                       lambda p: p['checks'][0].update(tool='shell'),
                       lambda p: p['checks'][0]['args'].update(start='2020-01-01'),
                       lambda p: p['checks'][0]['args'].update(level='ERROR'),
                       lambda p: p['checks'][1]['args'].update(url='https://example.com')):
            raw = c.diagnostic_pack.model_dump()
            mutate(raw)
            with self.assertRaises(ValidationError):
                DiagnosticPack.model_validate_json(json.dumps(raw))

    def test_malformed_later_check_does_not_allow_partial_earlier_reads(self):
        c = coordinator()
        raw = c.diagnostic_pack.model_dump()
        raw['checks'][1]['args']['metrics'] = [123]
        c.diagnostic_pack = DiagnosticPack.model_validate_json(json.dumps(raw))
        with patch.object(c.provider, 'query', side_effect=AssertionError('Preflight first')):
            with self.assertRaises(ExecutionError) as error:
                collect(c)
        self.assertEqual(error.exception.code, 'INVALID_ARGUMENTS')
        self.assertEqual(c.diagnostic_pack_result['status'], 'invalid_configuration')
        self.assertEqual(c.context.counts['tool_calls'], 0)

    def test_unregistered_channel_preserves_gap_without_external_read(self):
        c = coordinator()
        scope = c.ticket.scope
        binding = ServiceBinding(scope.tenant_id, (scope.user_id,), scope.service, scope.environment,
                                 scope.service_version, observations={})
        provider = HTTPObservationProvider(binding, c.ticket, P)
        c = Collaboration(scripted_models(), provider, P, diagnostic_pack=c.diagnostic_pack)
        with patch.object(provider, 'query', side_effect=AssertionError('No registered channels')):
            collect(c)
        self.assertEqual(c.diagnostic_pack_result['status'], 'channels_unavailable')
        self.assertEqual([r['status'] for r in c.diagnostic_pack_result['checks']], ['unavailable', 'unavailable'])
        self.assertEqual(c.context.counts['tool_calls'], 0)
        self.assertTrue(c.diagnostic_pack_result['handoff'])

    def test_later_covering_read_refreshes_state_and_removes_stale_handoff(self):
        c = coordinator()
        raw = c.diagnostic_pack.model_dump()
        raw['checks'][1]['args']['metrics'] = ['db_cpu']
        c.diagnostic_pack = DiagnosticPack.model_validate_json(json.dumps(raw))
        original_query = c.provider.query
        def fail_metrics(name, args, scope):
            if name == 'get_service_metrics':
                raise ProviderError('SOURCE_UNAVAILABLE')
            return original_query(name, args, scope)
        with patch.object(c.provider, 'query', side_effect=fail_metrics):
            collect(c)
        first = check_exit(c, 'diagnosis_entry')
        self.assertEqual(first['decision'], 'continue_with_limitations')
        old_gap = c.diagnostic_pack_result['handoff'][0]
        revision = c.diagnostic_pack_result['revision']
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        self.read_task(c, [{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['db_cpu', 'pool_wait']}}])
        self.assertEqual(c.diagnostic_pack_result['status'], 'collected')
        self.assertGreater(c.diagnostic_pack_result['revision'], revision)
        self.assertEqual(c.diagnostic_pack_result['blocking_check_ids'], [])
        self.assertNotIn(old_gap, c.extra_gaps)
        self.assertEqual(c.task_gaps(), [])
        second = check_exit(c, 'diagnosis_entry')
        self.assertEqual(second['decision'], 'ready_for_interpretation')
        self.assertEqual(c.diagnostic_pack_exit['history'][0], first)
        self.assertEqual(first['blocking_check_ids'], ['controls'])  # Immutable historical snapshot.
        counts = dict(c.context.counts)
        for _ in range(3):
            refresh(c)
            check_exit(c, 'diagnosis_entry')
        self.assertEqual(c.context.counts, counts)
        self.assertEqual(len(c.diagnostic_pack_exit['history']), 2)

    def test_unrelated_or_narrower_queries_do_not_discharge_a_full_window_gap(self):
        c = coordinator()
        c.provider._data['source_errors'] = {'get_service_metrics': 'SOURCE_UNAVAILABLE'}
        collect(c)
        del c.provider._data['source_errors']
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        self.read_task(c, [{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['db_cpu']}},
                          {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}}])
        self.assertEqual(c.diagnostic_pack_result['checks'][1]['status'], 'failed')
        self.assertEqual(c.diagnostic_pack_result['blocking_check_ids'], ['controls'])

    def test_successful_collection_alone_cannot_finish_without_independent_review(self):
        c = coordinator()
        collect(c)
        c.stop = 'FINISHED'
        exit_state = check_exit(c, 'finalization')
        self.assertFalse(exit_state['can_mark_completed'])
        self.assertFalse(exit_state['conditions']['independent_review_present'])
        self.assertEqual(c.stop, 'REVIEW_INCOMPLETE')
        self.assertEqual(c.status(), 'partial')

    def test_failed_empty_metric_and_truncated_checks_block_completed_even_with_a_review(self):
        for state in ('failed', 'empty', 'truncated', 'missing_samples'):
            with self.subTest(state=state):
                c = coordinator()
                if state == 'failed':
                    c.provider._data['source_errors'] = {'get_service_logs': 'SOURCE_UNAVAILABLE'}
                elif state == 'empty':
                    c.provider._data['metrics'] = []
                elif state == 'missing_samples':
                    c.provider._data['metrics'] = [r for r in c.provider._data['metrics'] if r['metric'] != 'db_cpu']
                else:
                    c.provider._data['logs'][0]['message'] = 'long record ' * 500
                collect(c)
                c.review = ReviewDecision(assessments=[{'target_id': 'F1', 'verdict': 'supported',
                    'reason': '脚本复核结果；不用于证明取证已完成'}])
                c.stop = 'FINISHED'
                self.assertEqual(c.status(), 'partial')
                exit_state = check_exit(c, 'finalization')
                self.assertFalse(exit_state['allowed'])
                self.assertTrue(exit_state['blocking_check_ids'])
                self.assertEqual(c.stop, 'DIAGNOSTIC_PACK_INCOMPLETE')
                self.assertEqual(c.context.counts['tool_calls'], 2)

    def test_unexecuted_and_unavailable_checks_are_checked_at_finalization(self):
        c = coordinator(limits=Limits(model_calls=6, tool_calls=1))
        report = c.run()
        state = report['diagnostic_pack_exit']['latest']
        self.assertEqual(state['stage'], 'finalization')
        self.assertEqual(state['blocking_check_ids'], ['business-log', 'controls'])
        self.assertFalse(state['allowed'])
        self.assertEqual(state['termination_reason'], report['run_summary']['termination_reason'])
        c = coordinator()
        scope = c.ticket.scope
        binding = ServiceBinding(scope.tenant_id, (scope.user_id,), scope.service, scope.environment,
                                 scope.service_version, observations={})
        provider = HTTPObservationProvider(binding, c.ticket, P)
        c = Collaboration(scripted_models(), provider, P, diagnostic_pack=c.diagnostic_pack)
        report = c.run()
        self.assertEqual(report['diagnostic_pack_exit']['latest']['blocking_check_ids'], ['business-log', 'controls'])
        self.assertTrue(report['diagnosis_readiness']['blocking_gaps'])

    def test_no_observations_prevents_diagnosis_entry_without_a_model_call(self):
        c = coordinator()
        c.provider._data['logs'], c.provider._data['metrics'] = [], []
        collect(c)
        with patch.object(c.models['diagnosis'], 'invoke', side_effect=AssertionError('No observation')):
            c.diagnose()
        self.assertEqual(c.diagnostic_pack_exit['latest']['decision'], 'handoff_required')
        self.assertEqual(c.context.counts['model_calls'], 0)
        self.assertEqual(c.stop, 'NEEDS_INFORMATION')

    def test_invalid_configuration_is_a_failure_in_both_exit_state_and_report(self):
        c = coordinator()
        raw = c.diagnostic_pack.model_dump()
        raw['checks'][1]['args']['metrics'] = [123]
        c.diagnostic_pack = DiagnosticPack.model_validate_json(json.dumps(raw))
        with patch.object(c.provider, 'query', side_effect=AssertionError('No invalid reads')):
            report = c.run()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['diagnostic_pack_exit']['latest']['decision'], 'failed')
        self.assertFalse(report['diagnostic_pack_exit']['latest']['conditions']['configuration_valid'])
        self.assertEqual(report['run_summary']['tool_calls'], 0)
        self.assertEqual(report['run_summary']['model_calls'], 0)

    def test_empty_complete_event_query_keeps_limitations_and_allows_reviewed_completion(self):
        provider = FixtureProvider('case_001', P)
        checks = [
            {'check_id': 'business-log', 'tool': 'get_service_logs', 'args': {'category': 'dependency'}, 'human_check': '核查业务失败记录'},
            {'check_id': 'changes', 'tool': 'get_recent_changes', 'args': {'category': 'scaling'}, 'human_check': '核查扩缩容记录'}]
        report = collaborate(collect_remaining_metrics_models(), provider, P, limits=Limits(model_calls=12, tool_calls=6),
                             diagnostic_pack=packet(provider, checks))
        self.assertEqual(report['diagnostic_pack']['checks'][1]['status'], 'empty')
        self.assertEqual(report['diagnostic_pack']['blocking_check_ids'], [])
        self.assertTrue(report['diagnostic_pack']['handoff'])
        self.assertEqual(report['collection_exits'][0]['reason'], 'baseline_collection_limits')
        self.assertEqual(report['status'], 'completed')
        stages = [s['stage'] for s in report['diagnostic_pack_exit']['history']]
        self.assertEqual(stages, ['diagnosis_entry', 'review_completion', 'finalization'])
        self.assertTrue(report['diagnostic_pack_exit']['latest']['can_mark_completed'])
        self.assertEqual(report['diagnosis_readiness']['report_status'], 'reviewed_with_limitations')

    def test_report_and_workflow_completion_event_agree_on_failed_collection(self):
        provider = FixtureProvider('case_001', P)
        checks = [
            {'check_id': 'business-log', 'tool': 'get_service_logs', 'args': {'category': 'dependency'}, 'human_check': '核查业务失败记录'},
            {'check_id': 'changes', 'tool': 'get_recent_changes', 'args': {}, 'human_check': '核查变更记录'}]
        original_query = provider.query
        def fail_changes(name, args, scope):
            if name == 'get_recent_changes':
                raise ProviderError('SOURCE_UNAVAILABLE')
            return original_query(name, args, scope)
        with patch.object(provider, 'query', side_effect=fail_changes):
            report = collaborate(collect_remaining_metrics_models(), provider, P, limits=Limits(model_calls=12, tool_calls=6),
                                 diagnostic_pack=packet(provider, checks))
        self.assertTrue(report['reviews'])
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['diagnostic_pack_exit']['latest']['blocking_check_ids'], ['changes'])
        event = next(e for e in report['events'] if e['type'] == 'workflow_completed')
        self.assertEqual(event['status'], report['status'])
        self.assertEqual(event['reason'], report['run_summary']['termination_reason'])
        self.assertEqual(sum(e['type'] == 'tool_result' and e['name'] == 'get_recent_changes' for e in report['events']), 1)


if __name__ == '__main__':
    unittest.main()
