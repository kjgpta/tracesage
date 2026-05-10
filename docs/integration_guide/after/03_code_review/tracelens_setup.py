"""tracelens initialization for the code review system."""
from __future__ import annotations

from tracelens import TraceLens, TraceLensConfig


DEFAULT_TAGS = ["code-review"]


async def init_tracer() -> TraceLens:
    cfg = TraceLensConfig()
    return await TraceLens.create(cfg)
