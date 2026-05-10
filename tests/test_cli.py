"""Tests for the tracelens CLI. Uses Typer's CliRunner for end-to-end invocation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from tracelens.cli import app
from tracelens.config import TraceLensConfig
from tracelens.storage import SQLiteBackend

runner = CliRunner()


def _bootstrap_empty_db(tmp_path: Path) -> Path:
    """Create an empty but valid tracelens data dir for read-only tests.

    The new CLI fail-loud behavior (B11.b) refuses to silently create a fresh
    DB on a typo'd --data-dir, so tests that need an empty data dir must
    pre-initialize one.
    """
    cfg = TraceLensConfig(data_dir=tmp_path)
    cfg.ensure_data_dirs()

    async def _init() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        await db.close()

    asyncio.run(_init())
    return tmp_path


def test_version_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "tracelens" in result.stdout
    assert "0.1.0" in result.stdout


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

    from tracelens.config import TraceLensConfig
    from tracelens.models import EventType, Run, RunStatus, StoredEvent
    from tracelens.storage import SQLiteBackend

    cfg = TraceLensConfig(data_dir=tmp_path)
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
