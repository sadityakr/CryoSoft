# ---
# description: |
#   Pytest configuration and shared fixtures for CryoSoft test suite.
# last_updated: 2026-04-06
# ---

import pytest
import logging

@pytest.fixture(autouse=True)
def configure_logging():
    """Ensure logging is configured for all tests."""
    logging.basicConfig(level=logging.DEBUG)
