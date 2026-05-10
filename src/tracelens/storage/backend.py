"""StorageBackend protocol — the contract that v0.2 backends (Postgres, JSONL, remote HTTP) implement.

v0.1 ships SQLiteBackend. The protocol is defined here so the protocol surface stays
backend-neutral.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tracelens.models import Run, RunStatus, StoredEvent, Topology


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
    ) -> tuple[list[Run], int]:
        """Return (runs, total_count). Sorted by started_at descending."""
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
    ) -> None:
        """Atomically add to total_steps, total_tokens_input, total_tokens_output."""
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

    async def get_journey(self, run_id: str) -> list[StoredEvent]:
        """Return all events for a run AND its descendants, in chronological order.

        Uses root_run_id matching (not run_id) so nested LangGraph runs are included.
        """
        ...

    async def get_event(self, event_id: str) -> StoredEvent | None:
        """Return a single event by event_id, or None."""
        ...

    # -- Stats and topology --

    async def get_stats(self) -> dict:
        """Return system-wide stats: total_runs, by-status counts, avg duration, totals."""
        ...

    async def get_topology(self) -> Topology:
        """Return the agent topology graph derived from observed events.

        Nodes: unique (agent_name | tool_name | retriever) seen across events.
        Edges: parent→child run relationships.
        """
        ...
