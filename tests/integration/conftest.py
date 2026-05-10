"""Integration test fixtures. Boots a real TraceLens stack against a temp data dir."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from tracelens.config import TraceLensConfig


async def wait_for_drain(tracer, timeout: float = 3.0) -> None:
    """Block until the tracer's queue has been fully processed by the worker.

    Use this after invoking a graph in tests, before asserting on the DB —
    otherwise events may still be in flight.
    """
    try:
        await asyncio.wait_for(tracer._queue.join(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    # Brief settle for trailing broadcasts/counter updates
    await asyncio.sleep(0.1)


@pytest_asyncio.fixture
async def integration_tracer(tmp_path: Path) -> AsyncIterator:
    """Real TraceLens instance pointed at a temp data directory.

    Server is NOT started by this fixture (avoid port conflicts in parallel test runs).
    Tests that need the HTTP server should start it explicitly.
    """
    from tracelens.tracer import TraceLens  # local import — package may not have it yet

    cfg = TraceLensConfig(
        data_dir=tmp_path / "tracelens_test",
        port=0,  # not used; server not started
        queue_maxsize=10_000,
        worker_batch_size=20,
        worker_batch_timeout=0.05,
    )
    tracer = await TraceLens.create(config=cfg, start_server=False)
    try:
        yield tracer
    finally:
        await tracer.stop()


@pytest_asyncio.fixture
async def integration_tracer_with_server(tmp_path: Path) -> AsyncIterator:
    """As above but with the HTTP server started on an ephemeral port."""
    from tracelens.tracer import TraceLens

    cfg = TraceLensConfig(
        data_dir=tmp_path / "tracelens_test",
        port=0,
        queue_maxsize=10_000,
    )
    tracer = await TraceLens.create(config=cfg, start_server=True)
    try:
        yield tracer
    finally:
        await tracer.stop()
