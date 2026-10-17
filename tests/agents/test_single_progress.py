"""Free regressions for SingleAgent's native-JSON/read/publication boundaries.

Uses development fixtures and distinct bound/unbound model doubles only. No
credentials, external model, independent evaluation records or gold answers.
"""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage
from agents.investigation import validation_details
from agents.single import single_agent, resolve_diagnosis
from evidence.render import render
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits, RunContext
from tests.tools.test_incident_tools import P
from tools.executor import ToolExecutor


class NativeJsonDouble:
    json_output = True

    def __init__(self, reply, *, bound=False, history=None):
        self.reply, self.bound = reply, bound
        self.history = [] if history is None else history

    def bind_tools(self, tools):
        assert tools
        return NativeJsonDouble(self.reply, bound=True, history=self.history)

    def invoke(self, messages):
        self.history.append({'tools': self.bound, 'messages': copy.deepcopy(messages)})
        result = self.reply(messages, len(self.history), self.bound)
        result.usage_metadata = {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}
        return result


def response(value):
    return AIMessage(content=json.dumps(value, ensure_ascii=False), response_metadata={'finish_reason': 'stop'})


def premature():
    return response({'hypotheses': [{'cause': 'canary-unobserved-cause',
        'evidence_explanation': 'canary-all-queries-returned-empty'}],
        'recommended_actions': [{'action': 'canary-repair-without-observation',
            'condition': '待核实', 'expected_result': '恢复', 'risk': '未知',
            'kind': 'manual_check', 'requires_approval': False}],
        'missing_information': ['canary-unverified-query-description']})


def read_logs(messages, number=1, *, absent=False):
    scope = json.loads(messages[1].content)['ticket']['scope']
    args = {key: scope[key] for key in ('start', 'end')} | {'category': 'dependency'}
    if absent:
        args['error_code'] = 'ABSENT'
    return AIMessage(content='', tool_calls=[{'name': 'get_service_logs', 'args': args,
        'id': f'local-source-{number}', 'type': 'tool_call'}], response_metadata={'finish_reason': 'tool_calls'})


def final_with_source(messages):
    evidence = [e for m in messages if isinstance(m, ToolMessage) for e in json.loads(m.content)['evidence']]
    source = next(e for e in evidence if 'message' in e['payload'])
    ref = next(o for o in source['reference_options'] if o['field_path'] == 'message')
    refs = [{'reference_id': ref['reference_id']}]
    return response({'findings': [{'statement': '当前来源', 'refs': refs}],
        'hypotheses': [{'cause': '依赖交互失败可能影响当前请求', 'support_refs': refs,
                       'pending_checks': ['人工核实触发原因']}],
        'recommended_actions': [{'action': '人工核实依赖观测', 'condition': '异常持续',
            'expected_result': '取得实际结果', 'risk': '只读核查', 'kind': 'manual_check', 'requires_approval': False}]})


