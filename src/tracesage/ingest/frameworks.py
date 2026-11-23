"""``tracesage.instrument(...)``: one call to route a framework's OpenTelemetry
spans into a tracesage tracer.

Each supported framework already emits OTel spans (natively, or through an
instrumentation package). ``instrument`` installs the in-process bridge
(:func:`tracesage.ingest.install_otel`) and then switches on the framework's
instrumentor against that provider. Every framework import is lazy and failures
raise an ``ImportError`` naming the extra to install.

Supported names: ``"pydantic_ai"``, ``"openai_agents"``, ``"crewai"``,
``"openai"``, ``"anthropic"``.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

log = logging.getLogger("tracesage.ingest")

# The GenAI instrumentations (OpenAI, Anthropic, OpenAI Agents) record prompts and
# completions only when this is set. tracesage exists to show you those, so default
# it on (span attributes only — no log events). An explicit user value wins.
_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"


def _enable_content_capture() -> None:
    os.environ.setdefault(_CONTENT_ENV, "span_only")


def _hint(extra: str, *extra_pkgs: str) -> str:
    pkgs = " ".join(extra_pkgs)
    tail = f" (plus the framework itself: pip install {pkgs})" if pkgs else ""
    return f'pip install "tracesage[{extra}]"{tail}'


def _pydantic_ai(provider: Any) -> None:
    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings
    except ImportError as e:
        raise ImportError(
            f"tracesage.instrument('pydantic_ai') needs Pydantic AI: {_hint('pydantic-ai', 'pydantic-ai-slim')}"
        ) from e
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider))


def _openai_agents(provider: Any) -> None:
    try:
        from opentelemetry.instrumentation.genai.openai_agents import (
            OpenAIAgentsInstrumentor,
        )
    except ImportError as e:
        raise ImportError(
            "tracesage.instrument('openai_agents') needs the OpenAI Agents instrumentation: "
            f"{_hint('openai-agents', 'openai-agents')}"
        ) from e
    _enable_content_capture()
    inst = OpenAIAgentsInstrumentor()
    if not inst.is_instrumented_by_opentelemetry:
        # disable_openai_trace_export: don't also ship traces to OpenAI's dashboard.
        inst.instrument(tracer_provider=provider, disable_openai_trace_export=True)
    # The agents instrumentation covers workflow/agent/tool spans; the OpenAI client
    # instrumentation adds the `chat` spans underneath. Best-effort.
    try:
        _openai(provider)
    except ImportError:
        log.debug("openai client instrumentation not installed; chat spans will be missing")


def _openai(provider: Any) -> None:
    try:
        from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor
    except ImportError as e:
        raise ImportError(
            f"tracesage.instrument('openai') needs the OpenAI instrumentation: {_hint('openai', 'openai')}"
        ) from e
    _enable_content_capture()
    inst = OpenAIInstrumentor()
    if not inst.is_instrumented_by_opentelemetry:
        inst.instrument(tracer_provider=provider)


def _anthropic(provider: Any) -> None:
    try:
        from opentelemetry.instrumentation.genai.anthropic import AnthropicInstrumentor
    except ImportError as e:
        raise ImportError(
            "tracesage.instrument('anthropic') needs the Anthropic instrumentation: "
            f"{_hint('anthropic', 'anthropic')}"
        ) from e
    _enable_content_capture()
    inst = AnthropicInstrumentor()
    if not inst.is_instrumented_by_opentelemetry:
        inst.instrument(tracer_provider=provider)


def _crewai(provider: Any) -> None:
    try:
        from openinference.instrumentation.crewai import CrewAIInstrumentor
    except ImportError as e:
        raise ImportError(
            f"tracesage.instrument('crewai') needs the OpenInference CrewAI instrumentation: "
            f"{_hint('crewai', 'crewai')}"
        ) from e
    # CrewAI ships its own usage telemetry that also produces kind-less OTel spans;
    # keep them out of the trace (and off the network). An explicit user value wins.
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
    inst = CrewAIInstrumentor()
    if not inst.is_instrumented_by_opentelemetry:
        inst.instrument(tracer_provider=provider)


_INSTRUMENTORS: dict[str, Callable[[Any], None]] = {
    "pydantic_ai": _pydantic_ai,
    "openai_agents": _openai_agents,
    "openai": _openai,
    "anthropic": _anthropic,
    "crewai": _crewai,
}

SUPPORTED_FRAMEWORKS: tuple[str, ...] = tuple(_INSTRUMENTORS)


def _normalise(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def instrument(*frameworks: str, tracer: Any, set_global: bool = True) -> Any:
    """Route ``frameworks``' OpenTelemetry spans into ``tracer``.

    Example::

        with tracesage.trace() as tl:
            tracesage.instrument("pydantic_ai", tracer=tl)
            agent.run_sync("...")          # shows up in the tracesage UI

    (or simply ``tracesage.trace(instrument=["pydantic_ai"])``). Returns the OTel
    ``TracerProvider`` the spans flow through, so you can add your own spans with
    ``provider.get_tracer(__name__)``. Raises ``ValueError`` for an unknown name and
    ``ImportError`` (with the install line) when a framework package is missing.
    """
    if not frameworks:
        raise ValueError(
            f"tracesage.instrument() needs at least one framework name: {', '.join(SUPPORTED_FRAMEWORKS)}"
        )
    names = [_normalise(f) for f in frameworks]
    unknown = [f for f in names if f not in _INSTRUMENTORS]
    if unknown:
        raise ValueError(
            f"unknown framework(s) {unknown}; supported: {', '.join(SUPPORTED_FRAMEWORKS)}"
        )
    from tracesage.ingest.exporter import install_otel

    provider = install_otel(tracer, set_global=set_global)
    if provider is None:
        return None  # tracer disabled: leave the frameworks untouched
    for name in names:
        _INSTRUMENTORS[name](provider)
    return provider


__all__ = ["SUPPORTED_FRAMEWORKS", "instrument"]
