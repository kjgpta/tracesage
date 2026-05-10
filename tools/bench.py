"""Synthetic-event throughput bench.

Fires N synthetic RawEvents through the tracer at maximum rate, then waits for
the worker to drain the queue. Reports sustained events/second.

Usage:
    python tools/bench.py [--n 5000] [--runs 100] [--blob-rate 0.2]

Output is intentionally machine-parseable for CI:
    BENCH N=5000 ELAPSED=4.21 EV_PER_SEC=1188 DROPPED=0
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

# allow running without install: src on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tracelens import TraceLens, TraceLensConfig, EventType, RawEvent


async def run_bench(n: int, num_runs: int, blob_rate: float, data_dir: Path) -> dict:
    """Fire n events spread across num_runs run_ids; blob_rate fraction are blob-eligible."""
    cfg = TraceLensConfig(
        data_dir=data_dir,
        port=0,
        queue_maxsize=100_000,
        worker_batch_size=100,
        worker_batch_timeout=0.05,
        log_level="ERROR",
    )
    tracer = await TraceLens.create(config=cfg, start_server=False)

    started = time.perf_counter()
    for i in range(n):
        run_id = f"bench-run-{i % num_runs}"
        is_blob = random.random() < blob_rate
        event = RawEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.LLM_END if is_blob else EventType.CHAIN_START,
            run_id=run_id,
            root_run_id=run_id,
            timestamp=datetime.now(UTC),
            agent_name=f"agent-{i % 5}",
            summary=f"bench event {i}",
            full_blob_eligible=is_blob,
            raw_payload={"i": i, "data": "x" * 500} if is_blob else {},
            token_input=100 if is_blob else None,
            token_output=50 if is_blob else None,
        )
        tracer.emit(event)

    enqueued = time.perf_counter()
    # Wait for drain (3 minutes — Windows NTFS gzip writes are slow)
    try:
        await asyncio.wait_for(tracer._queue.join(), timeout=180.0)
    except TimeoutError:
        pass
    drained = time.perf_counter()

    stats = tracer.stats
    await tracer.stop()

    return {
        "n": n,
        "enqueue_s": enqueued - started,
        "elapsed_s": drained - started,
        "ev_per_sec": n / (drained - started) if drained > started else 0.0,
        "dropped": stats.events_dropped,
        "processed": stats.events_processed,
        "p99_write_ms": stats.p99_write_latency_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=5000, help="event count")
    parser.add_argument("--runs", type=int, default=100, help="distinct run_ids to spread events")
    parser.add_argument(
        "--blob-rate", type=float, default=0.2, help="fraction of events that are blob-eligible"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("./bench_data"),
        help="data directory for the bench",
    )
    args = parser.parse_args()

    result = asyncio.run(run_bench(args.n, args.runs, args.blob_rate, args.data_dir))

    # Human-readable summary
    print(f"  events   : {result['n']}")
    print(f"  enqueued : {result['enqueue_s']:.2f}s")
    print(f"  total    : {result['elapsed_s']:.2f}s")
    print(f"  rate     : {result['ev_per_sec']:.0f} ev/s")
    print(f"  dropped  : {result['dropped']}")
    print(f"  processed: {result['processed']}")
    if result["p99_write_ms"] is not None:
        print(f"  p99 write: {result['p99_write_ms']:.2f} ms")

    # CI-parseable line
    print(
        f"\nBENCH N={result['n']} ELAPSED={result['elapsed_s']:.2f} "
        f"EV_PER_SEC={result['ev_per_sec']:.0f} DROPPED={result['dropped']}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
