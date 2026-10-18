"""Public demo boundaries; scripted success is not a model quality score."""
import unittest
from scripts.demo_interview import project_interview, render_page


class InterviewDemoExportTests(unittest.TestCase):
    def test_export_rejects_live_or_private_sources(self):
        for report in ({'execution_mode': 'live'},
            {'execution_mode': 'scripted_control_only', 'data_source': 'private_loki'},
            {'execution_mode': 'scripted_control_only', 'data_source': 'targeted_synthetic_fixture',
             'evidence': [{'provider': 'private_loki'}]}):
            with self.subTest(report=report), self.assertRaises(ValueError):
                project_interview(report)

    def test_html_escapes_source_content_and_labels_scripted_boundary(self):
        injection = '<script>alert(1)</script>'
        demo = {'key': 'history', 'name': injection, 'question': injection, 'capability': injection,
            'watch': injection, 'role_steps': {'reviewer': 1}, 'status': 'completed', 'review_status': 'passed',
            'model_steps': 1, 'tool_attempts': 0, 'rework_rounds': 0, 'drafts': [], 'reviews': [], 'events': [],
            'output': {'hypotheses': [{'status': 'supported', 'cause': injection}]}}
        page = render_page({'implementation_sha256_prefix': 'demo', 'demonstrations': [demo]})
        self.assertNotIn(injection, page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('所有选查、原因候选与复核意见均按演示脚本预设', page)
        self.assertIn('本页不提供准确率或胜率结论', page)


if __name__ == '__main__':
    unittest.main()
