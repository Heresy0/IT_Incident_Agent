"""Free owner-selection regression for the second live retest; no keys or network."""
import json
import unittest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from runtime.context import Limits
from workflow.coordinator import Collaboration, collaborate
from agents.contracts import DiagnosisSelection, SupervisorSelection
from evidence.contracts import InvestigationSelection
from agents.scripted import scripted_models, selector, control_need_assessments
from agents.investigation import investigate, resolve_output
from evidence.ownership import OwnerSelectionError, parse_owner_selection
from providers.fixtures import FixtureProvider
from agents.single import resolve_diagnosis
from tests.workflow.test_incident_collaboration import json_response
from tests.tools.test_incident_tools import P, executor, window


class OwnerFlowModel:
    """Replay three tasks, including duplicates and a repaired owner selector."""
    def __init__(self, role):
        self.role, self.steps = role, {}

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        data = json.loads(messages[1].content)
        if self.role == 'supervisor':
            return json_response({'action': 'dispatch', 'reason': 'Check missing observations',
                'tasks': [{'role': 'investigation', 'goal': f"ownerflow{len(data['input']['tasks']) + 1}"}]})
        if self.role == 'reviewer':
            return json_response({'need_assessments': control_need_assessments(data['input']),
                'assessments': [{'target_id': target, 'verdict': 'supported',
                'reason': 'Matches current source field', 'evidence_ids': []}
                for target in data['input']['targets']]})
        if self.role == 'diagnosis':
            sources = data['input']['evidence']
        else:
            objective = data.get('objective') or {}
            goal = objective['goal']
            step = self.steps[goal] = self.steps.get(goal, 0) + 1
            w = {k: data['ticket']['scope'][k] for k in ('start', 'end')}
            metrics = ('get_service_metrics', {**w, 'metrics': ['db_cpu', 'pool_usage', 'pool_wait']})
            changes = ('get_recent_changes', {**w, 'category': 'configuration'})
            warn = ('get_service_logs', {**w, 'category': 'resource', 'level': 'WARN'})
            checkout = ('get_service_owner', {'alias': 'checkout-api'})
            queries = []
            if step == 1:
                queries = ([metrics, ('get_service_logs', {**w, 'category': 'resource', 'level': 'ERROR'}), changes, checkout]
                    if goal == 'ownerflow1' else [warn, ('get_service_owner', {'alias': 'inventory-api'})]
                    if goal == 'ownerflow2' else [metrics, warn, changes, checkout])
            elif goal == 'ownerflow1' and step == 2:
                queries = [('get_service_logs', {**w, 'category': 'resource'})]
            if queries:
                return AIMessage(content='', tool_calls=[{'name': n, 'args': a, 'id': f'{goal}-{step}-{i}',
                    'type': 'tool_call'} for i, (n, a) in enumerate(queries)])
            sources = [*objective.get('evidence', []), *(e for m in messages if isinstance(m, ToolMessage)
                for e in json.loads(m.content)['evidence'])]
            if goal == 'ownerflow3' and step == 2:
                return json_response({'escalation_ref': {'reference_id': 'REF_' + '0' * 24}})
        owner = next(e for e in reversed(sources) if 'team' in e['payload'])
        cpu = next(e for e in sources if e['payload'].get('metric') == 'db_cpu')
        output = {'findings': [{'statement': 'Observed CPU', 'refs': [selector(cpu, 'value')]}],
            'escalation_ref': selector(owner, 'team')}
        if self.role == 'diagnosis':
            metrics = {e['payload']['metric']: e for e in sources if 'metric' in e['payload']}
            output.update(hypotheses=[{'cause': '应用连接池压力导致请求等待',
                'support_refs': [selector(metrics[m], 'value') for m in ('pool_usage', 'pool_wait')],
                'counter_refs': [selector(cpu, 'value')]}], recommended_actions=[{
                'action': '由值班人员核查连接池容量与配置', 'condition': '确认连接池使用率与等待异常仍存在',
                'expected_result': '处置后连接池等待下降', 'risk': '配置调整可能影响服务，需要人工批准',
                'requires_approval': True}])
        return json_response(output)


