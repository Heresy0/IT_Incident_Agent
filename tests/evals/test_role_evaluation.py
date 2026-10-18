"""Free controls for frozen role routing, accounting and reference isolation."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from config.incident import IncidentConfig
from evals.closed_loop_cases import build_cases
from runtime.context import ExecutionError
from scripts.evaluate_autonomous_roles import (ARMS, ROLES, FROZEN_LIMITS, RecordedRoleModel,
    main, models_for, parse_args, plan_for, role_map, role_usage, run_case, run_fixed,
    run_metrics, schedule_roles, summarize)


class FreeModel:
    json_output = True

    def __init__(self, judgments=None, *, coverage='covered', error=None, usage=True):
        self.judgments = judgments or {}
        self.coverage, self.error, self.usage = coverage, error, usage
        self.inputs = []
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        if self.error:
            raise self.error
        data = json.loads(messages[-1].content)
        self.inputs.append(data)
        if 'targets' not in data:
            body = {'kind': 'finish', 'question': 'Free routing check.'}
        else:
            refs = [option['reference_id'] for option in data['reference_options']][:1]
            rows = []
            for target in data['targets']:
                if 'challenge_policy_version' in data:
                    rows.append({'target_id': target['id'], 'coverage': self.coverage,
                        'uncovered_assertion': 'Untested scope' if self.coverage == 'uncovered' else '',
                        'missing_evidence': 'Scope observations' if self.coverage == 'uncovered' else '',
                        'refs': refs, 'reason': 'Free role-routing check.'})
                else:
                    field = 'decision' if target['review_kind'] == 'check_appropriateness' else 'relation'
                    rows.append({'target_id': target['id'], field: self.judgments.get(target['id'], 'supported'),
                                 'refs': refs, 'issue': 'Free wiring control; no semantic assessment.'})
            body = {'items': rows}
        return AIMessage(content=json.dumps(body), response_metadata={'finish_reason': 'stop'},
                         usage_metadata={'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10}
                         if self.usage else None)


class RoleEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cases = build_cases()
        self.config = IncidentConfig(api_key='private-key', base_url='https://private-endpoint',
                                     extra_body={'secret': 'hidden-extra'})

    def test_each_arm_changes_only_its_declared_role(self):
        baseline = role_map('flash_all')
        self.assertEqual(set(baseline), set(ROLES))
        for arm, changed in (('investigator_plus', {'investigation'}), ('reviewer_plus', {'reviewer'})):
            mapping = role_map(arm)
            self.assertEqual({role for role in ROLES if mapping[role] != baseline[role]}, changed)
            self.assertEqual(mapping['challenger'], baseline['challenger'])
            self.assertEqual(mapping['diagnosis'], baseline['diagnosis'])

    def test_schedule_rotates_whole_matched_blocks(self):
        schedule = list(schedule_roles(['a', 'b'], list(ARMS), 2))
        self.assertEqual(len(schedule), 12)
        self.assertEqual([row[1] for row in schedule[:3]], list(ARMS))
        self.assertEqual([row[1] for row in schedule[3:6]], [ARMS[1], ARMS[2], ARMS[0]])
        self.assertEqual([row[1] for row in schedule[6:9]], [ARMS[1], ARMS[2], ARMS[0]])
        self.assertEqual({(case, arm, repeat) for case, arm, repeat in schedule},
                         {(case, arm, repeat) for case in ('a', 'b') for arm in ARMS for repeat in (1, 2)})

    def test_limits_are_explicit_and_do_not_change_with_environment(self):
        with patch.dict('os.environ', {'INCIDENT_MAX_MODEL_CALLS': '64', 'INCIDENT_MAX_INPUT_CHARS': '4000'}):
            parser, args = parse_args(['--plan', '--cases', 'probe_scope'])
            plan = plan_for(args, self.config, self.cases)
        self.assertEqual(plan['limits']['model_calls'], 12)
        self.assertEqual(plan['limits']['tool_calls'], 8)
        self.assertEqual(plan['limits']['seconds'], 180)
        self.assertEqual(plan['limits']['input_chars'], 48000)

    def test_model_binding_has_no_implicit_challenger_fallback(self):
        calls, built = [], []

        def factory(key, name, **settings):
            built.append((name, settings))
            return FreeModel()

        models = models_for(role_map('reviewer_plus'), self.config, None, calls, factory=factory)
        self.assertEqual(set(models), set(ROLES))
        self.assertEqual(models['reviewer'].model_name, 'qwen3.7-plus')
        self.assertEqual(models['challenger'].model_name, 'qwen3.7-flash')
        self.assertTrue(all(settings['request_timeout'] == 30 for _, settings in built))
        self.assertTrue(all(settings['max_output_tokens'] == 2200 for _, settings in built))
        with self.assertRaises(ValueError):
            models_for({'investigation': 'flash'}, self.config, None, calls, factory=factory)

    def test_recorded_calls_include_actual_role_stage_hashes_and_usage(self):
        calls = []
        model = RecordedRoleModel(FreeModel(), 'investigation', 'qwen3.7-plus', calls)
        messages = [SystemMessage(content='Free system'), HumanMessage(content='{"stage":"revise"}')]
        model.invoke(messages)
        model.invoke(messages)
        self.assertEqual(calls[0]['role'], 'investigation')
        self.assertEqual(calls[0]['stage'], 'revise')
        self.assertEqual(calls[0]['request_sha256'], calls[1]['request_sha256'])
        self.assertEqual(calls[0]['output_sha256'], calls[1]['output_sha256'])
        self.assertEqual(calls[0]['usage'], {'input_tokens': 7, 'output_tokens': 3})
        self.assertNotIn('Free system', json.dumps(calls))
        self.assertEqual(role_usage(calls)[0]['input_tokens'], 14)

    def test_bind_tools_preserves_role_logging_and_changes_request_hash(self):
        calls = []
        model = RecordedRoleModel(FreeModel(), 'investigation', 'plus', calls)
        messages = [HumanMessage(content='{"stage":"investigate"}')]
        model.invoke(messages)
        bound = model.bind_tools([{'name': 'free-tool'}])
        bound.invoke(messages)
        self.assertNotEqual(calls[0]['request_sha256'], calls[1]['request_sha256'])
        self.assertEqual(calls[1]['role'], 'investigation')
        self.assertIs(bound.calls, calls)

    def test_unknown_usage_is_not_reported_as_zero_cost(self):
        calls = []
        RecordedRoleModel(FreeModel(usage=False), 'reviewer', 'flash', calls).invoke([HumanMessage(content='{}')])
        self.assertIsNone(calls[0]['usage'])
        usage = role_usage(calls)[0]
        self.assertEqual(usage['unknown_usage_calls'], 1)
        self.assertEqual(usage['known_usage_calls'], 0)

    def test_exception_log_excludes_exception_text_and_secrets(self):
        calls = []
        model = RecordedRoleModel(FreeModel(error=ValueError('private-api-key')), 'reviewer', 'flash', calls)
        with self.assertRaises(ValueError):
            model.invoke([HumanMessage(content='{}')])
        self.assertEqual(calls[0]['error']['exception_type'], 'ValueError')
        self.assertNotIn('private-api-key', json.dumps(calls))

    def test_fixed_review_separately_binds_primary_and_challenger(self):
        calls = []
        reviewer = FreeModel({'C1': 'supported', 'C2': 'supported'})
        challenger = FreeModel(coverage='uncovered')
        models = {role: object() for role in ROLES}
        models['reviewer'] = RecordedRoleModel(reviewer, 'reviewer', 'plus', calls)
        models['challenger'] = RecordedRoleModel(challenger, 'challenger', 'flash', calls)
        report = run_fixed(self.cases['probe_scope'], models, FROZEN_LIMITS, rework=False)
        self.assertNotIn('execution_error', report)
        self.assertEqual([row['role'] for row in calls], ['reviewer', 'challenger'])
        self.assertEqual(report['primary_batch']['items'][0]['relation'], 'supported')
        self.assertEqual(report['review_batch']['items'][0]['relation'], 'partial')
        metrics = run_metrics(self.cases['probe_scope'], report, 'review')
        self.assertTrue(metrics['primary_review_controls'][0]['false_accept'])
        self.assertFalse(metrics['final_review_controls'][0]['false_accept'])
        self.assertNotIn('provisional_reference', reviewer.inputs[0])

    def test_fixed_rework_passes_entire_explicit_map_to_existing_engine(self):
        models = dict.fromkeys(ROLES, FreeModel({'C1': 'insufficient'}))
        seen = []

        def fake(actual_models, draft, batch, executor, context, *, repairs, enable_challenge):
            seen.append(actual_models)
            return draft, batch, {'performed': True, 'changed_ids': ['C1'], 'withdrawn_ids': [], 'failures': []}

        with patch('workflow.v2.engine.execute_review_rework', new=fake):
            report = run_fixed(self.cases['followup_available'], models, FROZEN_LIMITS, rework=True)
        self.assertNotIn('execution_error', report)
        self.assertIs(seen[0], models)
        self.assertEqual(report['rework_rounds'], 1)
        self.assertNotIn('provisional_reference', report)
        self.assertIn('pending_human_review', report['revised_claim_accuracy'])
        metrics = run_metrics(self.cases['followup_available'], report, 'fixed_rework')
        self.assertEqual(metrics['review_controls'], [])
        self.assertTrue(metrics['primary_review_controls'])

    def test_e2e_uses_same_engine_features_and_limits_for_all_arms(self):
        seen = []

        def fake(models, provider, principal, *, context, event_log, enable_rework, enable_challenge):
            seen.append((models, context.limits, enable_rework, enable_challenge))
            return {'run_summary': context.summary()}

        with patch('workflow.v2.engine.investigate', new=fake):
            for arm in ARMS:
                run_case(self.cases['followup_available'], role_map(arm), 'e2e')
        self.assertTrue(all(row[1:] == (FROZEN_LIMITS, True, True) for row in seen))

    def test_plan_is_free_and_provider_secrets_are_only_hashed(self):
        with patch('scripts.evaluate_autonomous_roles.IncidentConfig.from_file', return_value=self.config), \
             patch('agents.models.build_model', side_effect=AssertionError('model initialized')), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--plan', '--cases', 'probe_scope']), 0)
        raw = output.getvalue()
        plan = json.loads(raw)
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paid_requests_sent'], 0)
        self.assertIn('provider_configuration_sha256', plan)
        self.assertIn('app/workflow/v2/engine.py', plan['app_source_sha256'])
        self.assertNotIn('app/agents/prompts.py', plan['app_source_sha256'])
        for secret in ('private-key', 'private-endpoint', 'hidden-extra'):
            self.assertNotIn(secret, raw)

    def test_live_requires_ledger_and_call_cap_supports_planned_stages(self):
        for argv in (['--live'], ['--cases', 'missing'], ['--arms', 'flash_all', 'flash_all'],
                     ['--flash-model', 'same', '--plus-model', 'same']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)
        self.assertEqual(parse_args(['--live', '--budget-ledger', 'ledger.json', '--max-model-calls', '216'])[1].max_model_calls, 216)

    def test_requested_source_mismatch_stops_before_build_or_paid_request(self):
        with patch('scripts.evaluate_autonomous_roles.IncidentConfig.from_file', return_value=self.config), \
             patch('agents.models.build_model', side_effect=AssertionError('model initialized')), \
             redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(['--plan', '--expected-source', 'wrong']), 2)
        self.assertNotIn('private', output.getvalue())

    def test_summary_never_turns_synthetic_results_into_human_approval(self):
        summary = summarize([{'metrics': {'execution_error': None}}], 1)
        self.assertTrue(summary['complete_execution'])
        self.assertEqual(summary['human_semantic_gate'], 'pending')
        self.assertEqual(summary['rollout_recommendation'], 'hold')
        self.assertFalse(summarize([], 1)['complete_execution'])


if __name__ == '__main__':
    unittest.main()
