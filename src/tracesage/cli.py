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
import os
import shutil
import sys
from contextlib import suppress
from datetime import UTC
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from tracesage import __version__
from tracesage.config import TraceSageConfig
from tracesage.models import Stats

app = typer.Typer(
    name="tracesage",
    help="tracesage: local-first observability for LangChain multi-agent systems.",
    no_args_is_help=True,
)


def _build_config(**kwargs: object) -> TraceSageConfig:
    """Build config, turning validation failures into a one-line error + exit 2."""
    try:
        return TraceSageConfig(**kwargs)  # type: ignore[arg-type]
    except ValidationError as e:
        msg = e.errors()[0].get("msg", str(e)) if e.errors() else str(e)
        typer.echo(f"error: {msg.removeprefix('Value error, ')}", err=True)
        raise typer.Exit(2) from None


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


async def _serve_async(
    cfg: TraceSageConfig, *, label: str, open_browser: bool, ingest: bool = False
) -> None:
    """Run the viewer for `cfg.data_dir`, announce the URL once the socket is bound
    (so --port 0 reports the real port), and optionally open a browser.

    ``ingest=True`` turns the viewer into a collector: a full tracer (queue +
    StorageWorker, so sampling, per-run cap, redaction and OTel export all apply
    exactly as in-process) backs ``POST /v1/traces``, and live events reach the
    UI over the same WebSocket manager.
    """
    from tracesage.server import WebSocketManager, create_app

    cfg.ensure_data_dirs()
    tracer: Any = None
    follower: asyncio.Task[None] | None = None
    chat_follower: asyncio.Task[None] | None = None
    if ingest:
        from tracesage.tracer import TraceSage, _DisabledTraceSage

        tracer = await TraceSage.create(cfg, start_server=False)
        if not isinstance(tracer, _DisabledTraceSage):
            from tracesage.ingest.agent_hooks import drain_spool, follow_locally
            from tracesage.ingest.claude_code import ClaudeCodeSource
            from tracesage.ingest.codex_sessions import CodexSource
            from tracesage.ingest.cursor_store import CursorSource
            from tracesage.ingest.desktop_audit import CoworkSource
            from tracesage.ingest.desktop_chats import claude_support_dir, follow_desktop_chats
            from tracesage.ingest.session_follow import follow_sessions, import_days

            sources = [
                source
                for source in (ClaudeCodeSource(), CursorSource(), CodexSource(), CoworkSource())
                if source.available()
            ]
            follow_locally({source.name for source in sources if source.name != "claude-desktop"})
            drained = drain_spool(
                cfg.data_dir / "spool",
                tracer.emit,
                metadata_only=cfg.hook_metadata_only,
            )
            if drained:
                typer.echo(f"  drained {drained} spooled hook event(s)")
            if sources:
                queue = getattr(tracer, "_queue", None)

                def busy() -> bool:
                    if queue is None or not queue.maxsize:
                        return False
                    return queue.qsize() > queue.maxsize // 2

                follower = asyncio.create_task(follow_sessions(
                    sources, tracer.emit, cfg.data_dir,
                    metadata_only=cfg.hook_metadata_only, busy=busy,
                ))
                names = ", ".join(source.name for source in sources)
                typer.echo(f"  local sessions ({names}), last {import_days()} days on first import")
            if claude_support_dir().is_dir():
                chat_follower = asyncio.create_task(follow_desktop_chats(
                    tracer.emit, cfg.data_dir, metadata_only=cfg.hook_metadata_only,
                ))
                typer.echo("  Claude Desktop chats: local cache and IndexedDB")
        if isinstance(tracer, _DisabledTraceSage):
            typer.echo(
                "warning: TRACESAGE_ENABLED=false — ingest is off, serving read-only.", err=True
            )
            tracer = None
    if tracer is not None:
        db = tracer.db
        app = create_app(
            db=db,
            blob_store=tracer.blob_store,
            ws_manager=tracer._ws_manager,
            config=cfg,
            stats=tracer.stats,
            emit=tracer.emit,
        )
    else:
        from tracesage.storage import BlobStore, SQLiteBackend

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

    # Same auto-port behavior as the embedded server: if cfg.port is busy and
    # port_auto is on, bind the next free port instead of failing.
    from tracesage.tracer import _resolve_bind_port

    bind_port = _resolve_bind_port(cfg.host, cfg.port, auto=cfg.port_auto)
    uv_config = uvicorn.Config(
        app, host=cfg.host, port=bind_port, log_level="info", lifespan="on"
    )
    server = uvicorn.Server(uv_config)

    async def _announce_when_ready() -> None:
        for _ in range(200):  # up to ~10s @ 50ms
            if getattr(server, "started", False):
                break
            await asyncio.sleep(0.05)
        bound_port = bind_port
        with suppress(Exception):
            servers = getattr(server, "servers", None) or []
            if servers:
                socks = getattr(servers[0], "sockets", None) or []
                if socks:
                    bound_port = socks[0].getsockname()[1]
        url = f"http://{cfg.host}:{bound_port}/ui"
        if tracer is not None:
            # Lets the tracer print per-run "view trace" links for ingested traces.
            tracer.bound_port = bound_port
            typer.echo(
                f"tracesage collector at {url}  ({label})\n"
                f"  OTLP/HTTP ingest: POST http://{cfg.host}:{bound_port}/v1/traces\n"
                f"  hook ingest: POST http://{cfg.host}:{bound_port}/v1/hooks/{{app}}\n"
                f"  e.g. OTEL_EXPORTER_OTLP_ENDPOINT=http://{cfg.host}:{bound_port}"
            )
        else:
            typer.echo(f"tracesage viewer at {url}  ({label})")
        if open_browser:
            import webbrowser

            with suppress(Exception):
                webbrowser.open(url)

    announcer = asyncio.create_task(_announce_when_ready())
    try:
        try:
            await server.serve()
        except asyncio.CancelledError:
            # Cancelled from outside (embedding / tests): uvicorn only closes its
            # listening sockets on its own signal-driven exit, so do it here.
            with suppress(Exception):
                await server.shutdown()
            raise
    finally:
        announcer.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await announcer
        if follower is not None:
            follower.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await follower
        if chat_follower is not None:
            chat_follower.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await chat_follower
        if tracer is not None:
            await tracer.stop()  # drains the queue, then closes the DB
        else:
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
    port: int = typer.Option(
        7842, "--port", "-p",
        help="Bind port. If busy, the next free port is used (set TRACESAGE_PORT_AUTO=false to disable).",
    ),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        envvar="TRACESAGE_AUTH_TOKEN",
        help="Required if --host is non-loopback.",
    ),
    open_browser: bool = typer.Option(
        False, "--open", "-o", help="Open the viewer in your browser once it's up."
    ),
    ingest: bool = typer.Option(
        False,
        "--ingest",
        help=(
            "Also accept OpenTelemetry spans on POST /v1/traces (OTLP/HTTP, protobuf or "
            "JSON) and store them in this data dir — a standalone collector for Pydantic AI, "
            "OpenAI Agents SDK, CrewAI, OpenAI/Anthropic clients or any other OTel source."
        ),
    ),
) -> None:
    """Start a viewer for a tracesage data directory.

    By default this is read-only: it does NOT ingest events — only tracesage running
    inside your Python code does that. Use it to inspect data after the fact, or to
    run the UI on a different host than the producer.

    With --ingest it also runs a collector: point any OpenTelemetry exporter at it
    (OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:7842) and the spans show up live.
    """
    # Build config; exits 2 on host=0.0.0.0 without token (production rail).
    cfg = _build_config(data_dir=data_dir, host=host, port=port, auth_token=auth_token)
    try:
        asyncio.run(
            _serve_async(
                cfg, label=f"data: {data_dir}", open_browser=open_browser, ingest=ingest
            )
        )
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


