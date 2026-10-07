"""Free comparison boundaries. No paid model, production observation or credential reads."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from mult_agents.harness.runtime import Limits, ExecutionError
from mult_agents.incident.collaboration_fake import ScriptedSingle, scripted_models
from mult_agents.incident.contracts import SINGLE_TOOLS, INVESTIGATION_TOOLS, KNOWLEDGE_TOOLS
from mult_agents.incident.investigation import investigate, ReferenceValidationError
from mult_agents.incident.single import single_agent, resolve_diagnosis
from scripts.compare_incident import capture, parse_args, main, sha256
from evals.incident.comparison import evaluate, review_entry
from evals.run import write_json
from tests.test_incident_tools import P, executor, window
from tests.test_incident_collaboration import AlterOutput

ROOT = Path(__file__).resolve().parents[1]
LIMITS = Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20)


def factory(flow):
    return ScriptedSingle() if flow == 'single' else scripted_models()


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Execute one representative pair once; the full six-case smoke belongs to the CLI.
        cls.pair = [capture('case_005', flow, factory, P, LIMITS) for flow in ('single', 'multi')]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def bundle(self, reports=None, mode='scripted_control_only'):
        bundle = {'configuration': {'cases': ['case_005'], 'limits': LIMITS.__dict__, 'model': 'scripted', 'execution_mode': mode},
                  'provenance': {'test_control': True}, 'runs': []}
        for original in reports or self.pair:
            report = {**copy.deepcopy(original), 'execution_mode': mode, 'model': 'scripted', 'provenance': bundle['provenance']}
            path = self.directory / f"{report['run_id']}.json"
            write_json(path, report)
            bundle['runs'].append({'case_id': report['incident_id'], 'workflow': report['workflow'],
                'report_file': path.name, 'report_sha256': sha256(path),
                'gold_sha256': sha256(ROOT / 'evals/incident/gold/case_005.json')})
        return bundle

    def test_equal_budgets_references_and_step_counts_with_no_scripted_quality(self):
        summary, _ = evaluate(self.bundle(), self.directory)
        self.assertEqual(summary['quality_status'], 'excluded_scripted')
        for row in summary['runs']:
            self.assertTrue(row['mechanical_pass'], row)
            self.assertEqual(row['necessary_check_coverage'], 1)
        for group in summary['workflows'].values():
            self.assertIsNone(group['cause_accuracy_reviewed'])
            self.assertIsNone(group['estimated_cost'])
        single = self.pair[0]
        self.assertEqual(single['reviews'], [])
        self.assertEqual(single['review_status'], 'not_performed')
        self.assertTrue(all(h['status'] == 'tentative' for h in single['output']['hypotheses']))
        self.assertEqual(single['run_summary']['tool_calls'], 7)

    def test_missing_pair_and_modified_report_rejected(self):
        bundle = self.bundle()
        with self.assertRaises(ValueError):
            evaluate({**bundle, 'runs': bundle['runs'][:1]}, self.directory)
        path = self.directory / bundle['runs'][0]['report_file']
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'changed'):
            evaluate(bundle, self.directory)

    def test_unequal_budget_cannot_enter_same_budget_comparison(self):
        bundle = self.bundle()
        entry = bundle['runs'][0]
        path = self.directory / entry['report_file']
        report = json.loads(path.read_text(encoding='utf-8'))
        report['run_summary']['limits']['tool_calls'] += 1
        write_json(path, report)
        entry['report_sha256'] = sha256(path)
        with self.assertRaisesRegex(ValueError, 'configuration'):
            evaluate(bundle, self.directory)

    def test_paths_unknown_cases_and_changed_gold_rejected(self):
        for mutate in (lambda b: b['runs'][0].update(report_file='../elsewhere.json'),
                       lambda b: b['configuration'].update(cases=['../private']),
                       lambda b: b['runs'][0].update(gold_sha256='outdated')):
            bundle = self.bundle()
            mutate(bundle)
            with self.assertRaises(ValueError):
                evaluate(bundle, self.directory)

    def test_failed_attempt_is_retained_in_denominator(self):
        def failing(flow):
            raise ExecutionError('NETWORK_ERROR', 'model')
        failed = capture('case_005', 'single', failing, P, LIMITS)
        summary, _ = evaluate(self.bundle([failed, self.pair[1]]), self.directory)
        self.assertEqual(summary['workflows']['single']['runs'], 1)
        self.assertEqual(summary['workflows']['single']['failed_runs'], 1)
        self.assertFalse(summary['runs'][0]['mechanical_pass'])
        self.assertEqual(failed['run_summary']['model_calls'], 0)

    def test_scripted_report_cannot_be_approved_for_business_scoring(self):
        bundle = self.bundle()
        _, reports = evaluate(bundle, self.directory)
        run = next(iter(reports.values()))
        gold = json.loads((ROOT / 'evals/incident/gold/case_005.json').read_text(encoding='utf-8'))
        row = review_entry(run['report'], run['sha256'], gold)
        row['approved'] = True
        with self.assertRaisesRegex(ValueError, 'live'):
            evaluate(bundle, self.directory, {'runs': [row]})

    def test_manual_semantic_failure_does_not_change_valid_reference_score(self):
        # Mark offline fixtures as live solely to test the offline review contract. No model runs here.
        bundle = self.bundle(mode='live')
        pending, reports = evaluate(bundle, self.directory)
        self.assertEqual(pending['quality_status'], 'manual_review_pending')
        run = next(iter(reports.values()))
        gold = json.loads((ROOT / 'evals/incident/gold/case_005.json').read_text(encoding='utf-8'))
        row = review_entry(run['report'], run['sha256'], gold)
        row.update(approved=True, reviewer='offline test reviewer', rationale='Invalid causal interpretation test',
                   cause_correct=False, overconfident=True, escalation_appropriate=False)
        for claim in row['claim_assessments']:
            claim.update(supported=False, reason='Reference validity alone is insufficient')
        result, _ = evaluate(bundle, self.directory, {'runs': [row]})
        self.assertTrue(result['runs'][0]['mechanical_pass'])
        self.assertEqual(result['runs'][0]['semantic_review'], 'reviewed')
        self.assertEqual(result['workflows']['single']['cause_accuracy_reviewed'], 0)
        row['claim_assessments'][0]['text'] = 'different claim'
        with self.assertRaisesRegex(ValueError, 'original claim'):
            evaluate(bundle, self.directory, {'runs': [row]})

    def test_gold_is_not_read_while_either_workflow_executes(self):
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            if 'gold' in path.parts:
                raise AssertionError('Evaluator answers entered execution')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guarded):
            for flow in ('single', 'multi'):
                result = capture('case_004', flow, factory, P, LIMITS)
                self.assertEqual(result['status'], 'completed')

    def test_plan_and_invalid_live_never_load_models_or_credentials(self):
        with patch('dotenv.load_dotenv', side_effect=AssertionError('Credential read')):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--plan', '--cases', 'case_004']), 0)
            self.assertTrue(json.loads(output.getvalue())['plan_only'])
            for args in (['--live'], ['--live', '--execute-live', '--cases', 'case_004'],
                         ['--live', '--cases', 'case_004', '--model-budget', '16', '--tool-budget', '8'],
                         ['--execute-live']):
                with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)


class SingleBoundaryTests(unittest.TestCase):
    def test_six_tools_keep_other_roles_and_scope_boundaries(self):
        for role, allowed in [('single', SINGLE_TOOLS), ('investigation', INVESTIGATION_TOOLS), ('knowledge', KNOWLEDGE_TOOLS)]:
            ex = executor(role=role)
            self.assertEqual({s['function']['name'] for s in ex.schemas()}, allowed)
        ex = executor(role='single')
        result = ex.execute('get_service_owner', {'alias': 'outside-service'})
        self.assertEqual(result.status, 'error')
        self.assertEqual(ex.context.counts['tool_calls'], 0)
        self.assertEqual(ex.execute('shell', {'command': 'anything'}).status, 'error')

    def test_final_can_use_reserve_after_tool_budget_and_never_execute_more_tools(self):
        ex = executor(role='single')
        limits = Limits(model_calls=6, tool_calls=6, reserve_model_calls=3, reserve_seconds=20)
        result = single_agent(ScriptedSingle(), ex.provider, P, limits=limits)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['run_summary']['model_calls'], 2)
        self.assertEqual(result['run_summary']['tool_calls'], 6)
        result = single_agent(ScriptedSingle(), ex.provider, P,
            limits=Limits(model_calls=3, tool_calls=6, reserve_model_calls=3))
        self.assertEqual(result['run_summary']['model_calls'], 1)
        self.assertEqual(result['run_summary']['tool_calls'], 0)
        self.assertNotEqual(result['status'], 'completed')

    def test_forged_refs_stop_after_one_repair_and_history_cannot_be_current_fact(self):
        def alter(response, messages, n):
            if response.tool_calls:
                return response
            data = json.loads(response.content)
            data['findings'][0]['refs'][0]['reference_id'] = 'REF_forged'
            return AIMessage(content=json.dumps(data))
        ex = executor(role='single')
        result = single_agent(AlterOutput(ScriptedSingle(), alter), ex.provider, P)
        self.assertEqual(result['repairs'], 1)
        self.assertNotEqual(result['status'], 'completed')
        self.assertFalse(result['output']['findings'])
        ex = executor('case_003', role='single')
        history = ex.execute('search_incidents', {'symptoms': 'latency database pool'}).evidence[0]
        option = next(r for r in history.reference_options if r.field_path == 'text')
        content = json.dumps({'findings': [{'statement': 'Current cause', 'refs': [{'reference_id': option.reference_id}]}]})
        with self.assertRaises(ReferenceValidationError):
            resolve_diagnosis(content, ex)

    def test_m2_leaf_step_limit_is_preserved(self):
        ex = executor()
        with self.assertRaises(ValueError):
            investigate(ScriptedSingle(), ex.provider, P, max_steps=5)


if __name__ == '__main__':
    unittest.main()
