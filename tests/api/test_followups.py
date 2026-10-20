"""Owner-bound, immutable, budgeted post-report diagnostic rounds. No paid models."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.api import test_incident_api as controls
from evidence.model_view import evidence_view
from incidents.followups import submitted_rows
from workflow.v2 import engine


class FollowupAPITests(unittest.TestCase):
    setUp = controls.IncidentAPITests.setUp
    tearDown = controls.IncidentAPITests.tearDown
    req = controls.IncidentAPITests.req
    create = controls.IncidentAPITests.create
    start = controls.IncidentAPITests.start
    result = controls.IncidentAPITests.result

    def parent(self):
        incident = self.create()
        rid = self.start(incident)
        self.result(rid)
        return self.store.get_incident(incident['id'], self.owner), rid

    def payload(self, incident, rid, key='followup_a', **changes):
        return {'request_key': key, 'execution_mode': 'scripted_control_only',
            'followup': {'parent_run_id': rid, 'revision': incident['revision'],
                'note': '回滚后故障仍存在，请核查发布导致故障的解释。', 'observations': [], **changes}}

    def submit(self, incident, payload, expected=202):
        response = self.req('POST', f'incidents/{incident["id"]}/followups', json=payload)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def test_followup_preserves_parent_and_restarts_all_reviews_on_a_new_run(self):
        incident, parent_id = self.parent()
        before, events = self.store.get(parent_id), self.store.events(parent_id)
        calls = []
        original = engine.call_model
        def observed(*args, **kwargs):
            calls.append(deepcopy(args[4]))
            return original(*args, **kwargs)
        with patch.object(engine, 'call_model', side_effect=observed):
            rid = self.submit(incident, self.payload(incident, parent_id))['run_id']
            result = self.result(rid)
        self.assertNotEqual(rid, parent_id)
        self.assertEqual(self.store.get(parent_id), before)
        self.assertEqual(self.store.events(parent_id), events)
        self.assertEqual(result['incident_snapshot']['revision'], 2)
        self.assertEqual(result['followup']['parent_run_id'], parent_id)
        self.assertIn('用户补充与关联诊断', result['final'])
        self.assertTrue(any(row.get('user_followup', {}).get('submissions') for row in calls))
        self.assertTrue(result['reviews'])
        self.assertLessEqual(result['rework_rounds'], 1)
        self.assertEqual(self.store.get_incident(incident['id'], self.owner)['revision'], 2)

    def test_double_submit_reuses_one_revision_and_exact_frozen_request(self):
        incident, parent_id = self.parent()
        payload = self.payload(incident, parent_id)
        with ThreadPoolExecutor(4) as pool:
            ids = list(pool.map(lambda _: self.service.start_followup(incident['id'], payload, self.owner)[0], range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.result(ids[0])
        self.assertTrue(self.submit(incident, payload)['replayed'])
        self.assertEqual(len(self.store.runs), 2)
        self.assertEqual(self.store.get_incident(incident['id'], self.owner)['revision'], 2)
        payload['followup']['note'] = '不同内容'
        self.submit(incident, payload, 409)

    def test_foreign_owner_cannot_submit_or_read_history(self):
        incident, rid = self.parent()
        for token in ('b', 'c'):
            for method, path in [('POST', 'followups'), ('GET', 'diagnoses')]:
                options = {'json': self.payload(incident, rid)} if method == 'POST' else {}
                self.assertEqual(self.req(method, f'incidents/{incident["id"]}/{path}', token, **options).status_code, 404)
        self.assertEqual(len(self.store.runs), 1)

    def test_stale_base_or_revision_rejected_without_new_run(self):
        incident, rid = self.parent()
        for changes in ({'parent_run_id': 'not-current'}, {'revision': 7}):
            self.submit(incident, self.payload(incident, rid, **changes), 409)
        self.assertEqual(len(self.store.runs), 1)

    def test_changed_ticket_requires_a_fresh_basis_report(self):
        incident, rid = self.parent()
        fields = {key: incident[key] for key in controls.catalog(self.owner)[0]['fields']}
        fields['symptoms'] += ' corrected'
        changed = self.req('PATCH', f'incidents/{incident["id"]}', json={'revision': 1, 'changes': fields}).json()
        self.submit(changed, self.payload(changed, rid), 409)
        self.assertEqual(len(self.store.runs), 1)

    def test_empty_or_malformed_source_and_live_budget_are_rejected(self):
        incident, rid = self.parent()
        for payload in [self.payload(incident, rid, note='  '),
                {**self.payload(incident, rid), 'execution_mode': 'live'},
                {**self.payload(incident, rid), 'execution_mode': 'live', 'model_budget': 17, 'tool_budget': 8},
                self.payload(incident, rid, observations=[{'source_name': 'x', 'service': incident['service'], 'content': 'log', 'observed_at': '2026-09-14T10:00:00'}])]:
            self.submit(incident, payload, 422)
        self.assertEqual(len(self.store.runs), 1)

    def test_manual_record_keeps_provenance_and_enters_fresh_evidence_pack(self):
        incident, rid = self.parent()
        observation = {'source_name': 'operator pasted pod log', 'service': incident['service'],
            'observed_at': incident['end'], 'content': 'pod exited with status 137; cause requires verification'}
        new_id = self.submit(incident, self.payload(incident, rid, observations=[observation]))['run_id']
        result = self.result(new_id)
        manual = [row for row in result['evidence'] if row['payload'].get('source_type') == 'user_submission']
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]['payload']['message'], observation['content'])
        self.assertEqual(manual[0]['payload']['verification_status'], 'unverified')
        self.assertIn('用户提供（未经独立核验', result['final'])
        self.assertFalse(result['repair_eligible'])
        self.assertTrue(result['reviews'])

    def test_manual_source_outside_authorized_service_or_window_is_rejected(self):
        incident, rid = self.parent()
        original = {'source_name': 'log', 'service': incident['service'], 'observed_at': incident['end'], 'content': 'text'}
        for changes in ({'service': 'foreign-secret-service'}, {'observed_at': '2020-01-01T00:00:00Z'}):
            self.submit(incident, self.payload(incident, rid, observations=[{**original, **changes}]), 422)
        self.assertEqual(len(self.store.runs), 1)

    def test_inherited_records_are_rechecked_when_dependency_permission_changes(self):
        incident, rid = self.parent()
        observation = {'source_name': 'dependency log', 'service': 'inventory-api',
            'observed_at': incident['end'], 'content': 'unverified operator-provided dependency record'}
        second = self.submit(incident, self.payload(incident, rid, observations=[observation]))['run_id']
        self.result(second)
        current = self.store.get_incident(incident['id'], self.owner)
        with patch.object(self.service._incident, 'scope', return_value=SimpleNamespace(
                service=incident['service'], allowed_dependencies=())):
            self.submit(current, self.payload(current, second, 'permission-changed'), 422)
        self.assertEqual(len(self.store.runs), 2)

    def test_followup_chain_preserves_notes_history_and_measured_totals(self):
        incident, first = self.parent()
        second = self.submit(incident, self.payload(incident, first))['run_id']
        self.result(second)
        current = self.store.get_incident(incident['id'], self.owner)
        third = self.submit(current, self.payload(current, second, 'followup_b', note='新增反证'))['run_id']
        result = self.result(third)
        self.assertEqual(result['followup']['round'], 2)
        self.assertEqual(len(result['followup']['submissions']), 2)
        history = self.req('GET', f'incidents/{incident["id"]}/diagnoses?offset=1&limit=1').json()
        self.assertEqual(history['totals']['runs'], 3)
        self.assertEqual(len(history['items']), 1)
        self.assertEqual(history['totals']['model_calls'], sum(r['result']['run_summary']['model_calls'] for r in self.store.runs.values()))
        self.assertIsNone(history['totals']['estimated_cost'])

    def test_followup_invalidates_old_resolution_but_current_report_can_be_confirmed(self):
        incident, first = self.parent()
        second = self.submit(incident, self.payload(incident, first))['run_id']
        self.result(second)
        payload = {'request_key': 'resolve', 'revision': 1, 'run_id': first, 'confirmed_cause': 'verified', 'resolution': 'checked'}
        self.assertEqual(self.req('POST', f'incidents/{incident["id"]}/resolution', 'op', json=payload).status_code, 409)
        self.assertEqual(self.req('POST', f'incidents/{incident["id"]}/resolution', 'op', json={**payload, 'revision': 2, 'run_id': second}).status_code, 200)

    def test_accumulated_context_limit_rejects_before_spending(self):
        incident, rid = self.parent()
        rows = [{'source_name': str(n), 'service': incident['service'], 'observed_at': incident['end'], 'content': 'x' * 2000} for n in range(3)]
        second = self.submit(incident, self.payload(incident, rid, note='n' * 2000, observations=rows))['run_id']
        self.result(second)
        current = self.store.get_incident(incident['id'], self.owner)
        self.submit(current, self.payload(current, second, 'too-long', note='n' * 2000, observations=rows), 422)
        self.assertEqual(len(self.store.runs), 2)
        self.assertEqual(self.store.get_incident(incident['id'], self.owner)['revision'], 2)
