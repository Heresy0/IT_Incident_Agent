"""Components must work without a coordinator and preserve execution/publication boundaries."""
import copy
import unittest

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict

from agents.contracts import DiagnosisDraft, Hypothesis, RecommendedAction, SupervisorSelection
from agents.role_runner import RoleRequest, RoleRunner
from providers.fixtures import FixtureProvider
from runtime.context import ExecutionError, Limits, RunContext, activate
from tests.tools.test_incident_tools import P
from tools.executor import ToolExecutor
from workflow.evidence_needs import initial_needs
from workflow.planning import PlanningContext, validate_plan
from workflow.reporting import PublicationInput, ReportSnapshot, build_report, publish_diagnosis


class Answer(BaseModel):
    model_config = ConfigDict(strict=True)
    ok: bool


class Replies:
    def __init__(self, contents):
        self.contents = iter(contents)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(content=next(self.contents))


class WorkflowComponentTests(unittest.TestCase):
    def run_answer(self, replies, *, calls=4):
        context = RunContext(limits=Limits(model_calls=calls, reserve_model_calls=0))
        repairs, events = {'repairs': 0}, []
        runner = RoleRunner(context, repairs, reviewer_repair_mode='shared',
                            role_corrections=True, purpose='diagnosis')
        request = RoleRequest('diagnosis', Answer, Answer.model_json_schema(), {}, 'Return JSON', True)
        def emit(kind, role, **fields):
            events.append({'type': kind, 'role': role, **fields})
        with activate(context):
            result = runner.run(request, replies, emit=emit, parse=Answer.model_validate_json)
        return result, context, repairs, events

    def test_runner_corrects_without_a_workflow_or_tool_execution(self):
        model = Replies(['invalid JSON', '{"ok":true}'])
        result, context, repairs, events = self.run_answer(model)
        self.assertTrue(result.ok)
        self.assertEqual(context.counts['model_calls'], 2)
        self.assertEqual(context.counts['tool_calls'], 0)
        self.assertEqual(repairs['role_repairs'], {'diagnosis': 1})
        self.assertEqual(sum(e['type'] == 'validation_repair' for e in events), 1)

    def test_correction_cannot_exceed_original_total_budget(self):
        model = Replies(['invalid JSON', '{"ok":true}'])
        with self.assertRaises(ExecutionError) as error:
            self.run_answer(model, calls=1)
        self.assertEqual(error.exception.code, 'BUDGET_EXCEEDED')
        self.assertEqual(model.calls, 1)

    def test_persistent_invalid_output_stops_after_one_correction(self):
        model = Replies(['invalid JSON', '{"ok":"not a boolean"}', '{"ok":true}'])
        with self.assertRaises(ExecutionError) as error:
            self.run_answer(model)
        self.assertEqual(error.exception.code, 'MODEL_OUTPUT_INVALID')
        self.assertEqual(model.calls, 2)

    def test_planning_returns_a_decision_without_running_tools_or_changing_needs(self):
        provider = FixtureProvider('case_001', P)
        ticket = provider.ticket()
        context = RunContext(scope=ticket.scope)
        executors = {role: ToolExecutor(provider, P, ticket.scope, context, role=role)
                     for role in ('investigation', 'knowledge')}
        needs = initial_needs(ticket)
        original = copy.deepcopy(needs)
        view = PlanningContext('collect', ticket.purpose, {}, {}, needs, executors,
                               'programmatic', 'programmatic', True, 0, False, None)
        window = ticket.scope.model_dump(mode='json', include={'start', 'end'})
        selection = SupervisorSelection(action='dispatch', reason='Inspect load', tasks=[{
            'role': 'investigation', 'goal': 'Inspect load', 'need_ids': ['N1'],
            'checks': [{'tool': 'get_service_metrics', 'args': {**window, 'metrics': ['db_cpu']}}]}])
        decision = validate_plan(selection, view, emit=lambda *args, **kwargs: None,
                                 can_collect=lambda count: True)
        self.assertEqual(len(decision.tasks), 1)
        self.assertEqual(needs, original)
        self.assertEqual(context.counts['tool_calls'], 0)
        self.assertFalse(executors['investigation'].evidence)

    def test_publication_does_not_promote_an_unreviewed_hypothesis_or_action(self):
        draft = DiagnosisDraft(findings=[], hypotheses=[Hypothesis(hypothesis_id='H1',
            cause='Possible resource pressure', support_refs=[], counter_refs=[], pending_checks=[])],
            recommended_actions=[RecommendedAction(action='Inspect resources', condition='Read only',
                expected_result='Obtain actual usage', risk='No writes', requires_approval=False,
                kind='manual_check')], missing_information=['Need current metrics'], escalation_team=None)
        gaps = ['Existing limitation']
        before = draft.model_dump()
        published = publish_diagnosis(PublicationInput(draft, None, 'diagnosis', 'partial',
            'NEEDS_INFORMATION', gaps, None, []))
        self.assertEqual(published.hypotheses[0]['status'], 'tentative')
        self.assertEqual(published.actions, [])
        self.assertEqual(len(published.pending_actions), 1)
        self.assertEqual(published.used_evidence_ids, [])
        self.assertEqual(draft.model_dump(), before)
        self.assertEqual(gaps, ['Existing limitation'])

    def test_response_mutation_cannot_modify_publication_or_audit_snapshot(self):
        published = publish_diagnosis(PublicationInput(None, None, 'diagnosis', 'partial',
            'NEEDS_INFORMATION', ['Missing evidence'], None, []))
        metadata = {'events': [{'type': 'final'}], 'diagnosis_readiness': {}}
        snapshot = ReportSnapshot(metadata, published)
        first = build_report(snapshot)
        first['events'][0]['type'] = 'changed'
        first['output']['missing_information'].clear()
        second = build_report(snapshot)
        self.assertEqual(second['events'], [{'type': 'final'}])
        self.assertIn('Missing evidence', second['output']['missing_information'])
        self.assertEqual(metadata['diagnosis_readiness'], {})
