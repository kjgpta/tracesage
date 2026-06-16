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

from tracesage.models import WSMessage
from tracesage.server.auth import check_ws_auth

if TYPE_CHECKING:
    from tracesage.config import TraceSageConfig
    from tracesage.storage.backend import StorageBackend


_LOG = logging.getLogger("tracesage.server.ws")

GLOBAL_FEED_KEY = "__all__"


class WebSocketManager:
    """Tracks per-run subscribers and a global feed under a single asyncio lock."""

    def __init__(self, send_timeout: float = 5.0) -> None:
        self._subscribers: defaultdict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._send_timeout = send_timeout
        # Per-socket send lock. A single WebSocket does not support overlapping
        # writes, so every send to a given socket — the per-run catchup snapshot
        # AND concurrent worker broadcasts — is serialized through its own lock.
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}

    async def connect(self, run_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._subscribers[run_id].add(websocket)
            self._send_locks.setdefault(websocket, asyncio.Lock())

    async def disconnect(self, run_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            subs = self._subscribers.get(run_id)
            if subs is not None:
                subs.discard(websocket)
                if not subs:
                    del self._subscribers[run_id]
            # Each connection subscribes under exactly one key, so once removed
            # the socket's send lock can be dropped.
            self._send_locks.pop(websocket, None)

    async def _send_one(self, ws: WebSocket, payload: str) -> WebSocket | None:
        """Send to one socket with a timeout, serialized per-socket. Returns the socket if dead, else None."""
        lock = self._send_locks.get(ws)
        try:
            if lock is not None:
                async with lock:
                    await asyncio.wait_for(ws.send_text(payload), timeout=self._send_timeout)
            else:
                await asyncio.wait_for(ws.send_text(payload), timeout=self._send_timeout)
            return None
        except Exception as e:
            _LOG.debug("ws send failed/timed out: %s", e)
            return ws

    async def send_personal(self, websocket: WebSocket, message: WSMessage) -> bool:
        """Send one message to a single socket, serialized with broadcasts via
        the per-socket lock. Returns True on success, False if the socket is dead."""
        return await self._send_one(websocket, message.model_dump_json()) is None

    async def broadcast(self, run_id: str, message: WSMessage) -> None:
        """Send `message` to every subscriber on `run_id`. Dead sockets are removed."""
        async with self._lock:
            subs = list(self._subscribers.get(run_id, set()))

        if not subs:
            return

        payload = message.model_dump_json()
        results = await asyncio.gather(
            *(self._send_one(ws, payload) for ws in subs), return_exceptions=True
        )
        # _send_one returns the socket itself when it died, or None on success.
        # Filter by identity (not isinstance WebSocket) so it works for any socket
        # object and ignores BaseExceptions gather may surface (e.g. CancelledError).
        dead = {r for r in results if r is not None and not isinstance(r, BaseException)}

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
        results = await asyncio.gather(
            *(self._send_one(ws, payload) for ws in all_subs), return_exceptions=True
        )
        # _send_one returns the socket itself when it died, or None on success.
        # Filter by identity (not isinstance WebSocket) so it works for any socket
        # object and ignores BaseExceptions gather may surface (e.g. CancelledError).
        dead = {r for r in results if r is not None and not isinstance(r, BaseException)}

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
    config: TraceSageConfig = websocket.app.state.config
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
            # Routed through the manager so it shares the per-socket send lock
            # with worker broadcasts — the socket is broadcast-eligible the
            # moment connect() returns, so this serializes the two writers.
            await manager.send_personal(websocket, catchup)
        except Exception as e:
            _LOG.warning("catchup failed for run %s: %s", run_id, e)
            err = WSMessage(
                msg_type="error",
                run_id=run_id,
                payload={"detail": "catchup failed"},
            )
            await manager.send_personal(websocket, err)

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
    config: TraceSageConfig = websocket.app.state.config
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
