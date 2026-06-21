"""Example 2: Multi-Agent Research Supervisor with tools per worker.

A supervisor agent routes queries to one of three worker agents, each of which
has its own tool. Demonstrates:

    - Multi-agent topology (supervisor + 3 workers + their tools).
    - Per-run path visualization: different queries route to different workers,
      so each run highlights a different sub-tree of the topology.

Run:
    python examples/getting_started/02_research_supervisor.py
    # Open http://localhost:7842/ui
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402

try:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
except ImportError:
    from langchain_core.language_models import FakeListChatModel  # type: ignore[attr-defined]

from tracesage import TraceSage, TraceSageConfig  # noqa: E402

# Dedicated data dir so this app's runs, topology, and "Tools by source" stay
# isolated from other applications (each app = its own data dir).
DATA_DIR = Path.home() / ".tracesage" / "research-supervisor"


# ---- Worker tools -------------------------------------------------------- #


@tool
def fetch_news_articles(topic: str) -> str:
    """Fetch the latest news articles for a topic."""
    return f"3 news articles about '{topic}'"


@tool
def fetch_research_papers(topic: str) -> str:
    """Fetch academic papers for a topic."""
    return f"5 papers about '{topic}' (peer reviewed)"


@tool
def fetch_internal_docs(topic: str) -> str:
    """Fetch internal company documentation."""
    return f"Internal wiki section for '{topic}'"


@tool
def synthesize_findings(text: str) -> str:
    """Combine findings from multiple sources into a single summary."""
    return f"Synthesized: {text[:80]}..."


class ResearchState(TypedDict):
    query: str
    next: str
    news: str
    papers: str
    docs: str
    summary: str


async def main() -> None:
    tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR, project_name="Research supervisor"))
    print(f"tracesage at {tracer.ui_url}")
    print(f"Data dir:     {DATA_DIR}")
    print(f"Inspect CLI:  tracesage runs -d {DATA_DIR}")

    # Supervisor decides which worker(s) to dispatch.
    supervisor_llm = FakeListChatModel(
        responses=[
            "news",      # query 1: news topic
            "papers",    # query 2: research topic
            "docs",      # query 3: internal topic
            "all",       # query 4: requires all sources
            "summary",
            "done",
        ]
    )
    news_llm = FakeListChatModel(responses=["news ok"] * 10)
    papers_llm = FakeListChatModel(responses=["papers ok"] * 10)
    docs_llm = FakeListChatModel(responses=["docs ok"] * 10)
    summary_llm = FakeListChatModel(responses=["final summary"] * 10)

    async def supervisor(state: ResearchState) -> dict:
        msg = await supervisor_llm.ainvoke(
            [HumanMessage(content=f"Route: {state['query']}, current: {state.get('next', '')}")]
        )
        return {"next": msg.content.strip().lower()}

    async def news_worker(state: ResearchState) -> dict:
        await news_llm.ainvoke([HumanMessage(content=state["query"])])
        result = await fetch_news_articles.ainvoke({"topic": state["query"]})
        return {"news": result}

    async def papers_worker(state: ResearchState) -> dict:
        await papers_llm.ainvoke([HumanMessage(content=state["query"])])
        result = await fetch_research_papers.ainvoke({"topic": state["query"]})
        return {"papers": result}

    async def docs_worker(state: ResearchState) -> dict:
        await docs_llm.ainvoke([HumanMessage(content=state["query"])])
        result = await fetch_internal_docs.ainvoke({"topic": state["query"]})
        return {"docs": result}

    async def summarize(state: ResearchState) -> dict:
        ctx = (
            f"news={state.get('news', 'n/a')} | "
            f"papers={state.get('papers', 'n/a')} | "
            f"docs={state.get('docs', 'n/a')}"
        )
        await summary_llm.ainvoke([HumanMessage(content=ctx)])
        result = await synthesize_findings.ainvoke({"text": ctx})
        return {"summary": result}

    def route(state: ResearchState) -> Literal["news", "papers", "docs", "all", "summary", "end"]:
        nxt = (state.get("next") or "").strip()
        if nxt in {"news", "papers", "docs", "summary"}:
            return nxt  # type: ignore[return-value]
        if nxt == "all":
            return "all"
        return "end"

    workflow: StateGraph = StateGraph(ResearchState)
    workflow.add_node("supervisor", supervisor)
    workflow.add_node("news_worker", news_worker)
    workflow.add_node("papers_worker", papers_worker)
    workflow.add_node("docs_worker", docs_worker)
    workflow.add_node("summarizer", summarize)

    workflow.set_entry_point("supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        route,
        {
            "news": "news_worker",
            "papers": "papers_worker",
            "docs": "docs_worker",
            "all": "news_worker",
            "summary": "summarizer",
            "end": END,
        },
    )
    workflow.add_edge("news_worker", "summarizer")
    workflow.add_edge("papers_worker", "summarizer")
    workflow.add_edge("docs_worker", "summarizer")
    workflow.add_edge("summarizer", END)
    graph = workflow.compile()

    queries = [
        "today's tech headlines",
        "transformer attention papers",
        "company wiki on auth",
        "everything about retrieval",
    ]
    for q in queries:
        result = await graph.ainvoke(
            {
                "query": q,
                "next": "",
                "news": "",
                "papers": "",
                "docs": "",
                "summary": "",
            },
            config={"callbacks": [tracer.handler], "tags": ["research"]},
        )
        print(f"  query={q!r:<32s} summary={result.get('summary', 'n/a')}")

    print(f"\nLeaving server up. Open {tracer.ui_url} — click each run to")
    print("see different worker subtrees light up. Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await tracer.stop()


if __name__ == "__main__":
    asyncio.run(main())