class IncidentOwnershipTests(unittest.TestCase):
    def registry(self):
        ex = executor('case_003')
        ex.execute('get_service_owner', {'alias': 'checkout-api'})
        ex.execute('get_service_metrics', {**window(ex), 'metrics': ['db_cpu']})
        return ex

    def owner_ref(self, ex, field='team'):
        owner = next(e for e in ex.evidence.values() if 'team' in e.payload)
        return {'reference_id': next(o.reference_id for o in owner.reference_options if o.field_path == field)}

    def coordinator(self, ex):
        c = Collaboration(scripted_models(), FixtureProvider('case_003', P), P)
        c.evidence.update(ex.evidence)
        c.references.update(ex.references)
        return c

    def test_model_contracts_expose_owner_selector_and_public_output_stays_compatible(self):
        ex = self.registry()
        c = self.coordinator(ex)
        payload = {'escalation_ref': self.owner_ref(ex)}
        for schema in (InvestigationSelection, DiagnosisSelection, SupervisorSelection):
            properties = schema.model_json_schema()['properties']
            self.assertIn('escalation_ref', properties)
            self.assertNotIn('escalation_team', properties)
        outputs = [resolve_output(json.dumps(payload), ex)[0], resolve_diagnosis(json.dumps(payload), ex)[0],
            c.supervisor_decision(SupervisorSelection.model_validate({**payload, 'action': 'escalate', 'reason': 'Human review'}))]
        for output in outputs:
            self.assertEqual(output.escalation_team, 'Checkout on-call')
            self.assertNotIn('escalation_ref', output.model_dump())
        self.assertEqual(ex.context.counts['tool_calls'], 2)

    def test_absent_owner_keeps_null_without_lookup_or_fallback(self):
        ex = executor('case_003')
        self.assertIsNone(resolve_output('{}', ex)[0].escalation_team)
        self.assertIsNone(resolve_diagnosis('{}', ex)[0].escalation_team)
        self.assertEqual(ex.context.counts['tool_calls'], 0)

    def test_unknown_non_owner_and_historical_owner_references_rejected_in_every_role(self):
        ex = self.registry()
        cpu = next(rid for rid, ref in ex.references.items() if ref.field_path == 'value')
        owner = next(e for e in ex.evidence.values() if 'team' in e.payload)
        historical = SimpleNamespace(references=ex.references,
            evidence={**ex.evidence, owner.evidence_id: owner.model_copy(update={'kind': 'past_incident'})})
        log_team = SimpleNamespace(references=ex.references,
            evidence={**ex.evidence, owner.evidence_id: owner.model_copy(update={'locator': 'get_service_logs/non-owner'})})
        for registry, selected in [(ex, {'reference_id': 'REF_' + '0' * 24}),
                (ex, {'reference_id': cpu}), (ex, self.owner_ref(ex, 'escalation')),
                (historical, self.owner_ref(ex)), (log_team, self.owner_ref(ex))]:
            with self.subTest(selected=selected, historical=registry is historical):
                payload = {'escalation_ref': selected}
                c = self.coordinator(registry)
                for resolve in (lambda: resolve_output(json.dumps(payload), registry),
                        lambda: resolve_diagnosis(json.dumps(payload), registry),
                        lambda: c.supervisor_decision(SupervisorSelection.model_validate({**payload, 'action': 'escalate', 'reason': 'Review'}))):
                    with self.assertRaises(OwnerSelectionError):
                        resolve()

    def test_legacy_exact_name_is_compatible_but_unknown_and_conflicting_names_fail(self):
        ex = self.registry()
        self.assertEqual(resolve_output('{"escalation_team":"Checkout on-call"}', ex)[0].escalation_team, 'Checkout on-call')
        self.assertEqual(resolve_diagnosis('{"escalation_team":"Checkout on-call"}', ex)[0].escalation_team, 'Checkout on-call')
        for payload in ({'escalation_team': 'checkout'}, {'escalation_team': 'Unobserved team'},
                {'escalation_team': None, 'escalation_ref': self.owner_ref(ex)}):
            with self.subTest(payload=payload):
                with self.assertRaises(OwnerSelectionError):
                    parse_owner_selection(DiagnosisSelection, json.dumps(payload), ex)

    def test_three_task_retest_path_repairs_once_then_reaches_diagnosis_and_reviewer(self):
        models = scripted_models()
        models.update({role: OwnerFlowModel(role) for role in ('supervisor', 'investigation', 'diagnosis', 'reviewer')})
        result = collaborate(models, FixtureProvider('case_003', P), P,
            limits=Limits(model_calls=16, tool_calls=8, reserve_model_calls=3, reserve_seconds=20))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['review_status'], 'passed')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['run_summary']['model_calls'], 13)
        self.assertEqual(result['run_summary']['tool_calls'], 7)
        self.assertEqual(result['output']['escalation_team'], 'Inventory on-call')
        self.assertEqual(len(result['output']['findings']), 1)
        self.assertEqual(result['output']['hypotheses'][0]['status'], 'supported')
        self.assertEqual(len(result['output']['recommended_actions']), 1)
        self.assertEqual(len(result['reviews']), 1)
        self.assertEqual(result['coverage_gaps'], [])
        self.assertTrue(any(e['type'] == 'collection_stalled' for e in result['events']))
        self.assertEqual(sum(e['type'] == 'validation_repair' for e in result['events']), 1)

    def test_repeated_bad_owner_selector_stops_after_one_repair_without_extra_tools(self):
        class InvalidOwner:
            def __init__(self): self.calls = 0
            def bind_tools(self, tools): return self
            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return AIMessage(content='', tool_calls=[{'name': 'get_service_owner',
                        'args': {'alias': 'checkout-api'}, 'id': 'owner', 'type': 'tool_call'}])
                return json_response({'escalation_ref': {'reference_id': 'REF_' + '0' * 24}})
        result = investigate(InvalidOwner(), FixtureProvider('case_003', P), P)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['run_summary']['termination_reason'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(result['run_summary']['model_calls'], 3)
        self.assertEqual(result['run_summary']['tool_calls'], 1)
        self.assertIsNone(result['output']['escalation_team'])


if __name__ == '__main__':
    unittest.main()
