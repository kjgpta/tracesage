"""tracelens initialization for the research pipeline."""
from __future__ import annotations

from tracelens import TraceLens, TraceLensConfig


# Tags attached to every run from this system. Filter the run list in the UI
# (or via /api/runs?tag=research-pipeline) to isolate this system's runs.
DEFAULT_TAGS = ["research-pipeline"]


async def init_tracer() -> TraceLens:
    """Construct a TraceLens. Picks up TRACELENS_* env vars from the environment."""
    cfg = TraceLensConfig()
    return await TraceLens.create(cfg)
