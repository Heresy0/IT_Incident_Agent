"""The opt-in closeout helper must never initialize a daily database by default."""
from contextlib import redirect_stdout, redirect_stderr
import io
import os
import unittest
from unittest.mock import patch
from scripts.closeout_demo_server import main


class CloseoutDemoGuardTests(unittest.TestCase):
    def test_default_plan_never_opens_database_or_starts_server(self):
        with patch('psycopg.connect') as connect, patch('uvicorn.run') as serve, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([]), 0)
        connect.assert_not_called(); serve.assert_not_called()
        self.assertIn('"plan_only": true', out.getvalue())

    def test_business_database_refused_before_pool_initialization(self):
        with patch.dict(os.environ, {'CLOSEOUT_GUARD_DSN': 'postgresql://user:secret-canary@127.0.0.1/business'}), \
                patch('storage.runs.PostgresRunStore') as store, redirect_stderr(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as stopped:
                main(['--serve', '--dsn-env', 'CLOSEOUT_GUARD_DSN'])
        self.assertEqual(stopped.exception.code, 2)
        store.assert_not_called()
        self.assertNotIn('secret-canary', out.getvalue())

    def test_invalid_dsn_refused_without_printing_its_contents(self):
        with patch.dict(os.environ, {'CLOSEOUT_GUARD_DSN': 'invalid-secret-canary'}), redirect_stderr(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as stopped:
                main(['--serve', '--dsn-env', 'CLOSEOUT_GUARD_DSN'])
        self.assertEqual(stopped.exception.code, 2)
        self.assertNotIn('secret-canary', out.getvalue())
