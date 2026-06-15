"""FastAPI app factory.

`create_app` accepts already-initialized dependencies (db, blob_store, ws_manager,
config, stats). Lifecycle is owned by `TraceLens`, not the app — `lifespan` exists
only to satisfy the `@asynccontextmanager` contract required by CLAUDE.md.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from tracelens.models import Stats
from tracelens.server.auth import auth_middleware
from tracelens.server.rest import router as rest_router
from tracelens.server.ws import WebSocketManager, ws_runs, ws_trace

if TYPE_CHECKING:
    from tracelens.config import TraceLensConfig
    from tracelens.storage.backend import StorageBackend
    from tracelens.storage.blob_store import BlobStore


_LOG = logging.getLogger("tracelens.server")


def create_app(
    db: StorageBackend,
    blob_store: BlobStore,
    ws_manager: WebSocketManager,
    config: TraceLensConfig,
    stats: Stats | None = None,
) -> FastAPI:
    """Build the FastAPI app. Caller owns the lifecycle of injected dependencies."""
    from tracelens import __version__

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Startup/shutdown of injected deps is owned by TraceLens, not the app.
        # This block exists so the app is built with the modern lifespan API
        # (see CLAUDE.md: never use deprecated @app.on_event).
        yield

    app = FastAPI(title="tracelens", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.state.blob_store = blob_store
    app.state.ws_manager = ws_manager
    app.state.config = config
    app.state.stats = stats if stats is not None else Stats()

    @app.middleware("http")
    async def _auth(request, call_next):
        return await auth_middleware(request, call_next)

    # CORS is registered AFTER the auth middleware so it becomes the OUTERMOST
    # layer. A browser CORS preflight (OPTIONS, no Authorization header) is then
    # answered by CORSMiddleware before the auth gate ever sees it; and auth's
    # own 401 responses still pass back out through CORS so they carry the
    # Access-Control-Allow-Origin header the browser requires.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(rest_router)

    @app.websocket("/ws/trace/{run_id}")
    async def _ws_trace(websocket: WebSocket, run_id: str) -> None:
        await ws_trace(websocket, run_id)

    @app.websocket("/ws/runs")
    async def _ws_runs(websocket: WebSocket) -> None:
        await ws_runs(websocket)

    ui_dir = Path(__file__).resolve().parent.parent / "ui"
    if ui_dir.exists() and any(ui_dir.iterdir()):
        try:
            app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")
        except Exception as e:
            _LOG.warning("Failed to mount UI from %s: %s", ui_dir, e)
    else:
        _LOG.warning(
            "UI directory not found or empty at %s — UI will be unavailable", ui_dir
        )

    return app
