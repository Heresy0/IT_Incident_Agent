"""Offline onboarding, safe diagnostics and compatibility with existing bindings."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from providers.configuration import read_configuration, validate_configuration, ServiceConfigurationError
from providers.registry import ServiceBinding, ServiceRegistry
from scripts.register_service import main, ROOT


class ServiceRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'services.json'
        self.data = read_configuration(ROOT / 'examples/services.example.json')

    def errors(self, data=None):
        return [item for item in validate_configuration(self.data if data is None else data)
                if item['level'] == 'error']

    def write(self, data=None):
        self.path.write_text(json.dumps(self.data if data is None else data), encoding='utf-8')
        return self.path

    def cli(self, args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(args)
        return code, output.getvalue()

    def test_public_examples_pass_with_explicit_gaps(self):
        for name in ('services.example.json', 'services.enterprise.example.json'):
            data = read_configuration(ROOT / 'examples' / name)
            self.assertFalse(self.errors(data))
            self.assertTrue(any(i['code'] == 'CHANNEL_EMPTY' for i in validate_configuration(data)))

    def test_generic_template_is_separate_scoped_and_read_only(self):
        code, output = self.cli(['template', '--service', 'my-api', '--tenant', 'my-team',
            '--user', 'alice', '--user', 'bob', '--environment', 'production', '--version', 'v2',
            '--owner-team', 'actual-team', '--output', str(self.path)])
        self.assertEqual(code, 0, output)
        row = read_configuration(self.path)['services'][0]
        self.assertEqual(row['users'], ['alice', 'bob'])
        self.assertEqual((row['tenant_id'], row['service'], row['environment'], row['version']),
                         ('my-team', 'my-api', 'production', 'v2'))
        self.assertEqual(row['repairs'], {})
        self.assertEqual(row['observations']['owners'][0]['team'], 'actual-team')
        self.assertIn('my-api', row['observations']['log_queries']['resource'])
        self.assertNotIn('staging', row['observations']['log_queries']['resource'])
        self.assertFalse(self.errors({'services': [row]}))

    def test_enterprise_template_preserves_known_queries_without_enabling_repairs(self):
        code, output = self.cli(['template', '--profile', 'enterprise', '--service', 'enterprise-indexing',
            '--tenant', 'team', '--user', 'alice', '--version', 'local-v1', '--output', str(self.path)])
        self.assertEqual(code, 0, output)
        row = read_configuration(self.path)['services'][0]
        self.assertEqual(row['repairs'], {})
        self.assertEqual(row['observations']['owners'], [])
        self.assertIn('enterprise_indexing_workers_active', row['observations']['metrics']['workers_active']['query'])

    def test_existing_output_is_never_overwritten(self):
        self.path.write_text('existing-secret', encoding='utf-8')
        code, output = self.cli(['template', '--service', 'api', '--tenant', 'team', '--user', 'alice',
                                '--version', 'v1', '--output', str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_text(), 'existing-secret')
        self.assertNotIn('existing-secret', output)

    def test_check_does_not_connect_or_execute_and_does_not_write(self):
        path = self.write()
        original = path.read_bytes()
        with patch('providers.http.httpx.Client', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.run', side_effect=AssertionError('execution forbidden')):
            code, output = self.cli(['check', '--file', str(path), '--json'])
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertFalse(result['network_checked'])
        self.assertFalse(result['repairs_performed'])
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(path.read_bytes(), original)

    def test_runtime_loader_uses_same_checks_and_preserves_raw_fingerprint(self):
        row = deepcopy(self.data['services'][0])
        row['users'] = tuple(row['users'])
        row['dependencies'] = tuple(row.get('dependencies', ()))
        original = ServiceBinding(**row)
        loaded = ServiceRegistry.from_file(self.write())._bindings[0]
        self.assertEqual(loaded.fingerprint, original.fingerprint)
        del self.data['services'][0]['observations']['metrics']['queue_depth']['unit']
        self.write()
        with self.assertRaises(ServiceConfigurationError) as raised:
            ServiceRegistry.from_file(self.path)
        self.assertIn('$.services[0].observations.metrics[1].unit', str(raised.exception))

    def test_sensitive_input_and_unknown_keys_are_absent_from_diagnostics(self):
        secret = 'private-secret-do-not-print'
        row = self.data['services'][0]
        row[secret] = secret
        row['observations']['prometheus']['url'] = 'https://user:' + secret + '@example.com'
        row['observations']['metrics'][secret] = {'query': secret, 'unit': secret}
        row['repairs'][secret] = {'executor': secret}
        code, output = self.cli(['check', '--file', str(self.write()), '--json'])
        self.assertEqual(code, 1)
        self.assertNotIn(secret, output)
        self.assertNotIn('example.com', output)
        self.assertIn('ENDPOINT_INVALID', output)
        with self.assertRaises(ServiceConfigurationError) as raised:
            ServiceRegistry.from_file(self.path)
        self.assertNotIn(secret, str(raised.exception))

    def test_endpoint_policy_matches_runtime_without_requests(self):
        for url, code in (('http://remote.example', 'ENDPOINT_TLS_REQUIRED'),
                          ('https://host.example/api', 'ENDPOINT_INVALID'),
                          ('http://127.0.0.1:bad', 'ENDPOINT_INVALID')):
            self.data['services'][0]['observations']['prometheus']['url'] = url
            self.assertIn(code, {i['code'] for i in self.errors()})
        self.data['services'][0]['observations']['prometheus']['url'] = 'http://127.0.0.1:9090'
        self.assertFalse(self.errors())

    def test_duplicate_services_users_and_unsupported_environment(self):
        self.data['services'].append(deepcopy(self.data['services'][0]))
        self.assertIn('DUPLICATE_SERVICE', {i['code'] for i in self.errors()})
        self.data['services'][1]['tenant_id'] = 'another-tenant'
        self.assertFalse(self.errors())
        self.data['services'][0]['users'].append(self.data['services'][0]['users'][0])
        self.assertIn('DUPLICATE_USERS', {i['code'] for i in self.errors()})
        self.data['services'][1]['environment'] = 'unsupported'
        self.assertIn('literal_error', {i['code'] for i in self.errors()})

    def test_queries_require_corresponding_fixed_source(self):
        del self.data['services'][0]['observations']['prometheus']
        del self.data['services'][0]['observations']['loki']
        paths = {i['path'] for i in self.errors()}
        self.assertIn('$.services[0].observations.prometheus', paths)
        self.assertIn('$.services[0].observations.loki', paths)

    def test_repair_validation_reuses_symptom_rules_and_limits(self):
        target = self.data['services'][0]['repairs']['api']
        target['symptom_checks'][0]['metric'] = 'unknown_metric'
        self.assertIn('METRIC_NOT_REGISTERED', {i['code'] for i in self.errors()})
        target['symptom_checks'][0]['metric'] = 'queue_depth'
        del self.data['services'][0]['observations']['metrics']['queue_depth']['freshness_query']
        self.assertIn('SYMPTOM_FRESHNESS_QUERY_REQUIRED', {i['code'] for i in self.errors()})
        self.data['services'][0]['observations']['metrics']['queue_depth']['freshness_query'] = 'time()-timestamp(queue)'
        target['symptom_checks'].append(deepcopy(target['symptom_checks'][0]))
        self.assertIn('SYMPTOM_CHECK_CONFIG_INVALID', {i['code'] for i in self.errors()})
        target['verification_attempts'] = 7
        target['actions'] = ['scale_service']
        self.assertTrue(self.errors())

    def test_cli_repair_requires_fixed_context_and_container(self):
        target = self.data['services'][0]['repairs']['api']
        target.update(executor='docker_cli', container_id='a' * 64)
        self.assertIn('DOCKER_CONTEXT_REQUIRED', {i['code'] for i in self.errors()})
        target['context'] = 'desktop-linux'
        self.assertFalse(self.errors())
        target['container_id'] = 'worker-name'
        self.assertTrue(self.errors())

    def test_metadata_gaps_are_specific_and_log_mapping_is_checked(self):
        observations = self.data['services'][0]['observations']
        observations['owners'] = [{'timestamp': 'bad-secret', 'team': '', 'aliases': []}]
        paths = {i['path'] for i in self.errors()}
        self.assertIn('$.services[0].observations.owners[0].timestamp', paths)
        self.assertIn('$.services[0].observations.owners[0].team', paths)
        observations['log_mapping'] = {'event_only': True, 'event_allowlist': []}
        self.assertIn('$.services[0].observations.log_mapping', {i['path'] for i in self.errors()})

    def test_bom_supported_and_malformed_duplicate_or_nonfinite_json_rejected_safely(self):
        self.path.write_bytes(b'\xef\xbb\xbf' + json.dumps(self.data).encode())
        self.assertFalse(self.errors(read_configuration(self.path)))
        for raw in ('private-secret {', '{"services":[],"services":[]}', '{"services":NaN}'):
            self.path.write_text(raw, encoding='utf-8')
            code, output = self.cli(['check', '--file', str(self.path), '--json'])
            self.assertEqual(code, 1)
            self.assertIn('CONFIG_JSON_INVALID', output)
            self.assertNotIn(raw, output)
        self.assertTrue(self.errors([]))

    def test_empty_registry_and_missing_file_have_safe_explicit_results(self):
        issues = validate_configuration({'services': []})
        self.assertEqual(issues[0]['code'], 'NO_SERVICES')
        self.assertEqual(issues[0]['level'], 'warning')
        code, output = self.cli(['check', '--file', str(self.path), '--json'])
        self.assertEqual(code, 1)
        self.assertIn('CONFIG_UNREADABLE', output)
        self.assertNotIn(str(self.path), output)
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(ServiceRegistry.from_environment()._bindings)

    def test_bad_cli_choice_does_not_echo_values(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            main(['template', '--profile', 'secret-not-a-profile'])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn('secret-not-a-profile', output.getvalue())
