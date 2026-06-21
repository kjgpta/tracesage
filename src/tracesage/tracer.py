"""TraceSage: top-level orchestrator.

One entry point users call: `tracer = await TraceSage.create(config)`. Holds the
queue, worker task, callback handler, and (optionally) an embedded uvicorn server.
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import logging
import re
import socket
import sys
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, cast

from tracesage.config import TraceSageConfig
from tracesage.models import RawEvent, Stats
from tracesage.worker import StorageWorker

if TYPE_CHECKING:
    from tracesage.adapters.langchain import TraceSageCallbackHandler
    from tracesage.storage.backend import StorageBackend

log = logging.getLogger("tracesage.tracer")


def _resolve_bind_port(host: str, port: int, *, auto: bool, scan: int = 20) -> int:
    """Pick the port the embedded UI server should bind.

    - port 0 -> 0 (let the OS assign an ephemeral port).
    - auto=False -> the configured port verbatim (caller's fixed-port intent).
    - auto=True -> the first free port scanning upward from `port` (so a second
      app on the same machine lands on port+1, port+2, …); if the whole window is
      busy, 0 (ephemeral) as a last resort so the UI still comes up.

    Probing is best-effort (a tiny TOCTOU window remains, covered by the caller's
    fail-soft serve guard).
    """
    if port == 0 or not auto:
        return port
    for candidate in range(port, port + scan + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return 0


class _NullWebSocketManager:
    """Stub used when the server module is unavailable. All methods are no-ops."""

    async def broadcast(self, run_id: str, message: Any) -> None:
        del run_id, message
        return None

    async def broadcast_all(self, message: Any) -> None:
        del message
        return None


class TraceSage:
    """Top-level tracer. Use `await TraceSage.create()` to construct."""

    def __init__(
        self,
        config: TraceSageConfig,
        db: StorageBackend,
        blob_store: Any,
        ws_manager: Any,
        queue: asyncio.Queue,
        stats: Stats,
        worker: StorageWorker,
        worker_task: asyncio.Task,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._config = config

        # Opt-in redaction. Compile patterns once; bad patterns are skipped with a
        # warning so a misconfigured regex never crashes the tracer.
        self._redactors: list[re.Pattern] = []
        for _p in (config.redact_patterns or []):
            try:
                self._redactors.append(re.compile(_p))
            except re.error as _e:
                log.warning("tracesage: skipping invalid redact pattern %r: %s", _p, _e)
        # Depth cap for recursive payload redaction: guards against deeply nested
        # or self-referential payloads (which would otherwise raise RecursionError).
        self._redact_max_depth = 60

        self._db = db
        self._blob_store = blob_store
        self._ws_manager = ws_manager
        self._queue = queue
        self._stats = stats
        self._worker = worker
        self._worker_task = worker_task
        self._loop = loop

        # Run-id bookkeeping for sampling, root resolution, throttling. All bounded
        # with FIFO eviction so long-running processes do not leak memory through
        # these maps. Entries naturally age out as new runs arrive.
        self._run_state_cap = 100_000
        # _root_map keys EVERY run_id (roots + nested sub-runs), so it grows faster
        # than the per-root maps; it shares the same cap to keep behaviour uniform.
        self._root_map_cap = 100_000
        self._run_event_counts: OrderedDict[str, int] = OrderedDict()
        self._throttled_runs: OrderedDict[str, None] = OrderedDict()
        self._root_map: OrderedDict[str, str] = OrderedDict()
        # Roots whose "view trace" link we've already printed (print once each).
        self._announced_roots: OrderedDict[str, None] = OrderedDict()
        self._dropped_warning_threshold = 100

        # tool_name -> MCP server name. Registered at setup (before invoking), read by
        # the callback handler during ingestion to attribute tool calls to their MCP
        # server. See tracesage.adapters.mcp for the registration helpers.
        self._tool_sources: dict[str, str] = {}

        # Guards the cross-thread run-state maps (_run_event_counts, _throttled_runs,
        # _root_map). emit() and get_or_set_root run on arbitrary LangChain executor
        # threads. Non-reentrant: helpers called under the lock MUST NOT re-acquire it.
        self._state_lock = threading.Lock()

        self._handler: TraceSageCallbackHandler | None = None
        self._server: Any = None
        self._server_task: asyncio.Task | None = None
        self._otel: Any = None  # optional OpenTelemetry span exporter
        self._stopped = False
        self.bound_port: int | None = None

        atexit.register(self._stop_sync)

    # ----------------------------------------------------------------- create

    @classmethod
    async def create(
        cls,
        config: TraceSageConfig | None = None,
        *,
        start_server: bool | None = None,
    ) -> TraceSage:
        cfg = config or TraceSageConfig()
        # The kwarg, when given, overrides config.start_server (env TRACESAGE_START_SERVER).
        start_server = cfg.start_server if start_server is None else start_server
        if not cfg.enabled:
            # Kill switch (TRACESAGE_ENABLED=false): return an inert tracer — no
            # server, no DB/worker, a no-op handler. Integration code is unchanged.
            return cast("TraceSage", _DisabledTraceSage(cfg))
        cfg.ensure_data_dirs()

        # Storage. Imports inside the function so import failures don't poison module load.
        from tracesage.storage.blob_store import BlobStore
        from tracesage.storage.sqlite_backend import SQLiteBackend

        db: StorageBackend = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        blob_store = BlobStore(cfg.blob_dir)

        queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_maxsize)
        stats = Stats(queue_max=cfg.queue_maxsize)

        ws_manager: Any
        try:
            from tracesage.server.ws import WebSocketManager  # type: ignore[import-not-found]

            ws_manager = WebSocketManager()
        except Exception:
            ws_manager = _NullWebSocketManager()

        # Optional OpenTelemetry export. Best-effort: a missing extra or a bad
        # endpoint must never stop tracing from starting.
        otel_exporter: Any = None
        if cfg.otlp_endpoint:
            try:
                from tracesage.exporters.otel import OTelSpanExporter

                otel_exporter = OTelSpanExporter(
                    endpoint=cfg.otlp_endpoint,
                    service_name=cfg.otlp_service_name,
                    headers=cfg.otlp_headers,
                )
            except Exception as e:
                log.warning(
                    "OpenTelemetry export disabled — could not initialize exporter "
                    "(is the `tracesage[otel]` extra installed?): %s",
                    e,
                )

        worker = StorageWorker(queue, db, blob_store, ws_manager, cfg, stats, otel_exporter)
        loop = asyncio.get_running_loop()
        worker_task = asyncio.create_task(worker.run(), name="tracesage.worker")

        instance = cls(
            config=cfg,
            db=db,
            blob_store=blob_store,
            ws_manager=ws_manager,
            queue=queue,
            stats=stats,
            worker=worker,
            worker_task=worker_task,
            loop=loop,
        )
        instance._otel = otel_exporter

        # Lazy import of handler so importing tracesage doesn't require langchain-core.
        from tracesage.adapters.langchain import TraceSageCallbackHandler

        instance._handler = TraceSageCallbackHandler(instance)

        if start_server:
            await instance._start_server()

        return instance

    @classmethod
    @contextlib.asynccontextmanager
    async def session(
        cls,
        config: TraceSageConfig | None = None,
        *,
        start_server: bool | None = None,
        install: bool = False,
    ) -> Any:
        """Async context manager: create a tracer, optionally install it globally, and
        stop it cleanly on exit.

            async with TraceSage.session(install=True) as tl:
                await agent.ainvoke(...)        # captured automatically
        """
        tl = await cls.create(config, start_server=start_server)
        if install:
            tl.install()
        try:
            yield tl
        finally:
            if install:
                with contextlib.suppress(Exception):
                    tl.uninstall()
            await tl.stop()

    # -------------------------------------------------------- public properties

    @property
    def handler(self) -> TraceSageCallbackHandler:
        if self._handler is None:  # pragma: no cover - guarded by create()
            raise RuntimeError("Handler not initialized; use TraceSage.create()")
        return self._handler

    @property
    def db(self) -> StorageBackend:
        return self._db

    @property
    def blob_store(self) -> Any:
        return self._blob_store

    @property
    def stats(self) -> Stats:
        self._stats.queue_depth = self._queue.qsize()
        return self._stats

    # ----------------------------------------------------------- dev ergonomics

    @property
    def ui_url(self) -> str | None:
        """Base URL of the embedded UI (e.g. ``http://127.0.0.1:7843/ui``), or None
        if no server is running. Reflects the actual bound port, which may differ
        from the configured one when auto-port picks a free port."""
        base = self._config.public_url
        if not base:
            if self.bound_port is None:
                return None
            host = self._config.host
            if host in ("0.0.0.0", "::"):  # noqa: S104 - display only, not a bind
                host = "127.0.0.1"
            base = f"http://{host}:{self.bound_port}"
        return f"{base.rstrip('/')}/ui"

    def run_url(self, run_id: str) -> str | None:
        """Deep link to a run in the UI, or None if no UI URL is known.

        Uses ``config.public_url`` if set (e.g. behind a proxy), else derives one
        from the bound server address. Returns None when no embedded server is
        running and no ``public_url`` is configured (no point linking to nothing).
        """
        base = self._config.public_url
        if not base:
            if self.bound_port is None:
                return None
            host = self._config.host
            if host in ("0.0.0.0", "::"):  # noqa: S104 - display only, not a bind
                host = "127.0.0.1"
            base = f"http://{host}:{self.bound_port}"
        return f"{base.rstrip('/')}/ui/#run={run_id}"

    def run_view(self, run_id: str) -> Any:
        """A notebook-displayable view of a run (embeds the live UI). Use in a Jupyter
        cell: ``tl.run_view(run_id)``."""
        from tracesage.render import TraceView

        return TraceView(run_id, self.run_url(run_id))

    async def render_tree(self, run_id: str, *, use_color: bool | None = None) -> str:
        """Render a run's events as an indented terminal tree (see `tracesage show`)."""
        from tracesage.render import render_run_tree

        run = await self._db.get_run(run_id)
        events = await self._db.get_journey(run_id)
        return render_run_tree(run, events, use_color=use_color)

    async def flush(self, timeout: float = 5.0) -> None:  # noqa: ASYNC109 - public convenience API
        """Wait until every queued event has been persisted by the worker.

        Useful in tests/notebooks: after invoking your chain, ``await tl.flush()``
        guarantees the events are in the DB before you read them back.
        """
        await asyncio.sleep(0)  # let cross-thread call_soon enqueues land first
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=timeout)

    def install(self) -> TraceSage:
        """Register this tracer's handler as a GLOBAL LangChain callback, so every
        chain/agent/LLM/tool call is captured without passing ``callbacks=`` to each
        invocation. Returns self for chaining. Call :meth:`uninstall` to remove it."""
        from tracesage.adapters.langchain import install_global_handler

        install_global_handler(self.handler)
        return self

    def uninstall(self) -> None:
        """Remove the global LangChain callback registered by :meth:`install`."""
        from tracesage.adapters.langchain import uninstall_global_handler

        uninstall_global_handler()

    # ---------------------------------------------------- MCP tool-source registry

    def register_tool_source(self, tool_name: str, server: str) -> None:
        """Attribute a tool (by name) to an MCP server, so its events/topology node
        are tagged with that provenance. Call at setup, before invoking the graph.

        See ``tracesage.adapters.mcp.register_mcp_client`` for the convenient path
        that does this for every tool of a langchain-mcp-adapters client.
        """
        if not tool_name or not server:
            return
        self._tool_sources[tool_name] = server

    def register_tool_sources(self, mapping: dict[str, str]) -> None:
        """Bulk register {tool_name: server}."""
        for name, server in (mapping or {}).items():
            self.register_tool_source(name, server)

    def tool_source(self, tool_name: str | None) -> str | None:
        """Return the registered MCP server for a tool name, or None if unattributed."""
        if not tool_name:
            return None
        return self._tool_sources.get(tool_name)

    # --------------------------------------------------------------- emit path

    def _is_sampled_in(self, root: str) -> bool:
        """Deterministic per-root sampling: same root always yields the same decision,
        so an evicted-then-reappearing root keeps its verdict (no mid-run flip)."""
        rate = self._config.sample_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        digest = hashlib.blake2b(root.encode("utf-8"), digest_size=8).digest()
        return (int.from_bytes(digest, "big") / 0xFFFFFFFFFFFFFFFF) < rate

    def _redact_text(self, s: str) -> str:
        for rx in self._redactors:
            s = rx.sub(self._config.redact_replacement, s)
        return s

    def _redact_obj(self, obj: Any, _depth: int = 0) -> Any:
        # Over the depth cap we fail CLOSED — drop the subtree to the replacement
        # rather than risk emitting it unredacted (also terminates reference cycles).
        if _depth > self._redact_max_depth:
            return self._config.redact_replacement
        if isinstance(obj, str):
            return self._redact_text(obj)
        if isinstance(obj, dict):
            return {k: self._redact_obj(v, _depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact_obj(v, _depth + 1) for v in obj]
        return obj

    def _redact_event(self, event: RawEvent) -> None:
        # Redact each field independently and FAIL CLOSED: if any field's redaction
        # errors, replace that field rather than leave un-redacted PII in place
        # (it would otherwise be persisted to the blob and broadcast over WS).
        if not self._redactors:
            return
        try:
            event.summary = self._redact_text(event.summary)
        except Exception:
            event.summary = self._config.redact_replacement
        if event.error_message:
            try:
                event.error_message = self._redact_text(event.error_message)
            except Exception:
                event.error_message = self._config.redact_replacement
        try:
            event.raw_payload = self._redact_obj(event.raw_payload)
        except Exception as e:
            log.warning(
                "tracesage: redaction failed for event %s; dropping raw_payload: %s",
                getattr(event, "event_id", "?"),
                e,
            )
            event.raw_payload = {"_redaction_failed": True}

    def emit(self, event: RawEvent) -> None:
        """Thread-safe enqueue with sampling + per-run cap.

        Sampling decision is taken at the root run level: once a root passes, all of
        its descendant events are captured. If it fails, all are dropped.
        """
        try:
            root = event.root_run_id

            if not self._is_sampled_in(root):
                self._stats.events_sampled_out += 1
                return

            with self._state_lock:
                count = self._run_event_counts.get(root, 0)
                if count >= self._config.per_run_event_cap:
                    if root not in self._throttled_runs:
                        self._lru_set(self._throttled_runs, root)
                        self._stats.runs_throttled += 1
                    return
                self._lru_set(self._run_event_counts, root, count + 1)

            # Opt-in redaction once we've decided to keep the event. Mutates only the
            # event (not shared maps), so it's fine outside the state lock. Default
            # config (no patterns) makes this an exact no-op.
            self._redact_event(event)

            # Cross-thread enqueue. If the loop is the same thread, call_soon_threadsafe
            # is still safe and well-defined.
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if running is self._loop:
                self._enqueue(event)
            else:
                self._loop.call_soon_threadsafe(self._enqueue, event)
        except Exception as e:  # pragma: no cover
            log.error("emit() failed: %s", e, exc_info=True)

    def _lru_set(self, store: OrderedDict, key: str, value: Any = None) -> None:
        """Bounded insert/update with FIFO eviction. value=None for set-style use."""
        if key in store:
            # Refresh recency on update.
            store.move_to_end(key)
            store[key] = value
        else:
            store[key] = value
            while len(store) > self._run_state_cap:
                store.popitem(last=False)

    def _enqueue(self, event: RawEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._stats.events_dropped += 1
            if self._stats.events_dropped % self._dropped_warning_threshold == 0:
                log.warning(
                    "tracesage: %d events dropped due to queue backpressure",
                    self._stats.events_dropped,
                )

    def get_or_set_root(self, run_id: str, parent_run_id: str | None) -> str:
        """Resolve root_run_id for this run. Caps the LRU map at ``_root_map_cap``.

        Lock-guarded: runs on arbitrary LangChain executor threads. _evict_root_map_if_needed
        is called under the lock, so it must not re-acquire it.
        """
        is_new_root = False
        with self._state_lock:
            if parent_run_id is None:
                existing = self._root_map.get(run_id)
                if existing is None:
                    self._root_map[run_id] = run_id
                    self._evict_root_map_if_needed()
                    result = run_id
                    is_new_root = True
                else:
                    self._root_map.move_to_end(run_id)
                    result = existing
            else:
                # Child run: inherit parent's root.
                parent_root = self._root_map.get(parent_run_id, parent_run_id)
                self._root_map[run_id] = parent_root
                if parent_run_id in self._root_map:
                    self._root_map.move_to_end(parent_run_id)
                # Touch the resolved ROOT too (not just the direct parent) so an actively
                # emitting subtree keeps its root entry fresh and it is not FIFO-evicted
                # mid-run, which would mis-root later descendants.
                if parent_root in self._root_map:
                    self._root_map.move_to_end(parent_root)
                self._root_map.move_to_end(run_id)
                self._evict_root_map_if_needed()
                result = parent_root

        # Announce the trace link OUTSIDE the lock (it does IO): once per new root.
        if is_new_root:
            self._maybe_announce_root(result)
        return result

    def _maybe_announce_root(self, root: str) -> None:
        """Print a clickable 'view trace' link to stderr the first time a root run is
        seen. No-op if disabled, already announced, or no UI URL is available."""
        if not self._config.print_run_url:
            return
        with self._state_lock:
            if root in self._announced_roots:
                return
            self._lru_set(self._announced_roots, root)
        url = self.run_url(root)
        if url:
            with contextlib.suppress(Exception):
                print(f"\N{LEFT-POINTING MAGNIFYING GLASS} tracesage: {url}", file=sys.stderr, flush=True)

    def _evict_root_map_if_needed(self) -> None:
        while len(self._root_map) > self._root_map_cap:
            self._root_map.popitem(last=False)

    # ------------------------------------------------------------------ server

    async def _start_server(self) -> None:
        try:
            from tracesage.server.app import create_app  # type: ignore[import-not-found]
        except Exception as e:
            log.warning("Server module unavailable, skipping server start: %s", e)
            return

        try:
            import uvicorn  # type: ignore[import-not-found]

            app = create_app(
                db=self._db,
                blob_store=self._blob_store,
                ws_manager=self._ws_manager,
                stats=self._stats,
                config=self._config,
            )
            bind_port = _resolve_bind_port(
                self._config.host, self._config.port, auto=self._config.port_auto
            )
            uv_config = uvicorn.Config(
                app,
                host=self._config.host,
                port=bind_port,
                log_level="warning",
                lifespan="on",
            )
            self._server = uvicorn.Server(uv_config)

            async def _serve_guarded() -> None:
                # The embedded UI is best-effort and MUST NEVER crash the host
                # application. uvicorn calls sys.exit(1) when it can't bind the
                # port (e.g. another tracesage/serve is already on it) — that is
                # a SystemExit (BaseException), which would otherwise escape this
                # background task and tear down the caller's asyncio.run(). Contain
                # it here; let CancelledError through for clean shutdown.
                try:
                    await self._server.serve()  # type: ignore[union-attr]
                except asyncio.CancelledError:
                    raise
                except SystemExit:
                    log.warning(
                        "tracesage embedded UI did not start — port %s is likely "
                        "already in use. Tracing continues normally without the "
                        "local UI (free the port or set a different one to enable it).",
                        self._config.port,
                    )
                except Exception as e:  # pragma: no cover - defensive
                    log.warning("tracesage embedded UI server stopped: %s", e)

            self._server_task = asyncio.create_task(
                _serve_guarded(), name="tracesage.server"
            )

            # Poll for startup completion within budget — stop early if the task
            # already finished (i.e. the server failed to bind and exited).
            deadline = self._loop.time() + self._config.startup_health_timeout_s
            while self._loop.time() < deadline:
                if getattr(self._server, "started", False) or self._server_task.done():
                    break
                await asyncio.sleep(0.05)

            if getattr(self._server, "started", False):
                # Capture the actual port. When bind_port is 0 (ephemeral — either
                # configured or the scan-exhausted fallback) read it off the socket.
                if bind_port == 0:
                    with contextlib.suppress(Exception):  # pragma: no cover
                        servers = getattr(self._server, "servers", None) or []
                        if servers:
                            socks = getattr(servers[0], "sockets", None) or []
                            if socks:
                                self.bound_port = socks[0].getsockname()[1]
                else:
                    self.bound_port = bind_port
            else:
                # Server never came up (bind failure / timeout). Leave bound_port
                # as None so callers don't advertise a UI URL that isn't serving.
                self.bound_port = None
        except Exception as e:
            log.warning("Failed to start embedded server: %s", e)

    # -------------------------------------------------------------- shutdown

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True

        # 1. Tell worker to stop accepting new batches.
        self._worker.request_shutdown()

        # 2. Best-effort drain: wait for any in-flight events to clear the queue.
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5.0)
        except TimeoutError:
            log.warning("Queue drain timed out; some events may be lost.")
        except Exception as e:  # pragma: no cover
            log.warning("Queue drain error: %s", e)

        # 3. Cancel and gather worker.
        if not self._worker_task.done():
            self._worker_task.cancel()
        await asyncio.gather(self._worker_task, return_exceptions=True)

        # 3b. Flush + shut down the OTel exporter (after the worker has drained, so
        # all spans are created first). Offloaded — provider.shutdown() may block on
        # a final network flush.
        if self._otel is not None:
            try:
                await self._loop.run_in_executor(None, self._otel.shutdown)
            except Exception as e:  # pragma: no cover
                log.warning("OTel exporter shutdown error: %s", e)

        # 4. Stop server.
        if self._server is not None and self._server_task is not None:
            try:
                self._server.should_exit = True
                await asyncio.wait_for(self._server_task, timeout=5.0)
            except TimeoutError:
                self._server_task.cancel()
                await asyncio.gather(self._server_task, return_exceptions=True)
            except Exception as e:  # pragma: no cover
                log.warning("Server stop error: %s", e)

        # 5. Close DB.
        try:
            await self._db.close()
        except Exception as e:  # pragma: no cover
            log.warning("DB close error: %s", e)

    def _stop_sync(self) -> None:
        """Best-effort cleanup hook for atexit. Only runs the async stop if a loop exists."""
        if self._stopped:
            return
        with contextlib.suppress(Exception):
            loop = self._loop
            if loop.is_closed():
                return
            if loop.is_running():
                # Schedule and forget; we cannot block atexit on a still-running loop.
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.stop()))
            else:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(self.stop())


