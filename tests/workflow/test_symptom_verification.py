from datetime import datetime, timezone, timedelta
import unittest
import httpx
from providers.registry import ServiceBinding
from providers.fixtures import ProviderError
from repairs.verification import SymptomVerifier, validate_checks


class SymptomVerificationTests(unittest.TestCase):
    def setUp(self):
        self.binding = ServiceBinding('t', ('u',), 's', 'staging', 'v1', observations={
            'prometheus': {'url': 'http://127.0.0.1:9090'},
            'metrics': {'error_rate': {'query': 'sum(rate(errors{service="s"}[1m]))',
                                     'freshness_query': 'max(time()-timestamp(errors{service="s"}))', 'unit': 'ratio'}}})
        self.target = {'symptom_checks': [{'metric': 'error_rate', 'operator': 'lte', 'threshold': .01}]}

    def verify(self, samples):
        verifier = SymptomVerifier(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={'status': 'success', 'data': {'resultType': 'vector', 'result': samples}})))
        return verifier.verify(self.binding, self.target)

    def test_absent_stale_and_multiple_series_never_prove_recovery(self):
        current = datetime.now(timezone.utc).timestamp()
        for samples in ([], [{'value': [current - 3600, '0']}], [{'value': [current, '0']}] * 2):
            self.assertFalse(self.verify(samples)['passed'])
        self.assertTrue(self.verify([{'value': [current, '0']}])['passed'])

    def test_nonfinite_measurements_are_rejected(self):
        for value in ('NaN', 'Inf', 'not-a-number'):
            with self.assertRaises(ProviderError):
                self.verify([{'value': [datetime.now(timezone.utc).timestamp(), value]}])

    def test_unregistered_metric_and_arbitrary_queries_are_rejected(self):
        for changes in ({'metric': 'unregistered'}, {'query': 'arbitrary'}, {'operator': 'shell'}, {'max_age_seconds': 3600}):
            with self.assertRaises(ProviderError):
                validate_checks(self.binding, {'symptom_checks': [{**self.target['symptom_checks'][0], **changes}]})

    def test_fresh_query_result_with_old_source_data_is_not_recovery(self):
        def reply(request):
            value = '3600' if 'timestamp' in request.url.params['query'] else '0'
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'vector',
                'result': [{'value': [datetime.now(timezone.utc).timestamp(), value]}]}})
        result = SymptomVerifier(transport=httpx.MockTransport(reply)).verify(self.binding, self.target)
        self.assertFalse(result['passed'])
        self.assertEqual(result['checks'][0]['code'], 'SYMPTOM_SOURCE_STALE')
