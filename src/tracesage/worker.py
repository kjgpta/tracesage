"""StorageWorker: drains the event queue, persists to DB + blob, broadcasts to WS subscribers.

Single background coroutine. Per-event isolation: one bad event must not abort the batch.
Per-batch isolation: one failing batch backs off but does not exit the loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict, deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tracesage.models import (
    BLOB_ELIGIBLE_EVENTS,
    EventType,
    RawEvent,
    Run,
    RunStatus,
    Stats,
    StoredEvent,
    WSMessage,
)

if TYPE_CHECKING:
    from tracesage.config import TraceSageConfig
    from tracesage.storage.backend import StorageBackend
    from tracesage.storage.blob_store import BlobStore

log = logging.getLogger("tracesage.worker")


# Map *_END events to a key prefix used to look up the matching *_START timestamp.
_END_TO_START_KEY: dict[EventType, str] = {
    EventType.CHAIN_END: "chain",
    EventType.CHAIN_ERROR: "chain",
    EventType.TOOL_END: "tool",
    EventType.TOOL_ERROR: "tool",
    EventType.LLM_END: "llm",
    EventType.LLM_ERROR: "llm",
    EventType.RETRIEVER_END: "retriever",
    EventType.RETRIEVER_ERROR: "retriever",
    EventType.AGENT_FINISH: "chain",
}

_START_TO_KEY: dict[EventType, str] = {
    EventType.CHAIN_START: "chain",
    EventType.TOOL_START: "tool",
    EventType.LLM_START: "llm",
    EventType.CHAT_MODEL_START: "llm",
    EventType.RETRIEVER_START: "retriever",
}


# Root-level (parent_run_id is None) events that mark a run terminal. Includes
# llm/tool/retriever ends so a "bare" root (e.g. a direct llm.ainvoke with no
# wrapping chain) is marked COMPLETED instead of stuck RUNNING forever. For a
# chain/graph root these never fire at root level (the llm/tool is nested under
# the chain), so this does not affect normal chain roots.
_ROOT_TERMINAL_OK: frozenset[EventType] = frozenset(
    {
        EventType.CHAIN_END,
        EventType.AGENT_FINISH,
        EventType.LLM_END,
        EventType.TOOL_END,
        EventType.RETRIEVER_END,
    }
)
_ROOT_TERMINAL_ERR: frozenset[EventType] = frozenset(
    {
        EventType.CHAIN_ERROR,
        EventType.LLM_ERROR,
        EventType.TOOL_ERROR,
        EventType.RETRIEVER_ERROR,
    }
)


class StorageWorker:
    def __init__(
        self,
        queue: asyncio.Queue,
        db: StorageBackend,
        blob_store: BlobStore,
        ws_manager: Any,
        config: TraceSageConfig,
        stats: Stats,
        otel_exporter: Any = None,
    ) -> None:
        self._queue = queue
        self._db = db
        self._blob_store = blob_store
        self._ws = ws_manager
        self._config = config
        self._stats = stats
        # Optional OpenTelemetry span exporter (best-effort; never breaks ingestion).
        self._otel = otel_exporter
        self._shutdown = False
        # {run_id: {prefix: timestamp}} for duration calculation. Both outer and inner
        # dicts are bounded so runs that start without ever ending (process kill,
        # cancelled subgraph) cannot leak memory.
        self._start_timestamps: OrderedDict[str, OrderedDict[str, datetime]] = OrderedDict()
        self._start_timestamps_outer_cap = 50_000  # max distinct active run_ids
        self._start_timestamps_inner_cap = 100  # max start types per run
        # Rolling window of write latencies for p99 calculation.
        self._latencies: deque[float] = deque(maxlen=1000)
        # OrderedDict (used as ordered set with None values) of run_ids we've already
        # created a runs row for. Deterministic FIFO eviction at the cap so long-running
        # processes never grow unboundedly.
        self._runs_seen: OrderedDict[str, None] = OrderedDict()
        self._runs_seen_cap = 50_000

    def request_shutdown(self) -> None:
        self._shutdown = True

    async def run(self) -> None:
        """Main worker loop. Cancellation drains remaining events before re-raise."""
        try:
            while not self._shutdown:
                events: list[RawEvent] = []
                try:
                    events = await self._drain_batch(
                        max_size=self._config.worker_batch_size,
                        timeout=self._config.worker_batch_timeout,
                    )
                    if events:
                        await self._process_batch(events)
                except asyncio.CancelledError:
                    # Do NOT ack here — the `finally` below acks exactly once.
                    # Acking in both places double-counts task_done() and
                    # corrupts the queue's unfinished-task counter.
                    raise
                except Exception as e:  # pragma: no cover - defensive top-level
                    log.error("StorageWorker batch error: %s", e, exc_info=True)
                    await asyncio.sleep(0.5)
                finally:
                    if events:
                        self._ack_batch(events)
        except asyncio.CancelledError:
            remaining = self._drain_remaining()
            if remaining:
                try:
                    await self._process_batch(remaining)
                except Exception as e:  # pragma: no cover
                    log.error("StorageWorker shutdown drain error: %s", e, exc_info=True)
                finally:
                    self._ack_batch(remaining)
            raise

    async def _drain_batch(
        self, max_size: int, timeout: float  # noqa: ASYNC109 - intentional polling boundary
    ) -> list[RawEvent]:
        """Wait up to `timeout` for the first event, then greedily drain up to max_size.

        NOTE: `task_done()` is intentionally NOT called here — the run loop calls it
        after `_process_batch` completes, so `queue.join()` accurately reflects "all
        events fully persisted" rather than "all events dequeued."
        """
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return []
        events: list[RawEvent] = [first]

        while len(events) < max_size:
            try:
                evt = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            events.append(evt)
        return events

    def _drain_remaining(self) -> list[RawEvent]:
        """Drain everything currently on the queue (used during cancellation).

        task_done() is NOT called here either — the cancel handler invokes
        `_process_batch` then calls `task_done` per event afterward.
        """
        events: list[RawEvent] = []
        while True:
            try:
                evt = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            events.append(evt)
        return events

    def _ack_batch(self, events: list[RawEvent]) -> None:
        """Mark every event in the batch as task_done() so queue.join() unblocks."""
        for _ in events:
            with contextlib.suppress(ValueError):
                self._queue.task_done()

    def _mark_run_seen(self, run_id: str) -> None:
        """Insert run_id into the bounded ordered-set, FIFO-evicting oldest at cap."""
        self._runs_seen[run_id] = None
        while len(self._runs_seen) > self._runs_seen_cap:
            self._runs_seen.popitem(last=False)

    async def _process_batch(self, events: list[RawEvent]) -> None:
        """Process a batch: blobs, stored events, run rows, ws broadcast.

        Each event is wrapped in try/except so one bad event does not abort the batch.
        """
        t0 = time.perf_counter()
        stored_events: list[StoredEvent] = []
        # (event, stored_event) pairs to replay for ws broadcast after DB commit.
        ws_pairs: list[tuple[RawEvent, StoredEvent]] = []
        # Run-level mutations to apply after the events batch commits.
        runs_to_upsert: list[Run] = []
        terminal_updates: list[tuple[str, RunStatus, datetime, str | None]] = []
        counter_updates: dict[str, dict[str, int]] = {}

        for event in events:
            try:
                stored = await self._build_stored_event(event)
                stored_events.append(stored)
                ws_pairs.append((event, stored))
                # Increment events_processed per event so /api/stats reflects
                # in-flight progress, not just batch-end totals.
                self._stats.events_processed += 1

                if event.event_type == EventType.RUN_START:
                    runs_to_upsert.append(self._raw_to_run(event))
                    self._mark_run_seen(event.run_id)
                elif event.run_id not in self._runs_seen:
                    # Auto-create a runs row for any new run_id we observe (sub-runs in
                    # nested LangGraph trees, retries, etc.) so that the events FK holds.
                    runs_to_upsert.append(self._raw_to_run(event))
                    self._mark_run_seen(event.run_id)

                # Terminal updates: only a ROOT-level event (parent_run_id is None)
                # marks a run terminal. A nested/recovered error must NOT fail the
                # whole root run — if it is uncaught it propagates and the root also
                # emits its own error (parent_run_id is None); if it is caught inside
                # the graph the root completes normally. With the monotonic backend
                # guard (WHERE status='running'), the first terminal event wins.
                is_error = event.event_type in _ROOT_TERMINAL_ERR
                is_terminal = event.parent_run_id is None and (
                    is_error or event.event_type in _ROOT_TERMINAL_OK
                )
                if is_terminal:
                    status = RunStatus.FAILED if is_error else RunStatus.COMPLETED
                    terminal_updates.append(
                        (event.root_run_id, status, event.timestamp, event.error_message)
                    )

                # Counters keyed by root_run_id so all sub-events feed the top-level run row.
                bucket = counter_updates.setdefault(
                    event.root_run_id,
                    {"steps": 0, "tokens_in": 0, "tokens_out": 0},
                )
                bucket["steps"] += 1
                if event.token_input:
                    bucket["tokens_in"] += event.token_input
                if event.token_output:
                    bucket["tokens_out"] += event.token_output
            except Exception as e:
                log.error(
                    "StorageWorker per-event error (event_id=%s type=%s): %s",
                    getattr(event, "event_id", "?"),
                    getattr(event, "event_type", "?"),
                    e,
                    exc_info=True,
                )

        # Run upserts first (FK target).
        for run in runs_to_upsert:
            try:
                await self._db.upsert_run(run)
            except Exception as e:
                log.error("upsert_run failed for %s: %s", run.run_id, e, exc_info=True)

        # Single batched event write for ACID.
        if stored_events:
            try:
                await self._db.upsert_events_batch(stored_events)
            except Exception as e:
                log.error("upsert_events_batch failed (%d events): %s", len(stored_events), e, exc_info=True)

        # Apply terminal status updates.
        for run_id, status, completed_at, error in terminal_updates:
            try:
                await self._db.update_run_status(
                    run_id, status, completed_at=completed_at, error=error
                )
            except Exception as e:
                log.error("update_run_status failed for %s: %s", run_id, e, exc_info=True)

        # Apply per-run counter increments.
        for run_id, counts in counter_updates.items():
            try:
                await self._db.increment_run_counters(
                    run_id,
                    steps=counts["steps"],
                    tokens_in=counts["tokens_in"],
                    tokens_out=counts["tokens_out"],
                )
            except Exception as e:
                log.error("increment_run_counters failed for %s: %s", run_id, e, exc_info=True)

        # Export to OpenTelemetry (if configured), in event order so a span's start
        # is seen before its end. Best-effort: failures must not affect ingestion.
        if self._otel is not None:
            for _raw, stored in ws_pairs:
                try:
                    self._otel.handle(stored)
                except Exception as e:  # pragma: no cover - defensive
                    log.warning("otel export failed: %s", e)

        # Broadcast events to per-run subscribers (/ws/trace/{run_id}) after DB commit.
        for raw_event, stored in ws_pairs:
            try:
                msg = WSMessage(
                    msg_type="event",
                    run_id=raw_event.root_run_id,
                    payload=stored.model_dump(mode="json"),
                )
                await self._ws.broadcast(raw_event.root_run_id, msg)
            except Exception as e:  # pragma: no cover
                log.warning("ws broadcast failed: %s", e)

        # Broadcast run_update only for ROOT runs entering or leaving the
        # system. Per-event progress is delivered through the per-run
        # /ws/trace/{run_id} channel; the global /ws/runs feed only needs
        # lifecycle pings.
        #
        # Sub-runs (created for FK consistency by the elif branch in the loop
        # above) are intentionally NOT broadcast — at high concurrency with
        # nested LangGraph trees, every event can be a new sub-run, which
        # would make broadcast_all the dominant cost in the worker hot path.
        # We use the synthetic RUN_START event (emitted by the handler only
        # for events with parent_run_id is None) as the "root run started"
        # signal.
        for event in events:
            if event.event_type != EventType.RUN_START:
                continue
            try:
                run = self._raw_to_run(event)
                msg = WSMessage(
                    msg_type="run_update",
                    run_id=run.run_id,
                    payload={"run": run.model_dump(mode="json")},
                )
                await self._ws.broadcast_all(msg)
            except Exception as e:  # pragma: no cover
                log.warning("ws run_update (start) broadcast failed: %s", e)
        for run_id, status, completed_at, error in terminal_updates:
            try:
                payload = {
                    "run_id": run_id,
                    "root_run_id": run_id,
                    "status": status.value,
                    "completed_at": completed_at.isoformat() if completed_at else None,
                    "error_message": error,
                }
                msg = WSMessage(
                    msg_type="run_update",
                    run_id=run_id,
                    payload={"run": payload},
                )
                await self._ws.broadcast_all(msg)
            except Exception as e:  # pragma: no cover
                log.warning("ws run_update (terminal) broadcast failed for %s: %s", run_id, e)

        # Stats.
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._stats.last_write_latency_ms = elapsed_ms
        self._latencies.append(elapsed_ms)
        self._stats.p99_write_latency_ms = self._compute_p99()
        self._stats.queue_depth = self._queue.qsize()

    async def _build_stored_event(self, event: RawEvent) -> StoredEvent:
        """Compute duration, write blob if eligible, return StoredEvent."""
        duration_ms: int | None = None

        # Track start timestamps for duration pairing.
        if event.event_type in _START_TO_KEY:
            key = _START_TO_KEY[event.event_type]
            run_dict = self._start_timestamps.get(event.run_id)
            if run_dict is None:
                run_dict = OrderedDict()
                self._start_timestamps[event.run_id] = run_dict
                # Bound the outer map (run_ids that started but never ended).
                while len(self._start_timestamps) > self._start_timestamps_outer_cap:
                    self._start_timestamps.popitem(last=False)
            else:
                # Refresh outer recency on activity.
                self._start_timestamps.move_to_end(event.run_id)
            run_dict[key] = event.timestamp
            run_dict.move_to_end(key)
            # Bound per-run dict (FIFO oldest start type).
            while len(run_dict) > self._start_timestamps_inner_cap:
                run_dict.popitem(last=False)

        if event.event_type in _END_TO_START_KEY:
            key = _END_TO_START_KEY[event.event_type]
            run_dict = self._start_timestamps.get(event.run_id)
            if run_dict and key in run_dict:
                start_ts = run_dict.pop(key)
                duration_ms = max(
                    0, int((event.timestamp - start_ts).total_seconds() * 1000)
                )
                if not run_dict:
                    self._start_timestamps.pop(event.run_id, None)

        blob_path: str | None = None
        if event.full_blob_eligible and event.event_type in BLOB_ELIGIBLE_EVENTS:
            try:
                blob_path = await self._blob_store.write(
                    event.run_id, event.event_id, event.raw_payload
                )
            except Exception as e:
                log.warning(
                    "blob_store.write failed for event %s: %s", event.event_id, e
                )

        return StoredEvent(
            event_id=event.event_id,
            run_id=event.run_id,
            parent_run_id=event.parent_run_id,
            root_run_id=event.root_run_id,
            event_type=event.event_type,
            timestamp=event.timestamp,
            agent_name=event.agent_name,
            tool_name=event.tool_name,
            mcp_server=event.mcp_server,
            summary=event.summary,
            blob_path=blob_path,
            duration_ms=duration_ms,
            token_input=event.token_input,
            token_output=event.token_output,
            error_message=event.error_message,
        )

    @staticmethod
    def _raw_to_run(event: RawEvent) -> Run:
        return Run(
            run_id=event.run_id,
            root_run_id=event.root_run_id,
            tags=event.tags,
            status=RunStatus.RUNNING,
            started_at=event.timestamp,
        )

    def _compute_p99(self) -> float | None:
        if not self._latencies:
            return None
        sorted_l = sorted(self._latencies)
        idx = max(0, int(len(sorted_l) * 0.99) - 1)
        return sorted_l[idx]