class SingleProgressTests(unittest.TestCase):
    def run_single(self, reply, *, provider=None, limits=None):
        model = NativeJsonDouble(reply)
        result = single_agent(model, provider or FixtureProvider('case_001', P), P,
            limits=limits or Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20))
        self.assertLessEqual(result['run_summary']['model_calls'], result['run_summary']['limits']['model_calls'])
        self.assertLessEqual(result['run_summary']['tool_calls'], result['run_summary']['limits']['tool_calls'])
        self.assertLessEqual(result['repairs'], 1)
        return result, model

    def assert_unpublished(self, result):
        self.assertFalse(result['output']['findings'])
        self.assertFalse(result['output']['hypotheses'])
        self.assertFalse(result['output']['recommended_actions'])
        self.assertNotIn('canary-', json.dumps(result['output']))
        self.assertEqual(result['query_execution']['state'], 'not_executed')
        self.assertEqual(result['query_execution']['queries'], [])
        self.assertIn('尚未执行工具查询', render(result))

    def test_distinct_protocol_errors_keep_codes_and_paths(self):
        registry = SimpleNamespace(evidence={}, references={}, provider=None)
        action = {'action': '核查', 'condition': '待核实', 'expected_result': '取得结果', 'risk': '未知',
                  'kind': 'manual_change', 'requires_approval': False}
        cases = [('UNKNOWN_REFERENCE', {'findings': [{'statement': '观测', 'refs': [
            {'reference_id': 'REF_ffffffffffffffffffffffff'}]}]}),
            ('CHANGE_APPROVAL_REQUIRED', {'recommended_actions': [action]}),
            ('READ_ONLY_ACTION_INVALID', {'recommended_actions': [action | {'kind': 'read_only_check'}]})]
        for code, value in cases:
            with self.subTest(code=code), self.assertRaises(ValueError) as error:
                resolve_diagnosis(json.dumps(value), registry)
            failure = validation_details(error.exception)
            self.assertEqual(failure['reason'], code)
            if code == 'UNKNOWN_REFERENCE':
                self.assertEqual(failure['details'][0]['path'], 'findings.0.refs.0.reference_id')

    def test_generic_errors_do_not_echo_exception_text(self):
        for error, reason in [(TypeError('canary-secret'), 'invalid_output_type'),
                              (ValueError('canary-secret'), 'output_validation_error')]:
            details = validation_details(error)
            self.assertEqual(details['reason'], reason)
            self.assertNotIn('canary-', json.dumps(details))

    def test_first_invalid_output_keeps_tools_then_can_read_and_finish(self):
        for invalid in [AIMessage(content='canary-not-json'), response({'findings': [{'statement': '未观测',
                'refs': [{'reference_id': 'REF_ffffffffffffffffffffffff'}]}]})]:
            def reply(messages, number, bound):
                return invalid if number == 1 else read_logs(messages, number) if number == 2 else final_with_source(messages)
            with self.subTest(content=invalid.content[:25]):
                result, model = self.run_single(reply)
                self.assertEqual([h['tools'] for h in model.history], [True, True, True])
                self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls']), (3, 1))
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['repairs'], 1)
                error = result['validation_failures'][0]
                self.assertEqual(error['stage'], 'output_resolution')
                self.assertEqual(error['content_type'], 'str')
                self.assertEqual(error['tool_call_count'], 0)
                self.assertNotIn('canary-not-json', json.dumps(result))

    def test_legal_premature_answer_gets_one_read_prompt_and_recovers(self):
        def reply(messages, number, bound):
            return premature() if number == 1 else read_logs(messages, number) if number == 2 else final_with_source(messages)
        result, model = self.run_single(reply)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(model.history), 3)
        self.assertEqual(result['repairs'], 0)
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 1)
        self.assertNotIn('canary-', json.dumps(result['output']))

    def test_persistent_no_read_safely_stops_without_publishing_prose_or_advice(self):
        result, model = self.run_single(lambda *_: premature())
        self.assertEqual(result['run_summary']['termination_reason'], 'NO_TOOL_PROGRESS')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual((len(model.history), result['run_summary']['tool_calls']), (2, 0))
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 1)
        self.assert_unpublished(result)

    def test_format_repair_and_progress_prompt_are_both_bounded(self):
        result, model = self.run_single(lambda messages, number, bound:
            AIMessage(content='bad-json') if number == 1 else premature())
        self.assertEqual((len(model.history), result['repairs']), (3, 1))
        self.assertEqual(sum(e['type'] == 'task_execution_correction' for e in result['events']), 1)
        self.assertEqual(result['run_summary']['termination_reason'], 'NO_TOOL_PROGRESS')
        self.assert_unpublished(result)

    def test_repeated_invalid_output_still_stops_after_one_format_repair(self):
        result, model = self.run_single(lambda *_: AIMessage(content='bad-json'))
        self.assertEqual((len(model.history), result['repairs']), (2, 1))
        self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assert_unpublished(result)

    def test_non_string_content_records_type_without_rejected_body(self):
        result, model = self.run_single(lambda *_: AIMessage(content=[{'type': 'text', 'text': 'canary-body'}]))
        self.assertEqual((len(model.history), result['repairs']), (2, 1))
        self.assertEqual(result['validation_failures'][0]['reason'], 'invalid_output_type')
        self.assertEqual(result['validation_failures'][0]['content_type'], 'list')
        self.assertNotIn('canary-body', json.dumps(result))

    def test_failed_and_empty_queries_keep_distinct_server_status_and_scope(self):
        for failed in (True, False):
            provider = FixtureProvider('case_001', P)
            original = provider.query
            def query(*args, **kwargs):
                if failed:
                    raise ProviderError('SOURCE_UNAVAILABLE', retryable=False)
                return original(*args, **kwargs)
            def reply(messages, number, bound):
                return read_logs(messages, absent=True) if number == 1 else response({'missing_information': ['需要补充观测。']})
            with self.subTest(failed=failed), patch.object(provider, 'query', side_effect=query):
                result, model = self.run_single(reply, provider=provider)
            self.assertEqual((len(model.history), result['run_summary']['tool_calls']), (2, 1))
            self.assertEqual(result['run_summary']['termination_reason'], 'DIAGNOSIS_INCOMPLETE')
            self.assertFalse(any(e['type'] == 'task_execution_correction' for e in result['events']))
            row = result['query_execution']['queries'][0]
            self.assertEqual(result['query_execution']['source'], 'server_read_ledger')
            self.assertEqual(row['status'], 'error' if failed else 'empty')
            self.assertEqual(row['error_code'], 'SOURCE_UNAVAILABLE' if failed else None)
            self.assertEqual(row['query_profile']['error_code'], 'ABSENT')
            self.assertIn('start', row['query_profile'])
            self.assertIn('end', row['query_profile'])
            self.assertIn('查询失败（SOURCE_UNAVAILABLE）' if failed else '当前过滤条件未匹配记录', render(result))
            collection = json.loads(model.history[1]['messages'][1].content)['collection']
            self.assertEqual(collection[0]['status'], row['status'])

    def test_format_correction_after_read_uses_json_without_tools(self):
        def reply(messages, number, bound):
            return read_logs(messages) if number == 1 else AIMessage(content='bad-json') if number == 2 else final_with_source(messages)
        result, model = self.run_single(reply)
        self.assertEqual([h['tools'] for h in model.history], [True, True, False])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['run_summary']['model_calls'], result['run_summary']['tool_calls'], result['repairs']), (3, 1, 1))

    def test_insufficient_remaining_steps_do_not_add_calls(self):
        result, model = self.run_single(lambda *_: premature(),
            limits=Limits(model_calls=2, tool_calls=8, reserve_model_calls=0))
        self.assertEqual((len(model.history), result['run_summary']['tool_calls']), (1, 0))
        self.assertFalse(any(e['type'] == 'task_execution_correction' for e in result['events']))
        self.assert_unpublished(result)

    def test_tool_budget_exit_uses_reserve_without_executing_tools(self):
        result, model = self.run_single(lambda *_: premature(),
            limits=Limits(model_calls=3, tool_calls=0, reserve_model_calls=1))
        self.assertEqual([h['tools'] for h in model.history], [False])
        self.assertEqual(result['run_summary']['termination_reason'], 'BUDGET_EXCEEDED')
        self.assertEqual(result['run_summary']['tool_calls'], 0)
        self.assert_unpublished(result)

    def test_late_model_response_is_discarded_without_more_calls(self):
        def reply(*_):
            from runtime.context import current
            current().started -= 200
            return premature()
        result, model = self.run_single(reply)
        self.assertEqual((len(model.history), result['run_summary']['tool_calls']), (1, 0))
        self.assertEqual(result['run_summary']['termination_reason'], 'TIME_BUDGET_EXCEEDED')
        self.assert_unpublished(result)

    def test_existing_observations_can_be_reused_without_new_read_prompt(self):
        provider = FixtureProvider('case_001', P)
        scope = provider.ticket().scope
        events = []
        context = RunContext(scope=scope, limits=Limits(), emit=events.append)
        executor = ToolExecutor(provider, P, scope, context, role='single')
        source = executor.execute('get_service_owner', {'alias': scope.service}).evidence[0]
        ref = next(o.reference_id for o in source.reference_options if o.field_path == 'team')
        value = {'findings': [{'statement': '负责人', 'refs': [{'reference_id': ref}]}]}
        model = NativeJsonDouble(lambda *_: response(value))
        result = single_agent(model, provider, P, context=context, event_log=events, executor=executor)
        self.assertEqual((len(model.history), result['run_summary']['tool_calls']), (1, 1))
        self.assertFalse(any(e['type'] == 'task_execution_correction' for e in result['events']))
        self.assertTrue(result['output']['findings'])


if __name__ == '__main__':
    unittest.main()
