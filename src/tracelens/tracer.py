"""TraceLens: top-level orchestrator.

One entry point users call: `tracer = await TraceLens.create(config)`. Holds the
queue, worker task, callback handler, and (optionally) an embedded uvicorn server.
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import random
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from tracelens.config import TraceLensConfig
from tracelens.models import RawEvent, Stats
from tracelens.worker import StorageWorker

if TYPE_CHECKING:
    from tracelens.adapters.langchain import TraceLensCallbackHandler
    from tracelens.storage.backend import StorageBackend

log = logging.getLogger("tracelens.tracer")


class _NullWebSocketManager:
    """Stub used when the server module is unavailable. All methods are no-ops."""

    async def broadcast(self, run_id: str, message: Any) -> None:
        del run_id, message
        return None

    async def broadcast_all(self, message: Any) -> None:
        del message
        return None


class TraceLens:
    """Top-level tracer. Use `await TraceLens.create()` to construct."""

    def __init__(
        self,
        config: TraceLensConfig,
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
        self._db = db
        self._blob_store = blob_store
        self._ws_manager = ws_manager
        self._queue = queue
        self._stats = stats
        self._worker = worker
        self._worker_task = worker_task
        self._loop = loop

        # Run-id bookkeeping for sampling, root resolution, throttling. All bounded
        # at ``_run_state_cap`` with FIFO eviction so long-running processes do not
        # leak memory through these maps. Entries naturally age out as new runs arrive.
        self._run_state_cap = 100_000
        self._sampled_in_runs: OrderedDict[str, None] = OrderedDict()
        self._sampled_out_runs: OrderedDict[str, None] = OrderedDict()
        self._run_event_counts: OrderedDict[str, int] = OrderedDict()
        self._throttled_runs: OrderedDict[str, None] = OrderedDict()
        self._root_map: OrderedDict[str, str] = OrderedDict()
        self._dropped_warning_threshold = 100

        self._handler: TraceLensCallbackHandler | None = None
        self._server: Any = None
        self._server_task: asyncio.Task | None = None
        self._stopped = False
        self.bound_port: int | None = None

        atexit.register(self._stop_sync)

    # ----------------------------------------------------------------- create

    @classmethod
    async def create(
        cls,
        config: TraceLensConfig | None = None,
        *,
        start_server: bool = True,
    ) -> TraceLens:
        cfg = config or TraceLensConfig()
        cfg.ensure_data_dirs()

        # Storage. Imports inside the function so import failures don't poison module load.
        from tracelens.storage.blob_store import BlobStore
        from tracelens.storage.sqlite_backend import SQLiteBackend

        db: StorageBackend = SQLiteBackend(cfg.db_path, cfg.db_pool_size)
        await db.init()
        blob_store = BlobStore(cfg.blob_dir)

        queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.queue_maxsize)
        stats = Stats(queue_max=cfg.queue_maxsize)

        ws_manager: Any
        try:
            from tracelens.server.ws import WebSocketManager  # type: ignore[import-not-found]

            ws_manager = WebSocketManager()
        except Exception:
            ws_manager = _NullWebSocketManager()

        worker = StorageWorker(queue, db, blob_store, ws_manager, cfg, stats)
        loop = asyncio.get_running_loop()
        worker_task = asyncio.create_task(worker.run(), name="tracelens.worker")

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

        # Lazy import of handler so importing tracelens doesn't require langchain-core.
        from tracelens.adapters.langchain import TraceLensCallbackHandler

        instance._handler = TraceLensCallbackHandler(instance)

        if start_server:
            await instance._start_server()

        return instance

    # -------------------------------------------------------- public properties

    @property
    def handler(self) -> TraceLensCallbackHandler:
        if self._handler is None:  # pragma: no cover - guarded by create()
            raise RuntimeError("Handler not initialized; use TraceLens.create()")
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

    # --------------------------------------------------------------- emit path

    def emit(self, event: RawEvent) -> None:
        """Thread-safe enqueue with sampling + per-run cap.

        Sampling decision is taken at the root run level: once a root passes, all of
        its descendant events are captured. If it fails, all are dropped.
        """
        try:
            root = event.root_run_id

            # Per-root sampling: decide once, remember.
            if root in self._sampled_out_runs:
                self._stats.events_sampled_out += 1
                return
            if root not in self._sampled_in_runs:
                # Sampling, not crypto.
                if self._config.sample_rate < 1.0 and random.random() > self._config.sample_rate:  # noqa: S311
                    self._lru_set(self._sampled_out_runs, root)
                    self._stats.events_sampled_out += 1
                    return
                self._lru_set(self._sampled_in_runs, root)

            # Per-run cap (circuit breaker).
            count = self._run_event_counts.get(root, 0)
            if count >= self._config.per_run_event_cap:
                if root not in self._throttled_runs:
                    self._lru_set(self._throttled_runs, root)
                    self._stats.runs_throttled += 1
                return
            self._lru_set(self._run_event_counts, root, count + 1)

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
                    "tracelens: %d events dropped due to queue backpressure",
                    self._stats.events_dropped,
                )

    def get_or_set_root(self, run_id: str, parent_run_id: str | None) -> str:
        """Resolve root_run_id for this run. Caps the LRU map at 10,000 entries."""
        if parent_run_id is None:
            existing = self._root_map.get(run_id)
            if existing is None:
                self._root_map[run_id] = run_id
                self._evict_root_map_if_needed()
                return run_id
            self._root_map.move_to_end(run_id)
            return existing

        # Child run: inherit parent's root.
        parent_root = self._root_map.get(parent_run_id, parent_run_id)
        self._root_map[run_id] = parent_root
        if parent_run_id in self._root_map:
            self._root_map.move_to_end(parent_run_id)
        self._root_map.move_to_end(run_id)
        self._evict_root_map_if_needed()
        return parent_root

    def _evict_root_map_if_needed(self) -> None:
        while len(self._root_map) > 10_000:
            self._root_map.popitem(last=False)

    # ------------------------------------------------------------------ server

    async def _start_server(self) -> None:
        try:
            from tracelens.server.app import create_app  # type: ignore[import-not-found]
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
            uv_config = uvicorn.Config(
                app,
                host=self._config.host,
                port=self._config.port,
                log_level="warning",
                lifespan="on",
            )
            self._server = uvicorn.Server(uv_config)
            self._server_task = asyncio.create_task(
                self._server.serve(), name="tracelens.server"
            )

            # Poll for startup completion within budget.
            deadline = self._loop.time() + self._config.startup_health_timeout_s
            while self._loop.time() < deadline:
                if getattr(self._server, "started", False):
                    break
                await asyncio.sleep(0.05)

            # Capture ephemeral port if requested.
            if self._config.port == 0:
                with contextlib.suppress(Exception):  # pragma: no cover
                    servers = getattr(self._server, "servers", None) or []
                    if servers:
                        socks = getattr(servers[0], "sockets", None) or []
                        if socks:
                            self.bound_port = socks[0].getsockname()[1]
            else:
                self.bound_port = self._config.port
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
