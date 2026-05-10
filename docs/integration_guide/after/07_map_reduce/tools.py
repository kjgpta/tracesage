"""Tools for the map-reduce summarizer."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def split_text(text: str, chunk_size: int = 200) -> str:
    """Split text into chunks of `chunk_size` chars. Returns chunks joined by `|||`."""
    chunks = []
    for i in range(0, len(text), chunk_size):
        c = text[i : i + chunk_size].strip()
        if c:
            chunks.append(c)
    return "|||".join(chunks)


@tool
def join_summaries(summaries: str) -> str:
    """Join summaries (separated by `|||`) into a single bulleted list."""
    items = [s.strip() for s in summaries.split("|||") if s.strip()]
    return "\n".join(f"- {s}" for s in items)
