"""Tests for the tracesage CLI. Uses Typer's CliRunner for end-to-end invocation."""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracesage.cli import app
from tracesage.config import TraceSageConfig
from tracesage.storage import SQLiteBackend

runner = CliRunner()


def _bootstrap_empty_db(tmp_path: Path) -> Path:
    """Create an empty but valid tracesage data dir for read-only tests.

    The new CLI fail-loud behavior (B11.b) refuses to silently create a fresh
    DB on a typo'd --data-dir, so tests that need an empty data dir must
    pre-initialize one.
    """
    cfg = TraceSageConfig(data_dir=tmp_path)
    cfg.ensure_data_dirs()

    async def _init() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        await db.close()

    asyncio.run(_init())
    return tmp_path


def test_version_prints_version() -> None:
    from tracesage import __version__

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "tracesage" in result.stdout
    assert __version__ in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("serve", "export", "stats", "gc", "version"):
        assert cmd in result.stdout, f"expected {cmd!r} in help output"


def test_stats_on_empty_dir(tmp_path: Path) -> None:
    """Stats on a pre-initialized but empty DB should print zero counts."""
    _bootstrap_empty_db(tmp_path)
    result = runner.invoke(app, ["stats", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "total_runs: 0" in result.stdout
    assert "running: 0" in result.stdout


def test_stats_missing_dir_fails_loudly(tmp_path: Path) -> None:
    """B11.b: stats on a non-existent dir must exit non-zero with a message."""
    bogus = tmp_path / "no_such_dir"
    result = runner.invoke(app, ["stats", "--data-dir", str(bogus)])
    assert result.exit_code == 1
    # Friendly error goes to stderr; CliRunner mixes by default.
    out = result.output + (result.stderr or "")
    assert "does not exist" in out or "no traces.db" in out


def test_stats_dir_without_db_fails_loudly(tmp_path: Path) -> None:
    """B11.b: dir exists but has no traces.db -> exit 1."""
    result = runner.invoke(app, ["stats", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    out = result.output + (result.stderr or "")
    assert "no traces.db" in out


def test_export_requires_run_id_or_all(tmp_path: Path) -> None:
    _bootstrap_empty_db(tmp_path)
    result = runner.invoke(app, ["export", "--data-dir", str(tmp_path)])
    assert result.exit_code != 0


def test_export_unknown_run_exits_nonzero(tmp_path: Path) -> None:
    """Bug fix: --run-id not found must exit non-zero so scripts can detect it."""
    _bootstrap_empty_db(tmp_path)
    out_file = tmp_path / "out.jsonl"
    result = runner.invoke(
        app,
        ["export", "--data-dir", str(tmp_path),
         "--run-id", "does-not-exist", "--output", str(out_file)],
    )
    assert result.exit_code == 1, f"expected exit 1, got {result.exit_code}"
    out = result.output + (result.stderr or "")
    assert "not found" in out


def test_gc_dry_run_on_empty_dir(tmp_path: Path) -> None:
    _bootstrap_empty_db(tmp_path)
    result = runner.invoke(
        app,
        ["gc", "--data-dir", str(tmp_path), "--max-runs", "10", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "nothing to do" in result.stdout


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_gc_rejects_non_positive_max_runs(tmp_path: Path, bad: str) -> None:
    _bootstrap_empty_db(tmp_path)
    result = runner.invoke(app, ["gc", "--data-dir", str(tmp_path), "--max-runs", bad])
    assert result.exit_code == 2
    assert "--max-runs must be >= 1" in result.output


def test_gc_message_is_ascii_only(tmp_path: Path) -> None:
    """B10: gc must not emit non-ASCII chars that crash on Windows cp1252."""
    _bootstrap_empty_db(tmp_path)
    result = runner.invoke(
        app,
        ["gc", "--data-dir", str(tmp_path), "--max-runs", "1000"],
    )
    assert result.exit_code == 0
    # The pre-fix version had `≤` (U+2264). Verify that's gone.
    assert "≤" not in result.stdout
    # The replacement should be `<=`.
    assert "<=" in result.stdout


def test_export_jsonl_format(tmp_path: Path) -> None:
    """Inserting a run+events via the backend, then exporting, should yield valid JSONL."""
    import asyncio
    from datetime import datetime, timezone

    from tracesage.config import TraceSageConfig
    from tracesage.models import EventType, Run, RunStatus, StoredEvent
    from tracesage.storage import SQLiteBackend

    cfg = TraceSageConfig(data_dir=tmp_path)
    cfg.ensure_data_dirs()

    async def setup() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        run = Run(
            run_id="r1",
            root_run_id="r1",
            tags=[],
            status=RunStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
        )
        await db.upsert_run(run)
        for i in range(3):
            await db.upsert_event(
                StoredEvent(
                    event_id=f"e{i}",
                    run_id="r1",
                    parent_run_id=None,
                    root_run_id="r1",
                    event_type=EventType.CHAIN_START,
                    timestamp=datetime.now(timezone.utc),
                    summary=f"event {i}",
                )
            )
        await db.close()

    asyncio.run(setup())

    out = tmp_path / "exp.jsonl"
    result = runner.invoke(
        app, ["export", "--data-dir", str(tmp_path), "--run-id", "r1", "--output", str(out)]
    )
    assert result.exit_code == 0, result.stdout
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 4, f"expected run + 3 events, got {len(lines)}"
    # First line should be the run row
    first = json.loads(lines[0])
    assert first["_kind"] == "run"
    assert first["run_id"] == "r1"
    # Subsequent lines are events
    events = [json.loads(l) for l in lines[1:]]
    assert all(e["_kind"] == "event" for e in events)
    assert {e["event_id"] for e in events} == {"e0", "e1", "e2"}


def _seed_run(tmp_path: Path) -> Path:
    """Seed a data dir with one run + three events; return the data dir."""
    from datetime import datetime, timezone

    from tracesage.models import EventType, Run, RunStatus, StoredEvent

    cfg = TraceSageConfig(data_dir=tmp_path)
    cfg.ensure_data_dirs()

    async def setup() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        run = Run(
            run_id="r1",
            root_run_id="r1",
            tags=[],
            status=RunStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
        )
        await db.upsert_run(run)
        for i in range(3):
            await db.upsert_event(
                StoredEvent(
                    event_id=f"e{i}",
                    run_id="r1",
                    parent_run_id=None,
                    root_run_id="r1",
                    event_type=EventType.CHAIN_START,
                    timestamp=datetime.now(timezone.utc),
                    summary=f"event {i}",
                )
            )
        await db.close()

    asyncio.run(setup())
    return tmp_path


def test_export_import_round_trip(tmp_path: Path) -> None:
    """export from one data dir, import into a second, run must be present."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _seed_run(src)

    exp = tmp_path / "exp.jsonl"
    result = runner.invoke(
        app, ["export", "--data-dir", str(src), "--run-id", "r1", "--output", str(exp)]
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        app, ["import", "--data-dir", str(dst), "--input", str(exp)]
    )
    assert result.exit_code == 0, result.output
    assert "imported 1 runs" in result.output

    # The run must now be present in the destination data dir.
    cfg = TraceSageConfig(data_dir=dst)

    async def _check() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        try:
            run = await db.get_run("r1")
            assert run is not None
            assert run.run_id == "r1"
            events = await db.get_journey("r1")
            assert {e.event_id for e in events} == {"e0", "e1", "e2"}
        finally:
            await db.close()

    asyncio.run(_check())


def test_export_import_round_trip_positional_args(tmp_path: Path) -> None:
    """export RUN_ID / import FILE positionally (consistent with show/watch/view),
    in addition to the --run-id / --input flags."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _seed_run(src)

    exp = tmp_path / "exp.jsonl"
    # run id given POSITIONALLY, not via --run-id
    result = runner.invoke(
        app, ["export", "--data-dir", str(src), "r1", "--output", str(exp)]
    )
    assert result.exit_code == 0, result.output

    # input file given POSITIONALLY, not via --input
    result = runner.invoke(
        app, ["import", "--data-dir", str(dst), str(exp)]
    )
    assert result.exit_code == 0, result.output
    assert "imported 1 runs" in result.output


def test_export_requires_a_run_id_even_positionally(tmp_path: Path) -> None:
    _bootstrap_empty_db(tmp_path)
    result = runner.invoke(app, ["export", "--data-dir", str(tmp_path)])
    assert result.exit_code != 0
    assert "run id" in result.output.lower() or "--all" in result.output


def test_python_dash_m_entrypoint_runs() -> None:
    """`python -m tracesage` must work (a __main__.py exists), not just the
    `tracesage` console script."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "tracesage", "version"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "tracesage" in result.stdout


def test_export_import_round_trip_preserves_nested_subrun_events(tmp_path: Path) -> None:
    """Regression: a trace with sub-runs (events whose run_id != root) must survive
    export → import. `export` only lists top-level runs, so import must synthesize a
    runs row for each sub-run's run_id or those events fail the events.run_id FK."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    # The demo seeds a nested run: root chain + sub-run llm + sub-run tool (6 events).
    seed = runner.invoke(app, ["demo", "-d", str(src), "--check"])
    assert seed.exit_code == 0, seed.output
    import re as _re
    rid = _re.search(r"seeded demo run (\S+)", seed.output).group(1)

    exp = tmp_path / "exp.jsonl"
    assert runner.invoke(app, ["export", "--all", "-d", str(src), "-o", str(exp)]).exit_code == 0
    imp = runner.invoke(app, ["import", "-d", str(dst), "--input", str(exp)])
    assert imp.exit_code == 0, imp.output
    assert "0 skipped" in imp.output, f"events were dropped on import: {imp.output}"

    cfg = TraceSageConfig(data_dir=dst)

    async def _check() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        try:
            events = await db.get_journey(rid)
            assert len(events) == 6, f"expected all 6 nested events, got {len(events)}"
        finally:
            await db.close()

    asyncio.run(_check())


def test_doctor_smoke(tmp_path: Path) -> None:
    """doctor on a seeded dir exits 0 and reports the run count."""
    _seed_run(tmp_path)
    result = runner.invoke(app, ["doctor", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = result.output + (result.stderr or "")
    assert "total runs: 1" in out


def test_doctor_blob_checks_cover_nested_runs(tmp_path: Path) -> None:
    """Blobs are stored under each event's own run_id (nested runs included), so a
    healthy nested trace must not be reported as orphaned; a deleted blob file and a
    stray dir must be."""
    import uuid

    from tracesage import TraceSage

    async def _capture() -> None:
        tl = await TraceSage.create(
            TraceSageConfig(data_dir=tmp_path, print_run_url=False), start_server=False
        )
        try:
            root, child = uuid.uuid4(), uuid.uuid4()
            h = tl.handler
            h.on_chain_start({"name": "graph"}, {"x": 1}, run_id=root)
            h.on_chain_start({"name": "node"}, {"x": 1}, run_id=child, parent_run_id=root)
            h.on_chain_end({"y": 2}, run_id=child, parent_run_id=root)
            h.on_chain_end({"y": 2}, run_id=root)
            await tl.flush()
        finally:
            await tl.stop()

    asyncio.run(_capture())
    blob_dir = TraceSageConfig(data_dir=tmp_path).blob_dir
    assert len([p for p in blob_dir.iterdir() if p.is_dir()]) == 2

    out = runner.invoke(app, ["doctor", "--data-dir", str(tmp_path)]).output
    assert "ok   orphan blob dirs: 0" in out
    assert "ok   runs with missing blob files: 0" in out

    next(blob_dir.rglob("*.json.gz")).unlink()
    (blob_dir / "stray-run").mkdir()
    out = runner.invoke(app, ["doctor", "--data-dir", str(tmp_path)]).output
    assert "warn orphan blob dirs (no events): 1" in out
    assert "warn runs with missing blob files: 1" in out


# ---- dev commands: demo / show / watch / diff -------------------------------

import re  # noqa: E402


def _seed_two_demo_runs(tmp_path: Path) -> tuple[str, str]:
    a = runner.invoke(app, ["demo", "-d", str(tmp_path), "--check"])
    b = runner.invoke(app, ["demo", "-d", str(tmp_path), "--check"])
    assert a.exit_code == 0, a.output
    assert b.exit_code == 0, b.output
    rid_a = re.search(r"seeded demo run (\S+)", a.output).group(1)
    rid_b = re.search(r"seeded demo run (\S+)", b.output).group(1)
    return rid_a, rid_b


def test_demo_check_seeds_run(tmp_path: Path) -> None:
    result = runner.invoke(app, ["demo", "-d", str(tmp_path), "--check"])
    assert result.exit_code == 0
    assert "seeded demo run" in result.output


def test_show_renders_tree(tmp_path: Path) -> None:
    rid, _ = _seed_two_demo_runs(tmp_path)
    result = runner.invoke(app, ["show", rid, "-d", str(tmp_path), "--no-color"])
    assert result.exit_code == 0, result.output
    assert "research_agent" in result.output
    assert "web_search" in result.output


def test_show_missing_run_exits_nonzero(tmp_path: Path) -> None:
    _seed_two_demo_runs(tmp_path)
    result = runner.invoke(app, ["show", "does-not-exist", "-d", str(tmp_path), "--no-color"])
    assert result.exit_code == 1
    assert "run not found" in result.output


def test_watch_once_prints_events(tmp_path: Path) -> None:
    rid, _ = _seed_two_demo_runs(tmp_path)
    result = runner.invoke(app, ["watch", rid, "-d", str(tmp_path), "--once"])
    assert result.exit_code == 0, result.output
    assert "llm_start" in result.output
    assert "tool_end" in result.output


def test_serve_public_host_without_token_is_clean_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["serve", "-d", str(tmp_path), "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 2
    assert "TRACESAGE_AUTH_TOKEN must be set" in result.output
    assert "Traceback" not in result.output


def test_serve_help_mentions_ingest() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--ingest" in result.output


@pytest.mark.asyncio
async def test_serve_ingest_accepts_otlp_and_persists(tmp_path: Path, monkeypatch) -> None:
    """`serve --ingest` runs a worker behind POST /v1/traces: spans posted to it
    are stored in the data dir and visible through the read API."""
    import re

    import httpx
    import typer

    import tracesage.cli as cli

    announced: list[str] = []
    real_echo = typer.echo

    def _capture(message=None, *a, **kw):
        if message:
            announced.append(str(message))
        return real_echo(message, *a, **kw)

    monkeypatch.setattr(cli.typer, "echo", _capture)

    cfg = TraceSageConfig(data_dir=tmp_path, port=0, print_run_url=False)
    task = asyncio.create_task(
        cli._serve_async(cfg, label="test", open_browser=False, ingest=True)
    )
    try:
        for _ in range(200):
            if any("/v1/traces" in m for m in announced):
                break
            await asyncio.sleep(0.05)
        line = next(m for m in announced if "/v1/traces" in m)
        port = int(re.search(r"127\.0\.0\.1:(\d+)/v1/traces", line).group(1))
        body = (Path(__file__).parent / "fixtures" / "otlp" / "openai_client.json").read_bytes()
        async with httpx.AsyncClient() as ac:
            resp = await ac.post(
                f"http://127.0.0.1:{port}/v1/traces",
                content=body,
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 200
            for _ in range(100):  # worker drains asynchronously
                run = (await ac.get(f"http://127.0.0.1:{port}/api/runs/3df7651916cd43dd8448eb211c80319f"))
                if run.status_code == 200 and run.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.05)
            assert run.status_code == 200
            assert run.json()["status"] == "completed"
            assert run.json()["total_tokens_input"] == 12
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # Persisted: a fresh read-only backend sees the run.
    async def _check() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        try:
            run = await db.get_run("3df7651916cd43dd8448eb211c80319f")
            assert run is not None
            assert run.tags == ["otlp", "chat-script"]
        finally:
            await db.close()

    await _check()


def test_watch_missing_run_exits_nonzero(tmp_path: Path) -> None:
    _seed_two_demo_runs(tmp_path)
    result = runner.invoke(app, ["watch", "does-not-exist", "-d", str(tmp_path), "--once"])
    assert result.exit_code == 1
    assert "run not found" in result.output


def test_diff_compares_runs(tmp_path: Path) -> None:
    rid_a, rid_b = _seed_two_demo_runs(tmp_path)
    result = runner.invoke(app, ["diff", rid_a, rid_b, "-d", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "tool_calls" in result.output
    assert "web_search" in result.output


def test_diff_missing_run_exits_nonzero(tmp_path: Path) -> None:
    rid_a, _ = _seed_two_demo_runs(tmp_path)
    result = runner.invoke(app, ["diff", rid_a, "nope", "-d", str(tmp_path)])
    assert result.exit_code == 1
    assert "run not found" in result.output
