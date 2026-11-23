"""Import Claude Code sessions from their transcripts.

Claude Code writes ``~/.claude/projects/<project>/<session>.jsonl``. Every
assistant record carries the model, token usage and tool_use blocks of one
API request. Subagents (the Agent / Task tool) write their own transcript to
``<session>/subagents/agent-<id>.jsonl`` with a ``.meta.json`` that names the
tool call that spawned them.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from tracesage.ingest.anthropic_log import SubStream, build_turns
from tracesage.ingest.session_follow import SessionRef, files_signature
from tracesage.ingest.traces import Turn

log = logging.getLogger("tracesage.ingest.claude_code")

APP = "claude-code"
_MAX_FILE = 64_000_000
_LARGE_FILE = 8_000_000


def projects_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "projects"


def read_jsonl(path: Path, limit: int = _MAX_FILE) -> list[dict[str, Any]]:
    """Parsed records of a JSONL file. Bad lines are skipped."""
    try:
        if path.stat().st_size > limit:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _subagent_files(session_file: Path) -> list[Path]:
    folder = session_file.with_suffix("") / "subagents"
    if not folder.is_dir():
        return []
    return sorted(folder.glob("agent-*.jsonl"))


def _subagents(session_file: Path, records: list[dict[str, Any]]) -> dict[str, SubStream]:
    by_agent: dict[str, str] = {}
    for record in records:
        result = record.get("toolUseResult")
        if not isinstance(result, dict) or not isinstance(result.get("agentId"), str):
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("tool_use_id"), str):
                    by_agent[result["agentId"]] = block["tool_use_id"]
    subs: dict[str, SubStream] = {}
    for path in _subagent_files(session_file):
        agent_id = path.stem.removeprefix("agent-")
        meta: dict[str, Any] = {}
        try:
            loaded = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            meta = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            meta = {}
        tool_id = meta.get("toolUseId") if isinstance(meta.get("toolUseId"), str) else None
        tool_id = tool_id or by_agent.get(agent_id)
        if not tool_id:
            continue
        name = meta.get("agentType") if isinstance(meta.get("agentType"), str) else None
        label = meta.get("description") if isinstance(meta.get("description"), str) else None
        subs[tool_id] = SubStream(records=read_jsonl(path), name=name, label=label)
    return subs


def load_session(session_file: Path) -> list[Turn]:
    """Turns of one Claude Code session, with subagents nested. Never raises."""
    try:
        records = read_jsonl(session_file)
        if not records:
            return []
        session = session_file.stem
        title = None
        for record in records:
            if record.get("type") == "ai-title" and isinstance(record.get("aiTitle"), str):
                title = record["aiTitle"]
        main = [r for r in records if not r.get("isSidechain")]
        turns = build_turns(main, app=APP, session=session, subs=_subagents(session_file, records))
        if turns and title and not turns[0].title:
            turns[0].title = title
        return turns
    except Exception as exc:
        log.warning("claude code session %s skipped: %s", session_file.name, exc)
        return []


class ClaudeCodeSource:
    name = APP

    def __init__(self, home: Path | None = None) -> None:
        self.root = projects_dir(home)

    def available(self) -> bool:
        return self.root.is_dir()

    def sessions(self) -> list[SessionRef]:
        # Claude Code deletes its own old transcripts (cleanupPeriodDays), so
        # everything still on disk is imported.
        if not self.root.is_dir():
            return []
        refs: list[SessionRef] = []
        for path in sorted(self.root.glob("*/*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > _MAX_FILE:
                continue
            files = [path, *_subagent_files(path)]
            refs.append(SessionRef(
                key=f"{APP}:{path}",
                signature=files_signature(files),
                load=lambda p=path: load_session(p),
                large=stat.st_size > _LARGE_FILE,
            ))
        return refs
