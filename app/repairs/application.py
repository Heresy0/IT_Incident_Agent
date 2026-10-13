"""Fixed application control protocol, scoped targets and conditional writes."""
import re
from uuid import uuid4
from providers.fixtures import ProviderError
from providers.http import request_json


class ApplicationHTTPExecutor:
    name = 'application_http'
    live = True
    operation_id_required = True
    actions = frozenset({'reload_config', 'resume_consumer', 'refresh_cache'})
    verification = '本次操作编号匹配、状态版本递增、目标状态生效且healthy；另需业务症状指标通过。'
    kinds = {'reload_config':'config', 'resume_consumer':'consumer', 'refresh_cache':'cache'}

    def __init__(self, *, transport=None):
        self.transport = transport

    def _read(self, target, action, parameters):
        if action not in self.actions or parameters:
            raise ProviderError('ACTION_UNSUPPORTED')
        identity, scope = target.get('control_target', ''), target.get('control_scope', '')
        if (not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', identity)
                or not isinstance(scope, str) or not scope.strip()):
            raise ProviderError('CONTROL_TARGET_INVALID')
        state = request_json(target['url'], 'GET', '/ops/targets/'+identity,
                             authorization_env=target.get('authorization_env'), transport=self.transport)
        if not isinstance(state, dict):
            raise ProviderError('CONTROL_STATE_INVALID')
        if state.get('target_id') != identity or state.get('scope') != scope or state.get('kind') != self.kinds[action]:
            raise ProviderError('CONTROL_SCOPE_MISMATCH')
        if (type(state.get('revision')) is not int or state['revision'] < 1
                or type(state.get('healthy')) is not bool or not isinstance(state.get('supported_actions'), list)
                or action not in state['supported_actions']):
            raise ProviderError('CONTROL_STATE_INVALID')
        return state

    def preflight(self, target, action, parameters):
        state = self._read(target, action, parameters)
        if action == 'reload_config':
            desired = target.get('desired_config_version')
            if (not isinstance(desired, str) or not desired.strip()
                    or state.get('available_config_version') != desired or state.get('config_valid') is not True
                    or not isinstance(state.get('loaded_config_version'), str)
                    or not 1 <= len(state['loaded_config_version']) <= 80):
                raise ProviderError('CONFIG_RELOAD_NOT_READY')
            if state.get('loaded_config_version') == desired:
                raise ProviderError('TARGET_ALREADY_APPLIED')
        elif action == 'resume_consumer':
            if state.get('consumer_paused') is not True or state.get('resume_allowed') is not True:
                raise ProviderError('CONSUMER_RESUME_NOT_READY')
        elif (state.get('refresh_allowed') is not True or type(state.get('cache_generation')) is not int
                or state['cache_generation'] < 0):
            raise ProviderError('CACHE_REFRESH_NOT_READY')
        field = {'reload_config':'loaded_config_version', 'resume_consumer':'consumer_paused',
                 'refresh_cache':'cache_generation'}[action]
        return {'revision':state['revision'], field:state[field]}

    def execute(self, target, action, parameters):
        return self.execute_checked(target, action, parameters, self.preflight(target, action, parameters))

    def execute_checked(self, target, action, parameters, before):
        expected = {key:value for key,value in before.items() if key != 'operation_id'}
        if self.preflight(target, action, parameters) != expected:
            raise ProviderError('CONTROL_STATE_CHANGED')
        operation = before.get('operation_id') or str(uuid4())
        response = request_json(target['url'], 'POST', '/ops/targets/'+target['control_target']+'/actions/'+action,
            authorization_env=target.get('authorization_env'), transport=self.transport,
            json_body={'operation_id':operation, 'expected_revision':before['revision'],
                       **({'config_version':target['desired_config_version']} if action == 'reload_config' else {})})
        if (not isinstance(response, dict) or response.get('accepted') is not True
                or response.get('operation_id') != operation or response.get('target_id') != target['control_target']
                or response.get('scope') != target['control_scope']):
            raise ProviderError('CONTROL_RECEIPT_INVALID')
        return {'accepted':True, 'operation_id':operation}

    def verify(self, target, action, parameters, before):
        state = self._read(target, action, parameters)
        changed = (state.get('loaded_config_version') == target.get('desired_config_version') if action == 'reload_config'
                   else state.get('consumer_paused') is False if action == 'resume_consumer'
                   else state.get('cache_ready') is True and type(state.get('cache_generation')) is int
                        and state['cache_generation'] > before['cache_generation'])
        return {'passed':bool(before.get('operation_id')) and state.get('last_operation_id') == before['operation_id']
                and state['revision'] > before['revision'] and state['healthy'] is True and changed,
                'revision':state['revision'], 'healthy':state['healthy'], 'target_state_verified':changed}
