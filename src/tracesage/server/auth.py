"""Bearer-token auth: HTTP middleware + WebSocket pre-accept check.

The HTTP middleware short-circuits a small set of public paths so the UI shell
can be loaded by an unauthenticated user (the user then enters the token in the
settings modal, which becomes the bearer for subsequent /api/* + /ws/* calls).

Public paths (no auth required even when a token is configured):
    /api/health         - liveness probes
    /                   - convenience: redirect-to-/ui handled at app level
    /ui                 - SPA index (served as /ui or /ui/)
    /ui/*               - SPA static assets (HTML, CSS, JS)

Everything else (i.e. /api/* except health, plus WebSocket endpoints) requires
`Authorization: Bearer <token>` when a token is configured. Comparison is
constant-time via `hmac.compare_digest`.

The /ui/* shell is purely a static SPA bundle — it carries no sensitive trace
data. Real data only flows through /api/* and /ws/*, which remain gated.

WebSocket auth is path-separate because middlewares cannot reject WS handshakes
cleanly across all uvicorn versions: the route handler calls `check_ws_auth`
before `WebSocketManager.connect`. Token is supplied via the `?token=` query
param or the `Sec-WebSocket-Protocol` subprotocol header.
"""
from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import WebSocket
    from starlette.responses import Response

    from tracesage.config import TraceSageConfig


_HEALTH_PATH = "/api/health"


def _is_public_path(path: str) -> bool:
    """Paths that bypass auth even when a token is configured.

    Limits public access to:
      - the health endpoint (so liveness probes work without credentials)
      - the static UI shell at /ui or /ui/* (HTML/CSS/JS only — the user
        needs to load the page to enter the token in the settings modal)
      - the root path "/" (typically a redirect to /ui/)
    """
    if path == _HEALTH_PATH:
        return True
    if path == "/":
        return True
    return path == "/ui" or path.startswith("/ui/")


async def auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """HTTP auth gate. Skips `/api/health` and the static `/ui/*` shell.

    No-ops when no token is configured.
    """
    # CORS preflight requests never carry an Authorization header. Let them
    # through so the (outer) CORSMiddleware can answer them; the subsequent
    # actual request is still gated normally. Defensive even though CORS is
    # registered as the outermost layer in app.py.
    if request.method == "OPTIONS":
        return await call_next(request)

    if _is_public_path(request.url.path):
        return await call_next(request)

    config: TraceSageConfig = request.app.state.config
    if config.auth_token is None:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    expected = f"Bearer {config.auth_token}"
    if not hmac.compare_digest(auth_header, expected):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    return await call_next(request)


async def check_ws_auth(websocket: WebSocket, config: TraceSageConfig) -> bool:
    """Validate WebSocket handshake. Closes with 4401 and returns False on failure.

    Token sources, in order:
        1. `?token=<value>` query param
        2. `Sec-WebSocket-Protocol` subprotocol (last value wins if multiple)
    """
    if config.auth_token is None:
        return True

    supplied: str | None = websocket.query_params.get("token")
    if supplied is None:
        # Subprotocol fallback: WebSocket clients in browsers can't add custom
        # headers, so a token-as-subprotocol is a common workaround.
        subprotocols = websocket.headers.get("sec-websocket-protocol", "")
        if subprotocols:
            supplied = subprotocols.split(",")[-1].strip()

    if supplied is not None and hmac.compare_digest(supplied, config.auth_token):
        return True

    await websocket.close(code=4401)
    return False
