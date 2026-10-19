"""Protect graph routing, failure boundaries and isolation without paid models."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

from langchain_core.callbacks import BaseCallbackHandler

from api.auth import Principal
from providers.fixtures import FixtureProvider
from runtime.context import Limits, RunContext
from workflow.v2 import engine, graph
from workflow.v2.scripted import scripted_model


class StageObserver(BaseCallbackHandler):
    def __init__(self):
        self.stages = []

    def on_chain_start(self, serialized, inputs, **kwargs):
        node = (kwargs.get('metadata') or {}).get('langgraph_node')
        if node and kwargs.get('name') == node:
            self.stages.append(node)


class GraphOrchestrationTests(unittest.TestCase):
    def test_real_graph_routes_single_pass_closed_loop_and_status_check(self):
        prefix, suffix = ['collect', 'diagnose', 'review'], ['publish', 'finalize']
        for rework, challenge, status, middle in (
            (False, True, False, []), (True, False, False, ['rework']),
            (True, True, False, ['challenge', 'rework']), (True, True, True, []),
        ):
            with self.subTest(rework=rework, challenge=challenge, status_check=status):
                principal = Principal('team_a', 'alice')
                provider = FixtureProvider('case_001', principal)
                ticket = provider.ticket()
                if status:
                    ticket = ticket.model_copy(update={'purpose': 'status_check'})
                observer = StageObserver()
                compiled = graph.GRAPH.with_config(callbacks=[observer])
                with patch.object(provider, 'ticket', return_value=ticket), patch.object(graph, 'GRAPH', compiled):
                    report = engine.investigate(scripted_model(provider), provider, principal,
                        enable_rework=rework, enable_challenge=challenge)
                self.assertEqual(observer.stages, prefix + middle + suffix)
                self.assertEqual(report['execution_variant'], 'closed_loop' if rework else 'single_pass')
                self.assertTrue(report['final'])
                self.assertEqual(sum(e['type'] == 'workflow_completed' for e in report['events']), 1)

    def test_compiled_graph_does_not_retry_failed_stage_or_publish_after_it(self):
        principal = Principal('team_a', 'alice')
        provider = FixtureProvider('case_001', principal)
        with patch.object(engine, 'collect', side_effect=RuntimeError('unexpected stage failure')) as collect, \
                patch.object(engine, 'publish') as publish:
            with self.assertRaisesRegex(RuntimeError, 'unexpected stage failure'):
                engine.investigate(scripted_model(provider), provider, principal, enable_rework=True)
        self.assertEqual(collect.call_count, 1)
        publish.assert_not_called()
        self.assertIs(graph.GRAPH.checkpointer, False)

    def test_concurrent_invocations_share_graph_but_not_runtime_evidence_or_events(self):
        barrier = Barrier(2)
        expected = {}
        for case in ('case_001', 'case_003'):
            principal = Principal('team_a', 'alice')
            provider = FixtureProvider(case, principal)
            report = engine.investigate(scripted_model(provider), provider, principal,
                limits=Limits(model_calls=16, tool_calls=8), enable_rework=True)
            expected[case] = {e['evidence_id'] for e in report['evidence']}

        def run(case):
            principal = Principal('team_a', 'alice')
            provider = FixtureProvider(case, principal)
            delegate = scripted_model(provider)
            calls = []

            class OverlapModel:
                def invoke(self, messages):
                    if not calls:
                        barrier.wait(timeout=10)
                    calls.append(messages)
                    return delegate.invoke(messages)

            events = []
            context = RunContext('graph-isolation-' + case, scope=provider.ticket().scope,
                limits=Limits(model_calls=16, tool_calls=8),
                emit=lambda e: events.append({**e, 'seq': len(events) + 1}))
            report = engine.investigate(OverlapModel(), provider, principal,
                context=context, event_log=events, enable_rework=True)
            self.assertEqual(report['run_summary']['model_calls'], len(calls))
            self.assertEqual({e['evidence_id'] for e in report['evidence']}, expected[case])
            self.assertEqual({e['run_id'] for e in events}, {context.run_id})
            self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
            return report

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(run, ('case_001', 'case_003')))
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertTrue(first['evidence'])
        self.assertTrue(second['evidence'])

    def test_shared_context_scope_guard_runs_before_any_model_or_tool_call(self):
        principal = Principal('team_a', 'alice')
        provider = FixtureProvider('case_001', principal)
        context = RunContext(scope=None)
        with patch.object(provider, 'query') as query:
            with self.assertRaisesRegex(ValueError, 'matching scope and event log'):
                engine.investigate(object(), provider, principal, context=context, event_log=[])
        query.assert_not_called()
        self.assertEqual(context.counts['model_calls'], 0)
        self.assertEqual(context.counts['tool_calls'], 0)

    def test_native_evaluation_hook_observes_the_same_executor_and_evidence(self):
        principal = Principal('team_a', 'alice')
        provider = FixtureProvider('case_001', principal)
        executors = []
        original = engine.ToolExecutor

        def capture(*args, **kwargs):
            executor = original(*args, **kwargs)
            executors.append(executor)
            return executor

        with patch.object(engine, 'ToolExecutor', side_effect=capture):
            report = engine.investigate(scripted_model(provider), provider, principal, enable_rework=True)
        self.assertEqual(len(executors), 1)
        self.assertEqual(set(executors[0].evidence), {e['evidence_id'] for e in report['evidence']})
        self.assertEqual(executors[0].context.counts['tool_calls'], report['run_summary']['tool_calls'])


if __name__ == '__main__':
    unittest.main()
