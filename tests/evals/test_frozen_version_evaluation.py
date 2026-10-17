"""No-network checks for cross-process application isolation and fair inputs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from threading import Lock
import unittest

from langchain_core.messages import AIMessage, HumanMessage

from evals.frozen_version_cases import build_case_specs, freeze_case_spec
from scripts.evaluate_frozen_versions import (ROOT, ARMS, LIMITS, ModelBroker, RequestBudget,
    application_freeze, execute_worker, fingerprint, normalize_report, parse_args, schedule, worker_case)


class FakeOwner:
    def __init__(self):
        self.count, self.settled, self.lock = 0, [], Lock()

    def reserve(self, *args):
        with self.lock:
            self.count += 1
        return {'requests': 1, 'input_tokens': args[0], 'output_tokens': args[1], 'model_seconds': args[2]}

    def settle(self, *args):
        with self.lock:
            self.settled.append(args)


class FreeChat:
    json_output = True

    def invoke(self, messages):
        return AIMessage(content='{}', usage_metadata={'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10},
                         response_metadata={'model_name': 'free-model'})

    def bind_tools(self, tools):
        return self


class FreeNativeBroker:
    """Native wiring double; all its judgments are excluded from quality results."""
    def __init__(self, arm, spec):
        self.arm, self.calls, self.lock = arm, [], Lock()
        if arm == 'current':
            from scripts.evaluate_frozen_versions import _worker_provider
            from workflow.v2.scripted import scripted_model
            provider, _ = _worker_provider(worker_case(spec))
            model = scripted_model(provider)
            self.models = dict.fromkeys(('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer', 'challenger'), model)
        else:
            from agents.scripted import ScriptedRole
            self.models = {role: ScriptedRole(role) for role in ('supervisor', 'investigation', 'knowledge', 'diagnosis', 'reviewer')}

    def handle(self, request):
        model = self.models[request['role']]
        if self.arm == 'original':
            model = LegacyWiringModel(model, request['role'])
        local = ModelBroker(model, None, 'free-model')
        response = local.handle(request)
        with self.lock:
            self.calls.extend(local.calls)
        return response


class LegacyWiringModel:
    def __init__(self, model, role):
        self.model, self.role = model, role

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        payload = json.loads(messages[-1].content).get('input', {})
        if self.role == 'reviewer' and isinstance(payload.get('targets'), dict):
            evidence = [row['evidence_id'] for row in payload.get('evidence', []) if row['kind'] == 'observation'][:4]
            body = {'assessments': [{'target_id': target, 'verdict': 'supported',
                'reason': 'Free native wiring control; no semantic accuracy claim.', 'evidence_ids': evidence,
                **({'supported_scope': 'full'} if target.startswith('H') else {})}
                for target in payload['targets']],
                'need_assessments': [{'need_id': need['need_id'], 'verdict': 'supported',
                    'evidence_ids': evidence, 'reason': 'Free wiring control.'}
                    for need in payload.get('evidence_needs', [])]}
            return AIMessage(content=json.dumps(body))
        response = self.model.invoke(messages)
        if self.role == 'supervisor':
            data = json.loads(response.content)
            scope = payload['ticket']['scope']
            for task in data.get('tasks', []):
                if task['role'] == 'investigation' and not task.get('checks'):
                    task['checks'] = [{'tool': 'get_service_logs', 'args': {
                        'start': scope['start'], 'end': scope['end'], 'category': 'dependency'}}]
                    task['expected_value'] = 'Free scoped observation wiring.'
            response = AIMessage(content=json.dumps(data))
        return response


class FrozenVersionEvaluationTests(unittest.TestCase):
    def test_application_freeze_reads_all_sources_and_changes_with_one_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app' / 'runtime').mkdir(parents=True)
            target = root / 'app' / 'runtime' / 'context.py'
            target.write_bytes(b'# one\r\n')
            first = application_freeze(root)
            target.write_bytes(b'# one\n')
            second = application_freeze(root)
            self.assertNotEqual(first['source_version'], second['source_version'])
            self.assertEqual(set(first['app_source_sha256']), {'app/runtime/context.py'})

    def test_schedule_pairs_worlds_and_counterbalances_order(self):
        rows = list(schedule(['a', 'b'], 2))
        self.assertEqual(len(rows), 8)
        self.assertEqual([row[1] for row in rows[:2]], list(ARMS))
        self.assertEqual([row[1] for row in rows[2:4]], list(reversed(ARMS)))
        self.assertEqual({tuple(row) for row in rows}, {(c, a, r) for c in ('a', 'b') for a in ARMS for r in (1, 2)})

    def test_worker_receives_no_reference_answer_or_key_record_list(self):
        spec = build_case_specs(ROOT)['world_probe_scope']
        data = worker_case(spec)
        raw = json.dumps(data)
        for key in ('expected_accept', 'reference_relation', 'reference_kind', 'key_observation_ids'):
            self.assertNotIn(key, raw)
        self.assertEqual(data['world'], spec['world'])

    def test_parent_accounts_each_parallel_request_once_and_records_actual_usage(self):
        owner = FakeOwner()
        broker = ModelBroker(FreeChat(), owner, 'free-model')
        message = HumanMessage(content='{}')
        request = {'role': 'reviewer', 'tools': [], 'messages': [{'type': message.type, 'data': message.model_dump(mode='json')}]}
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(broker.handle, [{**request, 'request_id': n} for n in range(6)]))
        self.assertEqual(owner.count, 6)
        self.assertEqual(len(owner.settled), 6)
        self.assertTrue(all(row['charged_request'] for row in broker.calls))
        self.assertEqual(sum(row['usage']['input_tokens'] for row in broker.calls), 42)
        self.assertEqual(len(results), 6)

    def test_rejected_request_is_not_marked_charged_when_other_threads_advance(self):
        class Refuse(FakeOwner):
            def reserve(self, *args):
                raise ValueError('private-endpoint-api-key')
        broker = ModelBroker(FreeChat(), Refuse(), 'free-model')
        message = HumanMessage(content='{}')
        result = broker.handle({'request_id': 1, 'role': 'reviewer', 'messages': [
            {'type': message.type, 'data': message.model_dump(mode='json')}], 'tools': []})
        self.assertFalse(broker.calls[0]['charged_request'])
        self.assertNotIn('private-endpoint', json.dumps(result))

    def test_old_uncertain_is_withheld_and_keeps_native_scope(self):
        native = {'final_review': {'assessments': [{'target_id': 'H1', 'verdict': 'uncertain', 'supported_scope': 'partial'}]},
                  'target_mapping': {'C1': 'H1'}}
        controls = [{'target_id': 'C1', 'kind': 'factual_relation', 'statement': 'A claim'}]
        report = normalize_report(native, arm='original', stage='review', controls=controls)
        item = report['review_controls'][0]
        self.assertIs(item['accept'], False)
        self.assertEqual(item['native_verdict'], 'uncertain')
        self.assertEqual(item['native_fields']['supported_scope'], 'partial')

    def test_protocol_failure_stays_unassessed_instead_of_false_rejection(self):
        report = normalize_report({}, arm='current', stage='review', controls=[
            {'target_id': 'C1', 'kind': 'factual_relation', 'statement': 'A claim'}])
        self.assertIsNone(report['review_controls'][0]['accept'])

    def test_legacy_successful_retry_is_recovered_and_unknown_is_explicit(self):
        native = {'events': [{'type': 'validation_failure', 'role': 'reviewer', 'reason': 'BAD_SHAPE'}],
                  'evaluation_ask_outcomes': [{'role': 'reviewer', 'event_start': 0, 'event_end': 1, 'validated_return': True}]}
        normalized = normalize_report(native, arm='original', stage='review')
        self.assertTrue(normalized['failures'][0]['recovered'])
        del native['evaluation_ask_outcomes']
        normalized = normalize_report(native, arm='original', stage='review')
        self.assertEqual(normalized['failures'][0]['category'], 'unclassified')
        self.assertFalse(normalized['failures'][0]['recovery_known'])

    def test_current_final_recovery_flag_overrides_unannotated_history(self):
        native = {'protocol_failure_history': [{'stage': 'investigate', 'code': 'STEP_INVALID', 'category': 'shape'}],
                  'protocol_failures': [{'stage': 'investigate', 'code': 'STEP_INVALID', 'category': 'shape', 'recovered': True}]}
        self.assertTrue(normalize_report(native, arm='current', stage='e2e')['failures'][0]['recovered'])

    def test_live_requires_frozen_roots_and_single_parent_ledger(self):
        for args in ([], ['--original-root', 'a', '--current-root', 'b', '--live']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)
        self.assertEqual(parse_args(['--original-root', 'a', '--current-root', 'b', '--live',
            '--budget-ledger', 'ledger.json', '--max-model-calls', '912'])[1].max_model_calls, 912)


@unittest.skipUnless((ROOT / 'output/frozen-version-study/snapshots/legacy/app').is_dir()
                     and (ROOT / 'output/frozen-version-study/snapshots/current/app').is_dir(),
                     'Exact frozen study snapshots are local study artifacts')
class FrozenNativeProcessSmokeTests(unittest.TestCase):
    def smoke(self, arm, stage, case_id):
        spec = build_case_specs(ROOT)[case_id]
        root = ROOT / 'output/frozen-version-study/snapshots' / ('legacy' if arm == 'original' else 'current')
        application = application_freeze(root)
        broker = FreeNativeBroker(arm, spec)
        report = execute_worker(spec, application, arm, stage, broker)
        self.assertNotIn('execution_error', report['native_report'], report['native_report'].get('execution_error'))
        self.assertGreater(report['run_summary']['model_calls'], 0)
        self.assertEqual(report['run_summary']['model_calls'], len(broker.calls))
        self.assertEqual(report['run_summary']['limits'], LIMITS)
        freeze = freeze_case_spec(spec)
        self.assertEqual(report['observed_world_sha256'], freeze['world_sha256'])
        self.assertEqual(report['observed_capability_sha256'], freeze['metadata_sha256'])
        self.assertTrue(all(Path(path).is_relative_to(root / 'app') for path in report['loaded_application_modules'].values()))
        if stage == 'review':
            self.assertTrue(all(row['protocol_status'] == 'valid' for row in report['normalized']['review_controls']))
        return report

    def test_original_native_e2e_pipeline_isolated(self):
        report = self.smoke('original', 'e2e', 'fixture_case_001')
        self.assertFalse(any(name.startswith('workflow.v2') for name in report['loaded_application_modules']))

    def test_current_native_e2e_pipeline_isolated(self):
        self.smoke('current', 'e2e', 'fixture_case_001')

    def test_original_fixed_review_uses_its_native_contract(self):
        report = self.smoke('original', 'review', 'world_probe_scope')
        self.assertIn('assessments', report['native_report']['primary_review'])

    def test_current_fixed_review_uses_primary_and_native_challenge(self):
        report = self.smoke('current', 'review', 'world_probe_scope')
        self.assertIn('items', report['native_report']['primary_review'])
        self.assertIn('challenge', report['native_report'])


if __name__ == '__main__':
    unittest.main()
