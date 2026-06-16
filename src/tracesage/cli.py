"""tracesage CLI: viewer + utilities. Does NOT ingest events.

Commands:
    tracesage serve     # start read-only viewer pointing at an existing data dir
    tracesage demo      # seed a sample trace and open the UI (fastest first look)
    tracesage show      # print a run's trace as a terminal tree
    tracesage watch     # live-tail a run's events in the terminal
    tracesage view      # open an exported JSONL trace in the UI directly
    tracesage diff      # compare two runs side by side
    tracesage export    # dump runs to JSONL
    tracesage import    # load a JSONL export into a data dir
    tracesage stats     # print summary stats
    tracesage runs      # list runs
    tracesage gc        # enforce retention (delete oldest runs over the cap)
    tracesage doctor    # read-only diagnostics
    tracesage version   # print version
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from datetime import UTC
from pathlib import Path

import typer

from tracesage import __version__
from tracesage.config import TraceSageConfig
from tracesage.models import Stats

app = typer.Typer(
    name="tracesage",
    help="tracesage: production observability for LangChain multi-agent systems.",
    no_args_is_help=True,
)


def _make_backend(data_dir: Path, *, read_only: bool = True):
    """Construct SQLiteBackend + BlobStore against a data dir.

    When `read_only=True` (default for stats/export/gc/runs), refuse to create
    a brand-new empty data dir on a typo — exit 1 with a clear message instead.
    Only `serve` (which legitimately may want to bind to a fresh dir) sets
    `read_only=False`.
    """
    from tracesage.storage import BlobStore, SQLiteBackend

    cfg = TraceSageConfig(data_dir=data_dir)
    if read_only:
        if not data_dir.exists():
            typer.echo(
                f"error: data dir does not exist: {data_dir}\n"
                f"hint: pass --data-dir/-d pointing at an existing tracesage "
                f"data directory (default: {Path.home() / '.tracesage'}).",
                err=True,
            )
            raise typer.Exit(1)
        if not cfg.db_path.exists():
            typer.echo(
                f"error: no traces.db at {cfg.db_path}\n"
                f"hint: this directory exists but has no tracesage data. "
                f"Either run your traced code with TraceSage.create() pointing "
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


async def _import_record(db, obj: dict, known_run_ids: set[str]) -> str:
    """Import one export record. Returns 'run' | 'event' | 'skip'.

    Ensures a `runs` row exists for every event's run_id before inserting it:
    `events.run_id` has a FK to `runs.run_id`, and `export` only emits top-level
    run rows (sub-runs from nested LangGraph nodes are not in the user-facing run
    list). Without this, a nested trace's sub-run events would fail the FK on
    import. Mirrors what real ingestion does (a runs row per run_id).
    """
    from tracesage.models import Run, RunStatus, StoredEvent

    kind = obj.get("_kind")
    payload = {k: v for k, v in obj.items() if k != "_kind"}
    if kind == "run":
        run = Run.model_validate(payload)
        await db.upsert_run(run)
        known_run_ids.add(run.run_id)
        return "run"
    if kind == "event":
        ev = StoredEvent.model_validate(payload)
        # The JSONL export carries only structured fields, not the gzipped blob
        # bytes — drop blob_path so we don't persist a dangling reference.
        if ev.blob_path is not None:
            ev = ev.model_copy(update={"blob_path": None})
        if ev.run_id not in known_run_ids:
            await db.upsert_run(Run(
                run_id=ev.run_id, root_run_id=ev.root_run_id or ev.run_id,
                tags=[], status=RunStatus.COMPLETED, started_at=ev.timestamp,
            ))
            known_run_ids.add(ev.run_id)
        await db.upsert_event(ev)
        return "event"
    return "skip"


async def _serve_async(cfg: TraceSageConfig, *, label: str, open_browser: bool) -> None:
    """Run the read-only viewer for `cfg.data_dir`, announce the URL once the socket
    is bound (so --port 0 reports the real port), and optionally open a browser."""
    from tracesage.server import WebSocketManager, create_app
    from tracesage.storage import BlobStore, SQLiteBackend

    cfg.ensure_data_dirs()
    db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
    await db.init()
    blob = BlobStore(cfg.blob_dir)
    app = create_app(
        db=db, blob_store=blob, ws_manager=WebSocketManager(), config=cfg, stats=Stats()
    )

    try:
        import uvicorn
    except ImportError as e:  # pragma: no cover
        typer.echo(f"uvicorn not installed: {e}", err=True)
        raise typer.Exit(2) from e

    uv_config = uvicorn.Config(
        app, host=cfg.host, port=cfg.port, log_level="info", lifespan="on"
    )
    server = uvicorn.Server(uv_config)

    async def _announce_when_ready() -> None:
        for _ in range(200):  # up to ~10s @ 50ms
            if getattr(server, "started", False):
                break
            await asyncio.sleep(0.05)
        bound_port = cfg.port
        with suppress(Exception):
            servers = getattr(server, "servers", None) or []
            if servers:
                socks = getattr(servers[0], "sockets", None) or []
                if socks:
                    bound_port = socks[0].getsockname()[1]
        url = f"http://{cfg.host}:{bound_port}/ui"
        typer.echo(f"tracesage viewer at {url}  ({label})")
        if open_browser:
            import webbrowser

            with suppress(Exception):
                webbrowser.open(url)

    announcer = asyncio.create_task(_announce_when_ready())
    try:
        await server.serve()
    finally:
        announcer.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await announcer
        await db.close()


@app.command()
def serve(
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage",
        "--data-dir",
        "-d",
        help="Path to an existing tracesage data directory.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind address."),
    port: int = typer.Option(7842, "--port", "-p", help="Bind port."),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        envvar="TRACESAGE_AUTH_TOKEN",
        help="Required if --host is non-loopback.",
    ),
    open_browser: bool = typer.Option(
        False, "--open", "-o", help="Open the viewer in your browser once it's up."
    ),
) -> None:
    """Start a read-only viewer for an existing tracesage data directory.

    This does NOT ingest events — only the TraceSage.create() API in your
    Python code does that. Use `serve` to inspect data after the fact, or to
    run the UI on a different host than the producer.
    """
    # Build config; will raise on host=0.0.0.0 without token (production rail).
    cfg = TraceSageConfig(data_dir=data_dir, host=host, port=port, auth_token=auth_token)
    try:
        asyncio.run(_serve_async(cfg, label=f"data: {data_dir}", open_browser=open_browser))
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


@app.command()
def show(
    run_id: str = typer.Argument(..., help="Run id to render."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
    color: bool | None = typer.Option(
        None, "--color/--no-color", help="Force ANSI colour on/off (default: auto)."
    ),
) -> None:
    """Print a run's trace as an indented tree in the terminal (no server needed)."""

    async def _show() -> int:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            from tracesage.render import render_run_tree

            run = await db.get_run(run_id)
            if run is None:
                typer.echo(f"run not found: {run_id}", err=True)
                return 1
            events = await db.get_journey(run_id)
            typer.echo(render_run_tree(run, events, use_color=color))
            return 0
        finally:
            await db.close()

    raise typer.Exit(asyncio.run(_show()))


