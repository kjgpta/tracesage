"""Unit tests for `WebSocketManager` fan-out, using fake sockets (no real server).

These exercise the concurrent broadcast path: healthy delivery, dead-socket
removal on raise, send-timeout treated as dead, and global broadcast_all across
multiple run_ids. The repo runs pytest-asyncio in `asyncio_mode="auto"`, so plain
`async def test_...` functions are collected as coroutine tests.
"""
from __future__ import annotations

import asyncio

from tracelens.models import WSMessage
from tracelens.server.ws import WebSocketManager

# ---------- Fake sockets ----------


class _FakeWS:
    """Minimal stand-in for a Starlette WebSocket that records sent payloads."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class _RaisingWS(_FakeWS):
    """A socket whose send always fails — should be marked dead and removed."""

    async def send_text(self, text: str) -> None:
        raise RuntimeError("boom")


class _SlowWS(_FakeWS):
    """A socket that takes longer than a short send_timeout to deliver."""

    async def send_text(self, text: str) -> None:
        await asyncio.sleep(0.2)
        self.sent.append(text)


def _msg(run_id: str, event_id: str = "e1") -> WSMessage:
    return WSMessage(
        msg_type="event",
        run_id=run_id,
        payload={"event_id": event_id, "summary": "broadcast"},
    )


# ---------- broadcast ----------


async def test_broadcast_delivers_to_all_healthy_sockets():
    manager = WebSocketManager()
    a = _FakeWS()
    b = _FakeWS()
    await manager.connect("run-1", a)
    await manager.connect("run-1", b)

    await manager.broadcast("run-1", _msg("run-1"))

    assert len(a.sent) == 1
    assert len(b.sent) == 1
    received = WSMessage.model_validate_json(a.sent[0])
    assert received.msg_type == "event"
    assert received.run_id == "run-1"
    assert received.payload["event_id"] == "e1"
    # Both sockets remain subscribed.
    assert await manager.subscriber_count("run-1") == 2


async def test_broadcast_removes_socket_that_raises():
    manager = WebSocketManager()
    healthy = _FakeWS()
    raising = _RaisingWS()
    await manager.connect("run-1", healthy)
    await manager.connect("run-1", raising)

    await manager.broadcast("run-1", _msg("run-1"))

    # Healthy socket still got the payload; raising socket is pruned.
    assert len(healthy.sent) == 1
    assert await manager.subscriber_count("run-1") == 1


async def test_broadcast_removes_socket_exceeding_send_timeout():
    manager = WebSocketManager(send_timeout=0.05)
    healthy = _FakeWS()
    slow = _SlowWS()
    await manager.connect("run-1", healthy)
    await manager.connect("run-1", slow)

    await manager.broadcast("run-1", _msg("run-1"))

    # The fast socket received the message; the slow one timed out and is dead.
    assert len(healthy.sent) == 1
    assert await manager.subscriber_count("run-1") == 1


async def test_broadcast_empty_run_is_noop():
    manager = WebSocketManager()
    # No subscribers for this run — must not raise.
    await manager.broadcast("nobody", _msg("nobody"))
    assert await manager.subscriber_count("nobody") == 0


# ---------- broadcast_all ----------


async def test_broadcast_all_delivers_across_run_ids():
    manager = WebSocketManager()
    a = _FakeWS()
    b = _FakeWS()
    await manager.connect("run-1", a)
    await manager.connect("run-2", b)

    await manager.broadcast_all(_msg("run-1", event_id="global"))

    assert len(a.sent) == 1
    assert len(b.sent) == 1
    payload_a = WSMessage.model_validate_json(a.sent[0])
    assert payload_a.payload["event_id"] == "global"
    assert await manager.subscriber_count("run-1") == 1
    assert await manager.subscriber_count("run-2") == 1
