"""Tests for the TraceLens top-level orchestrator.

These tests require Agent A's storage modules (SQLiteBackend, BlobStore) — they
are imported lazily inside TraceLens.create. Server startup is disabled.
"""
from __future__ import annotations

import asyncio
import gc
import json
import uuid
import warnings
from datetime import UTC, datetime

import pytest

from tracelens.config import TraceLensConfig
from tracelens.models import EventType, RawEvent
from tracelens.tracer import TraceLens


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _raw(event_type: EventType, run_id: str, parent_run_id: str | None = None) -> RawEvent:
    return RawEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        run_id=run_id,
        parent_run_id=parent_run_id,
        root_run_id=run_id if parent_run_id is None else parent_run_id,
        timestamp=_utcnow(),
        summary=event_type.value,
    )


# ---------------------------------------------------------------------- 1
async def test_sampling_drops_events(tmp_data_dir) -> None:
    cfg = TraceLensConfig(data_dir=tmp_data_dir, sample_rate=0.0, queue_maxsize=100)
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        run_id = str(uuid.uuid4())
        for _ in range(10):
            tracer.emit(_raw(EventType.AGENT_ACTION, run_id))
        # Queue must remain empty (sample_rate=0.0 drops everything at root).
        assert tracer._queue.qsize() == 0
        assert tracer.stats.events_sampled_out == 10
    finally:
        await tracer.stop()


# ---------------------------------------------------------------------- 2
async def test_per_run_cap_circuit_breaker(tmp_data_dir) -> None:
    cfg = TraceLensConfig(
        data_dir=tmp_data_dir,
        per_run_event_cap=10,
        queue_maxsize=1000,
        sample_rate=1.0,
    )
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        run_id = str(uuid.uuid4())
        for _ in range(20):
            tracer.emit(_raw(EventType.AGENT_ACTION, run_id))

        # Only 10 enqueued; remaining 10 throttled.
        # Drain the queue without running the worker so we can count.
        await asyncio.sleep(0)  # allow call_soon_threadsafe scheduling (same loop is direct, but be safe)
        # The worker may have already processed some; combine queue + processed counts.
        # Simpler check: throttled run flag set and per-run counter capped at 10.
        assert tracer.stats.runs_throttled >= 1
        assert tracer._run_event_counts[run_id] == 10
    finally:
        await tracer.stop()


# ---------------------------------------------------------------------- 3
async def test_shutdown_releases_resources(tmp_data_dir) -> None:
    """Shutdown must terminate the worker and not leak ResourceWarnings from our code.

    aiosqlite owns a background thread that finalizes the sync sqlite3 connection
    asynchronously. We filter those out and assert no other ResourceWarnings appear,
    plus assert worker task is done.
    """
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        cfg = TraceLensConfig(data_dir=tmp_data_dir, sample_rate=1.0, queue_maxsize=100)
        tracer = await TraceLens.create(cfg, start_server=False)
        run_id = str(uuid.uuid4())
        # Synthetic RUN_START so SQLite FK checks pass.
        tracer.emit(
            RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.RUN_START,
                run_id=run_id,
                root_run_id=run_id,
                timestamp=_utcnow(),
                summary="run",
            )
        )
        for _ in range(5):
            tracer.emit(_raw(EventType.AGENT_ACTION, run_id))

        # Let the worker drain.
        await asyncio.sleep(0.2)
        await tracer.stop()

        # Worker task must be done.
        assert tracer._worker_task.done()
        # No tasks left for our worker.
        gc.collect()

    # aiosqlite spawns a thread-local event loop per connection on Windows.
    # When the connection finalizes during gc, both the sqlite3.Connection
    # and the inner ProactorEventLoop can emit ResourceWarning — these are
    # aiosqlite artifacts on Windows, not leaks in our code. Filter them.
    aiosqlite_artifact_markers = ("sqlite3.Connection", "event loop")
    leaked = [
        w
        for w in captured
        if issubclass(w.category, ResourceWarning)
        and not any(marker in str(w.message) for marker in aiosqlite_artifact_markers)
    ]
    assert not leaked, [str(w.message) for w in leaked]


# ---------------------------------------------------------------------- 4 (bonus)
async def test_get_or_set_root_propagation(tmp_data_dir) -> None:
    cfg = TraceLensConfig(data_dir=tmp_data_dir, queue_maxsize=100)
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        root = "root-123"
        child = "child-456"
        grand = "grand-789"

        assert tracer.get_or_set_root(root, None) == root
        assert tracer.get_or_set_root(child, root) == root
        assert tracer.get_or_set_root(grand, child) == root
    finally:
        await tracer.stop()


async def test_lru_eviction_caps_unbounded_state(tmp_data_dir) -> None:
    """Throttling/counter dicts must FIFO-evict at the cap.

    Regression for the audit finding that _throttled_runs/_run_event_counts grew
    unbounded. (Sampling is now deterministic and keeps no per-run state.)
    """
    cfg = TraceLensConfig(data_dir=tmp_data_dir, queue_maxsize=10_000, sample_rate=1.0)
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        # Shrink the cap so the test runs in milliseconds.
        tracer._run_state_cap = 100

        # Emit one event from each of 250 distinct root runs.
        for i in range(250):
            run_id = f"r-{i:04d}"
            tracer.emit(
                RawEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=EventType.RUN_START,
                    run_id=run_id,
                    parent_run_id=None,
                    root_run_id=run_id,
                    timestamp=_utcnow(),
                    summary="x",
                )
            )

        # The per-run counter map must respect the cap.
        assert len(tracer._run_event_counts) <= 100
        # Oldest entries evicted (FIFO): r-0000 should be gone, recent r-0249 kept.
        assert "r-0249" in tracer._run_event_counts
        assert "r-0000" not in tracer._run_event_counts
    finally:
        await tracer.stop()


