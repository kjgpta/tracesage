"""Integration test fixtures. Boots a real TraceSage stack against a temp data dir."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from tracesage.config import TraceSageConfig


async def wait_for_drain(tracer, timeout: float = 30.0) -> None:
    """Block until the tracer's queue has been fully processed by the worker.

    Use this after invoking a graph in tests, before asserting on the DB —
    otherwise events may still be in flight.

    `timeout` is a generous *ceiling*, not a delay: on a healthy run
    ``queue.join()`` returns near-instantly. A floor of 30s is enforced so
    callers passing a small value still tolerate slow CI runners (notably
    Windows, where sqlite + blob I/O is far slower and a 5s budget can expire
    before the worker finishes a deep nested-graph trace). Callers needing
    longer (e.g. the 100-run stress test) pass a larger value explicitly.
    """
    budget = max(timeout, 30.0)
    try:
        await asyncio.wait_for(tracer._queue.join(), timeout=budget)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    # Brief settle for trailing broadcasts/counter updates
    await asyncio.sleep(0.1)


@pytest_asyncio.fixture
async def integration_tracer(tmp_path: Path) -> AsyncIterator:
    """Real TraceSage instance pointed at a temp data directory.

    Server is NOT started by this fixture (avoid port conflicts in parallel test runs).
    Tests that need the HTTP server should start it explicitly.
    """
    from tracesage.tracer import TraceSage  # local import — package may not have it yet

    cfg = TraceSageConfig(
        data_dir=tmp_path / "tracesage_test",
        port=0,  # not used; server not started
        queue_maxsize=10_000,
        worker_batch_size=20,
        worker_batch_timeout=0.05,
    )
    tracer = await TraceSage.create(config=cfg, start_server=False)
    try:
        yield tracer
    finally:
        await tracer.stop()


@pytest_asyncio.fixture
async def integration_tracer_with_server(tmp_path: Path) -> AsyncIterator:
    """As above but with the HTTP server started on an ephemeral port."""
    from tracesage.tracer import TraceSage

    cfg = TraceSageConfig(
        data_dir=tmp_path / "tracesage_test",
        port=0,
        queue_maxsize=10_000,
    )
    tracer = await TraceSage.create(config=cfg, start_server=True)
    try:
        yield tracer
    finally:
        await tracer.stop()