@app.command()
def watch(
    run_id: str = typer.Argument(..., help="Run id to follow."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
    interval: float = typer.Option(1.0, "--interval", help="Poll interval (seconds)."),
    once: bool = typer.Option(
        False, "--once", help="Print the current events and exit (no follow loop)."
    ),
) -> None:
    """Live-tail a run's events in the terminal as they are written (poll-based).

    Useful while a long run is still in flight in another process. Ctrl-C to stop.
    """
    from tracesage.render import _KIND_ICON, _kind_of

    async def _watch() -> int:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        seen: set[str] = set()
        try:
            while True:
                events = await db.get_journey(run_id)
                for ev in events:
                    if ev.event_id in seen:
                        continue
                    seen.add(ev.event_id)
                    kind = _kind_of(ev.event_type.value)
                    icon = _KIND_ICON.get(kind, "•")
                    name = ev.agent_name or ev.tool_name or kind
                    ts = ev.timestamp.strftime("%H:%M:%S")
                    mark = ""
                    if ev.event_type.value.endswith("_error"):
                        mark = f"  ✗ {(ev.error_message or '').splitlines()[0][:80]}"
                    elif ev.duration_ms is not None:
                        mark = f"  {ev.duration_ms}ms"
                    typer.echo(f"[{ts}] {icon} {ev.event_type.value:<14} {name}{mark}")
                run = await db.get_run(run_id)
                if once or (run is not None and run.status.value in ("completed", "failed")):
                    return 0
                await asyncio.sleep(interval)
        finally:
            await db.close()

    try:
        raise typer.Exit(asyncio.run(_watch()))
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


@app.command()
def diff(
    run_a: str = typer.Argument(..., help="First run id."),
    run_b: str = typer.Argument(..., help="Second run id."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
) -> None:
    """Compare two runs side by side (status, steps, tokens, tools, errors)."""

    async def _summary(db, run_id: str) -> dict | None:
        run = await db.get_run(run_id)
        if run is None:
            return None
        events = await db.get_journey(run_id)
        tools = sorted({e.tool_name for e in events if e.tool_name})
        errors = sum(1 for e in events if e.event_type.value.endswith("_error"))
        return {
            "status": run.status.value,
            "steps": run.total_steps,
            "tok_in": run.total_tokens_input,
            "tok_out": run.total_tokens_output,
            "tool_calls": sum(1 for e in events if e.event_type.value == "tool_start"),
            "tools": ", ".join(tools) or "—",
            "errors": errors,
        }

    async def _diff() -> int:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            a = await _summary(db, run_a)
            b = await _summary(db, run_b)
            if a is None or b is None:
                missing = run_a if a is None else run_b
                typer.echo(f"run not found: {missing}", err=True)
                return 1
            rows = ["status", "steps", "tok_in", "tok_out", "tool_calls", "tools", "errors"]
            wa, wb = run_a[:18], run_b[:18]
            typer.echo(f"{'metric':<12} {wa:<22} {wb:<22}")
            typer.echo(f"{'-'*12} {'-'*22} {'-'*22}")
            for k in rows:
                va, vb = str(a[k]), str(b[k])
                flag = "" if va == vb else "  ≠"
                typer.echo(f"{k:<12} {va:<22} {vb:<22}{flag}")
            return 0
        finally:
            await db.close()

    raise typer.Exit(asyncio.run(_diff()))


@app.command()
def view(
    input: Path = typer.Argument(..., help="A JSONL export produced by `tracesage export`."),
    host: str = typer.Option("127.0.0.1", "--host", "-h"),
    port: int = typer.Option(7842, "--port", "-p"),
    open_browser: bool = typer.Option(False, "--open", "-o", help="Open in browser."),
) -> None:
    """Open an exported JSONL trace in the UI directly (imports into a temp dir, serves it)."""
    import tempfile

    from tracesage.storage import SQLiteBackend

    if not input.exists():
        typer.echo(f"file not found: {input}", err=True)
        raise typer.Exit(1)

    tmp = Path(tempfile.mkdtemp(prefix="tracesage-view-"))
    cfg = TraceSageConfig(data_dir=tmp, host=host, port=port)
    cfg.ensure_data_dirs()

    async def _load_and_serve() -> None:
        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        loaded = 0
        known_run_ids: set[str] = set()
        with input.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    if await _import_record(db, json.loads(line), known_run_ids) == "run":
                        loaded += 1
                except Exception as e:
                    typer.echo(f"warning: skipping bad line: {e}", err=True)
        await db.close()
        typer.echo(f"loaded {loaded} run(s) from {input}")
        await _serve_async(cfg, label=f"viewing {input.name}", open_browser=open_browser)

    try:
        asyncio.run(_load_and_serve())
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


@app.command()
def demo(
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage-demo", "--data-dir", "-d",
        help="Where to write the demo data (default: ~/.tracesage-demo).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", "-h"),
    port: int = typer.Option(7842, "--port", "-p"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open in browser."),
    check: bool = typer.Option(
        False, "--check", help="Seed the demo data and exit (no server)."
    ),
) -> None:
    """Seed a sample trace and open the UI — the fastest way to see tracesage working."""
    cfg = TraceSageConfig(data_dir=data_dir, host=host, port=port)
    cfg.ensure_data_dirs()

    async def _run() -> None:
        from tracesage.storage import SQLiteBackend

        db = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        run_id = await _seed_demo(db)
        await db.close()
        typer.echo(f"seeded demo run {run_id} into {data_dir}")
        if check:
            return
        await _serve_async(cfg, label="demo", open_browser=open_browser)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


async def _seed_demo(db) -> str:
    """Insert one realistic sample run (chain → llm + tool) so the UI has content."""
    import uuid
    from datetime import datetime, timedelta

    from tracesage.models import EventType, Run, RunStatus, StoredEvent

    t0 = datetime.now(UTC)
    root = f"demo-{uuid.uuid4().hex[:8]}"
    llm_id, tool_id = f"{root}-llm", f"{root}-tool"

    def ev(eid, rid, et, off, *, parent=root, name=None, tool=None, dur=None, ti=None, to=None):
        return StoredEvent(
            event_id=eid, run_id=rid, parent_run_id=(None if rid == root else parent),
            root_run_id=root, event_type=et, timestamp=t0 + timedelta(milliseconds=off),
            agent_name=name, tool_name=tool, summary=(name or tool or et.value),
            duration_ms=dur, token_input=ti, token_output=to,
        )

    await db.upsert_run(Run(
        run_id=root, root_run_id=root, tags=["demo"], status=RunStatus.COMPLETED,
        started_at=t0, completed_at=t0 + timedelta(milliseconds=620),
        total_steps=3, total_tokens_input=42, total_tokens_output=58,
    ))
    # Each distinct event run_id needs a runs row (events.run_id has an FK to
    # runs.run_id) — real ingestion creates one per sub-run; mirror that here.
    for sub in (llm_id, tool_id):
        await db.upsert_run(Run(
            run_id=sub, root_run_id=root, tags=[], status=RunStatus.COMPLETED,
            started_at=t0, completed_at=t0 + timedelta(milliseconds=620),
        ))
    for e in [
        ev(f"{root}-e1", root, EventType.CHAIN_START, 0, name="research_agent"),
        ev(f"{root}-e2", llm_id, EventType.LLM_START, 20, name="gpt-4o-mini"),
        ev(f"{root}-e3", llm_id, EventType.LLM_END, 230, name="gpt-4o-mini", dur=210, ti=42, to=58),
        ev(f"{root}-e4", tool_id, EventType.TOOL_START, 250, tool="web_search"),
        ev(f"{root}-e5", tool_id, EventType.TOOL_END, 600, tool="web_search", dur=350),
        ev(f"{root}-e6", root, EventType.CHAIN_END, 620, name="research_agent", dur=620),
    ]:
        await db.upsert_event(e)
    return root


@app.command()
def export(
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage", "--data-dir", "-d"
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Single run to export."),
    all_runs: bool = typer.Option(False, "--all", help="Export every run."),
    output: Path = typer.Option(Path("-"), "--output", "-o", help='Output file or "-" for stdout.'),
    format: str = typer.Option("jsonl", "--format", "-f", help="Currently only jsonl."),
) -> None:
    """Dump runs to JSONL: first line is the Run row; subsequent lines are events.

    Only structured event fields are exported — the gzipped raw-payload blobs
    are NOT included, so `import` will land events with no blob_path.
    """
    if format != "jsonl":
        typer.echo(f"Only 'jsonl' is supported; got {format!r}", err=True)
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
                    async for event in db.iter_journey(rid):
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
        Path.home() / ".tracesage", "--data-dir", "-d"
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
        Path.home() / ".tracesage", "--data-dir", "-d"
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
    feeding `tracesage export --run-id` per row.
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
        Path.home() / ".tracesage", "--data-dir", "-d"
    ),
    max_runs: int = typer.Option(10_000, "--max-runs", help="Keep at most N most-recent runs."),
    max_blob_size_gb: float | None = typer.Option(
        None,
        "--max-blob-size-gb",
        help="Also delete oldest runs until total blob size is under this many GB.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be deleted; no changes."),
) -> None:
    """Garbage-collect old runs beyond the retention cap."""

    async def _gc() -> None:
        _cfg, db, blob = _make_backend(data_dir)
        await db.init()
        try:
            runs, total = await db.list_runs(limit=1_000_000, offset=0)
            # runs is newest-first; the oldest beyond the cap are deleted first.
            deleted_ids: set[str] = set()
            if total <= max_runs:
                # ASCII-only: Windows cp1252 can't encode `<=` as U+2264.
                typer.echo(f"  {total} runs <= cap {max_runs}; nothing to do.")
            else:
                to_delete = runs[max_runs:]
                typer.echo(
                    f"  {len(to_delete)} runs over cap (total {total}, cap {max_runs})."
                )
                for r in to_delete:
                    if dry_run:
                        typer.echo(f"    would delete: {r.run_id} ({r.started_at})")
                        deleted_ids.add(r.run_id)
                    else:
                        try:
                            await db.delete_run(r.run_id)
                            await blob.delete_run(r.run_id)
                            typer.echo(f"    deleted: {r.run_id}")
                            deleted_ids.add(r.run_id)
                        except Exception as e:
                            typer.echo(
                                f"    warning: failed to delete {r.run_id}: {e}", err=True
                            )

            if max_blob_size_gb is not None:
                cap_bytes = int(max_blob_size_gb * 1024 ** 3)
                # Oldest-first list of runs not already deleted (runs is newest-first).
                remaining = [r for r in reversed(runs) if r.run_id not in deleted_ids]
                loop = asyncio.get_running_loop()
                size = await blob.total_size_bytes()
                typer.echo(
                    f"  blob size {size} bytes; cap {cap_bytes} bytes "
                    f"({max_blob_size_gb} GB)."
                )
                while size > cap_bytes and remaining:
                    r = remaining.pop(0)
                    # Measure this run's blob size once (off the event loop) and
                    # subtract it from a running total, instead of re-walking the
                    # whole blob tree after every deletion (was O(N*M) disk I/O).
                    freed = await loop.run_in_executor(None, blob.get_size_bytes, r.run_id)
                    if dry_run:
                        typer.echo(
                            f"    would delete (blob cap): {r.run_id} ({r.started_at}); "
                            f"~{freed} bytes"
                        )
                        size -= freed
                        continue
                    try:
                        await db.delete_run(r.run_id)
                        await blob.delete_run(r.run_id)
                    except Exception as e:
                        typer.echo(
                            f"    warning: failed to delete {r.run_id}: {e}", err=True
                        )
                        continue
                    size -= freed
                    typer.echo(
                        f"    deleted (blob cap): {r.run_id}; freed ~{freed} bytes, "
                        f"est {size} bytes remaining"
                    )
                if not dry_run:
                    if size > cap_bytes:
                        typer.echo(
                            f"  blob size {size} bytes still over cap {cap_bytes} bytes "
                            f"(no more runs to delete)."
                        )
                    else:
                        typer.echo(f"  blob size {size} bytes now under cap {cap_bytes} bytes.")
        finally:
            await db.close()

    asyncio.run(_gc())


