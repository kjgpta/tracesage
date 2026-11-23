"""Poll local AI-app session stores and import changed turns.

A source lists its sessions with a signature (file sizes and mtimes, or a
last-updated stamp). When a signature changes the session is re-read, and
only events whose content changed since the last import are emitted. Finished
turns are remembered across restarts in ``session-imports.json``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tracesage.ingest.traces import Turn, turn_events, turn_fingerprint

log = logging.getLogger("tracesage.ingest.sessions")

_STATE_VERSION = 1
_OPEN_RECHECK = timedelta(minutes=5)
_LARGE_RECHECK = timedelta(seconds=20)
DEFAULT_IMPORT_DAYS = 30


def import_days() -> int:
    """How far back a first import reaches. ``TRACESAGE_IMPORT_DAYS`` overrides."""
    raw = os.environ.get("TRACESAGE_IMPORT_DAYS", "")
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_IMPORT_DAYS
    return days if days > 0 else DEFAULT_IMPORT_DAYS


def import_cutoff(now: datetime | None = None) -> float:
    """Epoch seconds before which sessions are not imported."""
    current = now or datetime.now(UTC)
    return (current - timedelta(days=import_days())).timestamp()


@dataclass
class SessionRef:
    key: str
    signature: str
    load: Callable[[], list[Turn]]
    large: bool = False


class TraceSource(Protocol):
    name: str

    def sessions(self) -> list[SessionRef]: ...


def files_signature(paths: list[Path]) -> str:
    parts: list[str] = []
    for path in sorted(paths):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


class SessionImporter:
    """Remembers what was imported and emits only what changed."""

    def __init__(self, state_path: Path | None, *, metadata_only: bool = False) -> None:
        self.state_path = state_path
        self.metadata_only = metadata_only
        self.sessions: dict[str, dict[str, Any]] = {}
        self.digests: dict[str, dict[str, str]] = {}
        self.loaded_at: dict[str, datetime] = {}
        self.load_seconds: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if self.state_path is None:
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
            return
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            self.sessions = {
                key: value for key, value in sessions.items()
                if isinstance(key, str) and isinstance(value, dict)
            }

    def save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": _STATE_VERSION, "sessions": self.sessions}) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as exc:
            log.warning("session import state not saved: %s", exc)

    def due(self, ref: SessionRef, now: datetime) -> bool:
        state = self.sessions.get(ref.key)
        last = self.loaded_at.get(ref.key)
        if last is not None:
            wait = timedelta(seconds=10 * self.load_seconds.get(ref.key, 0.0))
            if ref.large:
                wait = max(wait, _LARGE_RECHECK)
            if now - last < wait:
                return False
        if state is None or state.get("sig") != ref.signature:
            return True
        return bool(state.get("open") and (last is None or now - last > _OPEN_RECHECK))

    def apply_turn(self, ref: SessionRef, turn: Turn, emit: Callable[[Any], None]) -> int:
        """Emit the changed events of one turn. Returns the number emitted."""
        state = self.sessions.setdefault(ref.key, {})
        done = state.setdefault("turns", {})
        fingerprint = turn_fingerprint(turn)
        if done.get(turn.turn_id) == fingerprint:
            return 0
        digests = self.digests.setdefault(ref.key, {})
        emitted = 0
        for event in turn_events(turn, metadata_only=self.metadata_only):
            digest = hashlib.sha256(
                event.model_dump_json(exclude={"timestamp"}).encode("utf-8")
            ).hexdigest()[:16]
            if digests.get(event.event_id) == digest:
                continue
            try:
                emit(event)
                emitted += 1
            except Exception as exc:
                log.warning("session event emit failed: %s", exc)
                continue
            digests[event.event_id] = digest
        if turn.complete:
            done[turn.turn_id] = fingerprint
        return emitted

    def finish(self, ref: SessionRef, turns: list[Turn]) -> None:
        state = self.sessions.setdefault(ref.key, {})
        still_open = any(not turn.complete for turn in turns)
        state["sig"] = ref.signature
        state["open"] = still_open
        if not still_open:
            self.digests.pop(ref.key, None)

    def apply(self, ref: SessionRef, turns: list[Turn], emit: Callable[[Any], None]) -> int:
        """Emit changed events for one session. Returns the number emitted."""
        emitted = sum(self.apply_turn(ref, turn, emit) for turn in turns)
        self.finish(ref, turns)
        return emitted


async def follow_sessions(
    sources: list[TraceSource],
    emit: Callable[[Any], None],
    data_dir: Path,
    *,
    metadata_only: bool = False,
    interval: float = 3.0,
    busy: Callable[[], bool] | None = None,
) -> None:
    """Import new and changed turns from every source until cancelled."""
    importer = SessionImporter(data_dir / "session-imports.json", metadata_only=metadata_only)
    loop = asyncio.get_running_loop()
    try:
        while True:
            for source in sources:
                try:
                    refs = await loop.run_in_executor(None, source.sessions)
                except Exception as exc:
                    log.warning("%s sessions not listed: %s", source.name, exc)
                    continue
                for ref in refs:
                    now = datetime.now(UTC)
                    if not importer.due(ref, now):
                        continue
                    began = loop.time()
                    try:
                        turns = await loop.run_in_executor(None, ref.load)
                    except Exception as exc:
                        log.warning("%s session %s skipped: %s", source.name, ref.key, exc)
                        continue
                    importer.load_seconds[ref.key] = loop.time() - began
                    importer.loaded_at[ref.key] = now
                    for turn in turns:
                        while busy is not None and busy():  # noqa: ASYNC110
                            await asyncio.sleep(0.25)
                        importer.apply_turn(ref, turn, emit)
                    importer.finish(ref, turns)
                    del turns
                    importer.save()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        importer.save()
        raise
    except Exception as exc:
        log.warning("session follower stopped: %s", exc)
