"""Import Cursor agent chats from Cursor's local state database.

Cursor keeps every composer (chat) in ``User/globalStorage/state.vscdb``:

* ``composerHeaders`` has one row per chat with ``lastUpdatedAt`` and
  ``isSubagent``, which is enough to notice changes cheaply.
* ``cursorDiskKV`` holds ``composerData:<id>`` (name, model, subagent ids)
  and ``bubbleId:<id>:<bubble>`` rows. A bubble is a user message, a thinking
  or text segment, or one tool call with its params, result and timings.

Cursor does not store one row per model request, so requests are inferred:
a thinking or text segment, or a gap between sequential tool calls, is one
request. Subagents (``task_v2``) have their own composer, linked through the
tool call.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tracesage.ingest.session_follow import SessionRef, import_cutoff
from tracesage.ingest.traces import (
    Agent,
    ModelCall,
    ToolCall,
    Turn,
    as_int,
    bounded,
    clip,
    parse_ts,
)

log = logging.getLogger("tracesage.ingest.cursor")

APP = "cursor"
_IDLE_COMPLETE = timedelta(minutes=30)
_SUBAGENT_TOOLS = frozenset({"task", "task_v2", "run_subagent"})
_DONE = frozenset({"completed", "error", "cancelled", "aborted", "rejected"})
_INPUT_LIMIT = 2000


def state_db(home: Path | None = None) -> Path:
    root = home or Path.home()
    if sys.platform == "darwin":
        base = root / "Library" / "Application Support" / "Cursor"
    elif sys.platform == "win32":
        base = root / "AppData" / "Roaming" / "Cursor"
    else:
        base = root / ".config" / "Cursor"
    return base / "User" / "globalStorage" / "state.vscdb"


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA query_only = 1")
    return con


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value


class _Store:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    def composer(self, composer_id: str) -> dict[str, Any]:
        row = self.con.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{composer_id}",)
        ).fetchone()
        data = _json(row[0]) if row else None
        return data if isinstance(data, dict) else {}

    def bubbles(self, composer_id: str, data: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Bubbles in conversation order, read one at a time to bound memory."""
        low = f"bubbleId:{composer_id}:"
        rows = self.con.execute(
            "SELECT key, json_extract(CAST(value AS TEXT), '$.createdAt') "
            "FROM cursorDiskKV WHERE key >= ? AND key < ?",
            (low, low + "\uffff"),
        ).fetchall()
        order: dict[str, int] = {}
        headers = data.get("fullConversationHeadersOnly")
        if isinstance(headers, list):
            for position, header in enumerate(headers):
                if isinstance(header, dict) and isinstance(header.get("bubbleId"), str):
                    order[header["bubbleId"]] = position
        if order:
            rows.sort(key=lambda row: order.get(row[0][len(low):], len(order)))
        else:
            rows.sort(key=lambda row: str(row[1] or ""))
        for key, _ in rows:
            found = self.con.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)
            ).fetchone()
            bubble = _json(found[0]) if found else None
            if isinstance(bubble, dict):
                yield bubble

    def header(self, composer_id: str) -> tuple[str | None, str | None]:
        row = self.con.execute(
            "SELECT subagentTypeName, value FROM composerHeaders WHERE composerId = ?",
            (composer_id,),
        ).fetchone()
        if not row:
            return None, None
        kind = row[0] if isinstance(row[0], str) and row[0] else None
        value = _json(row[1])
        name = value.get("name") if isinstance(value, dict) else None
        return kind, name if isinstance(name, str) else None


def _bubble_time(bubble: dict[str, Any]) -> datetime | None:
    return parse_ts(bubble.get("startedAtMs")) or parse_ts(bubble.get("createdAt"))


def _thinking(bubble: dict[str, Any]) -> str:
    value = bubble.get("thinking")
    if isinstance(value, dict):
        value = value.get("text")
    return value if isinstance(value, str) else ""


