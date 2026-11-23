"""StorageBackend protocol — the contract that future backends (Postgres, JSONL, remote HTTP) implement.

Ships SQLiteBackend. The protocol is defined here so the protocol surface stays
backend-neutral.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from tracesage.models import (
    Run,
    RunNote,
    RunStatus,
    SearchHit,
    StoredEvent,
    ThreadSummary,
    Topology,
)


@runtime_checkable
class StorageBackend(Protocol):
    """Async storage interface. All methods are coroutines.

    Implementations are responsible for:
    - Schema initialization (idempotent).
    - Concurrency safety (single writer assumed; multiple readers).
    - Crash safety (ACID-or-better for upsert_run/event/batch).
    - Query performance: get_journey, list_runs must serve <100ms p99 at 1M events.

    Implementations are NOT responsible for:
    - Blob storage (handled by separate BlobStore).
    - Authentication (handled by server middleware).
    - Sampling (handled by tracer before events reach storage).
    """

    async def init(self) -> None:
        """Initialize schema. Idempotent — safe to call multiple times."""
        ...

    async def close(self) -> None:
        """Release all resources (connections, pools)."""
        ...

    # -- Run operations --

    async def upsert_run(self, run: Run) -> None:
        """Insert a new run or update an existing one by run_id."""
        ...

    async def get_run(self, run_id: str) -> Run | None:
        """Return the run with this id, or None if not found."""
        ...

    async def list_runs(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tag: str | None = None,
        thread_id: str | None = None,
        q: str | None = None,
        model: str | None = None,
        tool: str | None = None,
        errors_only: bool = False,
        sort: str = "recent",
        no_source: bool = False,
    ) -> tuple[list[Run], int]:
        """Return (runs, total_count). Sorted by started_at descending.

        ``sort`` is one of ``recent`` (default), ``oldest``, ``steps``, ``tokens``
        or ``cost``; the last three put the largest first. ``no_source`` keeps
        only runs without a ``source:`` tag (agents you instrumented yourself).

        If ``tag`` is given, only runs whose tags contain it (substring match) are
        returned, and ``total_count`` reflects the filtered set. The filter is
        applied in SQL so it composes correctly with ``limit``/``offset``.
        ``thread_id`` is an exact match, grouping the runs of one conversation.
        ``q`` matches run id, thread id, tags, or any full-text event hit.
        ``model`` and ``tool`` match events inside the run. ``errors_only`` keeps
        failed runs.
        """
        ...

    async def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        completed_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Update run status terminally (COMPLETED or FAILED). No-op if run not found."""
        ...

    async def increment_run_counters(
        self,
        run_id: str,
        steps: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost: float | None = None,
    ) -> None:
        """Atomically add to step, token and (when ``cost`` is not None) cost totals."""
        ...

    async def recompute_run_counters(self, run_id: str) -> None:
        """Set the run's step and token totals from its stored events (idempotent)."""
        ...

    async def delete_run(self, run_id: str) -> None:
        """Delete a run and all its events. Blob deletion is a separate concern (BlobStore)."""
        ...

    # -- Event operations --

    async def upsert_event(self, event: StoredEvent) -> None:
        """Insert a new event or update an existing one by event_id."""
        ...

    async def upsert_events_batch(self, events: list[StoredEvent]) -> None:
        """Batch upsert in a single transaction. Failure of one event must not abort the batch."""
        ...

    async def get_journey(self, run_id: str, limit: int | None = None) -> list[StoredEvent]:
        """Return all events for a run AND its descendants, in chronological order.

        Uses root_run_id matching (not run_id) so nested LangGraph runs are included.
        With ``limit``, returns only the most recent ``limit`` events (still ascending).
        """
        ...

    def iter_journey(self, run_id: str, batch_size: int = 500) -> AsyncIterator[StoredEvent]:
        """Async-iterate all events for a run AND its descendants, chronologically.

        Streams in fetchmany(batch_size) chunks so large journeys stay O(batch_size)
        in memory. Same root_run_id matching semantics as get_journey.
        """
        ...

    async def get_event(self, event_id: str) -> StoredEvent | None:
        """Return a single event by event_id, or None."""
        ...

    async def search_events(
        self, q: str, limit: int = 50, run_id: str | None = None
    ) -> list[SearchHit]:
        """Full-text hits, best match first. ``run_id`` limits the search to one run."""
        ...

    async def list_threads(
        self,
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        tag: str | None = None,
        sort: str = "recent",
        min_turns: int = 1,
        no_source: bool = False,
    ) -> tuple[list[ThreadSummary], int]:
        """Conversation threads plus a total count.

        ``sort`` is ``recent`` (default, latest activity first), ``oldest``,
        ``turns`` or ``tokens``. ``q`` matches the thread id or any full-text
        event hit in its runs. ``min_turns`` drops shorter threads and ``no_source``
        keeps threads without a ``source:`` tag.
        """
        ...

    async def get_thread_runs(self, thread_id: str) -> list[Run]:
        """Root runs in one thread, oldest first."""
        ...

    async def add_note(self, run_id: str, text: str, author: str | None = None) -> RunNote:
        """Append a note to a run."""
        ...

    async def list_notes(self, run_id: str) -> list[RunNote]:
        """Notes on a run, oldest first."""
        ...

    async def set_run_tags(
        self, run_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> list[str] | None:
        """Add and remove tags. Returns the new tag list, or None if the run is missing."""
        ...

    async def recompute_run_costs(self, run_id: str) -> None:
        """Set the run's cost total from its stored events (idempotent)."""
        ...

    async def needs_reindex(self) -> bool:
        """True when an upgraded database still needs ``tracesage reindex``."""
        ...

    # -- Stats and topology --

    async def get_stats(self) -> dict:
        """Return system-wide stats: total_runs, by-status counts, avg duration, totals."""
        ...

    async def get_topology(self, scope: str | None = None) -> Topology:
        """Return the agent topology graph derived from observed events.

        Nodes: unique (agent_name | tool_name | retriever) seen across events.
        Edges: parent→child run relationships.

        `scope` bounds which runs are aggregated: None/"all" = all-time, "run:<id>" =
        a single run's structure, "last_n:<N>" = the N most-recent runs. Scoping keeps
        a removed component (tool, agent, MCP server) from lingering across versions.
        """
        ...

    async def get_tool_inventory(self, scope: str | None = None) -> dict:
        """Return tools grouped by source (MCP server name, or 'local').

        `scope` matches :meth:`get_topology`. Shape: {"sources": [{source, kind,
        tool_count, invocation_count, error_count, tools: [{name, invocations,
        errors}]}]}.
        """
        ...

    async def upsert_mcp_tools(self, server: str, tool_names: list[str]) -> None:
        """Persist which tools an MCP server provides (idempotent). Lets topology +
        inventory show a server's tools even when they were never invoked."""
        ...

    async def get_mcp_tools(self) -> dict[str, list[str]]:
        """Return {mcp_server: [tool names]} from the registry (empty if none)."""
        ...
