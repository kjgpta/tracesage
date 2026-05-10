"""Tools for the research pipeline.

These are LangChain `@tool`-decorated functions. They appear in tracelens as
`tool:<name>` topology nodes once a node calls them.
"""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for relevant pages. Returns newline-separated URLs."""
    results = [
        f"https://example.com/{i}-{query.replace(' ', '-')}"
        for i in range(max_results)
    ]
    return "\n".join(results)


@tool
def fetch_document(url: str) -> str:
    """Fetch the document at `url`. Returns plain text."""
    return (
        f"[fake corpus for {url}]\n\n"
        "Multi-agent systems combine roles to solve complex tasks. "
        "Observability provides visibility into running systems. "
        "LLM agents call tools, retrieve context, and reason iteratively."
    )


@tool
def cite_sources(answer: str) -> str:
    """Append source citations to a synthesis."""
    return f"{answer}\n\nSources: [1] doc-0  [2] doc-1  [3] doc-2"