@app.command("import")
def import_(
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage", "--data-dir", "-d"
    ),
    input: Path = typer.Option(
        Path("-"), "--input", "-i", help='JSONL file or "-" for stdin.'
    ),
) -> None:
    """Import a JSONL export back into a data dir (inverse of `export`).

    Reads lines produced by `tracesage export` (each tagged with
    "_kind": "run" or "_kind": "event") and upserts them into the target
    data dir, creating/initializing it if needed. One malformed line is
    skipped with a warning rather than aborting the whole import.

    Raw-payload blobs are not part of the export, so imported events have
    their blob_path cleared; structured fields are restored in full.
    """

    async def _import() -> None:
        # read_only=False so an import into a fresh target dir initializes it.
        _cfg, db, _blob = _make_backend(data_dir, read_only=False)
        await db.init()
        runs_imported = 0
        events_imported = 0
        skipped = 0
        in_fh = sys.stdin if str(input) == "-" else input.open("r", encoding="utf-8")
        known_run_ids: set[str] = set()
        try:
            for line in in_fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    kind = await _import_record(db, json.loads(line), known_run_ids)
                    if kind == "run":
                        runs_imported += 1
                    elif kind == "event":
                        events_imported += 1
                    else:
                        typer.echo("warning: skipping line with unknown _kind", err=True)
                        skipped += 1
                except Exception as e:
                    typer.echo(f"warning: skipping bad line: {e}", err=True)
                    skipped += 1
        finally:
            if in_fh is not sys.stdin:
                in_fh.close()
            await db.close()
        typer.echo(
            f"imported {runs_imported} runs, {events_imported} events "
            f"({skipped} skipped) into {data_dir}"
        )

    asyncio.run(_import())


