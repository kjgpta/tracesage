"""Action tools the executor can call to carry out plan steps."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def search(query: str) -> str:
    """Search for information on a topic."""
    return f"search('{query[:40]}') -> 5 hits"


@tool
def read_doc(url: str) -> str:
    """Read a document at a URL."""
    return f"read_doc('{url[:40]}') -> 2.1 KB extracted"


@tool
def take_notes(content: str) -> str:
    """Save notes for later reference."""
    return f"notes saved ({len(content)} chars)"


@tool
def synthesize(notes: str) -> str:
    """Synthesize notes into a final answer."""
    return f"synthesis: {notes[:80]}..."
