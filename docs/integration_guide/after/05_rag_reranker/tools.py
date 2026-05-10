"""Tools for the RAG-with-reranker pipeline."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def cite_sources(answer: str) -> str:
    """Append source citations to a final answer."""
    return f"{answer}\n\nSources: [1] doc-0  [2] doc-2  [3] doc-4"