@app.command()
def doctor(
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage", "--data-dir", "-d"
    ),
) -> None:
    """Run read-only diagnostics on a data dir and print a health report."""

    async def _doctor() -> None:
        cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            typer.echo(f"data_dir: {data_dir}")

            # traces.db presence.
            try:
                db_exists = cfg.db_path.exists()
                typer.echo(f"  ok   traces.db: {'present' if db_exists else 'MISSING'}")
            except Exception as e:
                typer.echo(f"  warn traces.db: probe failed: {e}")

            # Schema version (best-effort; PRAGMA can't be parameterized).
            try:
                import aiosqlite

                async with aiosqlite.connect(cfg.db_path) as conn:
                    cur = await conn.execute("PRAGMA user_version")
                    row = await cur.fetchone()
                    await cur.close()
                version = row[0] if row else 0
                typer.echo(f"  ok   schema user_version: {version}")
            except Exception as e:
                typer.echo(f"  warn schema user_version: probe failed: {e}")

            # Total runs.
            run_ids: set[str] = set()
            try:
                stats_dict = await db.get_stats()
                typer.echo(f"  ok   total runs: {stats_dict.get('total_runs', 0)}")
            except Exception as e:
                typer.echo(f"  warn total runs: probe failed: {e}")

            # Build the set of run dirs that should exist (root runs).
            try:
                runs, _total = await db.list_runs(limit=1_000_000, offset=0)
                run_ids = {r.run_id for r in runs}
            except Exception as e:
                typer.echo(f"  warn run listing: probe failed: {e}")

            # Orphan blobs: blob run-dirs with no corresponding run row.
            try:
                blob_dir = cfg.blob_dir
                if blob_dir.exists():
                    orphan_dirs = [
                        p.name
                        for p in blob_dir.iterdir()
                        if p.is_dir() and p.name not in run_ids
                    ]
                else:
                    orphan_dirs = []
                if orphan_dirs:
                    typer.echo(
                        f"  warn orphan blob dirs (no run row): {len(orphan_dirs)}"
                    )
                else:
                    typer.echo("  ok   orphan blob dirs: 0")
            except Exception as e:
                typer.echo(f"  warn orphan blob check: probe failed: {e}")

            # Missing blobs: runs whose events reference a blob_path but the
            # run's blob dir is absent.
            try:
                blob_dir = cfg.blob_dir
                missing = 0
                for rid in run_ids:
                    has_blob_event = False
                    async for ev in db.iter_journey(rid):
                        if ev.blob_path:
                            has_blob_event = True
                            break
                    if has_blob_event and not (blob_dir / rid).exists():
                        missing += 1
                if missing:
                    typer.echo(f"  warn runs with missing blob dir: {missing}")
                else:
                    typer.echo("  ok   runs with missing blob dir: 0")
            except Exception as e:
                typer.echo(f"  warn missing blob check: probe failed: {e}")
        finally:
            await db.close()

    asyncio.run(_doctor())


@app.command()
def version() -> None:
    """Print tracesage version."""
    typer.echo(f"tracesage {__version__}")


if __name__ == "__main__":
    app()
