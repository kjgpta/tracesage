"""Import Claude Desktop Cowork and agent-mode sessions from their audit logs.

Claude Desktop writes one ``audit.jsonl`` per Cowork session under
``local-agent-mode-sessions/``. The records are Claude Code SDK messages:
assistant rows carry the model, usage and tool_use blocks of one request, and
rows written by a subagent carry ``parent_tool_use_id`` (the Agent / Task call
that spawned it). Those sessions do not fire Claude Code hooks, so the
collector reads the logs directly.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from tracesage.ingest.anthropic_log import SubStream, build_turns
from tracesage.ingest.claude_code import read_jsonl
from tracesage.ingest.session_follow import SessionRef, files_signature
from tracesage.ingest.traces import Turn

log = logging.getLogger("tracesage.ingest.desktop")

APP = "claude-desktop"
DEFAULT_BACKFILL_BYTES = 64_000_000
_LARGE_FILE = 8_000_000


def audit_dir(home: Path | None = None) -> Path:
    """Directory that holds Claude Desktop Chat and Cowork session logs."""
    root = home or Path.home()
    if sys.platform == "darwin":
        return root / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    if sys.platform == "win32":
        return root / "AppData" / "Roaming" / "Claude" / "local-agent-mode-sessions"
    return root / ".config" / "Claude" / "local-agent-mode-sessions"


def split_streams(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, SubStream]]:
    """Main-agent records, and subagent records keyed by the spawning tool call."""
    main: list[dict[str, Any]] = []
    subs: dict[str, SubStream] = {}
    for record in records:
        if record.get("type") == "system" and record.get("subtype") == "task_started":
            tool_id = record.get("tool_use_id")
            if isinstance(tool_id, str) and tool_id:
                sub = subs.setdefault(tool_id, SubStream())
                kind = record.get("subagent_type")
                sub.name = kind if isinstance(kind, str) and kind else sub.name
                about = record.get("description")
                sub.label = about if isinstance(about, str) and about else sub.label
            continue
        parent = record.get("parent_tool_use_id")
        if isinstance(parent, str) and parent:
            subs.setdefault(parent, SubStream()).records.append(record)
        else:
            main.append(record)
    return main, subs


def load_audit(path: Path, max_bytes: int = DEFAULT_BACKFILL_BYTES) -> list[Turn]:
    """Turns of one Cowork session log. Never raises."""
    try:
        records = read_jsonl(path, limit=max_bytes)
        main, subs = split_streams(records)
        fallback = path.parent.name

        def session(record: dict[str, Any]) -> str:
            value = record.get("session_id")
            return value if isinstance(value, str) and value else fallback

        return build_turns(main, app=APP, session=session, subs=subs)
    except Exception as exc:
        log.warning("desktop audit %s skipped: %s", path, exc)
        return []


class CoworkSource:
    name = APP

    def __init__(self, home: Path | None = None, max_bytes: int = DEFAULT_BACKFILL_BYTES) -> None:
        self.root = audit_dir(home)
        self.max_bytes = max_bytes

    def available(self) -> bool:
        return self.root.is_dir()

    def sessions(self) -> list[SessionRef]:
        if not self.root.is_dir():
            return []
        root = self.root.resolve()
        refs: list[SessionRef] = []
        for path in sorted(self.root.rglob("audit.jsonl")):
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    continue
                size = resolved.stat().st_size
            except OSError:
                continue
            if size > self.max_bytes:
                continue
            refs.append(SessionRef(
                key=f"{APP}:{resolved}",
                signature=files_signature([resolved]),
                load=lambda p=resolved: load_audit(p, self.max_bytes),
                large=size > _LARGE_FILE,
            ))
        return refs
