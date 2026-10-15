"""No paid calls: frozen repetitions and observation/criteria separation."""
from collections import Counter
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from api.auth import Principal
from config.incident import IncidentConfig
from scripts.compare_orchestration_live import CloseoutProvider, execution_plan, main


class CloseoutUnseenTests(unittest.TestCase):
    def test_plan_only_has_eighteen_distinct_balanced_runs_without_models(self):
        with patch('scripts.compare_orchestration_live.IncidentConfig.from_file',
                   return_value=IncidentConfig(model='qwen3.7-flash')):
            with patch('scripts.compare_orchestration_live.build_roles', side_effect=AssertionError('Paid call')):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(main(['--suite', 'unseen_closeout']), 0)
        plan = json.loads(out.getvalue())
        jobs = plan['execution_order']
        self.assertEqual(len(jobs), 18)
        self.assertEqual(len({tuple(sorted(j.items())) for j in jobs}), 18)
        self.assertEqual(set(Counter((j['case_id'], j['orchestration']) for j in jobs).values()), {3})
        self.assertEqual(plan['maximum_paid_attempts'], 288)
        self.assertEqual(plan['seeded_turns'] + plan['replayed_turns'], 0)
        self.assertFalse(plan['independent_blind_holdout'])
        self.assertNotEqual(jobs[0]['orchestration'], jobs[6]['orchestration'])

    def test_application_and_model_drift_rejected(self):
        with patch('scripts.compare_orchestration_live.implementation_version', return_value='changed'):
            with self.assertRaisesRegex(ValueError, 'Application changed'):
                execution_plan(IncidentConfig(model='qwen3.7-flash'), 'unseen_closeout')
        with self.assertRaisesRegex(ValueError, 'Model changed'):
            execution_plan(IncidentConfig(model='other'), 'unseen_closeout')

    def test_runtime_receives_only_ticket_and_observations_not_criteria(self):
        principal = Principal('synthetic_demo', 'cli_reader')
        provider = CloseoutProvider('closeout_003', principal)
        provider.authorize(principal, provider.ticket().scope)
        self.assertEqual(provider._data['source_errors'], {'get_service_logs': 'SOURCE_UNAVAILABLE'})
        self.assertEqual(provider._data['changes'], [])
        self.assertFalse({'criteria', 'expected', 'answer', 'draft', 'review'} & provider._data.keys())
        self.assertEqual(set(provider._data), {'synthetic', 'ticket', 'metrics', 'logs', 'changes',
                                             'owners', 'runbooks', 'incidents', 'source_errors'})


if __name__ == '__main__':
    unittest.main()
