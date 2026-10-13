"""No live operations: real executors with HTTP/CLI stubs and approval lifecycle."""
from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch, MagicMock
import httpx
from agents.contracts import DiagnosisSelection
from evidence.diagnosis import expand_diagnosis
from providers.configuration import validate_configuration
from providers.fixtures import ProviderError
from providers.registry import ServiceBinding, ServiceRegistry
from repairs.application import ApplicationHTTPExecutor
from repairs.contracts import RepairIntent
from repairs.executors import DockerLifecycleHTTPExecutor, DockerLifecycleCLIExecutor
from incidents.support import IncidentError
from tests.workflow import test_remediation as remediation


ACTIONS = ['start_service', 'resume_service', 'reload_config', 'resume_consumer', 'refresh_cache']


def app_state(action):
    return {'target_id':'fixed-target', 'scope':'tenant-service-staging', 'revision':7, 'healthy':False,
        'kind':ApplicationHTTPExecutor.kinds[action], 'supported_actions':[action],
        'available_config_version':'v2', 'loaded_config_version':'v1', 'config_valid':True,
        'consumer_paused':True, 'resume_allowed':True, 'cache_generation':1, 'refresh_allowed':True,
        'cache_ready':False, 'private_config':'must-not-leak'}


def container_state(action):
    return {'Id':'a'*64, 'Config':{'Healthcheck':{'Test':['CMD','true']}, 'Env':['SECRET=must-not-leak']},
        'State':{'Running':action == 'resume_service', 'Paused':action == 'resume_service',
                 'Status':'paused' if action == 'resume_service' else 'exited', 'StartedAt':'old',
                 'Health':{'Status':'unhealthy'}}}


class MaintenanceExecutorTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.target = {'url':'http://127.0.0.1:8001', 'control_target':'fixed-target',
                       'control_scope':'tenant-service-staging', 'desired_config_version':'v2'}
        self.state = app_state('reload_config')
        def handle(request):
            self.requests.append(request)
            if request.method == 'POST':
                body = json.loads(request.content)
                self.assertEqual(body['expected_revision'],self.state['revision'])
                self.state.update(revision=self.state['revision']+1, healthy=True,
                    loaded_config_version='v2',consumer_paused=False,cache_ready=True,
                    cache_generation=self.state['cache_generation']+1,last_operation_id=body['operation_id'])
                return httpx.Response(200,json={'accepted':True,'operation_id':body['operation_id'],
                    'target_id':self.state['target_id'],'scope':self.state['scope']})
            return httpx.Response(200,json=self.state)
        self.executor = ApplicationHTTPExecutor(transport=httpx.MockTransport(handle))

    def test_application_actions_write_fixed_path_and_verify_operation_identity(self):
        for action in ApplicationHTTPExecutor.actions:
            with self.subTest(action=action):
                self.state = app_state(action)
                before = self.executor.preflight(self.target,action,{})
                self.assertNotIn('must-not-leak',str(before))
                receipt = self.executor.execute_checked(self.target,action,{},before)
                before['operation_id'] = receipt['operation_id']
                self.assertTrue(self.executor.verify(self.target,action,{},before)['passed'])
                post = [r for r in self.requests if r.method == 'POST'][-1]
                self.assertEqual(post.url.path,'/ops/targets/fixed-target/actions/'+action)
                self.assertEqual(json.loads(post.content).get('config_version'),'v2' if action == 'reload_config' else None)
                self.state['last_operation_id'] = 'unrelated'
                self.assertFalse(self.executor.verify(self.target,action,{},before)['passed'])

    def test_foreign_target_scope_kind_and_malformed_revision_rejected(self):
        for changes in ({'target_id':'foreign'},{'scope':'other'},{'kind':'cache'},
                        {'revision':True},{'supported_actions':[]}):
            self.state = {**app_state('reload_config'),**changes}
            with self.assertRaises(ProviderError):
                self.executor.preflight(self.target,'reload_config',{})
        self.assertFalse(any(r.method == 'POST' for r in self.requests))

    def test_invalid_config_already_applied_and_disallowed_resume_or_refresh_do_not_write(self):
        for action,changes in (('reload_config',{'config_valid':False}),
                               ('reload_config',{'loaded_config_version':'v2'}),
                               ('resume_consumer',{'resume_allowed':False}),
                               ('refresh_cache',{'refresh_allowed':False})):
            self.state = {**app_state(action),**changes}
            with self.assertRaises(ProviderError):
                self.executor.execute(self.target,action,{})
        self.assertFalse(any(r.method == 'POST' for r in self.requests))

    def test_revision_change_between_preflight_and_write_blocks_operation(self):
        before = self.executor.preflight(self.target,'reload_config',{})
        self.state['revision'] += 1
        with self.assertRaisesRegex(ProviderError,'CONTROL_STATE_CHANGED'):
            self.executor.execute_checked(self.target,'reload_config',{},before)
        self.assertFalse(any(r.method == 'POST' for r in self.requests))

    def test_healthy_without_cache_generation_change_does_not_prove_refresh(self):
        self.state = app_state('refresh_cache')
        before = self.executor.preflight(self.target,'refresh_cache',{})
        before['operation_id'] = 'expected'
        self.state.update(healthy=True,revision=8,last_operation_id='expected',cache_ready=True)
        self.assertFalse(self.executor.verify(self.target,'refresh_cache',{},before)['passed'])

    def test_malformed_config_value_is_not_persisted(self):
        self.state['loaded_config_version'] = {'secret':'must-not-leak'}
        with self.assertRaisesRegex(ProviderError,'CONFIG_RELOAD_NOT_READY'):
            self.executor.preflight(self.target,'reload_config',{})

    def test_wrong_operation_receipt_is_uncertain_and_not_retried(self):
        posts = []
        def handle(request):
            if request.method == 'POST':
                posts.append(request)
                return httpx.Response(200,json={'accepted':True,'operation_id':'foreign',
                    'target_id':'fixed-target','scope':'tenant-service-staging'})
            return httpx.Response(200,json=self.state)
        self.executor.transport = httpx.MockTransport(handle)
        with self.assertRaisesRegex(ProviderError,'CONTROL_RECEIPT_INVALID'):
            self.executor.execute(self.target,'reload_config',{})
        self.assertEqual(len(posts),1)

    def test_write_timeout_is_not_retried(self):
        posts = []
        def handle(request):
            if request.method == 'POST':
                posts.append(request)
                raise httpx.ReadTimeout('private details')
            return httpx.Response(200,json=self.state)
        self.executor.transport = httpx.MockTransport(handle)
        with self.assertRaisesRegex(ProviderError,'TIMEOUT'):
            self.executor.execute(self.target,'reload_config',{})
        self.assertEqual(len(posts),1)

    def test_parameters_and_path_injection_rejected_before_network(self):
        for action in ACTIONS:
            with self.assertRaises(ValueError):
                RepairIntent(action=action,target='api',parameters={'command':'shell'},evidence_ids=['EV_x'])
        with self.assertRaises(ProviderError):
            self.executor.preflight({**self.target,'control_target':'../other'},'reload_config',{})
        self.assertEqual(self.requests,[])

    def test_docker_start_and_resume_require_distinct_states_and_verify_correct_transition(self):
        for action in ('start_service','resume_service'):
            state = container_state(action)
            posts = []
            def handle(request):
                if request.method == 'POST':
                    posts.append(request.url.path)
                    state['State'].update(Running=True,Paused=False,Status='running',Health={'Status':'healthy'})
                    if action == 'start_service':
                        state['State']['StartedAt'] = 'new'
                    return httpx.Response(204)
                return httpx.Response(200,json=state)
            executor = DockerLifecycleHTTPExecutor(transport=httpx.MockTransport(handle))
            target = {'url':'http://127.0.0.1:2375','container_id':'a'*64}
            before = executor.preflight(target,action,{})
            self.assertNotIn('must-not-leak',str(before))
            executor.execute_checked(target,action,{},before)
            self.assertTrue(executor.verify(target,action,{},before)['passed'])
            self.assertEqual(posts,['/containers/'+'a'*64+('/start' if action == 'start_service' else '/unpause')])
            if action == 'resume_service':
                state['State']['StartedAt'] = 'unexpected-restart'
                self.assertFalse(executor.verify(target,action,{},before)['passed'])
            with self.assertRaises(ProviderError):
                executor.execute(target,action,{})
            self.assertEqual(len(posts),1)

    def test_docker_cli_has_fixed_arguments_and_no_shell(self):
        for action,command in (('start_service','start'),('resume_service','unpause')):
            state = container_state(action)
            def reply(args,**kwargs):
                return CompletedProcess(args,0,json.dumps([state]) if 'inspect' in args else '', '')
            with patch('repairs.executors.shutil.which',return_value='docker.exe'),patch(
                    'repairs.executors.subprocess.run',side_effect=reply) as run:
                executor = DockerLifecycleCLIExecutor()
                executor.execute({'container_id':'a'*64,'context':'desktop-linux'},action,{})
                self.assertEqual(run.call_args.args[0],['docker.exe','--context','desktop-linux',command,'a'*64])
                self.assertFalse(run.call_args.kwargs['shell'])

    def test_configurations_require_correct_executor_identity_and_reload_version(self):
        base = json.loads(Path('examples/services.example.json').read_text(encoding='utf-8'))
        target = deepcopy(base['services'][0]['repairs']['api'])
        for executor,actions,extra in (
            ('docker_lifecycle_http',['start_service','resume_service'],{}),
            ('application_http',['reload_config'],{'control_target':'fixed-target','control_scope':'scope','desired_config_version':'v2'}),
            ('application_http',['resume_consumer'],{'control_target':'fixed-target','control_scope':'scope'}),
            ('application_http',['refresh_cache'],{'control_target':'fixed-target','control_scope':'scope'})):
            row = {**target,'executor':executor,'actions':actions,**extra}
            if executor == 'application_http':
                row.pop('container_id')
            base['services'][0]['repairs'] = {'api':row}
            self.assertFalse([i for i in validate_configuration(base) if i['level'] == 'error'])
        for changes in ({'control_scope':None},{'container_id':'a'*64},{'actions':['restart_service']},
                        {'actions':['reload_config'],'desired_config_version':None}):
            base['services'][0]['repairs']['api'] = {**row,**changes}
            self.assertTrue([i for i in validate_configuration(base) if i['level'] == 'error'])

    def test_diagnosis_candidates_accept_only_registered_actions_and_observation_evidence(self):
        for action in ACTIONS:
            selection = DiagnosisSelection.model_validate({'recommended_actions':[{
                'action':'登记维护操作','condition':'人工确认','risk':'业务影响','expected_result':'验证恢复',
                'requires_approval':True,'kind':'registered_repair',
                'repair':{'action':action,'target':'api','parameters':{},'evidence_ids':['EV_current']}}]})
            registry = SimpleNamespace(provider=SimpleNamespace(repair_capabilities=[{'action':action,'target':'api'}]),
                evidence={'EV_current':SimpleNamespace(kind='observation',locator='get_service_metrics/row')},references={})
            self.assertEqual(expand_diagnosis(selection,registry).recommended_actions[0].repair.action,action)


