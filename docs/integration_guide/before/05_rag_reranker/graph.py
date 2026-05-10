"""LangGraph wiring for two-stage RAG.

Layout:
    retrieve → rerank → answer → END
       │         │        │
       │         │        └ answer_chain (LCEL: prompt | LLM | parser) + cite_sources tool
       │         └ rerank_chain (LCEL: prompt | LLM | parser)
       └ FastFakeRetriever returning 8 candidates
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from chains import answer_chain, rerank_chain
from retrievers import CORPUS, FastFakeRetriever
from tools import cite_sources


class RAGState(TypedDict, total=False):
    question: str
    candidates: list[str]
    reranked: list[str]
    answer: str
    cited: str


_retriever = FastFakeRetriever(documents=CORPUS)


async def retrieve_node(state: RAGState) -> dict:
    """Stage 1 — fetch candidate docs from the fast retriever."""
    docs = await _retriever.ainvoke(state["question"])
    return {"candidates": [d.page_content for d in docs]}


async def rerank_node(state: RAGState) -> dict:
    """Stage 2 — score candidates with an LLM and trim to top 3."""
    candidates = state.get("candidates") or []
    candidates_str = "\n".join(f"[{i}] {c}" for i, c in enumerate(candidates))
    ranking = await rerank_chain.ainvoke(
        {"query": state["question"], "candidates": candidates_str}
    )
    indices: list[int] = []
    for token in ranking.split(","):
        token = token.strip()
        if token.isdigit():
            indices.append(int(token))
    top = [candidates[i] for i in indices[:3] if 0 <= i < len(candidates)]
    return {"reranked": top}


async def answer_node(state: RAGState) -> dict:
    """Stage 3 — generate the grounded answer + add citations."""
    context = "\n".join(state.get("reranked") or [])
    raw = await answer_chain.ainvoke({"question": state["question"], "context": context})
    cited = await cite_sources.ainvoke({"answer": raw})
    return {"answer": raw, "cited": cited}


def build_graph() -> Any:
    sg: StateGraph = StateGraph(RAGState)
    sg.add_node("retrieve", retrieve_node)
    sg.add_node("rerank", rerank_node)
    sg.add_node("answer", answer_node)
    sg.set_entry_point("retrieve")
    sg.add_edge("retrieve", "rerank")
    sg.add_edge("rerank", "answer")
    sg.add_edge("answer", END)
    return sg.compile()
