"""tracelens initialization for the customer support system.

Centralizes tracer construction and the default tags applied to every run.
Other entry points in your codebase should `from tracelens_setup import init_tracer`
rather than calling `TraceLens.create()` directly — that way config and tags
live in one place.
"""
from __future__ import annotations

from tracelens import TraceLens, TraceLensConfig


# Tags attached to every run from this system. Use them to filter the run list
# in the UI or via /api/runs?tag=customer-support.
DEFAULT_TAGS = ["customer-support"]


async def init_tracer() -> TraceLens:
    """Construct a TraceLens. Picks up TRACELENS_* env vars from the environment.

    Common overrides:
        TRACELENS_DATA_DIR=~/.tracelens          # where to persist
        TRACELENS_PORT=7842                      # UI / REST port
        TRACELENS_AUTH_TOKEN=secret              # required for non-loopback host
        TRACELENS_SAMPLE_RATE=0.1                # for high-volume systems
    """
    cfg = TraceLensConfig()
    return await TraceLens.create(cfg)
