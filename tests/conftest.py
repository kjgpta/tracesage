"""Shared pytest fixtures. Agents add specialized fixtures in their own test files."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Per-test data directory. Isolated and cleaned by pytest's tmp_path."""
    data_dir = tmp_path / "tracelens_test"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "blobs").mkdir(exist_ok=True)
    return data_dir


@pytest_asyncio.fixture
async def event_queue() -> AsyncIterator[asyncio.Queue]:
    """Fresh asyncio queue. maxsize matches a real installation."""
    q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    yield q
