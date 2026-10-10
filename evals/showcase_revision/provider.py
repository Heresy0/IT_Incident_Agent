"""Curated demonstration observations; no answer access or live IT mutations."""
from pathlib import Path
from evals.independent.provider import IndependentProvider

DATA_ROOT = Path(__file__).resolve().parents[2] / 'fixtures' / 'incident_showcase_revision'


class ShowcaseRevisionProvider(IndependentProvider):
    name = 'showcase_revision_synthetic_fixture'
    data_root = DATA_ROOT