def _tool_from(bubble: dict[str, Any]) -> ToolCall:
    data = bubble.get("toolFormerData") if isinstance(bubble.get("toolFormerData"), dict) else {}
    name = data.get("name") if isinstance(data.get("name"), str) and data.get("name") else "tool"
    params = _json(data.get("params") or data.get("rawArgs"))
    server = None
    if name.startswith("mcp-") and isinstance(params, dict):
        tools = params.get("tools")
        if isinstance(tools, list) and tools and isinstance(tools[0], dict):
            first = tools[0]
            server = first.get("serverName") if isinstance(first.get("serverName"), str) else None
            inner = first.get("name")
            if isinstance(inner, str) and inner:
                name = inner
            args = _json(first.get("parameters"))
            params = args if args is not None else params
    extra = data.get("additionalData") if isinstance(data.get("additionalData"), dict) else {}
    status = str(extra.get("status") or data.get("status") or "")
    tool_id = data.get("toolCallId") if isinstance(data.get("toolCallId"), str) else None
    started = parse_ts(extra.get("startedAtMs")) or _bubble_time(bubble)
    ended = parse_ts(bubble.get("completedAtMs"))
    result = _json(data.get("result"))
    if isinstance(result, dict) and isinstance(result.get("agentId"), str):
        result = {"agentId": result["agentId"]}
    tool = ToolCall(
        id=tool_id or str(bubble.get("bubbleId")),
        name=name[:120],
        server=server,
        input=bounded(params),
        output=bounded(result),
        started=started,
        ended=ended,
        done=status in _DONE or str(data.get("status")) in _DONE or ended is not None,
    )
    if status in {"error", "cancelled", "aborted", "rejected"}:
        tool.error = status
    return tool


