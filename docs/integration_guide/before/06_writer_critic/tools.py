"""Tools the critic uses for ground-truth quality checks."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def word_count(text: str) -> str:
    """Return the word count of a draft."""
    return f"word_count={len(text.split())}"


@tool
def readability_check(text: str) -> str:
    """Return a synthetic readability assessment for the draft."""
    n = len(text.split())
    grade = "easy" if n < 30 else ("medium" if n < 80 else "hard")
    return f"readability={grade} ({n} words)"
