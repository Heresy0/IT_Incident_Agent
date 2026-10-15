"""Free checks for autonomous live evaluation accounting and fair execution."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from config.incident import IncidentConfig
from runtime.context import ExecutionError
from scripts.compare_orchestration_live import (ROOT, LiveRecorder, RequestLedger, execution_plan,
                                               main, reported_usage, summarize)


class AutonomousComparisonTests(unittest.TestCase):
    def test_recovery_suite_freezes_additional_cases_and_has_independent_run_caps(self):
        plan = execution_plan(IncidentConfig(model='fixture'), 'collection_recovery')
        self.assertEqual(plan['maximum_paid_attempts'], 160)
        self.assertEqual(len(plan['execution_order']), 10)
        self.assertEqual({j['case_id'] for j in plan['execution_order']},
                         {'fresh_001', 'fresh_002', 'fresh_003', 'eval_005', 'eval_008'})
        self.assertIn('evals/independent/answers/eval_005.json', plan['fixture_and_criteria_hashes'])
        self.assertEqual(plan['seeded_turns'], 0)
        self.assertEqual(plan['replayed_turns'], 0)

    def test_plan_only_does_not_construct_models_and_alternates_arm_order(self):
        out = io.StringIO()
        with patch('scripts.compare_orchestration_live.IncidentConfig.from_file', return_value=IncidentConfig(model='fixture')):
            with patch('scripts.compare_orchestration_live.build_roles', side_effect=AssertionError('No live models')):
                with redirect_stdout(out):
                    self.assertEqual(main([]), 0)
        plan = json.loads(out.getvalue())
        self.assertTrue(plan['plan_only'])
        self.assertEqual(plan['maximum_paid_attempts'], 96)
        self.assertEqual(plan['seeded_turns'], 0)
        self.assertEqual(plan['replayed_turns'], 0)
        self.assertEqual([j['orchestration'] for j in plan['execution_order']],
                         ['legacy', 'review_driven', 'review_driven', 'legacy', 'legacy', 'review_driven'])
        self.assertEqual(plan['limits']['reserve_model_calls'], 3)
        self.assertFalse(plan['independent_quality_score'])

    def test_frozen_input_drift_is_rejected_before_execution(self):
        with patch('scripts.compare_orchestration_live.digest', return_value='changed'):
            with self.assertRaisesRegex(ValueError, 'Frozen benchmark'):
                execution_plan(IncidentConfig())

    def test_failed_request_is_retained_and_counts_toward_cap_without_starving_next_run(self):
        class Failure:
            def invoke(self, messages):
                raise ExecutionError('NETWORK_ERROR')
        (ROOT / 'output').mkdir(exist_ok=True)
        with TemporaryDirectory(dir=ROOT / 'output') as directory:
            ledger = RequestLedger(maximum=1, path=Path(directory) / 'requests.json')
            model = LiveRecorder(Failure(), ledger, 'reviewer')
            with self.assertRaises(ExecutionError):
                model.invoke([])
            with self.assertRaises(ExecutionError) as denied:
                model.invoke([])
            self.assertEqual(denied.exception.code, 'BUDGET_EXCEEDED')
            saved = json.loads(ledger.path.read_text())
            self.assertEqual(saved['paid_attempts'], 1)
            self.assertEqual(saved['records'][0]['error_code'], 'NETWORK_ERROR')
            self.assertIsNone(saved['records'][0]['token_usage'])
            self.assertEqual(RequestLedger(maximum=1).claim(), 1)

    def test_response_usage_and_object_are_preserved(self):
        response = AIMessage(content='{}', usage_metadata={'input_tokens': 25, 'output_tokens': 8, 'total_tokens': 33})
        class Model:
            def invoke(self, messages):
                return response
        ledger = RequestLedger()
        self.assertIs(LiveRecorder(Model(), ledger, 'diagnosis').invoke([]), response)
        self.assertEqual(ledger.records[0]['token_usage'], {'input_tokens': 25, 'output_tokens': 8})

    def test_absent_usage_is_not_zero_cost_and_provider_prompt_tokens_are_supported(self):
        self.assertIsNone(reported_usage(AIMessage(content='{}')))
        response = AIMessage(content='{}', response_metadata={'token_usage': {'prompt_tokens': 2, 'completion_tokens': 4}})
        self.assertEqual(reported_usage(response), {'input_tokens': 2, 'output_tokens': 4})
        response.response_metadata['token_usage']['prompt_tokens'] = True
        self.assertIsNone(reported_usage(response))

    def test_partial_results_routes_and_contract_failures_remain_visible(self):
        ledger = RequestLedger()
        ledger.claim()
        ledger.append({'role': 'reviewer', 'token_usage': None, 'error_code': 'TIMEOUT'})
        report = {'status': 'partial', 'review_status': 'not_performed', 'run_summary': {},
            'events': [{'type': 'review_routed', 'route': 'handoff'}, {'type': 'validation_failure', 'reason': 'contract'}],
            'handoff': {'reason': 'source unavailable'}}
        row = summarize(report, ledger)
        self.assertEqual(row['status'], 'partial')
        self.assertEqual(row['routes'][0]['route'], 'handoff')
        self.assertEqual(len(row['validation_failures']), 1)
        self.assertIsNone(row['recorded_token_usage']['input_tokens'])
        self.assertEqual(row['recorded_token_usage']['status'], 'partial_or_unknown')
        self.assertEqual(row['handoff'], report['handoff'])


if __name__ == '__main__':
    unittest.main()