class _Builder:
    """Fold one composer's assistant bubbles into model and tool steps."""

    def __init__(self, store: _Store, model: str | None, depth: int) -> None:
        self.store = store
        self.model = model
        self.depth = depth
        self.steps: list[ModelCall | ToolCall] = []
        self.call: ModelCall | None = None
        self.last_tool: ToolCall | None = None
        self.pending = ""
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.final = ""

    def start(self, prompt: str, when: datetime | None) -> None:
        self.pending = prompt[:_INPUT_LIMIT]
        self.first = self.last = when

    def notice(self, text: str) -> None:
        """A message Cursor posts into the chat itself (a background task or
        subagent finished): input to the next model call, not a new user turn."""
        self.pending = text[:_INPUT_LIMIT]
        self.last_tool = None
        self.call = None

    def feed(self, bubble: dict[str, Any]) -> None:
        when = _bubble_time(bubble)
        if isinstance(bubble.get("toolFormerData"), dict):
            self._tool(bubble)
        else:
            self._segment(bubble, when)
        for stamp in (when, parse_ts(bubble.get("completedAtMs"))):
            if stamp is not None:
                self.first = stamp if self.first is None else min(self.first, stamp)
                self.last = stamp if self.last is None else max(self.last, stamp)

    def _new_call(self, bubble_id: str, started: datetime | None) -> ModelCall:
        call = ModelCall(id=f"cursor:{bubble_id}", model=self.model, started=started,
                         input_text=self.pending)
        self.pending = ""
        self.steps.append(call)
        self.call = call
        self.last_tool = None
        return call

    def _segment(self, bubble: dict[str, Any], when: datetime | None) -> None:
        text = bubble.get("text") if isinstance(bubble.get("text"), str) else ""
        thought = _thinking(bubble)
        if not text.strip() and not thought.strip() and not bubble.get("thinkingDurationMs"):
            return
        call = self.call
        if call is None or self.last_tool is not None:
            started = self.last_tool.ended if self.last_tool and self.last_tool.ended else when
            call = self._new_call(str(bubble.get("bubbleId")), started)
        if thought.strip():
            call.thinking = (call.thinking + "\n" + thought).strip()
        if text.strip():
            call.output_text = (call.output_text + "\n" + text).strip()
            self.final = text
        end = parse_ts(bubble.get("completedAtMs"))
        duration = as_int(bubble.get("thinkingDurationMs"))
        if end is None and when is not None and duration:
            end = when + timedelta(milliseconds=duration)
        end = end or when
        if end is not None:
            call.ended = end if call.ended is None else max(call.ended, end)
        info = bubble.get("modelInfo")
        if isinstance(info, dict) and isinstance(info.get("modelName"), str):
            call.model = info["modelName"]
        tokens = bubble.get("tokenCount")
        if isinstance(tokens, dict):
            used_in = as_int(tokens.get("inputTokens")) or 0
            used_out = as_int(tokens.get("outputTokens")) or 0
            if used_in or used_out:
                call.token_input = (call.token_input or 0) + used_in
                call.token_output = (call.token_output or 0) + used_out

    def _tool(self, bubble: dict[str, Any]) -> None:
        tool = _tool_from(bubble)
        previous = self.last_tool
        overlaps = (
            previous is not None
            and tool.started is not None
            and (previous.ended is None or tool.started < previous.ended)
        )
        if self.call is None or (previous is not None and not overlaps):
            started = previous.ended if previous is not None and previous.ended else tool.started
            call = self._new_call(str(bubble.get("bubbleId")), started)
            call.ended = tool.started
        call = self.call
        if call is not None:
            call.tool_names.append(tool.name)
            if call.ended is None or (tool.started and tool.started < call.ended):
                call.ended = tool.started or call.ended
        self.steps.append(tool)
        self.last_tool = tool
        if tool.output is not None:
            self.pending = clip(tool.output, _INPUT_LIMIT)
        data = bubble.get("toolFormerData") or {}
        if str(data.get("name")) in _SUBAGENT_TOOLS and self.depth < 4:
            tool.agent = self._subagent(data, tool)

    def _subagent(self, data: dict[str, Any], tool: ToolCall) -> Agent | None:
        extra = _json(data.get("additionalData"))
        result = tool.output
        child = None
        if isinstance(extra, dict) and isinstance(extra.get("subagentComposerId"), str):
            child = extra["subagentComposerId"]
        elif isinstance(result, dict) and isinstance(result.get("agentId"), str):
            child = result["agentId"]
        if not child:
            return None
        params = tool.input if isinstance(tool.input, dict) else {}
        kind, title = self.store.header(child)
        name = params.get("subagent_type") or kind or "subagent"
        agent = Agent(
            id=child,
            name=str(name)[:60],
            label=str(params.get("description") or title or "")[:200],
            prompt=clip(params.get("prompt")),
        )
        data_child = self.store.composer(child)
        builder = _Builder(self.store, _composer_model(data_child) or self.model, self.depth + 1)
        for bubble in self.store.bubbles(child, data_child):
            if bubble.get("type") == 1:
                text = bubble.get("text") if isinstance(bubble.get("text"), str) else ""
                if bubble.get("isSimulatedMsg"):
                    builder.notice(text)
                    continue
                agent.started = agent.started or _bubble_time(bubble)
                builder.start(text or agent.prompt, _bubble_time(bubble))
                continue
            agent.started = agent.started or _bubble_time(bubble)
            builder.feed(bubble)
        agent.steps = builder.steps
        agent.output = builder.final
        agent.model = builder.model
        agent.ended = builder.last
        return agent


def _composer_model(data: dict[str, Any]) -> str | None:
    config = data.get("modelConfig")
    if isinstance(config, dict) and isinstance(config.get("modelName"), str):
        return config["modelName"]
    return None


