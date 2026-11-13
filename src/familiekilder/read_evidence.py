"""Actionable, source-version-bound diagnostics; never grant reading credit."""
import json
import time

from .core import FamiliekilderError
from .sources import REGISTRY


def receipt_current(runtime, observation):
    checker = getattr(runtime, 'receipt_is_current', None)
    return checker(observation) if checker else bool(observation and time.monotonic() - observation['at'] <= 900)


def snapshot_key(runtime):
    resolver = getattr(runtime, 'snapshot_key', None)
    return resolver() if resolver else None


class ReadEvidenceError(FamiliekilderError):
    def __init__(self, message, **diagnostic):
        self.diagnostic = diagnostic
        super().__init__(message + '\n' + json.dumps(diagnostic, ensure_ascii=False))


def next_detail(observation):
    if not observation or not observation.get('source'):
        return []
    source = observation['source'].replace('-', '_')
    entry = REGISTRY.get(observation['source'])
    if observation.get('reference', {}).get('kind') == 'notification' and entry and entry.notifications:
        return [{'tool': source + '_list_items', 'arguments': {}}]
    return [{'tool': source + '_get_item', 'arguments': {
        'reference': observation['reference'], 'freshness': 'recent'}}]
