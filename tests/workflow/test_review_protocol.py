"""Recovery across roles without relaxing evidence, permissions or budgets."""
import copy
import json
import unittest

from langchain_core.messages import AIMessage
from agents.protocol import corrections_within_policy
from agents.contracts import DiagnosisSelection, ObservationReview
from evidence.diagnosis import ProtocolError, preserve_diagnosis
from evidence.render import render
from agents.scripted import scripted_models
from providers.fixtures import FixtureProvider
from runtime.context import Limits
from tests.tools.test_incident_tools import P
from tests.tools.test_incident_tools import executor, window
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import collaborate
from tests.workflow import test_review_readiness


class ReviewProtocolTests(unittest.TestCase):
    def double_correction(self, limits=None):
        models = scripted_models()
        models['investigation'] = AlterOutput(models['investigation'],
            lambda response, messages, calls: AIMessage(content='invalid JSON') if calls == 2 else response)

        def review(response, messages, calls):
            value = json.loads(response.content)
            if calls == 1:
                value['need_assessments'][0]['blocking'] = False
            return json_response(value)
        models['reviewer'] = AlterOutput(models['reviewer'], review)
        return collaborate(models, FixtureProvider('case_001', P), P,
                           **({'limits': limits} if limits else {}))

    def test_collection_failure_does_not_consume_review_correction(self):
        report = self.double_correction()
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['repairs'], 2)
        self.assertEqual(report['repair_counts'], {'collection': 1, 'reviewer': 1})
        self.assertTrue(corrections_within_policy(report))
        self.assertEqual(report['run_summary']['model_calls'], 8)

    def test_reserved_correction_never_expands_total_call_budget(self):
        report = self.double_correction(Limits(model_calls=7, tool_calls=3,
                                              reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(report['status'], 'partial')
        self.assertLessEqual(report['run_summary']['model_calls'], 7)
        self.assertFalse(report['output']['recommended_actions'])

    def test_mechanical_score_rejects_forged_or_excess_correction_counts(self):
        report = self.double_correction()
        changed = copy.deepcopy(report)
        changed['repair_counts']['collection'] = 0
        self.assertFalse(corrections_within_policy(changed))
        changed = copy.deepcopy(report)
        changed['events'].append({'type': 'validation_repair', 'role': 'reviewer'})
        changed['repairs'] += 1
        changed['repair_counts']['reviewer'] += 1
        self.assertFalse(corrections_within_policy(changed))
        self.assertFalse(corrections_within_policy({'repairs': 2}))

    def test_reviewer_assesses_raw_evidence_without_prior_need_verdicts(self):
        models = scripted_models()
        report = collaborate(models, FixtureProvider('case_001', P), P)
        payload = json.loads(models['reviewer'].histories[0][1].content)
        view = payload['input']
        self.assertEqual(view['core_need_ids'], ['N1', 'N2'])
        self.assertTrue(view['evidence'])
        self.assertTrue(view['collection'])
        for need in view['evidence_needs']:
            self.assertNotIn('status', need)
            self.assertNotIn('reason', need)
        for task in view['task_results']:
            self.assertNotIn('candidate_mechanisms', task)
            self.assertIn('completion', task)
        definition = payload['output_schema']['$defs']['NeedAssessment']
        self.assertEqual(definition['allOf'][0]['then']['properties']['blocking']['enum'], [True, None])
        self.assertEqual(report['status'], 'completed')

    def test_first_dispatch_cannot_advertise_unobserved_evidence_identifiers(self):
        models = scripted_models()
        collaborate(models, FixtureProvider('case_001', P), P)
        first = json.loads(models['supervisor'].histories[0][1].content)['output_schema']
        self.assertEqual(first['$defs']['TaskRequest']['properties']['evidence_ids']['maxItems'], 0)
        self.assertEqual(first['$defs']['EvidenceNeedUpdate']['properties']['evidence_ids']['maxItems'], 0)
        later = json.loads(models['supervisor'].histories[-1][1].content)
        permitted = later['output_schema']['$defs']['ObservedEvidenceId']['enum']
        self.assertEqual(set(permitted), {e['evidence_id'] for e in later['input']['evidence']})

    def test_log_filter_choices_are_observed_fields_not_words_from_ticket(self):
        ex = executor()
        def parameters():
            return next(s['function']['parameters'] for s in ex.schemas()
                        if s['function']['name'] == 'get_service_logs')['properties']
        self.assertEqual(parameters()['error_code']['enum'], [None])
        self.assertEqual(parameters()['level']['enum'], [None])
        result = ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        self.assertTrue(result.evidence)
        self.assertEqual(set(parameters()['error_code']['enum']),
                         {None, *(e.payload['error_code'] for e in result.evidence)})
        self.assertNotIn('CONNECTION_REFUSED', parameters()['error_code']['enum'])

    def test_read_only_model_tools_declare_their_actual_service_target(self):
        ex = executor()
        for tool in ex.schemas():
            if tool['function']['name'] in {'get_service_metrics', 'get_service_logs', 'get_recent_changes'}:
                description = tool['function']['description']
                self.assertIn(ex.scope.service, description)
                self.assertIn('remote', description)

    def test_coordinator_keeps_argument_contract_once_with_role_catalog(self):
        models = scripted_models()
        report = collaborate(models, FixtureProvider('case_001', P), P)
        view = json.loads(models['supervisor'].histories[0][1].content)
        catalog = view['input']['read_only_tools']
        advertised = {tool['name'] for entries in catalog.values() for tool in entries}
        definitions = view['output_schema']['$defs']['ReadOnlyCheck']['oneOf']
        self.assertEqual(advertised, {d['properties']['tool']['const'] for d in definitions})
        self.assertTrue(all(d['properties']['args']['properties'] for d in definitions))
        self.assertTrue(all('parameters' not in t and 'args_schema' in t
                            for entries in catalog.values() for t in entries))
        self.assertEqual(report['status'], 'completed')

    def test_role_tool_contract_and_permission_failure_never_execute_wrong_role(self):
        models = scripted_models()
        def wrong_role(response, messages, calls):
            data = json.loads(response.content)
            if calls > 1:
                scope = json.loads(messages[1].content)['input']['ticket']['scope']
                data.update(action='dispatch', tasks=[{'role': 'knowledge', 'goal': '错误角色',
                    'checks': [{'tool': 'get_recent_changes', 'args': {
                        'start': scope['start'], 'end': scope['end']}}]}])
            return json_response(data)
        models['supervisor'] = AlterOutput(models['supervisor'], wrong_role)
        report = collaborate(models, FixtureProvider('case_001', P), P)
        schema = json.loads(models['supervisor'].model.histories[0][1].content)['output_schema']
        rules = schema['$defs']['TaskRequest']['allOf']
        knowledge = next(r for r in rules if r['if']['properties']['role']['const'] == 'knowledge')
        self.assertEqual(set(knowledge['then']['properties']['checks']['items']['properties']['tool']['enum']),
                         {'search_runbooks', 'search_incidents'})
        self.assertEqual(report['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertFalse(report['protocol_finalization_used'])
        self.assertEqual(models['knowledge'].histories, [])
        self.assertEqual(report['run_summary']['tool_calls'], 2)

    def test_manual_read_only_next_step_requires_no_fictional_tool_or_write(self):
        def action(data):
            data['recommended_actions'][0].update(kind='manual_check', checks=[], repair=None,
                action='请负责人只读核对未登记的外部协议说明', requires_approval=False)
        report, _ = test_review_readiness.ReviewReadinessTests().run_report(draft_change=action)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(report['output']['recommended_actions'][0]['kind'], 'manual_check')
        self.assertEqual(report['output']['recommended_actions'][0]['checks'], [])
        self.assertIn('人工只读核实', render(report))

    def test_persistent_invalid_advice_does_not_discard_valid_hypothesis(self):
        def no_tool(data):
            data['recommended_actions'][0].update(kind='read_only_check', checks=[])
        report, models = test_review_readiness.ReviewReadinessTests().run_report(draft_change=no_tool)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['review_status'], 'needs_information')
        self.assertTrue(report['output']['findings'])
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'supported')
        self.assertEqual(report['output']['recommended_actions'], [])
        self.assertEqual(report['rejected_actions'][0]['code'], 'READ_ONLY_ACTION_INVALID')
        self.assertEqual(models['reviewer'].calls, 1)
        self.assertEqual(report['run_summary']['tool_calls'], 2)

    def test_invalid_reference_remains_fatal_even_when_advice_is_quarantined(self):
        ex = executor()
        ex.execute('get_service_logs', {**window(ex), 'category': 'dependency'})
        selection = DiagnosisSelection(hypotheses=[{'cause': 'unsupported',
            'support_refs': [{'reference_id': 'REF_' + '0' * 24}]}], recommended_actions=[{
                'action': '无效核查', 'condition': '待查', 'expected_result': '待查',
                'risk': '只读', 'requires_approval': False, 'kind': 'read_only_check', 'checks': []}])
        with self.assertRaises(ProtocolError) as caught:
            preserve_diagnosis(selection, ex)
        self.assertEqual(caught.exception.code, 'UNKNOWN_REFERENCE')

    def test_scheduler_enum_failure_finalizes_existing_evidence_without_running_bad_plan(self):
        models = scripted_models()
        def bad_update(response, messages, calls):
            value = json.loads(response.content)
            if calls > 1:
                value['additional_needs'] = [{'need_id': 'N4', 'purpose': 'mechanism',
                    'question': '错误新增核心问题', 'reason': '错误枚举'}]
            return json_response(value)
        models['supervisor'] = AlterOutput(models['supervisor'], bad_update)
        report = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertTrue(report['protocol_finalization_used'])
        self.assertEqual(report['review_status'], 'passed')
        self.assertTrue(report['output']['findings'])
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(len(report['task_results']), 1)
        self.assertTrue(all(n['need_id'] != 'N4' for n in report['evidence_needs']))

    def blind_models(self, unsupported=False, wide=False):
        models = scripted_models()
        def review(response, messages, calls):
            data = json.loads(messages[1].content)
            if 'directly_supported' in data['output_schema']['properties']:
                evidence = data['input']['evidence']
                source = next(e for e in evidence if 'metric' in e['payload'])
                ref = next(r for r in source['reference_options'] if r['field_path'] == 'value')
                refs = ([{'reference_id': r['reference_id']} for e in evidence if e['kind'] == 'observation'
                         for r in e['reference_options']][:8] if wide else [{'reference_id': ref['reference_id']}])
                return json_response({'directly_supported': [{'statement': '伪造基线与原因不应发布',
                    'refs': refs}],
                    'unverified_causes': ['具体部署触发原因尚未证实']})
            value = json.loads(response.content)
            if unsupported:
                for assessment in value['assessments']:
                    if assessment['target_id'] == 'H1':
                        assessment.update(verdict='uncertain', reason='具体部署原因超出盲审证据范围')
                value.update(request_evidence=None, follow_up_reason='需要未登记的协议比对，保留限制')
            return json_response(value)
        base = models['reviewer']
        class BlindModel:
            blind_review = True
            def __init__(self):
                self.histories = []
            def invoke(self, messages):
                self.histories.append(list(messages))
                data = json.loads(messages[1].content)
                response = (json_response({}) if 'directly_supported' in data['output_schema']['properties']
                            else base.invoke(messages))
                return review(response, messages, len(self.histories))
        models['reviewer'] = BlindModel()
        return models

    def test_blind_review_sees_no_draft_then_supplies_separate_evidence_limits(self):
        models = self.blind_models()
        report = collaborate(models, FixtureProvider('case_001', P), P)
        histories = models['reviewer'].histories
        self.assertEqual(len(histories), 2)
        blind = json.loads(histories[0][1].content)['input']
        self.assertTrue(blind['evidence'])
        self.assertNotIn('targets', blind)
        self.assertNotIn('evidence_needs', blind)
        self.assertNotIn('task_results', blind)
        final = json.loads(histories[1][1].content)['input']
        self.assertEqual(final['independent_observation_review']['unverified_causes'],
                         ['具体部署触发原因尚未证实'])
        self.assertEqual(report['run_summary']['model_calls'], 7)
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(len(report['observation_reviews']), 1)
        actual = report['observation_reviews'][0]['review']['directly_supported'][0]['statement']
        self.assertIn('(mean)=0.92 ratio', actual)
        self.assertNotIn('伪造基线', actual)

    def test_blind_review_does_not_promote_uncertain_cause_or_expand_budget(self):
        report = collaborate(self.blind_models(unsupported=True), FixtureProvider('case_001', P), P,
            limits=Limits(model_calls=7, tool_calls=3, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['output']['hypotheses'][0]['status'], 'unresolved')
        self.assertLessEqual(report['run_summary']['model_calls'], 7)
        self.assertEqual(report['run_summary']['tool_calls'], 2)

    def test_joined_blind_source_statement_remains_valid_after_server_rendering(self):
        report = collaborate(self.blind_models(wide=True), FixtureProvider('case_001', P), P)
        audit = report['observation_reviews'][0]['review']
        rendered = ObservationReview.model_validate(audit)
        statement = rendered.directly_supported[0].statement
        self.assertGreater(len(statement), 400)
        self.assertLessEqual(len(statement), 500)
        self.assertIn('see references', statement)


if __name__ == '__main__':
    unittest.main()
