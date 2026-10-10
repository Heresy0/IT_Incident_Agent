import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from api.auth import Principal
from evidence.contracts import TOOL_ARGS
from evals.showcase_live import suite
from evals.showcase_live.provider import ShowcaseProvider
from scripts import compare_independent
from scripts import compare_showcase_live
from evals.showcase_revision.provider import ShowcaseRevisionProvider


class ShowcaseLiveTests(unittest.TestCase):
    def test_runtime_reads_observations_only_and_filters_log_category(self):
        original = Path.read_text

        def guarded(path, *args, **kwargs):
            if 'answers' in path.parts:
                raise AssertionError('Runtime accessed evaluation answer')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', guarded):
            provider = ShowcaseProvider('show_002', Principal('synthetic_demo', 'cli_reader'))
            ticket = provider.ticket()
            args = TOOL_ARGS['get_service_logs'].model_validate({
                'start': ticket.scope.start, 'end': ticket.scope.end, 'category': 'dependency'})
            rows, truncated = provider.query('get_service_logs', args, ticket.scope)
            self.assertFalse(truncated)
            self.assertEqual({r['error_code'] for r in rows}, {'PROBE_OK', 'STORAGE_PERMISSION_DENIED'})
            self.assertTrue(all('search_text' not in r for r in rows))

    def test_live_requires_explicit_execution_cases_and_budgets(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (['--live'], ['--live', '--cases', 'show_001', '--execute-live']):
                with self.assertRaises(SystemExit) as raised:
                    compare_independent.parse_args(argv, suite=suite)
                self.assertEqual(raised.exception.code, 2)
        self.assertEqual(suite.verify_freeze()['dataset_version'], 'curated-showcase-v1')

    def test_plan_does_not_capture_or_load_credentials(self):
        with patch.object(compare_independent, 'capture') as capture, \
                patch('dotenv.load_dotenv', side_effect=AssertionError('Plan loaded credentials')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compare_showcase_live.main([]), 0)
        capture.assert_not_called()

    def test_revision_preserves_the_same_observations(self):
        principal = Principal('synthetic_demo', 'cli_reader')
        original = ShowcaseProvider('show_001', principal)._data
        revised = ShowcaseRevisionProvider('show_101', principal)._data
        for kind in ('metrics', 'logs', 'changes', 'owners', 'runbooks', 'incidents'):
            def observations(rows):
                return [{key: value for key, value in row.items() if key not in {'data_version', 'search_text'}}
                        for row in rows]
            self.assertEqual(observations(original[kind]), observations(revised[kind]))


if __name__ == '__main__':
    unittest.main()
