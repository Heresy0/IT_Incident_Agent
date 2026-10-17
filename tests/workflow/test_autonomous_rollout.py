"""Admission and rollback controls, using free models and existing worker/SSE paths."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.auth import Principal
from providers.bound import catalog
from providers.registry import ServiceBinding, ServiceRegistry
from storage.runs import MemoryRunStore
from tests.support import TestConfig
from workflow.rollout import AcceptanceGate, RolloutPolicy, configuration_fingerprint
from workflow.service import WorkflowService
from workflow.v2.review import REVIEW_POLICY_VERSION


SOURCE = 'a' * 16
EVALUATION = 'b' * 64
CONFIGURATION = 'c' * 64


def accepted(**changes):
    return AcceptanceGate(**{'source_version': SOURCE, 'review_policy_version': REVIEW_POLICY_VERSION,
                              'evaluation_hash': EVALUATION, 'configuration_sha256': CONFIGURATION,
                              'verdict': 'pass', **changes})


class RolloutPolicyTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal('private-tenant', 'reader')
        self.incident = {'id': 'fixed-incident', 'tenant_id': self.principal.tenant_id,
                         'user_id': self.principal.user_id, 'service': 'private-service',
                         'environment': 'staging', 'service_version': 'v1'}
        self.registry = ServiceRegistry([ServiceBinding(self.principal.tenant_id, (self.principal.user_id,),
                                                        'private-service', 'staging', 'v1')])

    def select(self, policy, incident=None, principal=None, **versions):
        return policy.select(incident or self.incident, principal or self.principal, self.registry,
            source_version=versions.get('source_version', SOURCE),
            review_policy_version=versions.get('review_policy_version', REVIEW_POLICY_VERSION),
            configuration_sha256=versions.get('configuration_sha256', CONFIGURATION))

    def test_percent_boundaries_and_request_independent_stable_incident_bucket(self):
        for percent, expected in ((0, 'legacy'), (100, 'autonomous_v2')):
            policy = RolloutPolicy(mode='canary', percent=percent, gate=accepted())
            self.assertEqual(self.select(policy)['engine_id'], expected)
        policy = RolloutPolicy(mode='canary', percent=37, salt='fixed-test-salt', gate=accepted())
        first = self.select(policy)
        for request_key in ('first', 'try-again', 'another-key'):
            decision = self.select(policy, incident={**self.incident, 'request_key': request_key})
            self.assertEqual(decision, first)
        self.assertIs(type(first['rollout']['bucket']), int)
        self.assertLess(first['rollout']['bucket'], 10000)

    def test_acceptance_missing_hold_or_drift_always_keeps_legacy(self):
        for gate in (None, accepted(verdict='hold'), accepted(source_version=SOURCE[:12]),
                     accepted(review_policy_version='obsolete-review'), accepted(configuration_sha256=None),
                     accepted(configuration_sha256='d' * 64)):
            for mode in ('canary', 'full'):
                with self.subTest(mode=mode, gate=gate):
                    decision = self.select(RolloutPolicy(mode=mode, percent=100, gate=gate))
                    self.assertEqual(decision['engine_id'], 'legacy')
                    self.assertEqual(decision['rollout']['acceptance']['verdict'], 'hold')
                    self.assertFalse(decision['enable_rework'])
        self.assertEqual(self.select(RolloutPolicy(mode='full', gate=accepted()))['engine_id'], 'autonomous_v2')

    def test_configuration_fingerprint_binds_model_variant_challenge_and_budget(self):
        limits = {'model_calls': 12, 'tool_calls': 8, 'input_chars': 48000, 'seconds': 180}
        digest = configuration_fingerprint('m' * 64, 'closed_loop', True, limits)
        gate = accepted(configuration_sha256=digest)
        policy = RolloutPolicy(mode='full', variant='closed_loop', gate=gate)
        self.assertEqual(self.select(policy, configuration_sha256=digest)['engine_id'], 'autonomous_v2')
        variants = [configuration_fingerprint('other-model-settings', 'closed_loop', True, limits),
                    configuration_fingerprint('m' * 64, 'single_pass', False, limits),
                    configuration_fingerprint('m' * 64, 'closed_loop', False, limits),
                    configuration_fingerprint('m' * 64, 'closed_loop', True, {**limits, 'model_calls': 6}),
                    configuration_fingerprint('m' * 64, 'closed_loop', True, {**limits, 'tool_calls': 4}),
                    configuration_fingerprint('m' * 64, 'closed_loop', True, limits,
                                              execution_mode='scripted_control_only')]
        for changed in variants:
            decision = self.select(policy, configuration_sha256=changed)
            self.assertEqual(decision['engine_id'], 'legacy')
            self.assertEqual(decision['rollout']['acceptance']['reason'], 'acceptance_configuration_changed')

    def test_allowlists_filter_authorized_audience_and_do_not_grant_access(self):
        policy = RolloutPolicy(mode='full', gate=accepted(), tenants=(self.principal.tenant_id,),
                               services=('private-service',), variant='closed_loop')
        decision = self.select(policy)
        self.assertEqual(decision['engine_id'], 'autonomous_v2')
        self.assertTrue(decision['enable_rework'])
        self.assertTrue(decision['enable_challenge'])
        for principal in (Principal('other-tenant', 'reader'), Principal('private-tenant', 'other-reader')):
            self.assertEqual(self.select(policy, principal=principal)['engine_id'], 'legacy')
        self.assertEqual(self.select(policy, incident={**self.incident, 'service': 'unregistered'})['engine_id'], 'legacy')
        self.assertEqual(self.select(replace(policy, services=('not-selected',)))['engine_id'], 'legacy')
        self.assertEqual(self.select(replace(policy, tenants=('not-selected',)))['engine_id'], 'legacy')

    def test_public_summary_contains_digests_and_counts_without_other_audience_identifiers(self):
        policy = RolloutPolicy(mode='full', salt='private-routing-salt', tenants=('private-tenant',),
                               services=('private-service',), shadow=True, gate=accepted())
        summary = self.select(policy)['rollout']
        text = json.dumps(summary)
        for private in ('private-routing-salt', 'private-tenant', 'private-service'):
            self.assertNotIn(private, text)
        self.assertEqual(summary['tenant_allowlist_count'], 1)
        self.assertEqual(summary['service_allowlist_count'], 1)
        self.assertEqual(summary['shadow']['mode'], 'offline_snapshot_only')
        self.assertFalse(summary['shadow']['production_executed'])
        self.assertFalse(summary['shadow']['production_dual_reads'])
        self.assertFalse(summary['shadow']['can_authorize_repair'])

    def test_environment_configuration_is_parsed_data_and_defaults_to_no_policy(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(RolloutPolicy.from_environment())
        with patch.dict(os.environ, {'INCIDENT_ROLLOUT_MODE': 'canary', 'INCIDENT_ROLLOUT_PERCENT': '25',
                'INCIDENT_ROLLOUT_VARIANT': 'closed_loop', 'INCIDENT_ROLLOUT_TENANTS': 'one,two',
                'INCIDENT_ROLLOUT_SHADOW': 'true', 'INCIDENT_ROLLOUT_GATE': json.dumps(accepted().__dict__)}, clear=True):
            policy = RolloutPolicy.from_environment()
        self.assertEqual(policy.percent, 25)
        self.assertEqual(policy.tenants, ('one', 'two'))
        self.assertTrue(policy.shadow)
        for value in ({'mode': 'unknown'}, {'percent': 101}, {'percent': True}, {'variant': 'unknown'},
                      {'shadow': 'yes'}, {'gate': {'source_version': SOURCE, 'review_policy_version': 'r',
                                                  'evaluation_hash': 'not-a-hash', 'verdict': 'pass'}}):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                RolloutPolicy.parse(value)


class RolloutLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('runtime.version.implementation_version', return_value=SOURCE))
        self.owner = Principal('rollout-test', 'alice')
        self.store = MemoryRunStore()
        self.service = WorkflowService('unused', config=TestConfig(), store=self.store, max_concurrency=1,
            incident_rollout=RolloutPolicy(mode='off'))
        self.accept_policy(variant='closed_loop', shadow=True)
        self.addCleanup(self.service.close)

    def accept_policy(self, **options):
        provisional = RolloutPolicy(mode='full', **options)
        self.service.configure_incident_rollout(provisional)
        metadata = self.service._incident.metadata(self.execution('preview'))
        self.service.configure_incident_rollout(replace(provisional, gate=accepted(
            configuration_sha256=metadata['run_spec']['configuration_sha256'])))

    @staticmethod
    def execution(key):
        return {'request_key': key, 'execution_mode': 'scripted_control_only',
                'model_budget': None, 'tool_budget': None}

    def create(self):
        fields = next(row['fields'] for row in catalog(self.owner) if row['case_id'] == 'case_001')
        return self.service.create_incident(fields, self.owner)

    def start(self, incident, key):
        return self.service.start_diagnosis(incident['id'], self.execution(key), self.owner)

    def result(self, run_id):
        return asyncio.run(self.service.wait_result(run_id, self.owner))

    @staticmethod
    def fake_v2(models, provider, principal, *, context, event_log, enable_rework, enable_challenge):
        context.emit({'type': 'test_execution', 'engine_id': 'autonomous_v2',
                      'enable_rework': enable_rework, 'enable_challenge': enable_challenge})
        return {'run_id': context.run_id, 'status': 'partial', 'review_status': 'not_performed',
                'business_result': 'needs_information', 'repair_eligible': False,
                'run_summary': context.summary(), 'final': 'free rollout wiring test',
                'evidence': [], 'sources': [], 'events': list(event_log)}

    def test_rollback_affects_new_admissions_only_and_replay_sse_keep_frozen_spec(self):
        incident = self.create()
        submitted = []
        with patch.object(self.service._executor, 'submit', side_effect=lambda fn, *args: submitted.append((fn, args))):
            run_id, replayed = self.start(incident, 'stable-key')
        self.assertFalse(replayed)
        fn, args = submitted[0]
        request = args[1]
        frozen = deepcopy(request['run_spec'])
        self.assertEqual(frozen['engine_id'], 'autonomous_v2')
        self.assertEqual(frozen['source_version_full'], SOURCE)
        self.assertEqual(frozen['execution_variant'], 'closed_loop')
        self.assertTrue(frozen['enable_rework'])
        self.assertTrue(frozen['enable_challenge'])
        self.assertEqual(frozen['challenge_policy_version'], 'blind_scope_challenge_v1')
        self.service.configure_incident_rollout({'mode': 'off'})
        with patch.dict(os.environ, {'INCIDENT_MAX_MODEL_CALLS': '6', 'INCIDENT_MAX_SECONDS': '20'}), \
             patch('workflow.v2.engine.investigate', side_effect=self.fake_v2) as execute:
            fn(*args)
        first = self.result(run_id)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(first['run_spec'], frozen)
        self.assertEqual(first['run_summary']['limits'], frozen['limits'])
        self.assertFalse(first['repair_eligible'])
        self.assertEqual(self.start(incident, 'stable-key'), (run_id, True))
        subsequent = self.create()
        next_run, _ = self.start(subsequent, 'after-rollback')
        second = self.result(next_run)
        self.assertEqual(second['engine_id'], 'legacy')
        self.assertFalse(second['run_spec']['enable_rework'])
        self.assertEqual(second['run_spec']['rollout']['mode'], 'off')

        async def replay(principal):
            return [event async for event in self.service.events(run_id, 0, principal)]

        events = asyncio.run(replay(self.owner))
        self.assertEqual(sum(event['type'] == 'final' for event in events), 1)
        self.assertEqual([event['seq'] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(next(event for event in events if event['type'] == 'final')['run_spec'], frozen)
        self.assertEqual(asyncio.run(replay(Principal('another-tenant', 'alice'))), [])

    def test_default_hold_prevents_full_rollout_even_with_explicit_development_engine(self):
        self.service.incident_engine = 'autonomous_v2'
        self.service.configure_incident_rollout({'mode': 'full', 'variant': 'closed_loop'})
        run_id, _ = self.start(self.create(), 'hold')
        result = self.result(run_id)
        self.assertEqual(result['engine_id'], 'legacy')
        self.assertEqual(result['run_spec']['rollout']['acceptance']['verdict'], 'hold')

    def test_shadow_is_only_an_offline_snapshot_request_and_never_runs_a_second_engine(self):
        self.accept_policy(shadow=True)
        with patch('workflow.v2.engine.investigate', side_effect=self.fake_v2) as primary, \
             patch('workflow.coordinator.collaborate', side_effect=AssertionError('production shadow read')):
            run_id, _ = self.start(self.create(), 'shadow-intent')
            result = self.result(run_id)
        self.assertEqual(primary.call_count, 1)
        self.assertFalse(primary.call_args.kwargs['enable_rework'])
        self.assertFalse(primary.call_args.kwargs['enable_challenge'])
        self.assertEqual(result['run_spec']['rollout']['shadow'], {
            'requested': True, 'mode': 'offline_snapshot_only', 'production_executed': False,
            'production_dual_reads': False, 'can_authorize_repair': False})
        self.assertFalse(result['repair_eligible'])

    def test_old_specs_keep_single_pass_even_when_current_policy_requests_closed_loop(self):
        incident = self.create()
        submitted = []
        with patch.object(self.service._executor, 'submit', side_effect=lambda fn, *args: submitted.append((fn, args))):
            run_id, _ = self.start(incident, 'old-spec')
        fn, args = submitted[0]
        args[1]['run_spec'].pop('enable_rework')
        args[1]['run_spec'].pop('enable_challenge')
        with patch('workflow.v2.engine.investigate', side_effect=self.fake_v2) as execute:
            fn(*args)
        self.assertFalse(execute.call_args.kwargs['enable_rework'])
        self.assertFalse(execute.call_args.kwargs['enable_challenge'])
        self.assertEqual(self.result(run_id)['engine_id'], 'autonomous_v2')

    def test_source_drift_before_worker_start_fails_before_provider_or_model(self):
        incident = self.create()
        submitted = []
        with patch.object(self.service._executor, 'submit', side_effect=lambda fn, *args: submitted.append((fn, args))):
            run_id, _ = self.start(incident, 'source-drift')
        fn, args = submitted[0]
        frozen = deepcopy(args[1]['run_spec'])
        with patch('runtime.version.implementation_version', return_value='d' * 16), \
             patch('workflow.execution.BoundIncidentProvider', side_effect=AssertionError('provider constructed')) as provider, \
             patch('workflow.v2.engine.investigate', side_effect=AssertionError('model invoked')) as execute:
            fn(*args)
        result = self.result(run_id)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['run_summary']['termination_reason'], 'ENGINE_SOURCE_CHANGED')
        self.assertEqual(result['run_spec'], frozen)
        self.assertEqual(result['run_summary']['model_calls'], 0)
        self.assertEqual(result['run_summary']['tool_calls'], 0)
        provider.assert_not_called()
        execute.assert_not_called()

    def test_actual_admission_configuration_changes_invalidate_previous_pass(self):
        incident = self.create()
        execution = self.execution('configuration-check')
        metadata = self.service._incident.metadata(execution, incident=incident, principal=self.owner)
        self.assertEqual(metadata['run_spec']['engine_id'], 'autonomous_v2')
        for changed_execution, changed_config in (
                ({**execution, 'model_budget': 6}, None),
                ({**execution, 'tool_budget': 3}, None),
                (execution, SimpleNamespace(model='different-model', base_url='', extra_body={})),
                (execution, SimpleNamespace(model='test-model', base_url='', extra_body={'setting': 'changed'}))):
            changed = self.service._incident.metadata(changed_execution, incident=incident,
                principal=self.owner, config=changed_config)
            self.assertEqual(changed['run_spec']['engine_id'], 'legacy')
            self.assertEqual(changed['run_spec']['rollout']['acceptance']['reason'],
                             'acceptance_configuration_changed')
        original = self.service.incident_rollout
        for policy in (replace(original, variant='single_pass'), replace(original, enable_challenge=False)):
            self.service.configure_incident_rollout(policy)
            changed = self.service._incident.metadata(execution, incident=incident, principal=self.owner)
            self.assertEqual(changed['run_spec']['engine_id'], 'legacy')


if __name__ == '__main__':
    unittest.main()
