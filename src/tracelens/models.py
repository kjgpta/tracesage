"""Pydantic v2 data models. Framework-neutral — no LangChain imports."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    CHAIN_START = "chain_start"
    CHAIN_END = "chain_end"
    CHAIN_ERROR = "chain_error"
    AGENT_ACTION = "agent_action"
    AGENT_FINISH = "agent_finish"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TOOL_ERROR = "tool_error"
    LLM_START = "llm_start"
    LLM_END = "llm_end"
    LLM_ERROR = "llm_error"
    CHAT_MODEL_START = "chat_model_start"
    RETRIEVER_START = "retriever_start"
    RETRIEVER_END = "retriever_end"
    RETRIEVER_ERROR = "retriever_error"
    RUN_START = "run_start"
    RUN_END = "run_end"
    RETRY = "retry"


# Event types whose payloads are large enough to warrant separate gzipped blob storage.
BLOB_ELIGIBLE_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.LLM_END,
        EventType.CHAIN_END,
        EventType.AGENT_FINISH,
        EventType.TOOL_END,
        EventType.RETRIEVER_END,
        # Error events carry a (small) traceback payload worth persisting for debugging.
        EventType.CHAIN_ERROR,
        EventType.TOOL_ERROR,
        EventType.LLM_ERROR,
        EventType.RETRIEVER_ERROR,
    }
)


# Event types that pair with a *_END event for duration calculation.
START_END_PAIRS: dict[EventType, EventType] = {
    EventType.CHAIN_START: EventType.CHAIN_END,
    EventType.TOOL_START: EventType.TOOL_END,
    EventType.LLM_START: EventType.LLM_END,
    EventType.CHAT_MODEL_START: EventType.LLM_END,
    EventType.RETRIEVER_START: EventType.RETRIEVER_END,
}


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RawEvent(BaseModel):
    """In-flight event on the queue. raw_payload is gzipped to disk for blob-eligible events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    event_id: str
    event_type: EventType
    run_id: str
    parent_run_id: str | None = None
    root_run_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    agent_name: str | None = None
    tool_name: str | None = None
    mcp_server: str | None = None  # provenance: MCP server this tool came from (None = local)
    summary: str
    full_blob_eligible: bool = False
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    token_input: int | None = None
    token_output: int | None = None
    error_message: str | None = None
    tags: list[str] = Field(default_factory=list)


class StoredEvent(BaseModel):
    """Persisted event row in DB. raw_payload not present (in blob if eligible)."""

    event_id: str
    run_id: str
    parent_run_id: str | None = None
    root_run_id: str
    event_type: EventType
    timestamp: datetime
    agent_name: str | None = None
    tool_name: str | None = None
    mcp_server: str | None = None  # provenance: MCP server this tool came from (None = local)
    summary: str
    blob_path: str | None = None
    duration_ms: int | None = None
    token_input: int | None = None
    token_output: int | None = None
    error_message: str | None = None


class Run(BaseModel):
    run_id: str
    root_run_id: str
    tags: list[str] = Field(default_factory=list)
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime
    completed_at: datetime | None = None
    total_steps: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    graph_definition: str | None = None
    error_message: str | None = None


class WSMessage(BaseModel):
    """Message broadcast over WebSocket to UI subscribers."""

    msg_type: Literal["event", "run_update", "catchup", "topology_update", "error"]
    run_id: str
    payload: dict[str, Any]


class Stats(BaseModel):
    """Tracer runtime stats — exposed via /api/stats."""

    queue_depth: int = 0
    queue_max: int = 50_000
    events_dropped: int = 0
    events_processed: int = 0
    events_sampled_out: int = 0
    runs_throttled: int = 0
    last_write_latency_ms: float | None = None
    p99_write_latency_ms: float | None = None
    db_size_bytes: int = 0
    blob_size_bytes: int = 0


class TopologyNode(BaseModel):
    """One node in the agent topology graph (derived from observed events)."""

    id: str  # e.g., "agent:OrderProcessor", "tool:search_web"
    name: str
    type: Literal["agent", "tool", "llm", "retriever", "chain", "mcp"]
    source: str | None = None  # MCP server name (for tool + mcp nodes); None = local/other
    invocation_count: int = 0
    error_count: int = 0
    total_duration_ms: int = 0
    avg_duration_ms: float = 0.0
    p99_duration_ms: int | None = None
    last_seen: datetime | None = None


class TopologyEdge(BaseModel):
    """Directed edge between two topology nodes (parent agent → child agent or tool)."""

    source: str
    target: str
    count: int = 0
    last_seen: datetime | None = None


class Topology(BaseModel):
    """Agent topology graph for the dashboard's visualization layer."""

    nodes: list[TopologyNode] = Field(default_factory=list)
    edges: list[TopologyEdge] = Field(default_factory=list)
