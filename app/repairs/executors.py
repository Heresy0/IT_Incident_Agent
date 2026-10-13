"""Platform operations are explicitly registered; no shell or automatic retries."""
import re
import json
import shutil
import subprocess
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


class DockerCLIExecutor(DockerExecutor):
    """Use the installed Docker CLI and its current context, without opening a TCP socket."""
    name = 'docker_cli'

    def _request(self, target, method, suffix, params=None):
        container = target.get('container_id', '')
        if not re.fullmatch(r'[0-9a-f]{64}', container):
            raise ProviderError('CONTAINER_ID_INVALID')
        executable = shutil.which('docker')
        if not executable:
            raise ProviderError('DOCKER_CLI_UNAVAILABLE')
        context = target.get('context', '')
        if not isinstance(context, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', context):
            raise ProviderError('DOCKER_CONTEXT_REQUIRED')
        command = [executable, '--context', context]
        if method == 'GET' and suffix == '/json':
            arguments = [*command, 'inspect', '--type', 'container', container]
        elif method == 'POST' and suffix == '/restart':
            arguments = [*command, 'restart', '--time', '3', container]
        elif method == 'POST' and suffix in {'/start', '/unpause'}:
            arguments = [*command, suffix[1:], container]
        else:
            raise ProviderError('ACTION_UNSUPPORTED')
        try:
            result = subprocess.run(arguments, shell=False, capture_output=True, text=True, encoding='utf-8',
                timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise ProviderError('TIMEOUT', True) from None
        except OSError:
            raise ProviderError('DOCKER_CLI_UNAVAILABLE') from None
        if result.returncode:
            raise ProviderError('DOCKER_OPERATION_FAILED')
        if len(result.stdout.encode()) > 1_000_000:
            raise ProviderError('PROVIDER_RESPONSE_TOO_LARGE')
        if method == 'POST':
            return {}
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError()
            return rows[0]
        except (ValueError, TypeError):
            raise ProviderError('PROVIDER_FORMAT_INVALID') from None


class DockerLifecycleHTTPExecutor(DockerExecutor):
    """Start or unpause one registered container, with state-specific checks."""
    name = 'docker_lifecycle_http'
    actions = frozenset({'start_service', 'resume_service'})
    verification = '运行且未暂停、HEALTHCHECK为healthy；启动时间符合动作预期，且业务症状指标通过。'

    def preflight(self, target, action, parameters):
        if action not in self.actions or parameters:
            raise ProviderError('ACTION_UNSUPPORTED')
        data = self._request(target, 'GET', '/json')
        healthcheck = data.get('Config', {}).get('Healthcheck', {}).get('Test', [])
        if data.get('Id') != target['container_id'] or not healthcheck or healthcheck[0] == 'NONE':
            raise ProviderError('TARGET_HEALTHCHECK_REQUIRED')
        state = data.get('State', {})
        if state.get('Restarting') or state.get('Dead') or state.get('Status') == 'removing':
            raise ProviderError('TARGET_BUSY')
        if action == 'start_service':
            if (state.get('Running') is not False or state.get('Paused') is not False
                    or state.get('Status') not in {'created', 'exited'}):
                raise ProviderError('TARGET_NOT_STOPPED')
        elif (state.get('Running') is not True or state.get('Paused') is not True or not state.get('StartedAt')):
            raise ProviderError('TARGET_NOT_PAUSED')
        return {'started_at':state.get('StartedAt'), 'running':state.get('Running'),
                'paused':state.get('Paused'), 'status':state.get('Status')}

    def execute(self, target, action, parameters):
        return self.execute_checked(target, action, parameters, self.preflight(target, action, parameters))

    def execute_checked(self, target, action, parameters, before):
        if self.preflight(target, action, parameters) != before:
            raise ProviderError('CONTROL_STATE_CHANGED')
        self._request(target, 'POST', '/start' if action == 'start_service' else '/unpause')
        return {'accepted':True}

    def verify(self, target, action, parameters, before):
        if action not in self.actions or parameters:
            raise ProviderError('ACTION_UNSUPPORTED')
        data = self._request(target, 'GET', '/json')
        state = data.get('State', {})
        started = state.get('StartedAt')
        transition = (bool(started) and started != before.get('started_at') if action == 'start_service'
                      else bool(started) and started == before.get('started_at'))
        return {'passed':data.get('Id') == target['container_id'] and state.get('Running') is True
                and state.get('Paused') is False and not state.get('Restarting') and not state.get('Dead')
                and state.get('Health', {}).get('Status') == 'healthy' and transition,
                'running':state.get('Running'), 'paused':state.get('Paused'),
                'health':state.get('Health', {}).get('Status', 'unknown')}


class DockerLifecycleCLIExecutor(DockerLifecycleHTTPExecutor):
    name = 'docker_lifecycle_cli'
    _request = DockerCLIExecutor._request
