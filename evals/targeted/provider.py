"""Observation-only adapter, reusing all existing scope and tool filtering."""
from pathlib import Path
from evals.independent.provider import IndependentProvider

DATA_ROOT = Path(__file__).resolve().parents[2] / 'fixtures' / 'incident_targeted'


class TargetedProvider(IndependentProvider):
    name = 'targeted_synthetic_fixture'
    data_root = DATA_ROOT
