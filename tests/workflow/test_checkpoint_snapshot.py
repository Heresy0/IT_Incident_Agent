"""JSONB round trips preserve source identity and invalidate stale approvals."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from api.auth import Principal
from providers.fixtures import FixtureProvider
from runtime.context import RunContext
from storage.recovery import RecoveryConflict, digest, request_digest
from tools.executor import ToolExecutor
from workflow.v2 import engine
from workflow.v2.contracts import parse_draft
from workflow.v2.questions import InvestigationQuestions
from workflow.v2.review import evidence_pack_version
from workflow.v2.scripted import scripted_model
from workflow.v2.snapshot import capture, restore


class CheckpointSnapshotTests(unittest.TestCase):
    def test_rebuilt_span_reuses_committed_identity_and_original_call_duration(self):
        from runtime.journal import Journal
        saved = {'collect/event/0': {'type': 'call_start', 'kind': 'model', 'name': 'test',
            'span_id': 'original-span', 'parent_span_id': 'root', 'seq': 1}}
        def emit(key, event, clocks):
            created = key not in saved
            if created:
                saved[key] = {**event, 'seq': len(saved) + 1}
            return saved[key], created
        backend = SimpleNamespace(read=lambda: {'elapsed': 0.0, 'recoveries': 1}, emit=emit)
        context = RunContext()
        context.root_span = 'root'
        journal = Journal(backend, context)
        journal.stage_name, journal.run = 'collect', SimpleNamespace(events=[])
        stamp = datetime.now(timezone.utc)
        journal.active = {'kind': 'model', 'saved': {'reserved_at': stamp, 'settled_at': stamp + timedelta(seconds=2)}}
        with context.span('model', 'test'):
            pass
        events = journal.run.events
        self.assertEqual(events[0]['span_id'], events[1]['span_id'])
        self.assertEqual(events[1]['duration_ms'], 2000)

    def test_committed_new_event_is_observed_once_and_replay_is_not_recounted(self):
        from runtime.journal import Journal
        saved = {}
        def emit(key, event, clocks):
            created = key not in saved
            if created:
                saved[key] = {**event, 'seq': 1}
            return saved[key], created
        backend = SimpleNamespace(read=lambda: {'elapsed': 0.0, 'recoveries': 0}, emit=emit)
        context = RunContext()
        journal = Journal(backend, context)
        journal.stage_name, journal.run = 'collect', SimpleNamespace(events=[])
        event = {'type': 'call_end', 'kind': 'model', 'name': 'investigate', 'status': 'ok', 'duration_ms': 1}
        with patch('observability.telemetry.record_event') as observed:
            context.emit(event)
            journal.event_cursor = 0
            context.emit(event)
        self.assertEqual(observed.call_count, 1)
        self.assertEqual(len(journal.run.events), 1)

    def test_storage_failure_escapes_model_and_tool_failure_conversion(self):
        from runtime.context import activate, invoke_chat_model
        from storage.recovery import RecoveryInterrupted, RecoveryStore
        import psycopg
        store = SimpleNamespace(pool=MagicMock())
        store.pool.connection.side_effect = psycopg.OperationalError('offline test storage')
        with self.assertRaises(RecoveryInterrupted):
            RecoveryStore(store, 'test', 1).read()
        run = self.runtime()
        journal = MagicMock()
        journal.active = {'kind': 'model'}
        journal.reserve.side_effect = RecoveryInterrupted('offline test storage')
        journal.elapsed.return_value = 0.0
        @contextmanager
        def operation(*args):
            yield
        journal.operation = operation
        run.context.journal = journal
        model = MagicMock()
        with activate(run.context), self.assertRaises(RecoveryInterrupted):
            invoke_chat_model(model, [], 'test')
        model.invoke.assert_not_called()
        scope = run.context.scope
        with self.assertRaises(RecoveryInterrupted):
            run.executor.execute('get_service_logs', {'start': scope.start.isoformat(),
                'end': scope.end.isoformat(), 'category': 'dependency'})

    def runtime(self):
        principal = Principal('team_a', 'alice')
        provider = FixtureProvider('case_001', principal)
        context = RunContext(scope=provider.ticket().scope)
        executor = ToolExecutor(provider, principal, context.scope, context, role='single')
        return SimpleNamespace(context=context, executor=executor, events=[],
            questions=InvestigationQuestions(executor))

    def populated(self):
        run = self.runtime()
        scope = run.context.scope
        run.executor.execute('get_service_logs', {'start': scope.start.isoformat(),
            'end': scope.end.isoformat(), 'category': 'dependency'})
        draft, _ = parse_draft(json.dumps({'claims': [{'statement': '有限来源记录', 'refs': ['R1']}]}), run.executor)
        reviews = engine.review_draft(scripted_model(run.executor.provider), draft, run.executor,
            run.context, collection=engine.collection_view(run.executor))
        state = {'draft': draft, 'reviews': reviews, 'failures': [], 'repairs': {'repairs': 0, 'reviewer_repairs': 0}}
        return run, state

    def test_jsonb_round_trip_preserves_evidence_refs_review_key_and_question_baselines(self):
        run, state = self.populated()
        payload = json.loads(json.dumps(capture(state, run), sort_keys=True))
        recovered = self.runtime()
        restored = restore(payload, recovered)
        self.assertEqual(restored['draft'], state['draft'])
        self.assertEqual(restored['reviews'], state['reviews'])
        self.assertEqual(recovered.executor.references, run.executor.references)
        self.assertEqual(recovered.executor.evidence, run.executor.evidence)
        self.assertEqual(recovered.executor.check_results, run.executor.check_results)
        self.assertEqual(recovered.questions.__dict__, run.questions.__dict__)
        self.assertEqual(evidence_pack_version(recovered.executor), evidence_pack_version(run.executor))

    def test_tampered_source_is_rejected_even_if_snapshot_container_is_readable(self):
        run, state = self.populated()
        payload = capture(state, run)
        payload['executor']['evidence'][0]['payload']['message'] = 'changed source'
        with self.assertRaises(RecoveryConflict):
            restore(payload, self.runtime())

    def test_new_read_after_restore_does_not_inherit_old_review_approval(self):
        run, state = self.populated()
        recovered = self.runtime()
        restored = restore(json.loads(json.dumps(capture(state, run))), recovered)
        scope = recovered.context.scope
        recovered.executor.execute('get_service_logs', {'start': scope.start.isoformat(),
            'end': scope.end.isoformat(), 'category': 'resource'})
        result = engine.publish(restored['draft'], restored['reviews'], recovered.executor,
            recovered.context, [], 'FINISHED', [], recovered.executor.provider.ticket(), 2, {'repairs': 0})
        self.assertTrue(all(item['issue'] == 'REVIEW_EVIDENCE_PACK_STALE' for item in result['item_reviews']))
        self.assertTrue(all(item['status'] != 'supported' for item in result['output']['hypotheses']))

    def test_request_digest_ignores_json_object_order_but_preserves_content(self):
        one = {'messages': [{'type': 'human', 'content': '{"budget":{"models":7},"claim":"a"}'}]}
        two = {'messages': [{'type': 'human', 'content': '{"claim":"a","budget":{"models":7}}'}]}
        changed = {'messages': [{'type': 'human', 'content': '{"claim":"b","budget":{"models":7}}'}]}
        self.assertEqual(request_digest(one), request_digest(two))
        self.assertNotEqual(request_digest(one), request_digest(changed))
