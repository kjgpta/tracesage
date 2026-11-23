"""Latency waterfall rows built from stored events.

Pure function: REST, the CLI and the MCP server all call :func:`build_spans`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from tracesage.models import EventType, StoredEvent

SpanStatus = Literal["ok", "error", "interrupted", "running"]

_START = frozenset(
    {
        EventType.CHAIN_START,
        EventType.TOOL_START,
        EventType.LLM_START,
        EventType.CHAT_MODEL_START,
        EventType.RETRIEVER_START,
        EventType.AGENT_ACTION,
    }
)
_END_OK = frozenset(
    {
        EventType.CHAIN_END,
        EventType.TOOL_END,
        EventType.LLM_END,
        EventType.RETRIEVER_END,
        EventType.AGENT_FINISH,
    }
)
_END_ERR = frozenset(
    {
        EventType.CHAIN_ERROR,
        EventType.TOOL_ERROR,
        EventType.LLM_ERROR,
        EventType.RETRIEVER_ERROR,
    }
)
_SKIP = frozenset({EventType.RUN_START, EventType.RUN_END, EventType.RETRY})


class SpanRow(BaseModel):
    """One step on the waterfall. Times are milliseconds from the run start."""

    run_id: str
    parent_run_id: str | None = None
    name: str
    kind: str
    depth: int = 0
    start_ms: int = 0
    end_ms: int = 0
    duration_ms: int = 0
    status: SpanStatus = "ok"
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    mcp_server: str | None = None
    event_id: str
    children: list[str] = Field(default_factory=list)


def _kind(event: StoredEvent) -> str:
    et = event.event_type
    if et in (EventType.LLM_START, EventType.LLM_END, EventType.LLM_ERROR, EventType.CHAT_MODEL_START):
        return "llm"
    if et in (EventType.TOOL_START, EventType.TOOL_END, EventType.TOOL_ERROR):
        return "tool"
    if et in (EventType.RETRIEVER_START, EventType.RETRIEVER_END, EventType.RETRIEVER_ERROR):
        return "retriever"
    if event.tool_name:
        return "tool"
    return "agent" if event.agent_name else "chain"


def build_spans(
    events: list[StoredEvent],
    *,
    now: datetime | None = None,
    running: bool = False,
) -> list[SpanRow]:
    """Pair start and end events into waterfall rows. Never raises."""
    grouped: dict[str, list[StoredEvent]] = {}
    for event in events:
        if event.event_type in _SKIP:
            continue
        grouped.setdefault(event.run_id, []).append(event)
    if not grouped:
        return []

    origin = min(ev.timestamp for ev in events)
    latest = max(ev.timestamp for ev in events)
    close_at = now if running and now is not None else latest

    rows: dict[str, SpanRow] = {}
    for run_id, group in grouped.items():
        starts = [ev for ev in group if ev.event_type in _START]
        errors = [ev for ev in group if ev.event_type in _END_ERR]
        interrupts = [ev for ev in group if ev.event_type == EventType.CHAIN_INTERRUPT]
        ends = [ev for ev in group if ev.event_type in _END_OK]
        anchor = starts[0] if starts else group[0]
        closer: StoredEvent | None
        if errors:
            closer = errors[0]
        elif interrupts:
            closer = interrupts[0]
        elif ends:
            closer = ends[0]
        else:
            closer = None
        # Min/max across the step, so an OTLP span whose end is stored before
        # its start still gets a positive duration.
        start_ts = min(ev.timestamp for ev in group)
        if closer is not None:
            end_ts = max(ev.timestamp for ev in group)
            if errors:
                status: SpanStatus = "error"
            elif interrupts:
                status = "interrupted"
            else:
                status = "ok"
        else:
            end_ts = max(close_at, start_ts)
            status = "running"
        source = closer or anchor
        start_ms = max(0, int((start_ts - origin).total_seconds() * 1000))
        end_ms = max(start_ms, int((end_ts - origin).total_seconds() * 1000))
        rows[run_id] = SpanRow(
            run_id=run_id,
            parent_run_id=anchor.parent_run_id,
            name=anchor.tool_name or anchor.agent_name or anchor.event_type.value,
            kind=_kind(anchor),
            start_ms=start_ms,
            end_ms=end_ms,
            duration_ms=end_ms - start_ms,
            status=status,
            tokens_in=source.token_input,
            tokens_out=source.token_output,
            cost_usd=source.cost_usd,
            model=source.model or anchor.model,
            mcp_server=source.mcp_server or anchor.mcp_server,
            event_id=(closer or anchor).event_id,
        )

    def depth_of(run_id: str, seen: set[str]) -> int:
        if run_id in seen:
            return 0
        row = rows.get(run_id)
        if row is None or not row.parent_run_id or row.parent_run_id not in rows:
            return 0
        return 1 + depth_of(row.parent_run_id, seen | {run_id})

    for run_id, row in rows.items():
        row.depth = depth_of(run_id, set())
        parent = rows.get(row.parent_run_id or "")
        if parent is not None and run_id not in parent.children:
            parent.children.append(run_id)

    return sorted(rows.values(), key=lambda row: (row.start_ms, row.depth, row.name))
