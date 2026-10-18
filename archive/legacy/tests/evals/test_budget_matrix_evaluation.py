"""Free tests of paired admission, experimental isolation and native wiring."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from scripts.evaluate_budget_matrix import (ADAPTER_POLICY, ARMS, ARM_CONFIGS, ROOT,
    MatrixBroker, MemoryPaidBudget, application_freeze, execute_worker,
    install_budget_adapter, normalize_controls, parse_args, schedule, worker_case)


def ledger(path, requests=100):
    value = {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'model_seconds': 0,
        'unknown_usage_calls': 0, 'max_requests': requests, 'max_input_tokens': 1000000,
        'max_output_tokens': 1000000, 'max_model_seconds': 10000}
    path.write_text(json.dumps(value), encoding='utf-8')
    return value


class FreeChat:
    json_output = True
    def invoke(self, messages):
        return AIMessage(content='{"kind":"finish","question":"free"}',
            usage_metadata={'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10},
            response_metadata={'model_name': 'free-model'})
    def bind_tools(self, tools):
        return self


def request(identifier=1, phase='collect'):
    messages = [HumanMessage(content=json.dumps({'stage': 'investigate', 'input': {
        'review_feedback': {} if phase == 'review_rework' else None, 'feedback': None}}))]
    return {'request_id': identifier, 'role': 'investigation', 'tools': [],
        'messages': [{'type': message.type, 'data': message.model_dump(mode='json')} for message in messages]}


class BudgetMatrixTests(unittest.TestCase):
    def test_full_rotated_blocks_have_each_arm_once_without_missing_pairs(self):
        rows = list(schedule(['a', 'b', 'c'], 3))
        self.assertEqual(len(rows), 36)
        for offset in range(0, len(rows), 4):
            block = rows[offset:offset + 4]
            self.assertEqual({row['arm'] for row in block}, set(ARMS))
            self.assertEqual(len({(row['case_id'], row['repeat']) for row in block}), 1)
        self.assertEqual(len({tuple(row.values()) for row in rows}), 36)
        self.assertNotEqual([row['arm'] for row in rows[:4]], [row['arm'] for row in rows[4:8]])

    def test_only_two_declared_budget_values_differ(self):
        baseline = deepcopy(ARM_CONFIGS['m12_r2'])
        for arm, config in ARM_CONFIGS.items():
            copy = deepcopy(config)
            self.assertIn(copy['limits'].pop('model_calls'), (12, 16))
            self.assertIn(copy.pop('max_new_reads'), (2, 4))
            expected = deepcopy(baseline)
            expected['limits'].pop('model_calls')
            expected.pop('max_new_reads')
            self.assertEqual(copy, expected)

    def test_adapter_preserves_initial_collection_and_all_other_arguments(self):
        calls, events = [], []
        original = lambda *args, **kwargs: calls.append((args, kwargs)) or ('FINISHED', 0, 0)
        engine = SimpleNamespace(collect=original)
        audit = install_budget_adapter(engine, 'm12_r4', events)
        sentinel = object()
        engine.collect(sentinel, max_new_reads=2, reserve_models=3)
        engine.collect(sentinel, phase='review_rework', max_new_reads=2, reserve_models=3)
        self.assertEqual(calls[0][1]['max_new_reads'], 2)
        self.assertEqual(calls[1][1], {'phase': 'review_rework', 'max_new_reads': 4, 'reserve_models': 3})
        self.assertIs(calls[1][0][0], sentinel)
        self.assertEqual(audit['calls'], [{'phase': 'review_rework', 'native_max_new_reads': 2, 'effective_max_new_reads': 4}])
        self.assertEqual(audit['policy'], ADAPTER_POLICY)

    def test_parallel_admission_never_exceeds_cap(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            ledger(path, requests=3)
            owner = MemoryPaidBudget(path, 3)
            def attempt(index):
                try:
                    reservation = owner.reserve(100, 50, 30)
                    owner.settle(reservation, {'input_tokens': 7, 'output_tokens': 3}, .1)
                    return True
                except Exception:
                    return False
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(attempt, range(12)))
            self.assertEqual(sum(results), 3)
            summary = owner.summary()
            self.assertEqual(summary['session_requests'], 3)
            self.assertEqual(summary['charged_delta']['input_tokens'], 21)
            self.assertEqual(summary['charged_delta']['output_tokens'], 9)
            self.assertEqual(summary['pending_reservation_ids'], [])
            self.assertEqual(len(summary['reservation_events']), 6)
            self.assertEqual(json.loads(path.read_text())['requests'], 3)

    def test_unknown_usage_stays_reserved_and_explicit(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            ledger(path)
            owner = MemoryPaidBudget(path, 4)
            reservation = owner.reserve(100, 50, 30)
            self.assertEqual(reservation['model_seconds'], 35)
            owner.settle(reservation, None, .2)
            self.assertEqual(owner.summary()['unknown_usage_calls_delta'], 1)
            self.assertEqual(owner.summary()['charged_delta']['input_tokens'], 100)
            self.assertEqual(owner.summary()['charged_delta']['output_tokens'], 50)
            self.assertAlmostEqual(owner.summary()['charged_delta']['model_seconds'], .2)
            with self.assertRaises(ValueError):
                owner.settle(reservation, None, .2)

    def test_settlement_persistence_failure_preserves_usage_and_closes_admission(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            ledger(path)
            owner = MemoryPaidBudget(path, 4)
            reservation = owner.reserve(100, 50, 30)
            with patch.object(owner, '_persist', side_effect=PermissionError):
                owner.settle(reservation, {'input_tokens': 7, 'output_tokens': 3}, .1)
            summary = owner.summary()
            self.assertEqual(summary['charged_delta']['input_tokens'], 7)
            self.assertEqual(summary['unknown_usage_calls_delta'], 0)
            self.assertTrue(summary['admission_closed'])
            self.assertEqual(summary['persistence_errors'][0]['phase'], 'settle')
            self.assertEqual(summary['pending_reservation_ids'], [])

    def test_failed_reservation_is_not_a_paid_request(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            ledger(path)
            owner = MemoryPaidBudget(path, 4)
            with patch.object(owner, '_persist', side_effect=PermissionError), self.assertRaises(Exception):
                owner.reserve(100, 50, 30)
            self.assertEqual(owner.summary()['session_requests'], 0)
            self.assertEqual(owner.summary()['charged_delta']['requests'], 0)
            self.assertTrue(owner.summary()['admission_closed'])

    def test_broker_records_public_decisions_and_phase(self):
        broker = MatrixBroker(FreeChat(), None, 'free-model')
        broker.handle(request(1))
        broker.handle(request(2, 'review_rework'))
        self.assertEqual([row['phase'] for row in broker.decision_trace], ['collect', 'review_rework'])
        self.assertEqual(broker.decision_trace[0]['step_json']['kind'], 'finish')
        self.assertEqual(len(broker.calls), 2)
        self.assertEqual(broker.calls[0]['usage']['input_tokens'], 7)

    def test_broker_paid_log_binds_exact_reservation_to_case_and_request(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as directory:
            path = Path(directory) / 'ledger.json'
            ledger(path)
            owner = MemoryPaidBudget(path, 4)
            identity = {'case_id': 'case', 'arm': 'm12_r2', 'repeat': 1, 'stage': 'e2e'}
            broker = MatrixBroker(FreeChat(), owner, 'free-model', run_identity=identity)
            broker.handle(request(42))
            row = broker.calls[0]
            self.assertTrue(row['charged_request'])
            event = owner.summary()['reservation_events'][0]
            self.assertEqual(row['reservation_id'], event['reservation_id'])
            self.assertEqual(event['request_context'], {**identity, 'request_id': 42, 'role': 'investigation'})
            self.assertEqual(owner.summary()['charged_delta']['input_tokens'], 7)

    def test_gold_labels_do_not_cross_worker_pipe(self):
        from evals.budget_matrix_cases import build_case_specs
        spec = build_case_specs(ROOT)['world_followup_available']
        public = worker_case(spec)
        serialized = json.dumps(public)
        for forbidden in ('expected_accept', 'reference_relation', 'key_observation_ids', 'stage_acquisition'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(public['fixed_draft'], spec['fixed_draft'])

    def test_unassessed_controls_are_not_accepted_or_semantically_correct(self):
        rows = normalize_controls({}, [{'target_id': 'C1', 'kind': 'factual_relation', 'statement': 'x'}])
        self.assertIsNone(rows[0]['accept'])
        self.assertEqual(rows[0]['protocol_status'], 'not_assessed')

    def test_live_requires_prespecification_and_fixed_four_worker_policy(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(['--application-root', '.', '--live'])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(['--application-root', '.', '--jobs', '1'])
        args = parse_args(['--application-root', '.', '--plan'])[1]
        self.assertEqual(args.jobs, 4)


@unittest.skipUnless((ROOT / 'output/frozen-version-study/snapshots/current/app').is_dir(),
                     'Local frozen current application is required')
class NativeWorkerSmokeTests(unittest.TestCase):
    def smoke(self, arm, stage):
        from evals.budget_matrix_cases import build_case_specs, freeze_case_spec
        from tests.evals.test_frozen_version_evaluation import FreeNativeBroker
        spec = build_case_specs(ROOT)['world_followup_available']
        application = application_freeze(ROOT / 'output/frozen-version-study/snapshots/current')
        broker = FreeNativeBroker('current', spec)
        if stage == 'fixed_rework':
            class FreeObjectionModel:
                def invoke(self, messages):
                    request_body = json.loads(messages[-1].content)
                    payload = request_body.get('input', request_body)
                    return AIMessage(content=json.dumps({'items': [
                        {'target_id': target['id'],
                         **({'relation': 'insufficient'} if target['id'].startswith('C')
                            else {'decision': 'appropriate'}),
                         'refs': [], 'issue': 'Free wiring objection; not a semantic judgment.'}
                        for target in payload['targets']]}), response_metadata={'finish_reason': 'stop'})
            broker.models['reviewer'] = FreeObjectionModel()
        report = execute_worker(spec, application, arm, stage, broker)
        self.assertNotIn('execution_error', report['native_report'], report['native_report'].get('execution_error'))
        self.assertEqual(report['run_summary']['model_calls'], len(broker.calls))
        self.assertEqual(report['run_summary']['limits'], ARM_CONFIGS[arm]['limits'])
        self.assertEqual(report['budget_adapter_audit']['max_new_reads'], ARM_CONFIGS[arm]['max_new_reads'])
        self.assertEqual(report['observed_world_sha256'], freeze_case_spec(spec)['world_sha256'])
        self.assertTrue(all(Path(value).is_relative_to(Path(application['app_root']))
                            for value in report['loaded_application_modules'].values()))
        return report

    def test_e2e_worker_uses_frozen_application(self):
        self.smoke('m16_r4', 'e2e')

    def test_four_concurrent_fixed_workers_each_keep_declared_budget(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            reports = list(pool.map(lambda arm: self.smoke(arm, 'fixed_rework'), ARMS))
        for report in reports:
            self.assertIn('fixed_prefix', report['native_report'])
            self.assertIn('first_pass', report['native_report'])
            self.assertTrue(report['normalized']['review_controls_primary'])
            self.assertTrue(report['budget_adapter_audit']['calls'])


if __name__ == '__main__':
    unittest.main()
