"""Public task registration must keep pairs comparable and prose current."""
from copy import deepcopy
from pathlib import Path
import unittest

from evals.rca_contracts import load_dataset
from scripts.evaluate_rca_integration import load_tasks, task_spec
from scripts.prepare_rca_integration_tasks import check_registered_measurements

ROOT = Path(__file__).resolve().parents[2]


class IntegrationTaskTests(unittest.TestCase):
    def test_all_registered_public_groups_validate_without_private_answers(self):
        tasks = load_tasks(ROOT / 'evals/datasets/rca_integration_v1/tasks.json')['tasks']
        self.assertEqual(len(tasks), 15)
        self.assertEqual(len({r['group_id'] for r in tasks}), 6)
        for task in tasks:
            spec = task_spec(task)
            self.assertEqual(set(spec), {'world', 'capability_metadata', 'initial_checks', 'review_controls'})
            self.assertNotIn('expected_relation', spec['review_controls'][0])
            self.assertNotIn('machine_evidence', spec)

    def test_changed_experiment_cannot_silently_keep_old_registered_sentence(self):
        case = deepcopy(load_dataset(ROOT / 'evals/datasets/rca_reproduced_v1/manifest.json')['cases'][1])
        check_registered_measurements(case)
        import json
        row = case['public']['world']['logs'][0]
        body = json.loads(row['message'])
        body['configured_deadline_ms'] = 800
        row['message'] = json.dumps(body)
        with self.assertRaisesRegex(ValueError, 'register a new task wording'):
            check_registered_measurements(case)

    def test_changed_measured_outcome_cannot_keep_same_parameter_based_sentence(self):
        import json
        case = deepcopy(load_dataset(ROOT / 'evals/datasets/rca_reproduced_v1/manifest.json')['cases'][0])
        row = next(r for r in case['public']['world']['logs'] if r['id'] == 'row_03')
        body = json.loads(row['message'])
        body['trials'][0]['acquisition_timeouts'] = 0
        row['message'] = json.dumps(body)
        with self.assertRaisesRegex(ValueError, 'register a new task wording'):
            check_registered_measurements(case)


if __name__ == '__main__':
    unittest.main()
