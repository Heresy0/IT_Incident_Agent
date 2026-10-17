"""Six free control families for Diagnosis schema feedback, not LLM quality scores.

Invalid candidates are synthetic. Saved v4 traces contain error paths/types, not
the rejected JSON bodies. All model responses below are local scripted doubles.
"""
from copy import deepcopy
import json
import unittest

from agents.contracts import DiagnosisSelection, ObservationReview, ReviewDecision, SupervisorSelection
from agents.protocol import correction_instruction
from agents.scripted import scripted_models, selector
from evidence.contracts import InvestigationSelection
from tests.tools.test_incident_tools import P
from tests.workflow.test_diagnosis_completion import WorkerDiagnosisModel, WorkerObservationModel, worker_provider
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_multi_minimal_handoff import OutageProvider, closure_models
from workflow.coordinator import collaborate


def schema_error(*items):
    return {'reason': 'schema_mismatch', 'details': [{'path': path, 'type': kind} for path, kind in items]}


FORMAT_ERROR = ('findings.1.refs.3.reference_id', 'string_pattern_mismatch')
EMPTY_ERRORS = (('findings.1.refs', 'too_short'), ('findings.2.refs', 'too_short'))
FORMAT_HINT = '引用编号格式错误'
EMPTY_HINT = 'finding 引用列表为空'


