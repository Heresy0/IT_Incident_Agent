"""Platform operations are explicitly registered; no shell or automatic retries."""
import re
from providers.http import request_json
from providers.fixtures import ProviderError


class DockerExecutor:
    name = 'docker_http'
    live = True
    actions = frozenset({'restart_service'})
    verification = '容器启动时间改变，并且Docker HEALTHCHECK返回healthy；检查次数和间隔见方案中的验证预算。'

    def __init__(self, *, transport=None):
        self.transport = transport

    def _request(self, target, method, suffix, params=None):
        container = target.get('container_id', '')
        if not re.fullmatch(r'[0-9a-f]{64}', container):
            raise ProviderError('CONTAINER_ID_INVALID')
        return request_json(target['url'], method, '/containers/' + container + suffix,
            params=params, authorization_env=target.get('authorization_env'), transport=self.transport)

    def preflight(self, target, action, parameters):
        data = self._request(target, 'GET', '/json')
        healthcheck = data.get('Config', {}).get('Healthcheck', {}).get('Test', [])
        if data.get('Id') != target['container_id'] or not healthcheck or healthcheck[0] == 'NONE':
            raise ProviderError('TARGET_HEALTHCHECK_REQUIRED')
        state = data.get('State', {})
        if state.get('Paused') or state.get('Restarting'):
            raise ProviderError('TARGET_BUSY')
        return {'started_at': state.get('StartedAt'), 'running': bool(state.get('Running'))}

    def execute(self, target, action, parameters):
        if action not in self.actions:
            raise ProviderError('ACTION_UNSUPPORTED')
        self._request(target, 'POST', '/restart', {'t': 3})
        return {'accepted': True}

    def verify(self, target, action, parameters, before):
        data = self._request(target, 'GET', '/json')
        state = data.get('State', {})
        return {'passed': data.get('Id') == target['container_id'] and bool(state.get('Running'))
                and state.get('Health', {}).get('Status') == 'healthy'
                and bool(state.get('StartedAt')) and state['StartedAt'] != before['started_at'],
                'running': bool(state.get('Running')), 'health': state.get('Health', {}).get('Status', 'unknown')}
