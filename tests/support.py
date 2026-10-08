"""Minimal configuration for free IT tests, without research graph fixtures."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TestConfig:
    model: str = 'test-model'
    api_key: str = 'test-only'
    postgres_dsn: str = ''
