"""Normalize Claude Code, Codex, Cursor and Claude Desktop hook payloads into RawEvents.

Ids are hashes of the session and the prompt or tool id, so a collector
restart and a duplicate delivery produce the same rows. Unknown fields are
dropped. A bad payload yields no events and never raises.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracesage.models import EventType, RawEvent

log = logging.getLogger("tracesage.ingest.hooks")

_APPS = frozenset({"claude-code", "codex", "cursor", "claude-desktop"})
# Apps whose local session store the collector imports. Their hooks would
# record the same turns again with fewer details, so they are skipped.
_FOLLOWED: set[str] = set()


def follow_locally(apps: set[str] | frozenset[str]) -> None:
    """Skip hook payloads from ``apps``. An empty set turns hooks back on."""
    _FOLLOWED.clear()
    _FOLLOWED.update(app for app in apps if app in _APPS)
_CURSOR_EVENTS = {
    "sessionstart": "SessionStart",
    "sessionend": "SessionEnd",
    "beforesubmitprompt": "UserPromptSubmit",
    "pretooluse": "PreToolUse",
    "posttooluse": "PostToolUse",
    "posttoolusefailure": "PostToolUseFailure",
    "subagentstart": "SubagentStart",
    "subagentstop": "SubagentStop",
    "afteragentresponse": "Stop",
    "afteragentthought": "LLMEnd",
    "stop": "Stop",
    "aftershellexecution": "PostToolUse",
    "aftermcpexecution": "PostToolUse",
    "afterfileedit": "PostToolUse",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _eid(*parts: str) -> str:
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _text(value: Any, limit: int = 8000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str)
        except Exception:
            text = str(value)
    return text[:limit]


def _event_name(payload: dict[str, Any]) -> str:
    raw = (
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event")
        or ""
    )
    name = str(raw).strip()
    mapped = _CURSOR_EVENTS.get(name.lower().replace("_", ""))
    return mapped or name


def _session(payload: dict[str, Any]) -> str:
    return str(
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("conversation_id")
        or "session"
    )


def _prompt(payload: dict[str, Any]) -> str:
    return str(
        payload.get("prompt_id")
        or payload.get("promptId")
        or payload.get("generation_id")
        or payload.get("generationId")
        or "turn"
    )


def _project_tag(payload: dict[str, Any]) -> str | None:
    cwd = payload.get("cwd") or payload.get("workspace_roots")
    if isinstance(cwd, list) and cwd:
        cwd = cwd[0]
    if not cwd:
        return None
    name = Path(str(cwd)).name
    return f"project:{name}" if name else None


def _mcp(tool_name: str, payload: dict[str, Any]) -> tuple[str, str | None]:
    server = payload.get("mcp_server_name") or payload.get("mcpServerName")
    if isinstance(server, str) and server:
        short = tool_name.split("__")[-1] if tool_name.startswith("mcp__") else tool_name
        return short, server
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return parts[2], parts[1]
    return tool_name, None


def _usage_dict(usage: dict[str, Any], model: Any) -> dict[str, Any]:
    found = {
        "model": model if isinstance(model, str) else None,
        "token_input": usage.get("input_tokens", usage.get("token_input")),
        "token_output": usage.get("output_tokens", usage.get("token_output")),
        "token_cache_read": usage.get("cache_read_input_tokens", usage.get("token_cache_read")),
        "token_cache_write": usage.get(
            "cache_creation_input_tokens", usage.get("token_cache_write")
        ),
    }
    return {key: value for key, value in found.items() if value is not None}


def _usage_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Token counts carried on the hook body. Empty when the body has none."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    model = payload.get("model")
    if not isinstance(model, str):
        named = payload.get("modelUsage") or payload.get("model_usage")
        if isinstance(named, dict) and named:
            first = next(iter(named))
            model = first if isinstance(first, str) else None
    return _usage_dict(usage, model)


def _usage_from_transcript(path: str | None) -> dict[str, Any]:
    """Best-effort model and token counts from a Claude Code transcript. Fails soft."""
    if not path:
        return {}
    try:
        file = Path(path)
        if not file.is_file():
            return {}
        raw = file.read_bytes()[-200_000:]
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return {}
    found: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception as exc:
            log.debug("transcript line skipped: %s", exc)
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        usage = message.get("usage") if isinstance(message, dict) else None
        model = None
        if isinstance(message, dict):
            model = message.get("model")
        if isinstance(usage, dict):
            found = _usage_dict(usage, model)
    return found


def _base(
    app: str,
    payload: dict[str, Any],
    *,
    event_id: str,
    event_type: EventType,
    run_id: str,
    parent: str | None,
    root: str,
    summary: str,
    tool_name: str | None = None,
    mcp_server: str | None = None,
    raw: dict[str, Any] | None = None,
    error: str | None = None,
    tokens: dict[str, Any] | None = None,
    agent_name: str | None = None,
) -> RawEvent:
    tags = [f"source:{app}"]
    project = _project_tag(payload)
    if project:
        tags.append(project)
    tokens = tokens or {}
    return RawEvent(
        event_id=event_id,
        event_type=event_type,
        run_id=run_id,
        parent_run_id=parent,
        root_run_id=root,
        timestamp=_now(),
        agent_name=agent_name or app,
        tool_name=tool_name,
        mcp_server=mcp_server,
        summary=summary[:500],
        full_blob_eligible=event_type.value.endswith("_start")
        or event_type.value.endswith("_end")
        or event_type.value.endswith("_error"),
        raw_payload=raw or {},
        error_message=error,
        tags=tags,
        thread_id=_session(payload),
        model=tokens.get("model"),
        token_input=_as_int(tokens.get("token_input")),
        token_output=_as_int(tokens.get("token_output")),
        token_cache_read=_as_int(tokens.get("token_cache_read")),
        token_cache_write=_as_int(tokens.get("token_cache_write")),
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize(
    app: str, payload: dict[str, Any], *, metadata_only: bool = False
) -> list[RawEvent]:
    """Turn one hook body into zero or more events. Never raises."""
    try:
        if app not in _APPS or app in _FOLLOWED or not isinstance(payload, dict):
            return []
        name = _event_name(payload)
        session = _session(payload)
        prompt = _prompt(payload)
        root = _eid(app, session, prompt)
        if name == "UserPromptSubmit":
            prompt_text = "" if metadata_only else _text(payload.get("prompt") or payload.get("text"))
            raw = {} if metadata_only else {"input": prompt_text}
            title = payload.get("title")
            if isinstance(title, str) and title.strip():
                summary = f"{app}: {title.strip()[:120]}"
            elif prompt_text:
                summary = f"{app}: {prompt_text[:120]}"
            else:
                summary = f"{app} prompt"
            return [
                _base(
                    app, payload,
                    event_id=_eid(root, "run_start"),
                    event_type=EventType.RUN_START,
                    run_id=root, parent=None, root=root,
                    summary=f"RUN_START {summary}",
                    raw={"_source": app},
                ),
                _base(
                    app, payload,
                    event_id=_eid(root, "start"),
                    event_type=EventType.CHAIN_START,
                    run_id=root, parent=None, root=root,
                    summary=summary, raw=raw,
                ),
            ]
        if name == "LLMEnd":
            text = "" if metadata_only else _text(
                payload.get("text")
                or payload.get("thought")
                or payload.get("message")
                or payload.get("response")
            )
            prompt = "" if metadata_only else _text(payload.get("prompt") or payload.get("input"))
            llm_id = str(payload.get("llm_id") or payload.get("generation_id") or _eid(text[:200], prompt[:200]))
            step = _eid(root, "llm", llm_id)
            model = payload.get("model") if isinstance(payload.get("model"), str) else None
            name = model or "model"
            return [
                _base(
                    app, payload,
                    event_id=_eid(step, "start"),
                    event_type=EventType.CHAT_MODEL_START,
                    run_id=step, parent=root, root=root,
                    summary=f"{name} request",
                    raw={} if metadata_only else {"input": prompt},
                    tokens={"model": model} if model else None,
                    agent_name=name,
                ),
                _base(
                    app, payload,
                    event_id=_eid(step, "end"),
                    event_type=EventType.LLM_END,
                    run_id=step, parent=root, root=root,
                    summary=(text[:160] or "model"),
                    raw={} if metadata_only else {"input": prompt, "output": text},
                    tokens={"model": model} if model else None,
                    agent_name=name,
                ),
            ]
        if name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            tool_raw = str(payload.get("tool_name") or payload.get("toolName") or "tool")
            tool_name, server = _mcp(tool_raw, payload)
            tool_id = str(
                payload.get("tool_use_id") or payload.get("toolUseId") or tool_name
            )
            step = _eid(root, "tool", tool_id)
            if name == "PreToolUse":
                body = {} if metadata_only else {"input": payload.get("tool_input") or payload.get("toolInput")}
                return [
                    _base(
                        app, payload,
                        event_id=_eid(step, "start"),
                        event_type=EventType.TOOL_START,
                        run_id=step, parent=root, root=root,
                        summary=f"{tool_name} start",
                        tool_name=tool_name, mcp_server=server, raw=body,
                    )
                ]
            failed = name == "PostToolUseFailure" or bool(payload.get("error"))
            body = {}
            if not metadata_only:
                body = {
                    "input": payload.get("tool_input") or payload.get("toolInput"),
                    "output": payload.get("tool_response") or payload.get("toolResponse"),
                }
            error = _text(payload.get("error") or payload.get("error_message"), 500) or None
            return [
                _base(
                    app, payload,
                    event_id=_eid(step, "end"),
                    event_type=EventType.TOOL_ERROR if failed else EventType.TOOL_END,
                    run_id=step, parent=root, root=root,
                    summary=f"{tool_name} {'error' if failed else 'end'}",
                    tool_name=tool_name, mcp_server=server,
                    raw=body, error=error if failed else None,
                )
            ]
        if name == "SubagentStart":
            sub_id = str(payload.get("subagent_id") or payload.get("agent_id") or "sub")
            step = _eid(root, "sub", sub_id)
            label = str(payload.get("name") or payload.get("subagent_type") or "subagent")
            return [
                _base(
                    app, payload,
                    event_id=_eid(step, "start"),
                    event_type=EventType.CHAIN_START,
                    run_id=step, parent=root, root=root,
                    summary=f"subagent {label}",
                    agent_name=label[:60],
                )
            ]
        if name == "SubagentStop":
            sub_id = str(payload.get("subagent_id") or payload.get("agent_id") or "sub")
            step = _eid(root, "sub", sub_id)
            return [
                _base(
                    app, payload,
                    event_id=_eid(step, "end"),
                    event_type=EventType.CHAIN_END,
                    run_id=step, parent=root, root=root,
                    summary="subagent end",
                )
            ]
        if name == "Stop":
            failed = bool(payload.get("error") or payload.get("failed"))
            text = "" if metadata_only else _text(
                payload.get("last_assistant_message")
                or payload.get("text")
                or payload.get("response")
            )
            tokens: dict[str, Any] = {}
            if not metadata_only:
                tokens = _usage_from_payload(payload) or _usage_from_transcript(
                    payload.get("transcript_path")
                    if isinstance(payload.get("transcript_path"), str)
                    else None
                )
                model = payload.get("model")
                if isinstance(model, str) and model and not tokens.get("model"):
                    tokens = {**tokens, "model": model}
            error = _text(payload.get("error"), 500) or None
            return [
                _base(
                    app, payload,
                    event_id=_eid(root, "end"),
                    event_type=EventType.CHAIN_ERROR if failed else EventType.CHAIN_END,
                    run_id=root, parent=None, root=root,
                    summary=(text[:160] or f"{app} end"),
                    raw={} if metadata_only else {"output": text},
                    error=error if failed else None,
                    tokens=tokens,
                ),
                _base(
                    app, payload,
                    event_id=_eid(root, "run_end"),
                    event_type=EventType.RUN_END,
                    run_id=root, parent=None, root=root,
                    summary=f"RUN_END {app}",
                    error=error if failed else None,
                    raw={"_source": app},
                ),
            ]
        return []
    except Exception as exc:
        log.warning("hook normalize failed for %s: %s", app, exc)
        return []


def drain_spool(spool_dir: Path, emit: Any, *, metadata_only: bool = False) -> int:
    """Import queued hook lines and delete the spool files. Never raises."""
    count = 0
    try:
        if not spool_dir.is_dir():
            return 0
        for path in sorted(spool_dir.glob("*.jsonl")):
            app = path.stem
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                log.warning("spool read failed for %s: %s", path, exc)
                continue
            try:
                path.unlink()
            except OSError as exc:
                log.warning("spool unlink failed for %s: %s", path, exc)
            for line in lines:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except Exception as exc:
                    log.debug("spool line skipped: %s", exc)
                    continue
                if not isinstance(payload, dict):
                    continue
                for event in normalize(app, payload, metadata_only=metadata_only):
                    try:
                        emit(event)
                        count += 1
                    except Exception as exc:
                        log.warning("spool emit failed: %s", exc)
    except Exception as exc:
        log.warning("spool drain failed: %s", exc)
    return count
