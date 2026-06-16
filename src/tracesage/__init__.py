"""tracesage: production observability for LangChain/LangGraph multi-agent systems."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from tracesage.config import TraceSageConfig
from tracesage.models import EventType, RawEvent, Run, Stats
from tracesage.tracer import BackgroundTracer, TraceSage, start, trace

try:
    __version__ = version("tracesage")
except PackageNotFoundError:  # editable/source tree before install
    __version__ = "0.0.0+dev"

__all__ = [
    "BackgroundTracer",
    "EventType",
    "RawEvent",
    "Run",
    "Stats",
    "TraceSage",
    "TraceSageConfig",
    "__version__",
    "start",
    "trace",
]