class DiagnosisReferenceFeedbackTests(unittest.TestCase):
    def run_worker(self, alter):
        models = scripted_models()
        models['investigation'] = WorkerObservationModel()
        history = []
        def mutate(response, messages, number):
            history.append(deepcopy(messages))
            return json_response(alter(json.loads(response.content), messages, number))
        models['diagnosis'] = AlterOutput(WorkerDiagnosisModel(False), mutate)
        return collaborate(models, worker_provider(), P, investigation_mode='agent'), history

    def assert_one_repair(self, report, history):
        self.assertEqual(len(history), 2)
        self.assertEqual(report['repairs'], 1)
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(history[0][:2], history[1][:2])
        rebuilt = [e for e in report['events'] if e['type'] == 'correction_input_rebuilt']
        self.assertEqual(len(rebuilt), 1)
        self.assertFalse(rebuilt[0]['evidence_omitted'])
        self.assertFalse(rebuilt[0]['schema_changed'])
        return history[1][-1].content

    def test_family_1_bad_format_at_saved_path_receives_actionable_second_request(self):
        for bad in ('EV_' + '0' * 24, 'REF_1234', 'REF_' + 'A' * 24, 'REF_' + 'g' * 24):
            with self.subTest(bad=bad):
                def mutate(value, messages, number):
                    if number == 1:
                        refs = [f['refs'][0] for f in value['findings'][:3]]
                        value['findings'][1]['refs'] = refs + [{'reference_id': bad}]
                    return value
                report, history = self.run_worker(mutate)
                hint = self.assert_one_repair(report, history)
                self.assertIn(FORMAT_HINT, hint)
                self.assertIn('24 位小写十六进制', hint)
                self.assertIn('input.evidence[].reference_options', hint)
                self.assertIn('不能代替', hint)
                self.assertIn('不能用含义无关', hint)
                error = next(e for e in report['events'] if e['type'] == 'validation_failure')
                self.assertEqual(error['reason'], 'schema_mismatch')
                self.assertEqual(error['details'], [{'path': FORMAT_ERROR[0], 'type': FORMAT_ERROR[1]}])
                self.assertEqual(report['status'], 'completed')
                self.assertEqual(report['run_summary']['model_calls'], 7)
                body = json.loads(history[1][1].content)
                shape = body['output_schema']['$defs']['ReferenceSelection']['properties']['reference_id']
                self.assertEqual(shape['pattern'], '^REF_[0-9a-f]{24}$')
                options = {r['reference_id'] for e in body['input']['evidence'] for r in e['reference_options']}
                self.assertEqual(set(shape['enum']), options)

    def test_family_2_empty_refs_can_select_observed_fields_without_erasing_good_findings(self):
        def mutate(value, messages, number):
            if number == 1:
                for index in (1, 2):
                    value['findings'][index]['refs'] = []
            return value
        report, history = self.run_worker(mutate)
        hint = self.assert_one_repair(report, history)
        self.assertIn(EMPTY_HINT, hint)
        self.assertIn('至少一个', hint)
        self.assertIn('保留其他已有证据支持的 finding', hint)
        error = next(e for e in report['events'] if e['type'] == 'validation_failure')
        self.assertEqual(error['details'], schema_error(*EMPTY_ERRORS)['details'])
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(len(report['output']['findings']), 5)

    def test_family_2_unsupported_claims_become_gaps_owner_and_manual_task_survive(self):
        for empty_owner in (False, True):
            with self.subTest(empty_owner=empty_owner):
                provider, models, history = OutageProvider(empty_owner=empty_owner), closure_models(), []
                def mutate(response, messages, number):
                    history.append(deepcopy(messages))
                    value = json.loads(response.content)
                    payload = json.loads(messages[1].content)['input']
                    owners = [e for e in payload['evidence'] if 'team' in e['payload']]
                    value['findings'] = [{'statement': '登记负责人：' + e['payload']['team'],
                        'refs': [selector(e, 'team')]} for e in owners]
                    if owners:
                        value['escalation_ref'] = selector(owners[0], 'team')
                    if number == 1:
                        value['findings'] += [{'statement': '合成无证据业务状态', 'refs': []},
                                              {'statement': '合成无证据原因', 'refs': []}]
                    else:
                        self.assertIn(EMPTY_HINT, messages[-1].content)
                        value['missing_information'] += ['当前指标与日志查询失败，未取得业务观测，业务状态及原因未知']
                    return json_response(value)
                models['diagnosis'] = AlterOutput(models['diagnosis'], mutate)
                report = collaborate(models, provider, P)
                self.assertEqual(len(history), 2)
                hint = history[1][-1].content
                for text in ('删除该 finding', 'missing_information', '查询失败', '查询为空', '负责人信息'):
                    self.assertIn(text, hint)
                self.assertEqual(history[0][:2], history[1][:2])
                self.assertEqual(report['status'], 'partial')
                self.assertEqual(report['run_summary']['model_calls'], 5)
                self.assertEqual(report['run_summary']['tool_calls'], 3)
                self.assertEqual(provider.calls.count('get_service_owner'), 1)
                self.assertEqual(len(report['output']['findings']), 0 if empty_owner else 1)
                self.assertFalse(report['output']['hypotheses'])
                self.assertTrue(report['output']['recommended_actions'])
                action = report['output']['recommended_actions'][0]
                self.assertEqual(action['kind'], 'manual_check')
                self.assertEqual(action['checks'], [])
                self.assertIsNone(action['repair'])
                self.assertIn('复现步骤及发生时间', action['action'])
                self.assertTrue(any('查询失败' in gap for gap in report['output']['missing_information']))
                self.assertFalse(any('合成无证据' in f['statement'] for f in report['output']['findings']))
                self.assertEqual(report['output']['escalation_team'], None if empty_owner else provider._data['owners'][0]['team'])
                self.assertTrue(all(n['status'] == 'pending' for n in report['evidence_needs']
                                    if n['purpose'] in {'symptom', 'mechanism'}))

    def test_family_3_mixed_errors_deliver_each_hint_once(self):
        def mutate(value, messages, number):
            if number == 1:
                value['findings'][0]['refs'][0]['reference_id'] = 'REF_bad'
                value['findings'][1]['refs'] = []
                value['findings'][2]['refs'] = []
            return value
        report, history = self.run_worker(mutate)
        hint = self.assert_one_repair(report, history)
        self.assertEqual(hint.count(FORMAT_HINT), 1)
        self.assertEqual(hint.count(EMPTY_HINT), 1)
        self.assertEqual(report['status'], 'completed')

    def test_family_3_unrelated_schema_errors_keep_generic_feedback(self):
        for path, kind in [('findings.1.statement', 'string_too_short'),
                           ('findings.1.refs', 'list_type'),
                           ('findings.1.refs.0.reference_id', 'string_type'),
                           ('hypotheses.0.support_refs.0.reference_id', 'string_pattern_mismatch'),
                           ('findings.x.refs', 'too_short'),
                           ('findings.1.refs.extra', 'too_short')]:
            with self.subTest(path=path, kind=kind):
                details = schema_error((path, kind))
                hint = correction_instruction('diagnosis', DiagnosisSelection, details, 'diagnosis')
                self.assertEqual(hint, '按提供的契约和已观测编号纠正一次。' + json.dumps(details, ensure_ascii=False))

    def test_family_4_valid_format_unregistered_or_duplicate_refs_still_rejected(self):
        for mode, reason in [('unknown', 'UNKNOWN_REFERENCE'), ('duplicate', 'DUPLICATE_REFERENCE')]:
            with self.subTest(mode=mode):
                def mutate(value, messages, number):
                    if number == 1:
                        value['findings'][0]['refs'][0]['reference_id'] = 'REF_bad'
                    elif mode == 'unknown':
                        value['findings'][0]['refs'][0]['reference_id'] = 'REF_' + '0' * 24
                    else:
                        value['findings'][0]['refs'] *= 2
                    return value
                report, history = self.run_worker(mutate)
                self.assert_one_repair(report, history)
                failures = [e['reason'] for e in report['events'] if e['type'] == 'validation_failure']
                self.assertEqual(failures, ['schema_mismatch', reason])
                self.assertEqual(report['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
                self.assertEqual(report['drafts'], [])
                self.assertEqual(report['reviews'], [])
                self.assertEqual(report['output']['findings'], [])

    def test_family_5_persistent_schema_errors_stop_in_original_allowance(self):
        for mode in ('format', 'empty'):
            with self.subTest(mode=mode):
                def mutate(value, messages, number):
                    if mode == 'format':
                        value['findings'][0]['refs'][0]['reference_id'] = 'REF_bad'
                    else:
                        value['findings'][0]['refs'] = []
                    return value
                report, history = self.run_worker(mutate)
                self.assert_one_repair(report, history)
                self.assertEqual(report['status'], 'partial')
                self.assertEqual(report['review_status'], 'not_performed')
                self.assertEqual(report['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
                self.assertEqual(report['run_summary']['model_calls'], 6)
                self.assertEqual(len([e for e in report['events'] if e['type'] == 'validation_failure']), 2)
                self.assertEqual(report['drafts'], [])
                self.assertEqual(report['reviews'], [])
                self.assertEqual(report['output']['findings'], [])

    def test_family_6_other_roles_and_contracts_do_not_get_diagnosis_schema_hints(self):
        details = schema_error(FORMAT_ERROR, *EMPTY_ERRORS)
        for role, schema in [('supervisor', SupervisorSelection), ('reviewer', ReviewDecision),
                             ('reviewer', ObservationReview), ('investigation', InvestigationSelection),
                             ('diagnosis', InvestigationSelection)]:
            with self.subTest(role=role, schema=schema.__name__):
                hint = correction_instruction(role, schema, details, 'diagnosis')
                self.assertNotIn(FORMAT_HINT, hint)
                self.assertNotIn(EMPTY_HINT, hint)

    def test_family_6_existing_reference_feedback_and_status_scope_remain(self):
        for reason in ('UNKNOWN_REFERENCE', 'DUPLICATE_REFERENCE', 'reference_mismatch'):
            with self.subTest(reason=reason):
                hint = correction_instruction('diagnosis', DiagnosisSelection,
                    {'reason': reason, 'details': []}, 'diagnosis')
                self.assertIn('不能混用', hint)
                self.assertIn('列表内不得重复', hint)
                self.assertNotIn(FORMAT_HINT, hint)
                self.assertNotIn(EMPTY_HINT, hint)
        for details in (schema_error(FORMAT_ERROR), schema_error(*EMPTY_ERRORS)):
            hint = correction_instruction('diagnosis', DiagnosisSelection, details, 'status_check')
            self.assertIn('hypotheses 和 recommended_actions 必须为空列表', hint)
            self.assertIn('只输出可引用的观测和信息缺口', hint)


if __name__ == '__main__':
    unittest.main()
