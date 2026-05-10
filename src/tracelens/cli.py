"""tracelens CLI: viewer + utilities. Does NOT ingest events.

Commands:
    tracelens serve     # start read-only viewer pointing at an existing data dir
    tracelens export    # dump runs to JSONL
    tracelens stats     # print summary stats
    tracelens gc        # enforce retention (delete oldest runs over the cap)
    tracelens version   # print version
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer

from tracelens import __version__
from tracelens.config import TraceLensConfig
from tracelens.models import Stats

app = typer.Typer(
    name="tracelens",
    help="tracelens: production observability for LangChain multi-agent systems.",
    no_args_is_help=True,
)


def _make_backend(data_dir: Path, *, read_only: bool = True):
    """Construct SQLiteBackend + BlobStore against a data dir.

    When `read_only=True` (default for stats/export/gc/runs), refuse to create
    a brand-new empty data dir on a typo — exit 1 with a clear message instead.
    Only `serve` (which legitimately may want to bind to a fresh dir) sets
    `read_only=False`.
    """
    from tracelens.storage import BlobStore, SQLiteBackend

    cfg = TraceLensConfig(data_dir=data_dir)
    if read_only:
        if not data_dir.exists():
            typer.echo(
                f"error: data dir does not exist: {data_dir}\n"
                f"hint: pass --data-dir/-d pointing at an existing tracelens "
                f"data directory (default: {Path.home() / '.tracelens'}).",
                err=True,
            )
            raise typer.Exit(1)
        if not cfg.db_path.exists():
            typer.echo(
                f"error: no traces.db at {cfg.db_path}\n"
                f"hint: this directory exists but has no tracelens data. "
                f"Either run your traced code with TraceLens.create() pointing "
                f"here, or pass --data-dir to a different location.",
                err=True,
            )
            raise typer.Exit(1)
    else:
        cfg.ensure_data_dirs()
    db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
    blob = BlobStore(cfg.blob_dir)
    return cfg, db, blob


def _open_output(path: Path):
    """Return a writable file-like object. '-' means stdout (no close)."""
    if str(path) == "-":
        return sys.stdout
    return path.open("w", encoding="utf-8")


@app.command()
def serve(
    data_dir: Path = typer.Option(
        Path.home() / ".tracelens",
        "--data-dir",
        "-d",
        help="Path to an existing tracelens data directory.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind address."),
    port: int = typer.Option(7842, "--port", "-p", help="Bind port."),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        envvar="TRACELENS_AUTH_TOKEN",
        help="Required if --host is non-loopback.",
    ),
) -> None:
    """Start a read-only viewer for an existing tracelens data directory.

    This does NOT ingest events — only the TraceLens.create() API in your
    Python code does that. Use `serve` to inspect data after the fact, or to
    run the UI on a different host than the producer.
    """
    from tracelens.server import WebSocketManager, create_app

    async def _run() -> None:
        # Build config; will raise on host=0.0.0.0 without token (production rail).
        cfg = TraceLensConfig(data_dir=data_dir, host=host, port=port, auth_token=auth_token)
        cfg.ensure_data_dirs()

        from tracelens.storage import BlobStore, SQLiteBackend

        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        blob = BlobStore(cfg.blob_dir)
        ws_manager = WebSocketManager()
        stats = Stats()

        app = create_app(db=db, blob_store=blob, ws_manager=ws_manager, config=cfg, stats=stats)

        try:
            import uvicorn
        except ImportError as e:  # pragma: no cover
            typer.echo(f"uvicorn not installed: {e}", err=True)
            raise typer.Exit(2) from e

        uv_config = uvicorn.Config(app, host=host, port=port, log_level="info", lifespan="on")
        server = uvicorn.Server(uv_config)

        # Spawn a watcher that emits the URL banner *after* uvicorn has bound
        # the socket (so --port 0 reports the actual ephemeral port).
        async def _announce_when_ready() -> None:
            for _ in range(200):  # up to ~10s @ 50ms
                if getattr(server, "started", False):
                    break
                await asyncio.sleep(0.05)
            bound_port = port
            try:
                servers = getattr(server, "servers", None) or []
                if servers:
                    socks = getattr(servers[0], "sockets", None) or []
                    if socks:
                        bound_port = socks[0].getsockname()[1]
            except Exception:
                pass  # fall back to configured port
            typer.echo(
                f"tracelens viewer at http://{host}:{bound_port}/ui  "
                f"(data: {data_dir})"
            )

        announcer = asyncio.create_task(_announce_when_ready())
        try:
            await server.serve()
        finally:
            announcer.cancel()
            try:
                await announcer
            except (asyncio.CancelledError, Exception):
                pass
            await db.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


@app.command()
def export(
    data_dir: Path = typer.Option(
        Path.home() / ".tracelens", "--data-dir", "-d"
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Single run to export."),
    all_runs: bool = typer.Option(False, "--all", help="Export every run."),
    output: Path = typer.Option(Path("-"), "--output", "-o", help='Output file or "-" for stdout.'),
    format: str = typer.Option("jsonl", "--format", "-f", help="Currently only jsonl."),
) -> None:
    """Dump runs to JSONL: first line is the Run row; subsequent lines are events."""
    if format != "jsonl":
        typer.echo(f"Only 'jsonl' is supported in v0.1; got {format!r}", err=True)
        raise typer.Exit(2)
    if not run_id and not all_runs:
        typer.echo("Specify --run-id <id> or --all", err=True)
        raise typer.Exit(2)

    async def _export() -> int:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            run_ids: list[str]
            if run_id:
                run_ids = [run_id]
            else:
                runs, _ = await db.list_runs(limit=100_000, offset=0)
                run_ids = [r.run_id for r in runs]

            out_fh = _open_output(output)
            written_any = False
            missing: list[str] = []
            try:
                for rid in run_ids:
                    run = await db.get_run(rid)
                    if run is None:
                        typer.echo(f"warning: run {rid} not found", err=True)
                        missing.append(rid)
                        continue
                    written_any = True
                    out_fh.write(json.dumps({"_kind": "run", **run.model_dump(mode="json")}))
                    out_fh.write("\n")
                    journey = await db.get_journey(rid)
                    for event in journey:
                        out_fh.write(
                            json.dumps({"_kind": "event", **event.model_dump(mode="json")})
                        )
                        out_fh.write("\n")
            finally:
                if out_fh is not sys.stdout:
                    out_fh.close()
            # If a single --run-id was specified and not found, exit non-zero
            # so scripts can detect the failure. --all with zero runs found is
            # a softer warning (legitimate empty-DB case).
            if run_id and missing:
                return 1
            if run_id and not written_any:
                return 1
            return 0
        finally:
            await db.close()

    raise typer.Exit(asyncio.run(_export()))


@app.command()
def stats(
    data_dir: Path = typer.Option(
        Path.home() / ".tracelens", "--data-dir", "-d"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit a single JSON object instead of human-readable kv pairs.",
    ),
) -> None:
    """Print summary stats from a data directory."""

    async def _stats() -> None:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            stats_dict = await db.get_stats()
            if as_json:
                typer.echo(json.dumps(stats_dict, default=str))
            else:
                for k, v in stats_dict.items():
                    typer.echo(f"  {k}: {v}")
        finally:
            await db.close()

    asyncio.run(_stats())


@app.command()
def runs(
    data_dir: Path = typer.Option(
        Path.home() / ".tracelens", "--data-dir", "-d"
    ),
    status: str = typer.Option(
        "all", "--status",
        help="Filter by status: all | running | completed | failed.",
    ),
    limit: int = typer.Option(50, "--limit", "-l", help="Max rows to return."),
    offset: int = typer.Option(0, "--offset", help="Pagination offset."),
    tag: str | None = typer.Option(
        None, "--tag",
        help="Only show runs whose tags contain this substring (substring match).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit one JSON object per line (newline-delimited).",
    ),
) -> None:
    """List root runs from a data directory.

    Default human format is one row per line:

        <run_id>  <status>  <started_at>  <total_steps>  tags=[...]

    Pass --json for machine-readable NDJSON. Useful for piping into jq, or
    feeding `tracelens export --run-id` per row.
    """
    if status not in {"all", "running", "completed", "failed"}:
        typer.echo(f"--status must be one of: all running completed failed (got {status!r})", err=True)
        raise typer.Exit(2)

    async def _runs() -> None:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            rows, total = await db.list_runs(
                status=None if status == "all" else status,
                limit=limit,
                offset=offset,
            )
            if tag:
                rows = [r for r in rows if any(tag in t for t in (r.tags or []))]

            if as_json:
                for r in rows:
                    typer.echo(r.model_dump_json())
                # Status line on stderr so it doesn't pollute the NDJSON.
                typer.echo(
                    f"# {len(rows)} of {total} returned (status={status}, "
                    f"limit={limit}, offset={offset}, tag={tag!r})",
                    err=True,
                )
            else:
                if not rows:
                    typer.echo("(no runs)")
                    return
                for r in rows:
                    started = r.started_at.isoformat() if r.started_at else "—"
                    tags_s = ",".join(r.tags) if r.tags else ""
                    typer.echo(
                        f"{r.run_id}  {r.status.value:9s}  {started}  "
                        f"steps={r.total_steps:<4d}  tags=[{tags_s}]"
                    )
                typer.echo(
                    f"# {len(rows)} of {total} (status={status}, "
                    f"limit={limit}, offset={offset})"
                )
        finally:
            await db.close()

    asyncio.run(_runs())


@app.command()
def gc(
    data_dir: Path = typer.Option(
        Path.home() / ".tracelens", "--data-dir", "-d"
    ),
    max_runs: int = typer.Option(10_000, "--max-runs", help="Keep at most N most-recent runs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be deleted; no changes."),
) -> None:
    """Garbage-collect old runs beyond the retention cap."""

    async def _gc() -> None:
        _cfg, db, blob = _make_backend(data_dir)
        await db.init()
        try:
            runs, total = await db.list_runs(limit=1_000_000, offset=0)
            if total <= max_runs:
                # ASCII-only: Windows cp1252 can't encode `<=` as U+2264.
                typer.echo(f"  {total} runs <= cap {max_runs}; nothing to do.")
                return
            to_delete = runs[max_runs:]
            typer.echo(
                f"  {len(to_delete)} runs over cap (total {total}, cap {max_runs})."
            )
            for r in to_delete:
                if dry_run:
                    typer.echo(f"    would delete: {r.run_id} ({r.started_at})")
                else:
                    await db.delete_run(r.run_id)
                    await blob.delete_run(r.run_id)
                    typer.echo(f"    deleted: {r.run_id}")
        finally:
            await db.close()

    asyncio.run(_gc())


@app.command()
def version() -> None:
    """Print tracelens version."""
    typer.echo(f"tracelens {__version__}")


if __name__ == "__main__":
    app()
