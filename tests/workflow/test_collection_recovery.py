"""Channel decisions preserve scope, quotas, unknowns and human authority."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.contracts import RevisionReviewDecision
from providers.fixtures import FixtureProvider, ProviderError
from runtime.context import Limits, ExecutionError
from tests.tools.test_incident_tools import P
from tests.workflow.test_planned_default import planned_models
from tests.workflow.test_review_driven import review_models
from tests.workflow.test_incident_collaboration import AlterOutput, json_response
from workflow.coordinator import Collaboration
from workflow.collection_recovery import channel_failure, local_checks, channel_inventory, failed_request_channel
from workflow.handoff import recovery_tasks
import json


def coordinator(*, provider=None, models=None, tools=8, mode='review_driven'):
    return Collaboration(models or planned_models(), provider or FixtureProvider('case_001', P), P,
        orchestration=mode, limits=Limits(model_calls=16, tool_calls=tools, seconds=360))


class CollectionRecoveryTests(unittest.TestCase):
    def test_local_baseline_has_no_model_turn_and_never_promotes_a_question(self):
        c = coordinator()
        c.supervise()
        c.execute_task('investigation')
        self.assertEqual(c.next_node, 'investigation')
        before = c.context.counts['model_calls']
        self.assertEqual(len(c.models['supervisor'].model.histories), 1)
        self.assertTrue(all(n.status == 'pending' for n in c.evidence_needs.values()))
        c.execute_task('investigation')
        self.assertEqual(c.next_node, 'diagnosis')
        self.assertEqual(c.context.counts['tool_calls'], 4)
        self.assertEqual(c.context.counts['model_calls'], before)
        self.assertFalse(local_checks(c.executors['investigation'], c.ticket.scope))
        local_reads = [e for e in c.events if e['type'] == 'tool_result' and e['query_profile'].get('category') in {'resource', 'configuration'}]
        self.assertTrue(all(e['execution_source'] == 'program_registered_boundaries' for e in local_reads))
        c.route_collection()
        self.assertEqual(c.context.counts['tool_calls'], 4)
        self.assertEqual(sum(e['type'] == 'local_coverage_selected' for e in c.events), 1)

    def test_standard_default_does_not_add_local_batch(self):
        report = coordinator(mode='legacy').run()
        self.assertEqual(report['run_summary']['tool_calls'], 2)
        self.assertFalse(any(e['type'] == 'local_coverage_selected' for e in report['events']))
        self.assertNotIn('handoff', report)

    def test_tiny_tool_allowance_keeps_slot_and_never_exceeds_budget(self):
        c = coordinator(tools=3)
        c.supervise()
        c.execute_task('investigation')
        self.assertEqual(c.next_node, 'diagnosis')
        self.assertEqual(c.context.counts['tool_calls'], 2)
        self.assertTrue(c.local_coverage_attempted)
        self.assertFalse(any(e['type'] == 'local_coverage_selected' for e in c.events))

    def test_unregistered_local_categories_are_not_proposed(self):
        c = coordinator()
        executor = c.executors['investigation']
        with patch.object(executor, 'validate_args', side_effect=ExecutionError('LOG_CATEGORY_NOT_REGISTERED')):
            self.assertEqual(local_checks(executor, c.ticket.scope), [])
        self.assertEqual(c.context.counts['tool_calls'], 0)

    def test_global_channel_failure_blocks_local_probes_but_empty_and_category_failure_do_not(self):
        c = coordinator()
        executor = c.executors['investigation']
        args = {'start': c.ticket.scope.start.isoformat(), 'end': c.ticket.scope.end.isoformat(), 'category': 'dependency'}
        row = {'tool': 'get_service_logs', 'status': 'error', 'error': 'SOURCE_UNAVAILABLE',
               'args': args, 'truncated': False, 'evidence_ids': []}
        executor.check_results['failure'] = row
        self.assertEqual(local_checks(executor, c.ticket.scope), [])
        for status, error in [('empty', None), ('error', 'LOG_CATEGORY_NOT_REGISTERED'), ('error', 'TIMEOUT')]:
            row.update(status=status, error=error)
            self.assertEqual(len(local_checks(executor, c.ticket.scope)), 2)
        row.update(status='error', error='SOURCE_UNAVAILABLE')
        executor.check_results['later_success'] = {**row, 'status': 'ok', 'error': None}
        self.assertIsNone(channel_failure(executor, 'get_service_logs'))

    def test_inventory_distinguishes_unqueried_empty_and_failed(self):
        c = coordinator()
        initial = channel_inventory(c.executors, c.ticket.scope)
        self.assertEqual(next(r for r in initial['channels'] if r['tool'] == 'get_recent_changes')['availability'], 'not_queried')
        args = {'start': c.ticket.scope.start.isoformat(), 'end': c.ticket.scope.end.isoformat(), 'category': 'configuration'}
        c.executors['investigation'].execute('get_service_logs', args)
        inventory = channel_inventory(c.executors, c.ticket.scope)
        self.assertNotEqual(next(r for r in inventory['channels'] if r['tool'] == 'get_service_logs')['availability'], 'failed_channel')
        self.assertEqual(next(r for r in inventory['local_boundaries'] if r['category'] == 'configuration')['status'], 'empty')

    def test_failed_channel_goes_to_handoff_without_a_second_supervisor_call(self):
        class Outage(FixtureProvider):
            def query(self, name, args, scope):
                if name == 'get_service_logs' and args.category != 'dependency':
                    raise ProviderError('SOURCE_UNAVAILABLE')
                return super().query(name, args, scope)
        models = review_models(gap='not_planned')
        def source(response, messages, calls):
            data = json.loads(response.content)
            if data.get('request_evidence'):
                data['request_source'] = {'tool': 'get_service_logs', 'category': 'dependency'}
            return json_response(data)
        models['reviewer'] = AlterOutput(models['reviewer'], source)
        c = coordinator(models=models, provider=Outage('case_001', P))
        report = c.run()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['handoff']['reason'], 'source_recovery_required')
        self.assertEqual(len(c.models['supervisor'].model.histories), 1)
        self.assertEqual(report['rework_rounds'], 0)
        self.assertTrue(any(e['type'] == 'failed_channel_handoff' for e in report['events']))
        task = report['handoff']['recovery_tasks'][0]
        self.assertFalse(task['model_reviewed'])
        self.assertIsNone(task['owner'])
        self.assertTrue(task['completion_criteria'])
        self.assertTrue(task['changes_require_separate_approval'])
        self.assertTrue(any(e['type'] == 'planned_check_channel_blocked' for e in report['events']))
        self.assertEqual(sum(e['type'] == 'tool_result' and e['name'] == 'get_service_logs' for e in report['events']), 2)

    def test_source_hint_without_a_request_cannot_authorize_or_route_a_read(self):
        from evidence.diagnosis import ProtocolError
        c = coordinator()
        c.run()
        value = c.review.model_dump(mode='json')
        value['request_source'] = {'tool': 'get_service_logs', 'category': 'resource'}
        before = dict(c.context.counts)
        with self.assertRaises(ProtocolError) as error:
            c.validate_review(RevisionReviewDecision.model_validate(value))
        self.assertEqual(error.exception.code, 'REVIEW_SOURCE_HINT_INVALID')
        self.assertEqual(c.context.counts, before)

    def test_fresh_alternative_hint_prevents_failed_channel_shortcut(self):
        c = coordinator()
        executor = c.executors['investigation']
        executor.check_results['outage'] = {'tool': 'get_service_logs', 'status': 'error', 'error': 'SOURCE_UNAVAILABLE'}
        review = SimpleNamespace(request_source=SimpleNamespace(tool='get_service_logs'), request_evidence=SimpleNamespace(
            target_role='investigation', checks=[]))
        self.assertIsNotNone(failed_request_channel(review, c.executors))
        from agents.contracts import ReadOnlyCheck
        review.request_evidence.checks = [ReadOnlyCheck.model_validate_json(json.dumps({'tool': 'get_recent_changes',
            'args': {'start': c.ticket.scope.start.isoformat(), 'end': c.ticket.scope.end.isoformat()}}))]
        self.assertIsNone(failed_request_channel(review, c.executors))

    def test_recovery_tasks_are_scoped_proposals_without_fabricated_owners_or_health(self):
        tasks = recovery_tasks(checks=[{'tool': 'get_service_logs', 'status': 'error',
            'error_code': 'SOURCE_UNAVAILABLE', 'query_profile': {'category': 'dependency'}}],
            unresolved=[], owners=[], service='demo', environment='staging', reviewed_actions=[])
        self.assertEqual(tasks[0]['requested_observation']['query_profile']['category'], 'dependency')
        self.assertFalse(tasks[0]['owner_resolved'])
        self.assertFalse(tasks[0]['model_reviewed'])
        self.assertEqual(recovery_tasks(checks=[{'tool': 'get_service_logs', 'status': 'empty', 'error_code': None}],
            unresolved=[], owners=[], service='demo', environment='staging', reviewed_actions=[]), [])


if __name__ == '__main__':
    unittest.main()
