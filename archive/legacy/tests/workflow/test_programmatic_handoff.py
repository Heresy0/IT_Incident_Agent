"""Raw observations remain useful on failure without becoming approved claims."""
import json
import unittest
from unittest.mock import patch

from agents.contracts import SupervisorSelection
from evidence.contracts import InvestigationOutput
from agents.investigation import validate_output
from evidence.render import render
from providers.fixtures import FixtureProvider, ProviderError
from tests.tools.test_incident_tools import P
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from tests.workflow.test_planned_default import planned_models
from workflow.coordinator import Collaboration, collaborate


class ProgrammaticHandoffTests(unittest.TestCase):
    def test_rejected_diagnosis_keeps_all_current_sources_separate_from_reviewed_findings(self):
        models = planned_models()
        def invalid(response, messages, calls):
            data = json.loads(response.content)
            data['findings'][0]['refs'][0]['reference_id'] = 'REF_' + '0' * 24
            return json_response(data)
        models['diagnosis'] = AlterOutput(models['diagnosis'], invalid)
        report = collaborate(models, FixtureProvider('case_001', P), P)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(report['output']['findings'], [])
        self.assertEqual(report['output']['hypotheses'], [])
        self.assertEqual(report['output']['recommended_actions'], [])
        self.assertEqual(report['review_status'], 'not_performed')
        self.assertTrue(all(t['output']['findings'] == [] for t in report['task_results']))
        shown = {ref['evidence_id'] for f in report['unreviewed_observations'] for ref in f['refs']}
        self.assertEqual(shown, {e['evidence_id'] for e in report['evidence'] if e['kind'] == 'observation'})
        self.assertFalse(report['used_evidence_ids'])
        self.assertIn('尚未获独立语义复核', render(report))
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertEqual(report['repair_counts']['diagnosis'], 1)

    def collect(self, c, role, check):
        task = c.supervisor_decision(SupervisorSelection(action='dispatch', reason='Collect actual source',
            tasks=[{'role': role, 'goal': 'Collect actual source', 'checks': [check],
                    'need_ids': ['N3'], 'expected_value': 'Evidence is not proof of a cause'}])).tasks[0]
        objective = c.task_objective(task, False)
        result = c.collect_task(task, objective, c.executors[role], c.context, c.events, 4)
        c.record_task(task, objective, result)

    def test_history_and_empty_queries_are_not_fabricated_as_current_facts(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        self.collect(c, 'knowledge', {'tool': 'search_runbooks', 'args': {'query': 'dependency'}})
        self.assertTrue(c.evidence)
        self.collect(c, 'investigation', {'tool': 'get_service_logs', 'args': {**window, 'category': 'configuration'}})
        c.stop = 'NEEDS_INFORMATION'
        report = c.result()
        self.assertEqual(report['unreviewed_observations'], [])
        self.assertEqual(report['output']['findings'], [])
        self.assertTrue(all(e['kind'] != 'observation' for e in report['evidence']))

    def test_failed_log_query_stays_a_gap_not_a_current_observation(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
        window = c.ticket.scope.model_dump(mode='json', include={'start', 'end'})
        with patch.object(c.provider, 'query', side_effect=ProviderError('SOURCE_UNAVAILABLE')):
            self.collect(c, 'investigation', {'tool': 'get_service_logs', 'args': {**window, 'category': 'dependency'}})
        c.stop = 'NEEDS_INFORMATION'
        report = c.result()
        self.assertEqual(report['unreviewed_observations'], [])
        self.assertFalse(report['evidence'])
        self.assertIn('查询失败', json.dumps(report['output']['missing_information'], ensure_ascii=False))

    def test_projection_validates_values_times_versions_and_deduplicates_approved_fields(self):
        c = Collaboration(planned_models(), FixtureProvider('case_001', P), P)
        c.run()
        c.stop = 'MODEL_OUTPUT_INVALID'
        approved = [c.draft.findings[0].model_dump(mode='json')]
        projected = c.unreviewed_observations(approved)
        approved_keys = {(r['evidence_id'], r['field_path']) for f in approved for r in f['refs']}
        keys = [(r['evidence_id'], r['field_path']) for f in projected for r in f['refs']]
        self.assertFalse(set(keys) & approved_keys)
        self.assertEqual(len(keys), len(set(keys)))
        for finding in projected:
            value = InvestigationOutput.model_validate_json(json.dumps({'findings': [finding]}))
            validate_output(value, c.evidence)

    def test_completed_report_does_not_duplicate_approved_facts(self):
        report = collaborate(planned_models(), FixtureProvider('case_001', P), P)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['unreviewed_observations'], [])
        self.assertTrue(report['output']['findings'])


if __name__ == '__main__':
    unittest.main()