def _make_noop_handler() -> Any:
    """A LangChain callback handler the framework skips entirely.

    Used by the disabled tracer. Every ``ignore_*`` flag is True, so LangChain's
    ``handle_event`` does a single boolean check and NEVER dispatches an event to it —
    meaning an existing ``callbacks=[tracer.handler]`` integration costs ~nothing when
    disabled. Lazy import — only needed if ``.handler`` is accessed."""
    from langchain_core.callbacks import BaseCallbackHandler

    class _NoopHandler(BaseCallbackHandler):
        raise_error = False
        ignore_llm = True
        ignore_chat_model = True
        ignore_chain = True
        ignore_agent = True
        ignore_retriever = True
        ignore_retry = True
        ignore_custom_event = True

    return _NoopHandler()


class _DisabledTraceSage:
    """Inert tracer returned when ``config.enabled`` is False.

    Mirrors the public surface of :class:`TraceSage` but does nothing: no embedded
    server, no DB/worker/queue, a no-op callback handler. Lets you keep tracesage
    wired into your code and switch it off per-environment (e.g. ``TRACESAGE_ENABLED=
    false`` in prod) with near-zero overhead and no integration changes.
    """

    def __init__(self, config: TraceSageConfig) -> None:
        self._config = config
        self._handler: Any = None
        self.bound_port: int | None = None
        self._stopped = True

    @property
    def handler(self) -> Any:
        if self._handler is None:
            self._handler = _make_noop_handler()
        return self._handler

    @property
    def db(self) -> Any:
        raise RuntimeError("tracesage is disabled (set TRACESAGE_ENABLED=true to enable)")

    @property
    def blob_store(self) -> Any:
        raise RuntimeError("tracesage is disabled (set TRACESAGE_ENABLED=true to enable)")

    @property
    def stats(self) -> Stats:
        return Stats()

    # --- no-op surface (matches TraceSage) ---
    def run_url(self, run_id: str) -> str | None:
        return None

    def run_view(self, run_id: str) -> Any:
        from tracesage.render import TraceView

        return TraceView(run_id, None)

    def install(self) -> _DisabledTraceSage:
        return self

    def uninstall(self) -> None:
        return None

    def register_tool_source(self, tool_name: str, server: str) -> None:
        return None

    def register_tool_sources(self, mapping: dict[str, str]) -> None:
        return None

    def tool_source(self, tool_name: str | None) -> str | None:
        return None

    def emit(self, event: Any) -> None:
        return None

    def get_or_set_root(self, run_id: str, parent_run_id: str | None) -> str:
        return run_id

    async def flush(self, timeout: float = 5.0) -> None:  # noqa: ASYNC109 - mirrors TraceSage API
        return None

    async def render_tree(self, run_id: str, *, use_color: bool | None = None) -> str:
        return "(tracesage disabled)"

    async def stop(self) -> None:
        return None


