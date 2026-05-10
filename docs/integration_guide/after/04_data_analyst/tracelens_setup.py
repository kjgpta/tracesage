"""tracelens initialization for the data analyst system."""
from __future__ import annotations

from tracelens import TraceLens, TraceLensConfig


DEFAULT_TAGS = ["data-analyst"]


async def init_tracer() -> TraceLens:
    cfg = TraceLensConfig()
    return await TraceLens.create(cfg)
