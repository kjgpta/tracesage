"""tracelens initialization for the writer-critic system."""
from __future__ import annotations

from tracelens import TraceLens, TraceLensConfig


DEFAULT_TAGS = ["writer-critic"]


async def init_tracer() -> TraceLens:
    cfg = TraceLensConfig()
    return await TraceLens.create(cfg)
