"""tracelens: production observability for LangChain/LangGraph multi-agent systems."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from tracelens.config import TraceLensConfig
from tracelens.models import EventType, RawEvent, Run, Stats
from tracelens.tracer import BackgroundTracer, TraceLens, start, trace

try:
    __version__ = version("tracelens")
except PackageNotFoundError:  # editable/source tree before install
    __version__ = "0.0.0+dev"

__all__ = [
    "BackgroundTracer",
    "EventType",
    "RawEvent",
    "Run",
    "Stats",
    "TraceLens",
    "TraceLensConfig",
    "__version__",
    "start",
    "trace",
]
