"""Recovery scheduling and paid embedding calls nested inside read-only RAG."""
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from api.auth import Principal
from providers.bound import BoundIncidentProvider
from runtime.context import RunContext, activate, invoke_chat_model
from runtime.journal import Journal
from storage.recovery import RecoveryStore
from tools.executor import ToolExecutor
from workflow.recovery import execution_request
from workflow.v2.questions import InvestigationQuestions
from tests.checkpoint_support import RecoveryModel, audit
from tests.storage import test_checkpoint_recovery as controls


@unittest.skipUnless(controls.DSN, 'requires isolated *_test PostgreSQL')
class CheckpointEdgeTests(unittest.TestCase):
    setUp = controls.CheckpointRecoveryTests.setUp
    tearDown = controls.CheckpointRecoveryTests.tearDown
    begin = controls.CheckpointRecoveryTests.begin
    worker = controls.CheckpointRecoveryTests.worker
    attempts = controls.CheckpointRecoveryTests.attempts
    assert_delivery = controls.CheckpointRecoveryTests.assert_delivery

    def test_external_response_not_committed_is_unknown_and_never_resent(self):
        for stage, ordinal in (('collect', 1), ('collect', 2), ('rework', 4)):
            with self.subTest(stage=stage, ordinal=ordinal):
                run_id = self.begin()
                self.assertEqual(self.worker(run_id, 'after_external_response', stage, ordinal).returncode, 73)
                before = self.attempts()
                self.worker(run_id)
                run, execution, operations = self.assert_delivery(run_id)
                self.assertEqual(run['status'], 'partial')
                self.assertEqual(self.attempts(), before)
                self.assertEqual(sum(op['status'] == 'unknown' for op in operations), 1)

    def test_service_start_resumes_frozen_run_even_when_new_admission_switch_is_off(self):
        run_id = self.begin()
        self.assertEqual(self.worker(run_id, 'after_response_saved', 'collect', 1).returncode, 73)
        self.service.checkpoint_recovery = False
        self.service._incident.model_factory = lambda *args: RecoveryModel(self.audit)
        recoverable = self.store.recoverable
        self.store.recoverable = lambda: [row for row in recoverable() if row['tenant_id'] == self.owner.tenant_id]
        original = BoundIncidentProvider.query
        def query(provider, name, args, scope):
            audit(self.audit, {'kind': 'tool', 'name': name, 'args': args.model_dump(mode='json')})
            return original(provider, name, args, scope)
        with patch.object(BoundIncidentProvider, 'query', query):
            self.service.start()
            deadline = time.monotonic() + 20
            while self.store.get(run_id)['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(.05)
        run, _, operations = self.assert_delivery(run_id)
        self.assertEqual(run['status'], 'completed', run['result'])
        self.assertEqual(len(self.attempts()), len(operations))

    def test_nested_embedding_receipt_reuses_whole_tool_result_and_accounts_for_child(self):
        from knowledge.retrieval import embed
        run_id = self.begin()
        snapshot = self.store.get(run_id)['config']['incident_snapshot']
        config = SimpleNamespace(embedding_api_key_env='CHECKPOINT_FAKE_EMBEDDING_KEY',
            embedding_url='https://example.invalid', embedding_model='test-embedding', embedding_dimension=2)
        original = BoundIncidentProvider.query
        reads = []
        def query(provider, name, args, scope):
            reads.append(name)
            embed([args.query], config, query=True)
            return original(provider, name, args, scope)
        class Cut(BaseException):
            pass
        def fault(point, journal):
            if point == 'after_response_saved' and journal.active and journal.active['kind'] == 'tool':
                raise Cut()
        def runtime(backend):
            provider = BoundIncidentProvider(snapshot, self.owner, self.store)
            context = RunContext(run_id, scope=provider.ticket().scope)
            context.limits = self.service._incident.limits(execution_request(self.store.get(run_id), self.service))
            executor = ToolExecutor(provider, self.owner, context.scope, context, role='single')
            run = SimpleNamespace(context=context, executor=executor, events=self.store.events(run_id),
                questions=InvestigationQuestions(executor))
            journal = Journal(backend, context)
            return run, journal
        def operation(state, view):
            run = view.context
            with activate(run.context):
                result = run.executor.execute('search_runbooks', {'query': 'dependency timeout'})
                invoke_chat_model(SimpleNamespace(invoke=lambda messages: AIMessage(content='done')),
                    [HumanMessage(content='{}')], 'after_rag', terminal=True)
            return {'read_status': result.status}
        response = {'output': {'embeddings': [{'text_index': 0, 'embedding': [1.0, .5]}]}, 'usage': {'total_tokens': 7}}
        with patch.dict(os.environ, {'CHECKPOINT_FAKE_EMBEDDING_KEY': 'offline-only'}), \
                patch('providers.http.request_json', return_value=response) as http, \
                patch.object(BoundIncidentProvider, 'query', query):
            self.store._recovery_fault = fault
            with RecoveryStore.own(self.store, run_id) as backend:
                run, journal = runtime(backend)
                with self.assertRaises(Cut):
                    journal.stage('collect', operation, {}, run)
            self.store._recovery_fault = None
            with RecoveryStore.own(self.store, run_id) as backend:
                recovered, journal = runtime(backend)
                journal.stage('collect', operation, {}, recovered)
                row = backend.read()
                self.assertEqual(row['counts'], {'model_calls': 2, 'tool_calls': 1, 'web_calls': 0})
                self.assertEqual(recovered.context.counts, row['counts'])
                self.assertEqual(row['tokens']['input_tokens'], 7)
                self.assertEqual(row['tokens']['known_calls'], 1)
                self.assertEqual(journal.operation_cursor, 3)
            self.assertEqual(http.call_count, 1)
            self.assertEqual(reads, ['search_runbooks'])
