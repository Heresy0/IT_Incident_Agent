"""Protect the public showcase boundary, rather than retesting graph internals."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.showcase_incident import project_demo, render_page
from scripts.prepare_showcase_review import project_history


class ShowcaseExportTests(unittest.TestCase):
    def test_demo_export_refuses_live_sources_and_omits_unselected_fields(self):
        report = {'execution_mode': 'live', 'data_source': 'registered_private'}
        with self.assertRaises(ValueError):
            project_demo(report)
        report = {'execution_mode': 'scripted_control_only', 'data_source': 'synthetic_fixture',
                  'run_summary': {'model_calls': 1, 'tool_calls': 0, 'limits': {}, 'termination_reason': 'NO_EVIDENCE'},
                  'run_id': 'r1', 'incident_id': 'case_001', 'status': 'partial', 'review_status': 'not_performed',
                  'rework_rounds': 0, 'repairs': 0, 'evidence': [{'payload': 'PRIVATE_CANARY'}],
                  'task_results': [], 'output': {'hypotheses': [], 'missing_information': []},
                  'events': [{'seq': 1, 'type': 'phase', 'node': 'supervisor', 'raw_response': 'PRIVATE_CANARY'}],
                  'credentials': 'PRIVATE_CANARY'}
        self.assertNotIn('PRIVATE_CANARY', json.dumps(project_demo(report)))

    def test_html_escapes_values_instead_of_executing_report_content(self):
        value = '<script>alert(1)</script>'
        demo = {'name': value, 'status': 'partial', 'review_status': 'not_performed',
                'model_calls': 1, 'tool_calls': 0, 'termination_reason': 'NO_EVIDENCE', 'missing_information': [value]}
        page = render_page({'demonstrations': [demo]})
        self.assertNotIn(value, page)
        self.assertIn('&lt;script&gt;', page)

    def test_history_projection_refuses_private_evidence(self):
        bundle = {'configuration': {'execution_mode': 'live'}}
        reports = {'r': {'report': {'data_source': 'synthetic_fixture',
                                   'evidence': [{'provider': 'private_loki', 'data_version': 'live'}]}}}
        with patch('scripts.prepare_showcase_review.evaluate', return_value=({}, reports)):
            with self.assertRaisesRegex(ValueError, 'synthetic fixture'):
                project_history(bundle, Path('.'))

    def test_assistant_review_cannot_impersonate_human_or_change_report_binding(self):
        bundle = {'configuration': {'execution_mode': 'live'}}
        reports = {'r': {'sha256': 'expected', 'report': {'data_source': 'synthetic_fixture', 'evidence': []}}}
        with patch('scripts.prepare_showcase_review.evaluate', return_value=({}, reports)):
            with self.assertRaisesRegex(ValueError, 'impersonate'):
                project_history(bundle, Path('.'), {'reviewer_kind': 'human', 'human_review_status': 'approved'})
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                project_history(bundle, Path('.'), {'reviewer_kind': 'assistant', 'human_review_status': 'pending',
                    'runs': [{'run_id': 'r', 'report_sha256': 'changed'}]})


if __name__ == '__main__':
    unittest.main()