async def test_deterministic_sampling_is_stable(tmp_data_dir) -> None:
    """_is_sampled_in must be deterministic per root (no mid-run flip after eviction),
    and a 50% rate must sample some-but-not-all distinct roots.
    """
    cfg = TraceLensConfig(data_dir=tmp_data_dir, queue_maxsize=100, sample_rate=0.5)
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        # Determinism: repeated calls for the same root return the same verdict.
        root = "stable-root-abc"
        first = tracer._is_sampled_in(root)
        for _ in range(50):
            assert tracer._is_sampled_in(root) == first

        # Roughly-but-not-all roots are sampled in at rate=0.5.
        roots = [f"root-{i:05d}" for i in range(1000)]
        sampled = sum(1 for r in roots if tracer._is_sampled_in(r))
        assert 0 < sampled < len(roots)
        # Should land near the 50% mark; allow generous slack for hash distribution.
        assert 300 < sampled < 700

        # Edge rates short-circuit deterministically.
        tracer._config.sample_rate = 1.0
        assert tracer._is_sampled_in("anything") is True
        tracer._config.sample_rate = 0.0
        assert tracer._is_sampled_in("anything") is False
    finally:
        await tracer.stop()


async def test_redaction_scrubs_summary_and_payload(tmp_data_dir) -> None:
    """Opt-in redaction must scrub configured patterns from the summary, the
    error_message, and (recursively) the raw_payload that becomes the blob.
    """
    cfg = TraceLensConfig(
        data_dir=tmp_data_dir,
        queue_maxsize=100,
        redact_patterns=[r"sk-[A-Za-z0-9]+"],
    )
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        run_id = str(uuid.uuid4())
        event = RawEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.AGENT_ACTION,
            run_id=run_id,
            parent_run_id=None,
            root_run_id=run_id,
            timestamp=_utcnow(),
            summary="calling with key sk-SECRET123",
            raw_payload={
                "prompt": "use sk-SECRET123 now",
                "messages": ["nothing", "still sk-SECRET123 here"],
                "nested": {"token": "sk-SECRET123"},
            },
            error_message="failed using sk-SECRET123",
        )

        tracer._redact_event(event)

        assert "sk-SECRET123" not in event.summary
        assert tracer._config.redact_replacement in event.summary
        assert event.error_message is not None
        assert "sk-SECRET123" not in event.error_message
        assert "sk-SECRET123" not in json.dumps(event.raw_payload)
        assert tracer._config.redact_replacement in json.dumps(event.raw_payload)
    finally:
        await tracer.stop()


async def test_redaction_default_is_noop(tmp_data_dir) -> None:
    """Default config (no patterns) must leave the event untouched."""
    cfg = TraceLensConfig(data_dir=tmp_data_dir, queue_maxsize=100)
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        assert tracer._redactors == []
        run_id = str(uuid.uuid4())
        event = RawEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.AGENT_ACTION,
            run_id=run_id,
            parent_run_id=None,
            root_run_id=run_id,
            timestamp=_utcnow(),
            summary="key sk-SECRET123 stays",
            raw_payload={"prompt": "sk-SECRET123 stays"},
            error_message="sk-SECRET123 stays",
        )

        tracer._redact_event(event)

        assert event.summary == "key sk-SECRET123 stays"
        assert event.raw_payload == {"prompt": "sk-SECRET123 stays"}
        assert event.error_message == "sk-SECRET123 stays"
    finally:
        await tracer.stop()


async def test_redaction_handles_deep_and_cyclic_payload(tmp_data_dir) -> None:
    """Deeply nested or self-referential raw_payload must not raise/hang, and must
    fail CLOSED (top-level secrets still redacted, over-deep subtrees replaced)."""
    cfg = TraceLensConfig(data_dir=tmp_data_dir, redact_patterns=[r"sk-[A-Za-z0-9]+"])
    tracer = await TraceLens.create(cfg, start_server=False)
    try:
        # Nested far beyond the depth cap.
        deep: dict = {}
        cur = deep
        for _ in range(200):
            cur["child"] = {}
            cur = cur["child"]
        cur["secret"] = "sk-DEEP"

        # Self-referential cycle.
        cyc: dict = {"secret": "sk-CYC"}
        cyc["self"] = cyc

        for payload in (deep, cyc):
            event = RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.LLM_END,
                run_id="r",
                parent_run_id=None,
                root_run_id="r",
                timestamp=_utcnow(),
                summary="key sk-TOP here",
                raw_payload=payload,
            )
            tracer._redact_event(event)  # must terminate (no RecursionError/hang)
            assert "sk-TOP" not in event.summary
            # Fail-closed: no secret leaks through, and the structure is finite/JSON-able.
            dumped = json.dumps(event.raw_payload)
            assert "sk-DEEP" not in dumped
            assert "sk-CYC" not in dumped

        # The cyclic payload's top-level secret is redacted (not just dropped).
        cyc2: dict = {"secret": "sk-CYC2"}
        cyc2["self"] = cyc2
        event2 = RawEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.LLM_END,
            run_id="r",
            parent_run_id=None,
            root_run_id="r",
            timestamp=_utcnow(),
            summary="s",
            raw_payload=cyc2,
        )
        tracer._redact_event(event2)
        assert event2.raw_payload["secret"] == cfg.redact_replacement
    finally:
        await tracer.stop()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