class MaintenanceLifecycleTests(unittest.TestCase):
    def prepare(self, action):
        f = remediation.RemediationTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        state = app_state(action) if action in ApplicationHTTPExecutor.actions else container_state(action)
        posts = []
        def handle(request):
            if request.method == 'POST':
                posts.append(request)
                if action in ApplicationHTTPExecutor.actions:
                    body = json.loads(request.content)
                    state.update(revision=8,healthy=True,loaded_config_version='v2',consumer_paused=False,
                                 cache_ready=True,cache_generation=2,last_operation_id=body['operation_id'])
                    return httpx.Response(200,json={'accepted':True,'operation_id':body['operation_id'],
                        'target_id':state['target_id'],'scope':state['scope']})
                state['State'].update(Running=True,Paused=False,Health={'Status':'healthy'})
                if action == 'start_service':
                    state['State']['StartedAt'] = 'new'
                return httpx.Response(204)
            return httpx.Response(200,json=state)
        transport = httpx.MockTransport(handle)
        executor = ApplicationHTTPExecutor(transport=transport) if action in ApplicationHTTPExecutor.actions else DockerLifecycleHTTPExecutor(transport=transport)
        target = {'executor':executor.name,'actions':[action],'url':'http://127.0.0.1:8001',
            'container_id':'a'*64,'control_target':'fixed-target','control_scope':'tenant-service-staging',
            'desired_config_version':'v2','symptom_checks':[{'metric':'queue_depth','operator':'lte','threshold':0}]}
        binding = ServiceBinding(**{**f.binding.__dict__,'repairs':{'api':target},'observations':{
            'prometheus':{'url':'http://127.0.0.1:9090'},'metrics':{'queue_depth':{
                'query':'queue_depth','freshness_query':'time()-timestamp(queue_depth)','unit':'jobs'}}}})
        f.service.services = ServiceRegistry([binding])
        f.service.repairs.executors = {executor.name:executor}
        # Emulate a stored reviewed live result; every external call is still stubbed.
        f.store.incidents[f.incident['id']]['demo_case_id'] = None
        f.store.runs[f.payload['run_id']]['config']['execution']['execution_mode'] = 'live'
        f.payload.update(action=action,approval_details={'condition':'核对计划维护及业务前提','risk':'业务写入与积压负载',
                                                        'expected_result':'平台状态和业务指标均恢复'})
        f.service.repairs.symptom_verifier = MagicMock()
        f.service.repairs.symptom_verifier.verify.side_effect = [{'passed':False},{'passed':True}]
        return f, posts

    def test_all_new_actions_require_approval_and_execute_once_through_actual_service(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                f, posts = self.prepare(action)
                plan = f.plan()
                with self.assertRaises(IncidentError):
                    f.execute(plan)
                self.assertEqual(posts,[])
                f.approve(plan)
                result = f.execute(plan)
                self.assertEqual(result['status'],'verified')
                self.assertEqual(f.execute(plan)['status'],'verified')
                self.assertEqual(len(posts),1)
                self.assertEqual(f.service.repairs.symptom_verifier.verify.call_count,2)

    def test_new_actions_reject_missing_approval_details(self):
        f, posts = self.prepare('reload_config')
        f.payload.pop('approval_details')
        with self.assertRaisesRegex(IncidentError,'REPAIR_RECOMMENDATION_INCOMPLETE'):
            f.plan()
        self.assertEqual(posts,[])

    def test_reload_version_is_visible_and_bound_into_approved_plan(self):
        f, posts = self.prepare('reload_config')
        capability = f.service.repairs.incident_capabilities(f.incident['id'],f.owner)[0]
        self.assertEqual(capability['configured_parameters'],{'config_version':'v2'})
        self.assertEqual(f.plan()['configured_parameters'],{'config_version':'v2'})
        self.assertEqual(posts,[])

    def test_platform_success_without_business_recovery_is_manual(self):
        f, posts = self.prepare('resume_consumer')
        f.service.repairs.symptom_verifier.verify.side_effect = None
        f.service.repairs.symptom_verifier.verify.return_value = {'passed':False}
        plan = f.plan(); f.approve(plan)
        with patch('repairs.service.time.sleep'):
            self.assertEqual(f.execute(plan)['status'],'manual_required')
        self.assertEqual(len(posts),1)

    def test_application_timeout_keeps_operation_id_for_manual_lookup_without_retry(self):
        f, posts = self.prepare('reload_config')
        executor = f.service.repairs.executors['application_http']
        original = executor.transport
        def handle(request):
            if request.method == 'POST':
                posts.append(request)
                raise httpx.ReadTimeout('lost response')
            return original.handle_request(request)
        executor.transport = httpx.MockTransport(handle)
        plan = f.plan(); f.approve(plan)
        result = f.execute(plan)
        self.assertEqual(result['status'],'manual_required')
        started = next(e for e in result['events'] if e['type'] == 'action_started')
        self.assertEqual(started['operation_id'],json.loads(posts[0].content)['operation_id'])
        f.execute(plan)
        self.assertEqual(len(posts),1)
