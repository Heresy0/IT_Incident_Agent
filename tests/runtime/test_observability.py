import logging
import unittest
from observability.telemetry import JsonFormatter, Metrics


class ObservabilityTests(unittest.TestCase):
    def test_sensitive_message_fragments_are_redacted(self):
        record = logging.LogRecord('incident.api', logging.ERROR, __file__, 1,
            'sk-syntheticcanary Bearer synthetic-token postgresql://user:synthetic-password@localhost/db', (), None)
        output = JsonFormatter().format(record)
        for secret in ('sk-syntheticcanary','synthetic-token','synthetic-password'):
            self.assertNotIn(secret, output)

    def test_metrics_aggregate_calls_without_identity_labels(self):
        metrics = Metrics()
        metrics.event({'type':'call_end','kind':'tool','name':'get_service_logs','status':'ok',
            'duration_ms':250,'run_id':'private-run','tenant_id':'private-tenant'})
        output = metrics.render()
        self.assertIn('it_incident_calls_total{kind="tool",name="get_service_logs",status="ok"} 1.0', output)
        self.assertIn('it_incident_call_seconds_sum', output)
        self.assertNotIn('private-run', output)
        self.assertNotIn('private-tenant', output)
        self.assertNotIn('deepresearch', output)
