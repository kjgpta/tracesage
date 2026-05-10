"""Follow-up tools for the streaming agent."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def shorten(text: str, limit: int = 80) -> str:
    """Trim a streamed reply to `limit` chars."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


@tool
def add_disclaimer(text: str) -> str:
    """Append a standard disclaimer."""
    return f"{text}\n\n[disclaimer: this is a demo response.]"
