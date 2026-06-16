"""FastAPI server: REST + WebSocket endpoints + auth + lifespan."""
from __future__ import annotations

from tracesage.server.app import create_app
from tracesage.server.ws import WebSocketManager

__all__ = ["WebSocketManager", "create_app"]
