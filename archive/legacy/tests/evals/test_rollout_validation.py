"""Free route/budget wiring tests; scripted doubles are not live semantic evidence."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.scripted import control_queries, scripted_models
from config.incident import IncidentConfig
from providers.fixtures import FixtureProvider
from scripts.evaluate_autonomous import BudgetedModel, GlobalPaidBudget
from scripts.validate_autonomous_rollout import (GATE_KIND, LOCAL_ENV, PRINCIPAL, main,
                                                parse_args, plan_for, run_validation)
from workflow.v2.scripted import scripted_model


def make_ledger(path):
    path.write_text(json.dumps({'max_requests': 40, 'max_input_tokens': 2000000,
        'max_output_tokens': 100000, 'max_model_seconds': 2000, 'requests': 0,
        'input_tokens': 0, 'output_tokens': 0, 'model_seconds': 0, 'unknown_usage_calls': 0}), encoding='utf-8')


def free_model_factory(config, budget):
    def factory(*_):
        legacy = scripted_models()
        generic = scripted_model(FixtureProvider('case_001', PRINCIPAL))

        class Bridge:
            json_output = True

            def __init__(self, role, model=None):
                self.role, self.model = role, model or legacy.get(role)

            def bind_tools(self, tools):
                return Bridge(self.role, self.model.bind_tools(tools))

            def invoke(self, messages):
                request = json.loads(messages[-1].content)
                if request.get('stage') or 'targets' in request:
                    response = generic.invoke(messages)
                else:
                    response = self.model.invoke(messages)
                    if self.role == 'supervisor':
                        value = json.loads(response.content)
                        ticket = request.get('input', {}).get('ticket')
                        if value.get('action') == 'dispatch' and ticket:
                            for task in value.get('tasks', []):
                                task['checks'] = ([{'tool': name, 'args': args}
                                                   for name, args in (control_queries(ticket)[0], control_queries(ticket)[-1])]
                                                  if task['role'] == 'investigation' else [
                                                      {'tool': 'search_runbooks', 'args': {'query': 'timeout'}}])
                            response = response.model_copy(update={'content': json.dumps(value)})
                return response.model_copy(update={'usage_metadata': {
                    'input_tokens': 17, 'output_tokens': 3, 'total_tokens': 20}})

        return {role: BudgetedModel(Bridge(role), budget) for role in
                ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')}
    return factory


class RolloutValidationTests(unittest.TestCase):
    def config(self):
        return IncidentConfig(api_key='secret-key-never-print', model='free-control',
            base_url='https://private-endpoint.invalid', extra_body={'测试配置': 'private-extra'})

    def test_plan_is_default_free_and_never_prints_private_settings(self):
        with patch('scripts.validate_autonomous_rollout.IncidentConfig.from_file', return_value=self.config()), \
             patch('agents.models.build_model', side_effect=AssertionError('model built')):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([]), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['paid_requests_sent'], 0)
        self.assertEqual(plan['gate_fixture'], GATE_KIND)
        self.assertFalse(plan['production_rollout'])
        self.assertEqual(plan['human_evaluation'], 'pending')
        for secret in ('secret-key-never-print', 'private-endpoint.invalid', 'private-extra'):
            self.assertNotIn(secret, output.getvalue())

    def test_live_requires_explicit_ledger_and_global_call_cap(self):
        for args in (['--live'], ['--live', '--max-model-calls', '20'],
                     ['--live', '--budget-ledger', 'anything.json'],
                     ['--live', '--budget-ledger', 'anything.json', '--max-model-calls', '181']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)

    def run_free(self, max_calls):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / 'ledger.json'
        make_ledger(path)
        _, args = parse_args(['--live', '--budget-ledger', str(path), '--max-model-calls', str(max_calls)])
        config = self.config()
        budget = GlobalPaidBudget(path, max_calls)
        plan = plan_for(args, config)
        previous = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            with patch.dict(os.environ, LOCAL_ENV), \
                 patch('scripts.validate_autonomous_rollout.model_factory', side_effect=free_model_factory):
                return asyncio.run(run_validation(config, budget, plan))
        finally:
            logging.disable(previous)

    def test_actual_asgi_admission_sse_rollback_and_shared_budget_with_free_doubles(self):
        report = self.run_free(20)
        self.assertEqual(report['validation_status'], 'passed', report['checks'])
        self.assertTrue(all(report['checks'].values()))
        self.assertEqual(report['runs']['first']['engine_id'], 'autonomous_v2')
        self.assertEqual(report['runs']['second']['engine_id'], 'legacy')
        self.assertEqual(report['runs']['first']['execution_mode'], 'live')
        self.assertFalse(report['runs']['first']['repair_eligible'])
        self.assertLessEqual(report['paid_budget']['session_requests'], 20)
        self.assertFalse(report['production_rollout'])
        self.assertEqual(report['deployment_acceptance'], 'hold')
        raw = json.dumps(report)
        for secret in ('secret-key-never-print', 'private-endpoint.invalid', 'private-extra'):
            self.assertNotIn(secret, raw)

    def test_exhausted_shared_cap_saves_both_results_without_claiming_pass(self):
        report = self.run_free(1)
        self.assertEqual(report['validation_status'], 'not_passed')
        self.assertEqual(report['paid_budget']['session_requests'], 1)
        self.assertEqual(set(report['runs']), {'first', 'second'})
        self.assertFalse(all(report['checks'].values()))
        self.assertEqual(report['human_evaluation'], 'pending')


if __name__ == '__main__':
    unittest.main()
