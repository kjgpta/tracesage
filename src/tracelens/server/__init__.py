"""FastAPI server: REST + WebSocket endpoints + auth + lifespan."""
from __future__ import annotations

from tracelens.server.app import create_app
from tracelens.server.ws import WebSocketManager

__all__ = ["WebSocketManager", "create_app"]
