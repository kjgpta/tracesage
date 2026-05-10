"""WebSocket fan-out manager + per-run and global feed handlers.

`WebSocketManager` keeps a `run_id -> set[WebSocket]` map under an asyncio.Lock,
and the special key `__all__` for the global feed. Broadcasts snapshot subscribers
under lock, then send outside the lock so a slow client cannot block the worker.
Send failures mark the socket dead and remove it lazily.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

from tracelens.models import WSMessage
from tracelens.server.auth import check_ws_auth

if TYPE_CHECKING:
    from tracelens.config import TraceLensConfig
    from tracelens.storage.backend import StorageBackend


_LOG = logging.getLogger("tracelens.server.ws")

GLOBAL_FEED_KEY = "__all__"


class WebSocketManager:
    """Tracks per-run subscribers and a global feed under a single asyncio lock."""

    def __init__(self) -> None:
        self._subscribers: defaultdict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, run_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._subscribers[run_id].add(websocket)

    async def disconnect(self, run_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            subs = self._subscribers.get(run_id)
            if subs is None:
                return
            subs.discard(websocket)
            if not subs:
                del self._subscribers[run_id]

    async def broadcast(self, run_id: str, message: WSMessage) -> None:
        """Send `message` to every subscriber on `run_id`. Dead sockets are removed."""
        async with self._lock:
            subs = set(self._subscribers.get(run_id, set()))

        if not subs:
            return

        payload = message.model_dump_json()
        dead: set[WebSocket] = set()
        for ws in subs:
            try:
                await ws.send_text(payload)
            except Exception as e:
                _LOG.debug("ws send failed for run %s: %s", run_id, e)
                dead.add(ws)

        if dead:
            async with self._lock:
                live = self._subscribers.get(run_id)
                if live is not None:
                    live -= dead
                    if not live:
                        self._subscribers.pop(run_id, None)

    async def broadcast_all(self, message: WSMessage) -> None:
        """Fan out to every subscriber across every run plus the global feed."""
        async with self._lock:
            all_subs: set[WebSocket] = set()
            for subs in self._subscribers.values():
                all_subs |= subs

        if not all_subs:
            return

        payload = message.model_dump_json()
        dead: set[WebSocket] = set()
        for ws in all_subs:
            try:
                await ws.send_text(payload)
            except Exception as e:
                _LOG.debug("ws broadcast_all send failed: %s", e)
                dead.add(ws)

        if dead:
            async with self._lock:
                for run_id, subs in list(self._subscribers.items()):
                    subs -= dead
                    if not subs:
                        self._subscribers.pop(run_id, None)

    async def subscriber_count(self, run_id: str) -> int:
        async with self._lock:
            return len(self._subscribers.get(run_id, set()))


async def ws_trace(websocket: WebSocket, run_id: str) -> None:
    """Per-run WS endpoint: catchup snapshot then live tail."""
    config: TraceLensConfig = websocket.app.state.config
    if not await check_ws_auth(websocket, config):
        return

    db: StorageBackend = websocket.app.state.db
    manager: WebSocketManager = websocket.app.state.ws_manager

    await manager.connect(run_id, websocket)
    try:
        try:
            steps = await db.get_journey(run_id)
            catchup = WSMessage(
                msg_type="catchup",
                run_id=run_id,
                payload={"steps": [s.model_dump(mode="json") for s in steps]},
            )
            await websocket.send_text(catchup.model_dump_json())
        except Exception as e:
            _LOG.warning("catchup failed for run %s: %s", run_id, e)
            err = WSMessage(
                msg_type="error",
                run_id=run_id,
                payload={"detail": "catchup failed"},
            )
            try:
                await websocket.send_text(err.model_dump_json())
            except Exception:
                return

        # Keep connection open until the client disconnects. Reading drains any
        # ping/keepalive frames the client sends.
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                return
    finally:
        await manager.disconnect(run_id, websocket)


async def ws_runs(websocket: WebSocket) -> None:
    """Global feed WS endpoint."""
    config: TraceLensConfig = websocket.app.state.config
    if not await check_ws_auth(websocket, config):
        return

    manager: WebSocketManager = websocket.app.state.ws_manager
    await manager.connect(GLOBAL_FEED_KEY, websocket)
    try:
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                return
    finally:
        await manager.disconnect(GLOBAL_FEED_KEY, websocket)
