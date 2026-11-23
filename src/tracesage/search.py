"""Full-text helpers: payload text extraction and a safe FTS5 query builder.

User text never reaches SQLite as raw MATCH syntax. Each term is quoted as a
phrase, so operators like AND, NEAR and stray quotes cannot change the query.
"""
from __future__ import annotations

import re
from typing import Any

_TERM = re.compile(r"[\w]+", re.UNICODE)


def extract_search_text(payload: Any, *, limit: int) -> str:
    """Walk a JSON value and collect string leaves, capped at ``limit`` characters.

    Never raises. Non-string scalars are skipped so ids and token counts don't
    drown the index.
    """
    if limit <= 0:
        return ""
    parts: list[str] = []
    remaining = limit

    def walk(value: Any) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        if isinstance(value, str):
            text = " ".join(value.split())
            if not text:
                return
            if len(text) > remaining:
                text = text[:remaining]
            parts.append(text)
            remaining -= len(text)
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
                if remaining <= 0:
                    return
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
                if remaining <= 0:
                    return

    try:
        walk(payload)
    except Exception:
        return " ".join(parts)[:limit]
    return " ".join(parts)[:limit]


def fts_match_query(raw: str | None) -> str | None:
    """Turn free text into a parameterized-safe FTS5 MATCH string.

    Returns None when the input has no indexable terms (the caller should
    match nothing rather than run an unbounded query). A trailing ``*`` on the
    raw query keeps prefix search on the last term.
    """
    if not raw:
        return None
    terms = _TERM.findall(raw)
    if not terms:
        return None
    prefix = raw.rstrip().endswith("*")
    quoted = ['"' + term.replace('"', "") + '"' for term in terms]
    if prefix:
        quoted[-1] = quoted[-1] + "*"
    return " ".join(quoted)


def like_contains(raw: str) -> str:
    """A LIKE pattern that treats ``%``, ``_`` and ``\\`` in user text as literals."""
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
