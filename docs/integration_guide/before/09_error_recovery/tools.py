"""Tools for the error-recovery pipeline.

`flaky_fetch` raises on a deterministic schedule (every 3rd call) so the
demo exercises both the success path and the error/fallback path without
random flakiness.
"""
from __future__ import annotations

import itertools
from typing import Iterator

from langchain_core.tools import tool


# Deterministic flakiness: positions [3, 6, 9, ...] raise. Cycled across calls.
_call_counter: Iterator[int] = itertools.count(start=1)


@tool
def flaky_fetch(url: str) -> str:
    """Fetch a URL. Raises every 3rd call to simulate transient failures."""
    n = next(_call_counter)
    if n % 3 == 0:
        raise RuntimeError(f"transient network error fetching {url} (call #{n})")
    return f"OK: payload from {url} (call #{n}, 1.2 KB)"


@tool
def fallback_fetch(url: str) -> str:
    """Reliable fallback fetch — never fails. Used when flaky_fetch errors."""
    return f"FALLBACK OK: cached payload for {url} (3.4 KB, may be stale)"


@tool
def process_data(payload: str) -> str:
    """Process a fetched payload (no-op for the demo)."""
    return f"processed {len(payload)} bytes -> records=12"
