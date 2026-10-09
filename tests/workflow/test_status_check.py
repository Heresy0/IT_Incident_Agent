"""Bounded status checks and registered rework; no model API or real writes."""
import json
import unittest

from langchain_core.messages import AIMessage
from agents.contracts import DiagnosisDraft, Hypothesis, ReviewDecision
from agents.scripted import scripted_models
from evidence.render import render
from providers.fixtures import FixtureProvider
from providers.http import HTTPObservationProvider
from runtime.context import activate
from workflow.coordinator import Collaboration, collaborate
from tests.tools.test_incident_tools import P
from tests.tools import test_registered_provider
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


class StatusCheckTests(unittest.TestCase):
    def provider(self):
        provider = FixtureProvider('case_001', P)
        provider._data['ticket']['purpose'] = 'status_check'
        return provider

    def test_status_check_reviews_facts_without_cause_or_repair_and_renders_limits(self):
        result = collaborate(scripted_models(), self.provider(), P)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['review_status'], 'passed')
        self.assertEqual(result['business_result'], 'status_checked')
        self.assertEqual(result['run_summary']['termination_reason'], 'STATUS_CHECK_COMPLETED')
        self.assertTrue(result['output']['findings'])
        self.assertEqual(result['output']['hypotheses'], [])
        self.assertEqual(result['output']['recommended_actions'], [])
        self.assertTrue(all(a['target_id'].startswith('F') for a in result['reviews'][0]['review']['assessments']))
        text = render(result)
        self.assertIn('服务状态核查报告', text)
        self.assertIn('整体健康尚未确认', text)
        self.assertNotIn('原因假设', text)
        self.assertNotIn('处理建议', text)
        self.assertIn('采样点之间的短暂异常', text)

    def test_status_check_still_requires_supported_facts(self):
        def uncertain(response, *_):
            value = json.loads(response.content)
            value['assessments'][0]['verdict'] = 'uncertain'
            return json_response(value)
        models = scripted_models()
        models['reviewer'] = AlterOutput(models['reviewer'], uncertain)
        result = collaborate(models, self.provider(), P)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['business_result'], 'needs_information')
        self.assertEqual(result['review_status'], 'needs_information')

    def test_status_check_scope_repair_does_not_silently_remove_a_cause(self):
        histories = []
        def extra_cause(response, messages, calls):
            histories.append(messages)
            value = json.loads(response.content)
            if calls == 1:
                value['hypotheses'] = [{'cause': 'invented idle cause'}]
            return json_response(value)
        models = scripted_models()
        models['diagnosis'] = AlterOutput(models['diagnosis'], extra_cause)
        result = collaborate(models, self.provider(), P)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['output']['hypotheses'], [])
        schema = json.loads(histories[0][1].content)['output_schema']
        self.assertEqual(schema['properties']['hypotheses']['maxItems'], 0)
        self.assertEqual(schema['properties']['recommended_actions']['maxItems'], 0)
        self.assertIn('status_check', histories[1][-1].content)

    def test_status_check_without_findings_cannot_complete(self):
        models = scripted_models()
        models['diagnosis'] = AlterOutput(models['diagnosis'], lambda *_: json_response({}))
        result = collaborate(models, self.provider(), P)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['review_status'], 'not_performed')

    def test_registered_rework_schema_limits_metrics_and_denial_is_specific(self):
        fixture = test_registered_provider.RegisteredProviderTests()
        fixture.setUp()
        provider = HTTPObservationProvider(fixture.binding, fixture.ticket, fixture.principal)
        models = scripted_models()
        c = Collaboration(models, provider, fixture.principal)
        executor = c.executors['investigation']
        item = executor.register_snapshot(executor.prepare_snapshot('get_service_metrics', provider._row(
            fixture.start.isoformat(), metric='queue_depth', value=0, unit='jobs', granularity='1m')))
        c.evidence.update(executor.evidence)
        ref = next(r for r in executor.references.values() if r.field_path == 'value')
        c.draft = DiagnosisDraft(findings=[], hypotheses=[Hypothesis(hypothesis_id='H1', cause='待验证假设',
            support_refs=[ref], counter_refs=[], pending_checks=[])], recommended_actions=[],
            missing_information=[], escalation_team=None)
        response = {'assessments': [{'target_id': 'H1', 'verdict': 'uncertain', 'reason': '需要更多观测',
            'evidence_ids': [item.evidence_id]}], 'request_evidence': {'hypothesis_id': 'H1',
            'evidence_ids': [item.evidence_id], 'missing_observation': '缺少 CPU 指标',
            'proposed_check': '查询 CPU', 'expected_value': '区分负载状态', 'target_role': 'investigation',
            'checks': [{'tool': 'get_service_metrics', 'args': {'start': fixture.snapshot['start'],
                'end': fixture.snapshot['end'], 'metrics': ['cpu_usage']}}]}}
        captured = []
        class Reviewer:
            def invoke(self, messages):
                captured.append(messages)
                return AIMessage(content=json.dumps(response))
        c.models['reviewer'] = Reviewer()
        with activate(c.context):
            review = c.ask('reviewer', ReviewDecision, c.reviewer_input(), terminal=True, validate=c.validate_review)
        self.assertIsNone(review.request_evidence)
        self.assertIn('METRIC_NOT_REGISTERED', review.missing_information[0])
        self.assertIn('queue_depth', review.missing_information[0])
        self.assertNotIn('human changes', review.missing_information[0])
        self.assertEqual(c.context.counts['tool_calls'], 0)
        data = json.loads(captured[0][1].content)
        checks = data['output_schema']['$defs']['ReadOnlyCheck']['oneOf']
        metric = next(check for check in checks if check['properties']['tool']['const'] == 'get_service_metrics')
        self.assertEqual(metric['properties']['args']['properties']['metrics']['items']['enum'], ['queue_depth'])
        self.assertEqual(data['input']['read_only_capabilities']['investigation']['registered_log_categories'], ['resource'])
        coverage = c.status_check_coverage()
        self.assertEqual(coverage['unchecked_log_categories'], ['resource'])
