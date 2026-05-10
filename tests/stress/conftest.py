"""Stress fixtures: re-uses the integration tracer fixture."""
from __future__ import annotations

# Bring the integration fixtures into scope; pytest discovers them by name.
from tests.integration.conftest import (
    integration_tracer,
    integration_tracer_with_server,
    wait_for_drain,
)

__all__ = ["integration_tracer", "integration_tracer_with_server", "wait_for_drain"]
