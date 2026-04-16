"""
tests/conftest.py
─────────────────
Shared pytest fixtures.
"""

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
