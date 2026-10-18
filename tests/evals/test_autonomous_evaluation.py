"""Free evaluation boundaries; scripted controls do not establish model quality."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from config.incident import IncidentConfig
from evidence.contracts import MetricsArgs
from runtime.context import ExecutionError, RunContext
from scripts.evaluate_autonomous import (BudgetedModel, FrozenWorld, GlobalPaidBudget,
    PRINCIPAL, engineering_metrics, main, parse_args, review_inputs,
    safe_exception, schedule, summarize_review_controls)
from tools.executor import ToolExecutor


def ledger(path, **overrides):
    state = {'max_requests': 180, 'max_input_tokens': 600000, 'max_output_tokens': 120000,
             'max_model_seconds': 2700, 'requests': 14, 'input_tokens': 10000,
             'output_tokens': 1000, 'model_seconds': 20, 'unknown_usage_calls': 0, **overrides}
    path.write_text(json.dumps(state), encoding='utf-8')
    return state


class FakeChat:
    json_output = True

    def __init__(self, *, known=True, error=None):
        self.known, self.error, self.calls = known, error, 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return AIMessage(content='{}', usage_metadata={
            'input_tokens': 17, 'output_tokens': 3, 'total_tokens': 20} if self.known else None)


class AutonomousEvaluationTests(unittest.TestCase):
    def test_plan_is_free_and_does_not_expose_config_secrets(self):
        config = IncidentConfig(api_key='private-key-do-not-print', model='frozen-model',
                                base_url='https://private-endpoint.invalid',
                                extra_body={'secret_field': 'private-extra-do-not-print'})
        with patch('scripts.evaluate_autonomous.IncidentConfig.from_file', return_value=config), \
             patch('agents.models.build_model', side_effect=AssertionError('model initialized')):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--plan']), 0)
        raw = output.getvalue()
        plan = json.loads(raw)
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paid_requests_sent'], 0)
        self.assertEqual(plan['model_settings']['model'], 'frozen-model')
        self.assertEqual(plan['arms'], ['v2'])
        self.assertGreaterEqual(len(set(plan['world']['services'].values())), 2)
        self.assertFalse(plan['world']['profile_required'])
        self.assertFalse(plan['world']['external_blind_evaluation'])
        self.assertTrue(plan['source_files_sha256'])
        for secret in ('private-key-do-not-print', 'private-endpoint.invalid', 'private-extra-do-not-print'):
            self.assertNotIn(secret, raw)

    def test_live_requires_explicit_total_budget_and_ledger(self):
        for arguments in (['--live'], ['--live', '--max-model-calls', '12'],
                          ['--live', '--budget-ledger', 'anything.json'],
                          ['--live', '--max-model-calls', '181', '--budget-ledger', 'anything.json']):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(arguments)

    def test_scripted_and_review_do_not_pretend_to_compare_unsupported_arms(self):
        for args in (['--scripted', '--arms', 'legacy'], ['--stage', 'review', '--arms', 'legacy', 'v2']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)
        self.assertEqual(parse_args(['--stage', 'review'])[1].arms, ['v2'])
        self.assertEqual(parse_args(['--scripted'])[1].arms, ['v2'])

    def test_world_supports_new_queries_and_keeps_a_frozen_copy(self):
        world = FrozenWorld(['case_001', 'case_004'])
        provider = world.provider('case_001')
        scope = provider.ticket().scope
        metric = provider._data['metrics'][0]['metric']
        args = MetricsArgs.model_validate_json(json.dumps({'start': scope.start.isoformat(),
            'end': scope.end.isoformat(), 'metrics': [metric]}))
        full, _ = provider.query('get_service_metrics', args, scope)
        midpoint = sorted(row['timestamp'] for row in full)[len(full) // 2]
        narrower = MetricsArgs.model_validate_json(json.dumps({'start': midpoint,
            'end': scope.end.isoformat(), 'metrics': [metric]}))
        rows, _ = provider.query('get_service_metrics', narrower, scope)
        self.assertTrue(rows)
        self.assertTrue({row['id'] for row in rows} <= {row['id'] for row in full})
        self.assertLessEqual(len(rows), len(full))
        provider._data['metrics'][0]['value'] = 'mutated'
        self.assertNotEqual(world.provider('case_001')._data['metrics'][0]['value'], 'mutated')
        self.assertNotEqual(world.freeze()['services']['case_001'], world.freeze()['services']['case_004'])

    def test_unknown_duplicate_and_private_cases_rejected(self):
        for cases in (['case_001', 'case_001'], ['../../.env'], []):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                FrozenWorld(cases)

    def test_anonymization_is_optional_and_has_frozen_transformed_hashes(self):
        original = FrozenWorld(['case_001', 'case_004'])
        changed = FrozenWorld(['case_001', 'case_004'], anonymize=True)
        reordered = FrozenWorld(['case_004', 'case_001'], anonymize=True)
        self.assertFalse(original.freeze()['anonymization']['enabled'])
        self.assertFalse(hasattr(original.provider('case_001'), 'binding'))
        self.assertTrue(changed.freeze()['anonymization']['enabled'])
        self.assertEqual(original.hashes, changed.hashes)
        self.assertNotEqual(original.freeze()['runtime_world_sha256'], changed.freeze()['runtime_world_sha256'])
        self.assertEqual(changed.freeze()['runtime_world_sha256'], reordered.freeze()['runtime_world_sha256'])
        self.assertEqual(changed.freeze()['anonymization'], reordered.freeze()['anonymization'])

    def test_anonymization_preserves_observations_and_removes_original_runtime_identifiers(self):
        original = FrozenWorld(['case_001'])
        changed = FrozenWorld(['case_001'], anonymize=True)
        old, new = original.payloads['case_001'], changed.payloads['case_001']
        self.assertNotEqual(old['ticket']['scope']['service'], new['ticket']['scope']['service'])
        self.assertNotEqual(old['ticket']['scope']['allowed_dependencies'], new['ticket']['scope']['allowed_dependencies'])
        self.assertNotEqual(old['ticket']['incident_id'], new['ticket']['incident_id'])
        for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
            self.assertEqual(len(old[kind]), len(new[kind]))
            for before, after in zip(old[kind], new[kind]):
                for field in ('value', 'timestamp', 'unit', 'aggregation', 'granularity', 'error_code', 'level'):
                    if field in before:
                        self.assertEqual(before[field], after[field])
                self.assertNotEqual(before['id'], after['id'])
        provider = changed.provider('case_001')
        executor = ToolExecutor(provider, PRINCIPAL, provider.ticket().scope,
                                RunContext(scope=provider.ticket().scope), role='single')
        runtime = json.dumps({'ticket': provider.ticket().model_dump(mode='json'), 'data': provider._data,
                              'capabilities': executor.capabilities(generic=True),
                              'schemas': executor.schemas(generic=True)})
        for name in ('case_001', old['ticket']['scope']['service'],
                     *old['ticket']['scope']['allowed_dependencies'],
                     *{row['metric'] for row in old['metrics']}):
            self.assertNotIn(name, runtime)

    def test_renamed_metrics_are_queryable_with_public_meanings_and_scope_boundaries(self):
        world = FrozenWorld(['case_001'], anonymize=True)
        provider = world.provider('case_001')
        scope = provider.ticket().scope
        executor = ToolExecutor(provider, PRINCIPAL, scope, RunContext(scope=scope), role='single')
        metrics = executor.capabilities(generic=True)['registered_metrics']
        alias = world.anonymization['metric_mapping']['request_latency']
        self.assertIn('duration', metrics[alias]['description'])
        self.assertEqual(metrics[alias]['observed_object'], 'service inbound request duration')
        window = {k: v for k, v in scope.model_dump(mode='json').items() if k in ('start', 'end')}
        result = executor.execute('get_service_metrics', {**window, 'metrics': [alias]})
        self.assertEqual(result.status, 'ok')
        self.assertEqual({item.payload['metric'] for item in result.evidence}, {alias})
        self.assertEqual(sorted(item.payload['value'] for item in result.evidence), [120, 2900])
        denied = executor.execute('get_service_metrics', {**window, 'metrics': ['request_latency']})
        self.assertEqual(denied.error.code, 'METRIC_NOT_REGISTERED')
        outside = executor.execute('get_service_owner', {'alias': 'checkout-api'})
        self.assertEqual(outside.error.code, 'SERVICE_DENIED')
        owner = executor.execute('get_service_owner', {'alias': scope.service})
        self.assertEqual(owner.status, 'ok')
        self.assertEqual(owner.evidence[0].payload['service'], scope.service)

    def test_schedule_interleaves_arms_and_preserves_every_repeat(self):
        rows = list(schedule(['a', 'b'], ['legacy', 'v2'], 2))
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[:4], [('a', 'legacy', 1), ('a', 'v2', 1), ('b', 'v2', 1), ('b', 'legacy', 1)])
        self.assertEqual(rows[4:6], [('a', 'v2', 2), ('a', 'legacy', 2)])
        self.assertEqual(len(set(rows)), len(rows))

    def test_bound_models_share_strict_global_request_cap_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.json'
            initial = ledger(path)
            budget = GlobalPaidBudget(path, 2)
            chat = FakeChat()
            model = BudgetedModel(chat, budget)
            model.invoke([HumanMessage(content='hello')])
            model.bind_tools([]).invoke([HumanMessage(content='second')])
            with self.assertRaises(ExecutionError):
                model.invoke([HumanMessage(content='third')])
            state = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(chat.calls, 2)
            self.assertEqual(state['requests'], initial['requests'] + 2)
            self.assertEqual(state['input_tokens'], initial['input_tokens'] + 34)
            self.assertEqual(state['output_tokens'], initial['output_tokens'] + 6)
            self.assertEqual(budget.summary()['remaining']['requests'], 164)

    def test_token_admission_prevents_request_and_unknown_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.json'
            initial = ledger(path)
            chat = FakeChat(known=False)
            budget = GlobalPaidBudget(path, 3)
            BudgetedModel(chat, budget).invoke([HumanMessage(content='hello')])
            state = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(state['unknown_usage_calls'], 1)
            self.assertGreater(state['input_tokens'], initial['input_tokens'])
            self.assertEqual(state['output_tokens'], initial['output_tokens'] + 2200)
            ledger(path, output_tokens=119999)
            with self.assertRaises(ExecutionError):
                BudgetedModel(chat, budget).invoke([HumanMessage(content='denied')])
            self.assertEqual(chat.calls, 1)

    def test_failed_transport_counts_request_without_leaking_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.json'
            initial = ledger(path)
            budget = GlobalPaidBudget(path, 2)
            exc = RuntimeError('Bearer private-secret')
            with self.assertRaises(RuntimeError):
                BudgetedModel(FakeChat(error=exc), budget).invoke([HumanMessage(content='hello')])
            state = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(state['requests'], initial['requests'] + 1)
            self.assertEqual(state['unknown_usage_calls'], 1)
            self.assertNotIn('private-secret', json.dumps(safe_exception(exc)))

    def test_provider_bound_violation_closes_admission_and_records_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.json'
            initial = ledger(path)
            budget = GlobalPaidBudget(path, 3)
            reserved = budget.reserve(100, 20, 30)
            budget.settle(reserved, {'input_tokens': 101, 'output_tokens': 10}, 31)
            state = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue(state['admission_closed'])
            self.assertEqual(state['input_tokens'], initial['input_tokens'] + 101)
            self.assertEqual(state['model_seconds'], initial['model_seconds'] + 31)
            with self.assertRaises(ExecutionError):
                budget.reserve(1, 1, 1)

    def test_nonfinite_budget_cannot_disable_admission_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.json'
            ledger(path, max_requests=float('nan'))
            with self.assertRaises(ValueError):
                GlobalPaidBudget(path, 2)

    def test_review_controls_use_source_refs_and_keep_expected_labels_out_of_prompt(self):
        world = FrozenWorld(['case_001'])
        provider = world.provider('case_001')
        context = RunContext(scope=provider.ticket().scope)
        executor = ToolExecutor(provider, PRINCIPAL, context.scope, context, role='single')
        window = {k: v for k, v in context.scope.model_dump(mode='json').items() if k in ('start', 'end')}
        executor.execute('get_service_metrics', {**window, 'metrics': [provider._data['metrics'][0]['metric']]})
        controls = review_inputs(executor)
        self.assertEqual(len(controls), 5)
        self.assertEqual({relation for _, _, relation in controls},
                         {'supported', 'partial', 'contradicted', 'insufficient'})
        from workflow.v2.contracts import parse_draft
        for name, raw, expected in controls:
            draft, errors = parse_draft(raw, executor)
            self.assertFalse(errors, (name, errors))
            self.assertEqual(len(draft.claims), 1)
            self.assertNotIn('provisional_expected_relation', raw)
            self.assertNotIn('review', raw)

    def test_engineering_metrics_leave_semantics_pending(self):
        result = engineering_metrics({'status': 'partial', 'evidence': [{}], 'events': [],
                                      'run_summary': {'model_calls': 2}})
        self.assertEqual(result['evidence_count'], 1)
        self.assertEqual(result['semantic_quality_status'], 'pending_human_review')
        self.assertTrue(all(value is None for value in result['semantic_metrics'].values()))

    def test_runtime_errors_are_sanitized_without_free_text(self):
        with patch('scripts.evaluate_autonomous.IncidentConfig.from_file',
                   side_effect=ValueError('api_key=secret')), redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(['--plan']), 2)
        self.assertNotIn('secret', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['evaluation_error']['exception_type'], 'ValueError')

    def test_review_stability_is_separate_from_human_accuracy(self):
        runs = [{'case_id': 'a', 'control': 'fact', 'review_control': {
            'observed_relation': relation, 'agrees_with_provisional_reference': relation == 'supported'}}
            for relation in ('supported', 'insufficient')]
        summary = summarize_review_controls(runs)
        self.assertEqual(summary['repeated_groups'], 1)
        self.assertEqual(summary['groups_with_relation_changes'], 1)
        self.assertEqual(summary['agreement_count'], 1)
        self.assertFalse(summary['human_verified'])


if __name__ == '__main__':
    unittest.main()
