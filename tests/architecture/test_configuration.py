"""Configuration precedence and shared CLI resolution; no network requests."""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from config.incident import IncidentConfig, DEFAULT_MODEL
from config.settings import AppSettings


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory)
        self.enterContext(patch('config.incident.PROJECT_ROOT', self.root))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        (self.root / 'config.json').write_text(json.dumps({
            'model': 'json-model', 'api_key': 'json-canary', 'postgres_dsn': 'json-dsn'}), encoding='utf-8')

    def test_env_overrides_json_and_local_file_is_ignored(self):
        (self.root / '.env').write_text('MODEL=env-model\nDASHSCOPE_API_KEY=env-canary\nPOSTGRES_DSN=env-dsn\n', encoding='utf-8')
        (self.root / '.env.local').write_text('MODEL=obsolete-model\nDASHSCOPE_API_KEY=obsolete-canary\n', encoding='utf-8')
        config = IncidentConfig.from_file()
        self.assertEqual((config.model, config.api_key, config.postgres_dsn), ('env-model', 'env-canary', 'env-dsn'))
        self.assertNotIn('canary', repr(config))
        self.assertNotIn('env-dsn', repr(config))

    def test_process_overrides_env_and_config_path_is_shared(self):
        alternate = self.root / 'alternate.json'
        alternate.write_text('{"model":"alternate-model"}', encoding='utf-8')
        (self.root / '.env').write_text(f'CONFIG_PATH={alternate.as_posix()}\nMODEL=file-model\n', encoding='utf-8')
        os.environ['MODEL'] = 'process-model'
        self.assertEqual(IncidentConfig.from_file().model, 'process-model')
        del os.environ['MODEL']
        (self.root / '.env').write_text(f'CONFIG_PATH={alternate.as_posix()}\n', encoding='utf-8')
        self.assertEqual(IncidentConfig.from_file().model, 'alternate-model')

    def test_missing_files_use_defaults(self):
        (self.root / 'config.json').unlink()
        self.assertEqual(IncidentConfig.from_file().model, DEFAULT_MODEL)

    def test_compatible_endpoint_key_and_provider_options(self):
        (self.root / '.env').write_text('MODEL_API_KEY=generic-canary\nDASHSCOPE_API_KEY=old-canary\nMODEL_BASE_URL=https://example.invalid/v1\nMODEL_EXTRA_BODY={"enable_thinking":false}\n', encoding='utf-8')
        config = IncidentConfig.from_file()
        self.assertEqual(config.api_key, 'generic-canary')
        self.assertEqual(config.base_url, 'https://example.invalid/v1')
        self.assertEqual(config.extra_body, {'enable_thinking': False})
        self.assertNotIn('canary', repr(config))
        os.environ['MODEL_EXTRA_BODY'] = '[]'
        with self.assertRaises(ValueError):
            IncidentConfig.from_file()


    def test_app_settings_use_the_same_env_and_process_precedence(self):
        env_file = self.root / '.env'
        env_file.write_text('PORT=8123\n', encoding='utf-8')
        (self.root / '.env.local').write_text('PORT=8999\n', encoding='utf-8')
        self.assertEqual(AppSettings(_env_file=env_file).port, 8123)
        os.environ['PORT'] = '8456'
        self.assertEqual(AppSettings(_env_file=env_file).port, 8456)


if __name__ == '__main__':
    unittest.main()
