"""tracelens initialization for the error recovery pipeline."""
from __future__ import annotations

from tracelens import TraceLens, TraceLensConfig


DEFAULT_TAGS = ["error-recovery"]


async def init_tracer() -> TraceLens:
    cfg = TraceLensConfig()
    return await TraceLens.create(cfg)