def load_composer(
    db: Path, composer_id: str, *, cwd: str | None = None, now: datetime | None = None,
) -> list[Turn]:
    """Turns of one Cursor chat, with subagents nested. Never raises."""
    try:
        with closing(_connect(db)) as con:
            return _load(_Store(con), composer_id, cwd, now or datetime.now(UTC))
    except Exception as exc:
        log.warning("cursor chat %s skipped: %s", composer_id, exc)
        return []


def _load(store: _Store, composer_id: str, cwd: str | None, now: datetime) -> list[Turn]:
    data = store.composer(composer_id)
    if not data:
        return []
    name = data.get("name") if isinstance(data.get("name"), str) else None
    default_model = _composer_model(data)
    generating = data.get("status") == "generating"
    turns: list[Turn] = []
    turn: Turn | None = None
    builder: _Builder | None = None
    # Older Cursor bubbles carry no timestamps; such a turn is placed right
    # after the previous one (or at the chat's creation) instead of at import time.
    anchor = parse_ts(data.get("createdAt"))

    def close(complete: bool) -> None:
        nonlocal anchor
        if turn is None or builder is None:
            return
        turn.steps = builder.steps
        turn.final_text = builder.final
        turn.started = turn.started or builder.first or anchor
        turn.ended = builder.last or turn.started
        anchor = turn.ended or anchor
        turn.model = turn.model or builder.model
        turn.complete = complete
        if not complete and turn.ended is not None and now - turn.ended > _IDLE_COMPLETE:
            turn.complete = True
        turns.append(turn)

    for bubble in store.bubbles(composer_id, data):
        if bubble.get("type") == 1:
            text = bubble.get("text") if isinstance(bubble.get("text"), str) else ""
            if not text.strip():
                continue
            if bubble.get("isSimulatedMsg"):
                if builder is not None:
                    builder.notice(text)
                continue
            close(True)
            info = bubble.get("modelInfo")
            model = info.get("modelName") if isinstance(info, dict) else None
            model = model if isinstance(model, str) and model else default_model
            when = _bubble_time(bubble)
            turn = Turn(
                app=APP,
                session=composer_id,
                turn_id=str(bubble.get("bubbleId") or len(turns)),
                prompt=clip(text),
                title=name if not turns else None,
                started=when,
                cwd=cwd,
                model=model,
            )
            builder = _Builder(store, model, 0)
            builder.start(text, when)
            continue
        if builder is not None:
            builder.feed(bubble)
    close(not generating)
    return turns


def _workspace(value: Any) -> str | None:
    data = _json(value)
    if not isinstance(data, dict):
        return None
    ident = data.get("workspaceIdentifier")
    if isinstance(ident, dict):
        uri = ident.get("uri")
        if isinstance(uri, dict) and isinstance(uri.get("fsPath"), str):
            return uri["fsPath"]
    return None


class CursorSource:
    name = APP

    def __init__(self, home: Path | None = None) -> None:
        self.db = state_db(home)

    def available(self) -> bool:
        return self.db.is_file()

    def sessions(self) -> list[SessionRef]:
        if not self.db.is_file():
            return []
        cutoff_ms = int(import_cutoff() * 1000)
        try:
            with closing(_connect(self.db)) as con:
                rows = con.execute(
                    "SELECT composerId, lastUpdatedAt, value FROM composerHeaders "
                    "WHERE COALESCE(isSubagent, 0) = 0 AND lastUpdatedAt >= ?",
                    (cutoff_ms,),
                ).fetchall()
        except sqlite3.Error as exc:
            log.warning("cursor chats not listed: %s", exc)
            return []
        refs: list[SessionRef] = []
        for composer_id, updated, value in rows:
            header = _json(value)
            if not isinstance(composer_id, str) or (isinstance(header, dict) and header.get("isDraft")):
                continue
            cwd = _workspace(value)
            refs.append(SessionRef(
                key=f"{APP}:{composer_id}",
                signature=str(updated),
                load=lambda c=composer_id, w=cwd: load_composer(self.db, c, cwd=w),
                large=True,
            ))
        return refs
