"""tracesage: local-first observability for LLM agents — LangChain/LangGraph via
callbacks, and Pydantic AI, OpenAI Agents SDK, CrewAI, the OpenAI/Anthropic clients
(or any other OpenTelemetry source) via span ingest."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from tracesage.config import TraceSageConfig
from tracesage.ingest.frameworks import instrument
from tracesage.manual import Span, current_span
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
    "Span",
    "Stats",
    "TraceSage",
    "TraceSageConfig",
    "__version__",
    "current_span",
    "instrument",
    "start",
    "trace",
]
