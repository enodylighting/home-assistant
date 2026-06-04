"""Shared Home Assistant test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def enable_custom_component(enable_custom_integrations: None) -> None:
    """Enable loading integrations from custom_components."""
