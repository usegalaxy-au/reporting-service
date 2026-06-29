"""Top-level pytest config: gate the integration suite behind RUN_INTEGRATION."""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get('RUN_INTEGRATION'):
        return
    skip_integration = pytest.mark.skip(
        reason='Integration tests skipped (set RUN_INTEGRATION=1 to run)')
    for item in items:
        if 'integration' in item.keywords:
            item.add_marker(skip_integration)
