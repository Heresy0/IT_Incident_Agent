"""Standard flow diagnostics do not create new modes, queries or repair budgets."""
import json
import unittest
from unittest.mock import patch
from agents.contracts import SupervisorSelection
from agents.models import build_model
from config.incident import IncidentConfig
from evidence.diagnosis import ProtocolError
from tools.executor import ToolExecutor
from providers.fixtures import FixtureProvider
from workflow.coordinator import Collaboration
from tests.tools.test_incident_tools import P
from tests.workflow.test_planned_default import planned_models
from tests.workflow.test_incident_collaboration import AlterOutput, json_response


class GenericArgumentFeedbackTests(unittest.TestCase):
    def test_public_argument_diagnostics_redact_extra_names_and_values(self):
        errors = ToolExecutor.argument_errors('get_service_logs', {
            'start':'2026-10-15T10:00:00Z','end':'2026-10-15T10:20:00Z',
            'category':None,'private_secret':'sensitive'})
        self.assertEqual(errors[0]['path'],'category')
        self.assertEqual(errors[0]['expected']['enum'],['dependency','configuration','resource'])
        self.assertNotIn('private_secret',json.dumps(errors))
        self.assertNotIn('sensitive',json.dumps(errors))

    def test_standard_supervisor_can_correct_category_once_before_dispatch(self):
        models = planned_models(); readings=[]
        def wrong_category(response,messages,calls):
            readings.append(c.context.counts['tool_calls'])
            data=json.loads(response.content)
            if calls==1:data['tasks'][0]['checks'][0]['args']['category']=None
            return json_response(data)
        models['supervisor']=AlterOutput(models['supervisor'],wrong_category)
        c=Collaboration(models,FixtureProvider('case_001',P),P)
        report=c.run()
        failures=[e for e in report['events'] if e['type']=='validation_failure']
        self.assertEqual(len(failures),1)
        self.assertEqual(failures[0]['details'][0]['argument_errors'][0]['path'],'category')
        self.assertEqual(readings[:2],[0,0])
        self.assertEqual(report['repair_counts']['supervisor'],1)
        self.assertEqual(report['status'],'completed')

    def test_window_denial_is_not_reclassified_as_argument_repair(self):
        c=Collaboration(planned_models(),FixtureProvider('case_001',P),P)
        scope=c.ticket.scope.model_dump(mode='json')
        plan=SupervisorSelection(action='dispatch',reason='Read',tasks=[{
            'role':'investigation','goal':'Read logs','need_ids':['N1'],
            'checks':[{'tool':'get_service_logs','args':{'start':'2000-01-01T00:00:00Z',
                'end':'2000-01-01T00:20:00Z','category':'dependency'}}],
            'expected_value':'Observe actual scoped records'}])
        with self.assertRaises(ProtocolError) as caught:c.supervisor_decision(plan)
        self.assertEqual(caught.exception.details[0]['code'],'WINDOW_DENIED')
        self.assertNotIn('argument_errors',caught.exception.details[0])
        self.assertEqual(c.context.counts['tool_calls'],0)

    def test_optional_timeout_does_not_change_default_output_or_retry_settings(self):
        with patch('agents.models.ChatOpenAI') as chat, patch('agents.models.IncidentConfig.from_file',
                return_value=IncidentConfig(api_key='test-only')):
            build_model('test-only',request_timeout=60)
            self.assertEqual(chat.call_args.kwargs['timeout'],60)
            build_model('test-only')
            self.assertEqual(chat.call_args.kwargs['timeout'],30)
            self.assertEqual(chat.call_args.kwargs['max_tokens'],2200)
            self.assertEqual(chat.call_args.kwargs['max_retries'],0)


if __name__=='__main__':unittest.main()
