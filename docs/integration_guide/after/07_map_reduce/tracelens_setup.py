"""tracelens initialization for the map-reduce summarizer."""
from __future__ import annotations

from tracelens import TraceLens, TraceLensConfig


DEFAULT_TAGS = ["map-reduce"]


async def init_tracer() -> TraceLens:
    cfg = TraceLensConfig()
    return await TraceLens.create(cfg)
