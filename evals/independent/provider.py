"""Runtime observation adapter. No reference-answer imports or answer paths."""
import json
from pathlib import Path
from providers.fixtures import FixtureProvider, ProviderError, visible

DATA_ROOT = Path(__file__).resolve().parents[2] / 'fixtures' / 'incident_independent'


class IndependentProvider(FixtureProvider):
    name = 'independent_synthetic_fixture'
    data_root = DATA_ROOT

    def __init__(self, case_id, principal):
        data_root = self.data_root
        catalog = json.loads((data_root / 'catalog.json').read_text(encoding='utf-8'))
        entry = next((row for row in catalog if row['case_id'] == case_id and visible(row, principal)), None)
        if entry is None:
            raise ProviderError('CASE_NOT_VISIBLE')
        path = data_root / entry['file']
        if path.parent.resolve() != data_root.resolve() or path.suffix != '.json':
            raise ProviderError('CASE_NOT_VISIBLE')
        self._data = json.loads(path.read_text(encoding='utf-8'))
        self.principal = principal

    def query(self, name, args, scope):
        # Deterministic channel outage, identical for both arms and every attempt.
        code = self._data.get('source_errors', {}).get(name)
        if code:
            raise ProviderError(code, retryable=False)
        return super().query(name, args, scope)
