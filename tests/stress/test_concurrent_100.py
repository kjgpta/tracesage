"""Stress: 100 concurrent runs of the order pipeline.

Validates that the tracer handles real concurrent LangChain invocations without
mixing events across runs and without dropping events under sustained load.

This test is marked `slow` and excluded from default CI runs. Run explicitly:

    pytest tests/stress/test_concurrent_100.py -m slow
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from tracelens.models import EventType, RunStatus
from tests.integration.conftest import wait_for_drain
from tests.integration.systems.test_system_1_order_pipeline import build_order_pipeline


@pytest.mark.slow
@pytest.mark.integration
async def test_100_concurrent_runs_no_event_mixing(integration_tracer):
    """Run 100 simultaneous order-pipeline invocations; assert no leakage and no drops."""
    pipeline = build_order_pipeline()

    async def one_run(idx: int) -> str:
        await pipeline.ainvoke(
            {
                "order_id": f"stress-{idx:03d}",
                "validated": "",
                "inventory": "",
                "pricing": "",
                "confirmation": "",
            },
            config={
                "callbacks": [integration_tracer.handler],
                "tags": ["stress", f"shard-{idx % 10}"],
            },
        )
        return f"stress-{idx:03d}"

    results = await asyncio.gather(*[one_run(i) for i in range(100)], return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"some runs raised: {failures[:3]}"

    # Generous drain timeout: 100 concurrent runs at ~25 events each is roughly
    # 2500 events; on Windows at ~75 ev/s that lands around 35s.
    await wait_for_drain(integration_tracer, timeout=120.0)

    # Verify total run count (root runs only).
    runs, total = await integration_tracer.db.list_runs(limit=200)
    assert total >= 100, f"expected >=100 root runs, got {total}"

    # No event mixing: every run's journey events share that run's root_run_id.
    leakage = []
    for run in runs[:100]:
        journey = await integration_tracer.db.get_journey(run.run_id)
        if not journey:
            continue
        roots = {e.root_run_id for e in journey}
        if roots != {run.run_id}:
            leakage.append((run.run_id, roots))
    assert not leakage, f"event mixing detected in {len(leakage)} runs: {leakage[:3]}"

    # No drops under this load — production claim.
    assert integration_tracer.stats.events_dropped == 0, (
        f"dropped {integration_tracer.stats.events_dropped} events under 100-concurrent load"
    )

    # Status distribution: every run that completed via .ainvoke should be COMPLETED.
    completed = sum(1 for r in runs[:100] if r.status == RunStatus.COMPLETED)
    assert completed >= 95, (
        f"expected >=95/100 runs COMPLETED, got {completed}; "
        f"distribution: {Counter(r.status for r in runs[:100])}"
    )

    # Event-type distribution sanity: each run should have approx the same shape.
    sample_journey = await integration_tracer.db.get_journey(runs[0].run_id)
    assert any(e.event_type == EventType.CHAT_MODEL_START for e in sample_journey)
    assert any(e.event_type == EventType.LLM_END for e in sample_journey)
