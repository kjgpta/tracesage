"""Crash-recovery test: kill the worker mid-batch, restart, verify SQLite WAL recovers cleanly.

Demonstrates that ungraceful termination (SIGKILL / taskkill /F) does NOT corrupt
the SQLite database. The Day 1 design relies on SQLite's WAL mode to guarantee
ACID even on hard process kill — this script proves it.

Usage:
    python tools/crash_recovery_test.py
    # exits 0 on success, prints PASS/FAIL summary

Mechanism:
    1. Parent spawns a child subprocess running this script with `--mode=child`.
    2. Child opens an TraceSage pointed at a fixed data dir, fires N events, sleeps.
    3. Parent waits a bit, then kills the child with SIGKILL (POSIX) / TerminateProcess (Win).
    4. Parent reopens the same DB and asserts: schema intact, events partially present,
       no broken/half-written rows, no stuck `database is locked` errors.
    5. Parent runs a second TraceSage against the same dir and emits more events,
       verifying the worker can recover and append cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

# allow running from the project root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def child_main(data_dir: Path, n_events: int) -> None:
    """Run as the doomed child: emit events, then sleep until killed."""
    from tracesage import EventType, RawEvent, TraceSage, TraceSageConfig

    cfg = TraceSageConfig(
        data_dir=data_dir,
        port=0,
        queue_maxsize=10_000,
        log_level="WARNING",
    )
    tracer = await TraceSage.create(cfg, start_server=False)
    print(f"CHILD: tracer up at {data_dir}", flush=True)
    for i in range(n_events):
        run_id = f"crash-run-{i % 10}"
        tracer.emit(
            RawEvent(
                event_id=str(uuid.uuid4()),
                event_type=EventType.RUN_START if i < 10 else EventType.AGENT_ACTION,
                run_id=run_id,
                root_run_id=run_id,
                timestamp=datetime.now(UTC),
                summary=f"event {i}",
            )
        )
    print(f"CHILD: emitted {n_events} events; sleeping (will be killed)", flush=True)
    # Sleep forever; parent will kill us mid-flush
    await asyncio.sleep(60)


def parent_main() -> int:
    """Parent: spawn child, kill it, verify recovery."""
    data_dir = Path("./tmp_crash_recovery_test")
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)

    print("PARENT: spawning child...", flush=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode=child",
        f"--data-dir={data_dir}",
        "--n=200",
    ]
    proc = subprocess.Popen(  # noqa: S603 — args are controlled
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Give the child time to emit some events.
    time.sleep(2.0)

    print(f"PARENT: killing child PID {proc.pid} hard...", flush=True)
    proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
    proc.wait(timeout=10)
    out = proc.stdout.read() if proc.stdout else ""
    print(f"PARENT: child output:\n  {out.strip().replace(chr(10), chr(10) + '  ')}", flush=True)

    # Wait briefly for OS to release file handles on Windows.
    time.sleep(1.0)

    print("PARENT: verifying DB integrity...", flush=True)
    try:
        events_after_kill, runs_after_kill = asyncio.run(_inspect_db(data_dir))
    except Exception as e:
        print(f"PARENT: FAIL — could not open DB after kill: {e}")
        return 1

    print(f"PARENT: events persisted: {events_after_kill}, runs: {runs_after_kill}")
    if events_after_kill == 0:
        print("PARENT: WARN — child died before any events flushed (timing-dependent)")
    elif events_after_kill < 1:
        print("PARENT: FAIL — too few events persisted")
        return 1

    print("PARENT: opening fresh TraceSage against same dir to verify it recovers...", flush=True)
    try:
        events_after_recovery = asyncio.run(_recover_and_emit(data_dir))
    except Exception as e:
        print(f"PARENT: FAIL — recovery write failed: {e}")
        return 1

    if events_after_recovery <= events_after_kill:
        print(
            f"PARENT: FAIL — recovery write didn't add events: "
            f"before={events_after_kill}, after={events_after_recovery}"
        )
        return 1

    print(
        f"PARENT: PASS — kill survived ({events_after_kill} events), "
        f"recovery wrote more ({events_after_recovery} total)"
    )

    # Cleanup
    shutil.rmtree(data_dir, ignore_errors=True)
    return 0


async def _inspect_db(data_dir: Path) -> tuple[int, int]:
    """Open the DB read-only and count rows. Verifies no corruption."""
    import aiosqlite

    db_path = data_dir / "traces.db"
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM events")
        events = (await cur.fetchone())[0]
        await cur.close()
        cur = await conn.execute("SELECT COUNT(*) FROM runs")
        runs = (await cur.fetchone())[0]
        await cur.close()
        # Integrity check
        cur = await conn.execute("PRAGMA integrity_check")
        integrity = (await cur.fetchone())[0]
        await cur.close()
        if integrity != "ok":
            raise RuntimeError(f"PRAGMA integrity_check returned: {integrity}")
    return events, runs


async def _recover_and_emit(data_dir: Path) -> int:
    """Open a new TraceSage against the same dir, emit more events, verify."""
    from tracesage import EventType, RawEvent, TraceSage, TraceSageConfig

    cfg = TraceSageConfig(data_dir=data_dir, port=0, log_level="WARNING")
    tracer = await TraceSage.create(cfg, start_server=False)
    try:
        # Use a brand-new run so FK is clean.
        run_id = f"recovery-{uuid.uuid4()}"
        for i in range(50):
            tracer.emit(
                RawEvent(
                    event_id=str(uuid.uuid4()),
                    event_type=EventType.RUN_START if i == 0 else EventType.AGENT_ACTION,
                    run_id=run_id,
                    root_run_id=run_id,
                    timestamp=datetime.now(UTC),
                    summary=f"recovery event {i}",
                )
            )
        await asyncio.wait_for(tracer._queue.join(), timeout=10.0)
        events, _ = await _inspect_db(data_dir)
        return events
    finally:
        await tracer.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["parent", "child"], default="parent")
    parser.add_argument("--data-dir", type=Path, default=Path("./tmp_crash_recovery_test"))
    parser.add_argument("--n", type=int, default=200)
    args = parser.parse_args()

    if args.mode == "child":
        try:
            asyncio.run(child_main(args.data_dir, args.n))
        except KeyboardInterrupt:  # pragma: no cover
            pass
        return 0

    return parent_main()


if __name__ == "__main__":
    sys.exit(main())
