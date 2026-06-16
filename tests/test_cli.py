"""Tests for the tracesage CLI. Uses Typer's CliRunner for end-to-end invocation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
