"""Bearer-token auth: HTTP middleware + WebSocket pre-accept check.

The HTTP middleware short-circuits a small set of public paths so the UI shell
can be loaded by an unauthenticated user (the user then enters the token in the
settings modal, which becomes the bearer for subsequent /api/* + /ws/* calls).

Public paths (no auth required even when a token is configured):
    /api/health         - liveness probes
    /                   - convenience: redirect-to-/ui handled at app level
    /ui                 - SPA index (served as /ui or /ui/)
    /ui/*               - SPA static assets (HTML, CSS, JS)

Everything else (i.e. /api/* except health, the OTLP ingest endpoint /v1/traces,
plus WebSocket endpoints) requires `Authorization: Bearer <token>` when a token is
configured. Comparison is constant-time via `hmac.compare_digest`. OTel exporters
send the header via `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<token>`.

The /ui/* shell is purely a static SPA bundle — it carries no sensitive trace
data. Real data only flows through /api/* and /ws/*, which remain gated.

WebSocket auth is path-separate because middlewares cannot reject WS handshakes
cleanly across all uvicorn versions: the route handler calls `check_ws_auth`
before `WebSocketManager.connect`. Token sources, preferred first:
    Sec-WebSocket-Protocol: tracesage.v1, tracesage.bearer.<base64url(token)>
    Sec-WebSocket-Protocol: <raw token>   (legacy; last value wins)
    ?token=<value>                        (legacy; leaks into URLs/logs)

Browsers do not apply CORS to WebSocket handshakes, so `check_ws_auth` also
rejects handshakes whose `Origin` is neither same-host nor in `cors_origins`.
"""
from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import WebSocket
    from starlette.responses import Response

    from tracesage.config import TraceSageConfig


_HEALTH_PATH = "/api/health"
WS_SUBPROTOCOL = "tracesage.v1"
_WS_BEARER_PREFIX = "tracesage.bearer."


def _tokens_equal(supplied: str, expected: str) -> bool:
    """Constant-time string equality that never raises.

    `hmac.compare_digest` raises `TypeError` when given `str` operands containing
    non-ASCII characters, so a client sending a non-ASCII token would crash the
    auth path (HTTP 500 / unhandled WS handshake error) instead of failing closed.
    Comparing the UTF-8 byte encodings sidesteps that restriction while remaining
    timing-safe.
    """
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _is_public_path(path: str) -> bool:
    """Paths that bypass auth even when a token is configured.

    Limits public access to:
      - the health endpoint (so liveness probes work without credentials)
      - the static UI shell at /ui or /ui/* (HTML/CSS/JS only — the user
        needs to load the page to enter the token in the settings modal)
      - the root path "/" (redirects to /ui/)
    """
    if path == _HEALTH_PATH:
        return True
    if path == "/":
        return True
    if path == "/ui":
        return True
    if not path.startswith("/ui/") or "\\" in path or "%5c" in path.lower():
        return False
    # Fail closed on dot segments: `/ui/../api/runs` must not ride the shell exemption
    # and rely on the router to reject it.
    return not any(seg in (".", "..") for seg in path.split("/"))


async def auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """HTTP auth gate. Skips `/api/health` and the static `/ui/*` shell; gates
    everything else, including `/v1/traces` (OTLP ingest).

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
    if not _tokens_equal(auth_header, expected):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    return await call_next(request)


def _offered_subprotocols(websocket: WebSocket) -> list[str]:
    raw = websocket.headers.get("sec-websocket-protocol", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def select_ws_subprotocol(websocket: WebSocket) -> str | None:
    """Subprotocol to echo in the handshake response. Browsers fail the connection
    unless the server selects one of the offered values, so a client using the
    bearer-subprotocol scheme gets `tracesage.v1` back (never the token itself)."""
    return WS_SUBPROTOCOL if WS_SUBPROTOCOL in _offered_subprotocols(websocket) else None


def _decode_bearer_subprotocol(value: str) -> str | None:
    encoded = value[len(_WS_BEARER_PREFIX):]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        return None


def _origin_allowed(websocket: WebSocket, config: TraceSageConfig) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # non-browser client; browsers always send Origin on WS
    if "*" in config.cors_origins or origin in config.cors_origins:
        return True
    host = websocket.headers.get("host", "")
    return bool(host) and urlsplit(origin).netloc.lower() == host.lower()


async def check_ws_auth(websocket: WebSocket, config: TraceSageConfig) -> bool:
    """Validate WebSocket handshake. Closes and returns False on failure
    (4403 for a disallowed Origin, 4401 for a missing/wrong token)."""
    if not _origin_allowed(websocket, config):
        await websocket.close(code=4403)
        return False

    if config.auth_token is None:
        return True

    offered = _offered_subprotocols(websocket)
    supplied: str | None = None
    for proto in offered:
        if proto.startswith(_WS_BEARER_PREFIX):
            supplied = _decode_bearer_subprotocol(proto)
            break
    if supplied is None:
        supplied = websocket.query_params.get("token")
    if supplied is None and offered:
        supplied = offered[-1]

    if supplied is not None and _tokens_equal(supplied, config.auth_token):
        return True

    await websocket.close(code=4401)
    return False