@app.command()
def show(
    run_id: str = typer.Argument(..., help="Run id to render."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
    color: bool | None = typer.Option(
        None, "--color/--no-color", help="Force ANSI colour on/off (default: auto)."
    ),
    reverse: bool = typer.Option(
        False, "--reverse", "-r",
        help="Order siblings newest-first (default follows execution flow: oldest-first).",
    ),
    waterfall: bool = typer.Option(
        False, "--waterfall", help="Print ASCII latency bars instead of the tree."
    ),
) -> None:
    """Print a run's trace as an indented tree in the terminal (no server needed).

    Each element kind (chain, agent, tool, llm, retriever, …) is shown in its own
    colour, and tools backed by an MCP server are tagged with `mcp:<server>`.
    Siblings follow execution flow (oldest-first); pass --reverse for newest-first.
    """

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
            if waterfall:
                from tracesage.pricing import format_cost
                from tracesage.spans import build_spans

                rows = build_spans(events, running=run.status.value == "running")
                width = 40
                span = max((row.end_ms for row in rows), default=1) or 1
                cost = format_cost(run.total_cost_usd)
                typer.echo(
                    f"{run.run_id}  {run.status.value}  "
                    f"in={run.total_tokens_input} out={run.total_tokens_output}  cost={cost}"
                )
                for row in rows:
                    indent = "  " * row.depth
                    left = int(width * row.start_ms / span)
                    bar = int(max(1, width * row.duration_ms / span))
                    line = " " * left + "#" * min(bar, width - left)
                    extra = format_cost(row.cost_usd) if row.cost_usd is not None else ""
                    typer.echo(
                        f"{indent}{row.name[:24]:<24} {line:<{width}} "
                        f"{row.duration_ms}ms {row.status} {extra}"
                    )
            else:
                typer.echo(
                    render_run_tree(run, events, use_color=color, reverse=reverse)
                )
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
    if interval <= 0:
        typer.echo("error: --interval must be > 0", err=True)
        raise typer.Exit(2)

    from tracesage.render import _kind_of, kind_icon, sanitize_terminal, stream_supports_unicode

    ascii_only = not stream_supports_unicode()
    err_glyph = "x" if ascii_only else "✗"

    async def _watch() -> int:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        seen: set[str] = set()
        first_poll = True
        try:
            while True:
                events = await db.get_journey(run_id)
                for ev in events:
                    if ev.event_id in seen:
                        continue
                    seen.add(ev.event_id)
                    kind = _kind_of(ev.event_type.value)
                    icon = kind_icon(kind, ascii_only=ascii_only)
                    name = sanitize_terminal(ev.agent_name or ev.tool_name or kind)
                    ts = ev.timestamp.strftime("%H:%M:%S")
                    mark = ""
                    if ev.event_type.value.endswith("_error"):
                        first_line = ((ev.error_message or "").splitlines() or [""])[0]
                        mark = f"  {err_glyph} {sanitize_terminal(first_line[:80])}"
                    elif ev.duration_ms is not None:
                        mark = f"  {ev.duration_ms}ms"
                    typer.echo(f"[{ts}] {icon} {ev.event_type.value:<14} {name}{mark}")
                run = await db.get_run(run_id)
                if first_poll and run is None and not events:
                    typer.echo(f"run not found: {run_id}", err=True)
                    return 1
                first_poll = False
                if once or (run is not None and run.status.value != "running"):
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

    async def _diff() -> int:
        from tracesage.compare import compare_runs

        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            compared = await compare_runs(db, run_a, run_b)
            if compared is None:
                missing = run_a if await db.get_run(run_a) is None else run_b
                typer.echo(f"run not found: {missing}", err=True)
                return 1
            wa, wb = run_a[:18], run_b[:18]
            typer.echo(f"{'metric':<12} {wa:<22} {wb:<22}")
            typer.echo(f"{'-'*12} {'-'*22} {'-'*22}")
            for row in compared["rows"]:
                va, vb = str(row["a"]), str(row["b"])
                flag = "" if row["same"] else "  ≠"
                typer.echo(f"{row['metric']:<12} {va:<22} {vb:<22}{flag}")
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
    try:
        cfg = _build_config(data_dir=tmp, host=host, port=port)
    except typer.Exit:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    cfg = _build_config(data_dir=data_dir, host=host, port=port)
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
    run_id_arg: str | None = typer.Argument(
        None, metavar="[RUN_ID]",
        help="Run to export (positional alternative to --run-id).",
    ),
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage", "--data-dir", "-d"
    ),
    run_id: str | None = typer.Option(None, "--run-id", help="Single run to export."),
    all_runs: bool = typer.Option(False, "--all", help="Export every run."),
    output: Path = typer.Option(Path("-"), "--output", "-o", help='Output file or "-" for stdout.'),
    format: str = typer.Option("jsonl", "--format", "-f", help="Currently only jsonl."),
) -> None:
    """Dump runs to JSONL: first line is the Run row; subsequent lines are events.

    The run id may be given positionally (`tracesage export RUN_ID`) or via
    `--run-id RUN_ID`. Only structured event fields are exported — the gzipped
    raw-payload blobs are NOT included, so `import` will land events with no
    blob_path.
    """
    run_id = run_id or run_id_arg
    if format != "jsonl":
        typer.echo(f"Only 'jsonl' is supported; got {format!r}", err=True)
        raise typer.Exit(2)
    if not run_id and not all_runs:
        typer.echo("Specify a run id (positionally or via --run-id) or --all", err=True)
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
        help="Filter by status: all | running | completed | failed | interrupted.",
    ),
    limit: int = typer.Option(50, "--limit", "-l", help="Max rows to return."),
    offset: int = typer.Option(0, "--offset", help="Pagination offset."),
    tag: str | None = typer.Option(
        None, "--tag",
        help="Only show runs whose tags contain this substring (substring match).",
    ),
    thread: str | None = typer.Option(
        None, "--thread",
        help="Only show runs from this conversation thread (exact thread_id match).",
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
    statuses = ("all", "running", "completed", "failed", "interrupted")
    if status not in statuses:
        typer.echo(f"--status must be one of: {' '.join(statuses)} (got {status!r})", err=True)
        raise typer.Exit(2)

    async def _runs() -> None:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            rows, total = await db.list_runs(
                status=None if status == "all" else status,
                limit=limit,
                offset=offset,
                tag=tag,
                thread_id=thread,
            )

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
                    thread_s = f"  thread={r.thread_id}" if r.thread_id else ""
                    from tracesage.pricing import format_cost

                    typer.echo(
                        f"{r.run_id}  {r.status.value:11s}  {started}  "
                        f"steps={r.total_steps:<4d}  "
                        f"tok={r.total_tokens_input}+{r.total_tokens_output}  "
                        f"cost={format_cost(r.total_cost_usd)}  "
                        f"tags=[{tags_s}]{thread_s}"
                    )
                typer.echo(
                    f"# {len(rows)} of {total} (status={status}, "
                    f"limit={limit}, offset={offset}, tag={tag!r})"
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
    if max_runs < 1:
        typer.echo("error: --max-runs must be >= 1", err=True)
        raise typer.Exit(2)
    if max_blob_size_gb is not None and max_blob_size_gb <= 0:
        typer.echo("error: --max-blob-size-gb must be > 0", err=True)
        raise typer.Exit(2)

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
    input_arg: Path | None = typer.Argument(
        None, metavar="[INPUT]",
        help='JSONL file (positional alternative to --input).',
    ),
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage", "--data-dir", "-d"
    ),
    input: Path = typer.Option(
        Path("-"), "--input", "-i", help='JSONL file or "-" for stdin.'
    ),
) -> None:
    """Import a JSONL export back into a data dir (inverse of `export`).

    The file may be given positionally (`tracesage import trace.jsonl`) or via
    `--input`/`-i`; default is stdin. Reads lines produced by `tracesage export`
    (each tagged with "_kind": "run" or "_kind": "event") and upserts them into
    the target data dir, creating/initializing it if needed. One malformed line
    is skipped with a warning rather than aborting the whole import.

    Raw-payload blobs are not part of the export, so imported events have
    their blob_path cleared; structured fields are restored in full.
    """
    # Positional arg wins when given; otherwise fall back to --input (stdin default).
    if input_arg is not None:
        input = input_arg

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
def search(
    query: str = typer.Argument(..., help="Text to find in summaries and payloads."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
    run_id: str | None = typer.Option(None, "--run", help="Limit the search to one run."),
    limit: int = typer.Option(20, "--limit", "-l"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Search event summaries and payload text."""

    async def _search() -> None:
        _cfg, db, _blob = _make_backend(data_dir)
        await db.init()
        try:
            hits = await db.search_events(query, limit=limit, run_id=run_id)
            if as_json:
                for hit in hits:
                    typer.echo(hit.model_dump_json())
                return
            if not hits:
                typer.echo("(no matches)")
                return
            for hit in hits:
                typer.echo(f"{hit.run_id}  {hit.event_type}  {hit.name or ''}  {hit.snippet}")
        finally:
            await db.close()

    asyncio.run(_search())


@app.command()
def thread(
    thread_id: str = typer.Argument(..., help="Thread id to print as a conversation."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
) -> None:
    """Print one conversation thread."""

    async def _thread() -> int:
        from tracesage.conversation import build_turns
        from tracesage.pricing import format_cost

        _cfg, db, blob = _make_backend(data_dir)
        await db.init()
        try:
            runs = await db.get_thread_runs(thread_id)
            if not runs:
                typer.echo(f"thread not found: {thread_id}", err=True)
                return 1
            events = []
            payloads: dict[str, Any] = {}
            for run in runs:
                journey = await db.get_journey(run.run_id)
                events.extend(journey)
                for event in journey:
                    if event.blob_path and event.run_id == run.run_id:
                        try:
                            payloads[event.event_id] = await blob.read(event.blob_path)
                        except Exception as exc:
                            typer.echo(f"warning: skipped blob {event.event_id}: {exc}", err=True)
            for turn in build_turns(runs, events, payloads):
                typer.echo(f"--- {turn.run_id}  {turn.status}  {format_cost(turn.cost_usd)}")
                if turn.user_text:
                    typer.echo(f"user: {turn.user_text}")
                if turn.assistant_text:
                    typer.echo(f"assistant: {turn.assistant_text}")
                if turn.tools:
                    names = ", ".join(
                        t.name + (f" ({t.mcp_server})" if t.mcp_server else "") for t in turn.tools
                    )
                    typer.echo(f"tools: {names}")
                if turn.error:
                    typer.echo(f"error: {turn.error}")
            return 0
        finally:
            await db.close()

    raise typer.Exit(asyncio.run(_thread()))


@app.command()
def reindex(
    data_dir: Path = typer.Option(
        Path.home() / ".tracesage", "--data-dir", "-d"
    ),
) -> None:
    """Backfill payload search text and dollar cost for events already on disk."""

    async def _reindex() -> None:
        from tracesage.maintenance import reindex as _do

        cfg, db, blob = _make_backend(data_dir)
        await db.init()
        try:
            result = await _do(db, blob, cfg)
        finally:
            await db.close()
        typer.echo(
            f"reindexed {result['events']} events "
            f"({result['priced']} priced) across {result['runs']} runs"
        )

    asyncio.run(_reindex())


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

            try:
                if await db.needs_reindex():
                    typer.echo(
                        "  warn search/cost backfill pending: run `tracesage reindex -d "
                        f"{data_dir}`"
                    )
                else:
                    typer.echo("  ok   search/cost index: current")
            except Exception as e:
                typer.echo(f"  warn reindex check: probe failed: {e}")

            try:
                from tracesage.connect import status_lines

                for line in status_lines(Path(os.environ.get("TRACESAGE_CONNECT_HOME", str(Path.home()))), data_dir):
                    typer.echo(f"  {line}")
            except Exception as e:
                typer.echo(f"  warn app connect: probe failed: {e}")

            try:
                from tracesage.pricing import pricing_available

                if pricing_available():
                    typer.echo("  ok   pricing: genai-prices installed")
                else:
                    typer.echo(
                        '  warn pricing: not installed (cost shows as "n/a"). '
                        'pip install "tracesage[pricing]"'
                    )
            except Exception as e:
                typer.echo(f"  warn pricing: probe failed: {e}")

            # Total runs.
            run_ids: set[str] = set()
            try:
                stats_dict = await db.get_stats()
                typer.echo(f"  ok   total runs: {stats_dict.get('total_runs', 0)}")
            except Exception as e:
                typer.echo(f"  warn total runs: probe failed: {e}")

            try:
                runs, _total = await db.list_runs(limit=1_000_000, offset=0)
                run_ids = {r.run_id for r in runs}
            except Exception as e:
                typer.echo(f"  warn run listing: probe failed: {e}")

            # Blobs live under the run_id of the event that wrote them (often a
            # nested sub-run), at `<blob_dir>/<blob_path>`. One pass over every
            # root's journey yields both the dirs that should exist and the blob
            # files events point at.
            blob_dir = cfg.blob_dir
            referenced_dirs: set[str] = set()
            missing_runs: set[str] = set()
            try:
                for rid in run_ids:
                    async for ev in db.iter_journey(rid):
                        referenced_dirs.add(ev.run_id)
                        if ev.blob_path and not (blob_dir / ev.blob_path).exists():
                            missing_runs.add(rid)
                if missing_runs:
                    typer.echo(f"  warn runs with missing blob files: {len(missing_runs)}")
                else:
                    typer.echo("  ok   runs with missing blob files: 0")
            except Exception as e:
                typer.echo(f"  warn missing blob check: probe failed: {e}")

            # Orphan blobs: blob dirs that no stored event belongs to.
            try:
                orphan_dirs = (
                    [
                        p.name
                        for p in blob_dir.iterdir()
                        if p.is_dir() and p.name not in referenced_dirs
                    ]
                    if blob_dir.exists()
                    else []
                )
                if orphan_dirs:
                    typer.echo(f"  warn orphan blob dirs (no events): {len(orphan_dirs)}")
                else:
                    typer.echo("  ok   orphan blob dirs: 0")
            except Exception as e:
                typer.echo(f"  warn orphan blob check: probe failed: {e}")
        finally:
            await db.close()

    asyncio.run(_doctor())


def _connect_home() -> Path:
    override = os.environ.get("TRACESAGE_CONNECT_HOME")
    return Path(override) if override else Path.home()


@app.command()
def connect(
    app_name: str = typer.Argument("all", help="claude-code, codex, cursor, claude-desktop, chatgpt, vscode, windsurf, gemini, or all."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
    metadata_only: bool = typer.Option(False, "--metadata-only", help="Do not store prompt or tool text."),
    mcp: bool = typer.Option(False, "--mcp", help="Also let the app call tracesage over MCP."),
    no_trace: bool = typer.Option(False, "--no-trace"),
    no_service: bool = typer.Option(False, "--no-service"),
    otel: bool = typer.Option(False, "--otel", help="Also point the app's OpenTelemetry export at tracesage."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    status: bool = typer.Option(False, "--status"),
    print_only: str | None = typer.Option(None, "--print", help="Print the planned config for one app and write nothing."),
    mcpb: bool = typer.Option(False, "--mcpb", help="Also build tracesage.mcpb for Claude Desktop."),
) -> None:
    """Record sessions from installed AI apps in this tracesage collector."""
    from tracesage.connect import (
        INSTALL_HINT,
        apply_plan,
        apps_extra_ready,
        build_plan,
        detect_installed,
        executable_path,
        install_service_file,
        status_lines,
        write_forwarder,
    )
    from tracesage.service import choose_port, read_state, write_state

    home = _connect_home()
    if status:
        for line in status_lines(home, data_dir):
            typer.echo(line)
        return
    if not apps_extra_ready():
        typer.echo(f"tracesage connect needs the apps extra: {INSTALL_HINT}", err=True)
        raise typer.Exit(1)
    known = {
        "all", "claude-code", "codex", "cursor", "claude-desktop",
        "chatgpt", "vscode", "windsurf", "gemini",
    }
    target = print_only or app_name
    if target not in known:
        typer.echo(f"unknown app {target!r}", err=True)
        raise typer.Exit(2)
    if target == "all":
        apps = detect_installed(home) or ["claude-code", "codex", "cursor", "claude-desktop"]
    else:
        apps = [target]
    state = read_state(data_dir)
    port = int(state["port"]) if isinstance(state.get("port"), int) else choose_port()
    plan = build_plan(
        home,
        apps,
        data_dir=data_dir,
        executable=executable_path(),
        port=port,
        metadata_only=metadata_only,
        mcp=mcp,
        trace=not no_trace,
        otel=otel,
    )
    if not plan.changes:
        typer.echo("Nothing to change.")
    for change in plan.changes:
        typer.echo(f"{change.action}: {change.path}")
        if change.action == "write" and (dry_run or print_only):
            typer.echo(change.after)
    for note in plan.notes:
        typer.echo(f"note: {note}")
    if print_only or dry_run:
        return
    if plan.blocked:
        typer.echo("refusing to write; resolve the notes above", err=True)
        raise typer.Exit(1)
    if not yes and sys.stdin.isatty():
        answer = input("Apply these changes? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            typer.echo("cancelled")
            raise typer.Exit(1)
    write_forwarder(home)
    apply_plan(plan, dry_run=False)
    write_state(data_dir, {"port": port, "metadata_only": metadata_only})
    if not no_service:
        path = install_service_file(
            home,
            executable=executable_path(),
            data_dir=data_dir,
            port=port,
            metadata_only=metadata_only,
        )
        if path is not None:
            from tracesage.service import start_service

            code, message = start_service(
                home,
                data_dir=data_dir,
                executable=executable_path(),
                port=port,
                metadata_only=metadata_only,
            )
            typer.echo(message)
            if code != 0:
                typer.echo(f"service file: {path}", err=True)
    if mcpb:
        from tracesage.connect import pack_mcpb

        built = pack_mcpb(data_dir)
        if built is None:
            typer.echo("mcpb pack did not produce tracesage.mcpb", err=True)
        else:
            typer.echo(f"mcpb: {built}")
    typer.echo("Restart the apps you connected.")


@app.command()
def disconnect(
    app_name: str = typer.Argument("all"),
    home: Path | None = typer.Option(None, "--home", hidden=True),
) -> None:
    """Restore app configs from the backups connect made."""
    from tracesage.connect import disconnect as _disconnect

    root = home or _connect_home()
    apps = None if app_name == "all" else [app_name]
    restored = _disconnect(root, apps)
    if not restored:
        typer.echo("No tracesage backups to restore.")
        return
    for path in restored:
        typer.echo(f"restored {path}")


@app.command()
def service(
    action: str = typer.Argument(..., help="install, uninstall, start, stop, or status."),
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
) -> None:
    """Install or inspect the background collector."""
    from tracesage.connect import executable_path, install_service_file
    from tracesage.service import (
        launchd_path,
        platform_name,
        read_state,
        systemd_path,
    )

    home = _connect_home()
    if action == "status":
        from tracesage.connect import status_lines

        for line in status_lines(home, data_dir):
            typer.echo(line)
        return
    if action == "install":
        state = read_state(data_dir)
        port = int(state.get("port") or 7842)
        path = install_service_file(
            home,
            executable=executable_path(),
            data_dir=data_dir,
            port=port,
            metadata_only=bool(state.get("metadata_only")),
        )
        typer.echo(f"wrote {path} ({platform_name()})")
        return
    if action == "uninstall":
        for path in (launchd_path(home), systemd_path(home)):
            if path.exists():
                path.unlink()
                typer.echo(f"removed {path}")
        return
    if action == "start":
        from tracesage.service import start_service

        state = read_state(data_dir)
        port = int(state.get("port") or 7842)
        code, message = start_service(
            home,
            data_dir=data_dir,
            executable=executable_path(),
            port=port,
            metadata_only=bool(state.get("metadata_only")),
        )
        typer.echo(message)
        if code != 0:
            raise typer.Exit(code)
        return
    if action == "stop":
        from tracesage.service import stop_service

        code, message = stop_service(home)
        typer.echo(message)
        if code != 0:
            raise typer.Exit(code)
        return
    typer.echo("action must be install, uninstall, start, stop, or status", err=True)
    raise typer.Exit(2)


@app.command("mcp")
def mcp_cmd(
    data_dir: Path = typer.Option(Path.home() / ".tracesage", "--data-dir", "-d"),
    read_only: bool = typer.Option(False, "--read-only", help="Disable add_note, set_tags and delete_run."),
    allow_delete: bool = typer.Option(False, "--allow-delete", help="Allow the delete_run tool."),
) -> None:
    """Serve tracesage as an MCP server on stdio for a coding agent."""
    from tracesage.mcp_server import mcp_installed

    if not mcp_installed():
        typer.echo(
            'tracesage mcp needs the apps extra: pip install "tracesage[apps]"',
            err=True,
        )
        raise typer.Exit(1)
    from tracesage.mcp_server import serve

    serve(data_dir, read_only=read_only, allow_delete=allow_delete)


@app.command()
def version() -> None:
    """Print tracesage version."""
    typer.echo(f"tracesage {__version__}")


if __name__ == "__main__":
    app()