class BackgroundTracer:
    """A :class:`TraceSage` running on its own daemon thread + event loop, usable from
    synchronous code (plain scripts, notebooks).

    The callback handler is thread-safe (``emit`` hops to the background loop), so use
    ``.handler`` from your main thread exactly as you would the async tracer. Prefer
    the :func:`start` / :func:`trace` helpers over constructing this directly.
    """

    def __init__(
        self,
        config: TraceSageConfig | None = None,
        *,
        start_server: bool | None = None,
        install: bool = False,
        ready_timeout: float = 10.0,
    ) -> None:
        self._config = config
        self._start_server = start_server
        self._install = install
        self._ready_timeout = ready_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tl: TraceSage | None = None
        self._ready = threading.Event()
        self._err: BaseException | None = None

    def start(self) -> BackgroundTracer:
        if self._thread is not None or self._tl is not None:
            return self
        cfg = self._config or TraceSageConfig()
        if not cfg.enabled:
            # Kill switch: no thread, no loop, no server — just an inert tracer.
            self._tl = cast("TraceSage", _DisabledTraceSage(cfg))
            return self
        self._thread = threading.Thread(
            target=self._run, name="tracesage.background", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=self._ready_timeout):
            raise TimeoutError("tracesage background tracer failed to start in time")
        if self._err is not None:
            raise self._err
        # Install on the CALLING thread, not the background thread: the global hook
        # reads a ContextVar, which is thread/context-local, so it must be set in the
        # context where the user's LangChain calls actually run.
        if self._install and self._tl is not None:
            self._tl.install()
        return self

    def _run(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._tl = self._loop.run_until_complete(
                TraceSage.create(self._config, start_server=self._start_server)
            )
        except Exception as e:  # surface startup failure to start()
            self._err = e
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    @property
    def tracer(self) -> TraceSage:
        if self._tl is None:  # pragma: no cover - guarded by start()
            raise RuntimeError("BackgroundTracer not started")
        return self._tl

    @property
    def handler(self) -> Any:
        return self.tracer.handler

    def run_url(self, run_id: str) -> str | None:
        return self.tracer.run_url(run_id)

    def run_view(self, run_id: str) -> Any:
        return self.tracer.run_view(run_id)

    def flush(self, timeout: float = 5.0) -> None:
        """Block until queued events are persisted (synchronous wrapper)."""
        if self._loop is None or self._tl is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._tl.flush(timeout), self._loop)
        with contextlib.suppress(Exception):
            fut.result(timeout=timeout + 2.0)

    def install(self) -> BackgroundTracer:
        self.tracer.install()
        return self

    def uninstall(self) -> None:
        self.tracer.uninstall()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the tracer, drain the queue, and join the background thread."""
        if self._loop is None or self._tl is None or self._thread is None:
            return
        with contextlib.suppress(Exception):
            if self._install:
                self._tl.uninstall()
        with contextlib.suppress(Exception):
            fut = asyncio.run_coroutine_threadsafe(self._tl.stop(), self._loop)
            fut.result(timeout=timeout)
        with contextlib.suppress(Exception):
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)

    def __enter__(self) -> BackgroundTracer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def start(
    config: TraceSageConfig | None = None,
    *,
    start_server: bool = True,
    install: bool = False,
) -> BackgroundTracer:
    """Start a tracer on a background thread and return it (already running).

    For synchronous scripts/notebooks::

        tl = tracesage.start(install=True)   # global capture, no callbacks= needed
        agent.invoke(...)
        tl.stop()
    """
    return BackgroundTracer(
        config, start_server=start_server, install=install
    ).start()


@contextlib.contextmanager
def trace(
    config: TraceSageConfig | None = None,
    *,
    start_server: bool = True,
    install: bool = True,
) -> Any:
    """Synchronous context manager that starts a background tracer (installed globally
    by default) and stops it on exit::

        with tracesage.trace() as tl:
            agent.invoke(...)            # captured; print(tl.run_url(...)) for the link
    """
    bg = start(config, start_server=start_server, install=install)
    try:
        yield bg
    finally:
        bg.stop()
