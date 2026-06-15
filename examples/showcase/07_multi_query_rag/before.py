"""07 — Multi-Query RAG (plain LangGraph).

A query-expansion RAG graph. node1 turns the question into 3 search-query variants;
node2 retrieves for each variant against a small local Chroma store and de-duplicates
the hits; node3 answers from the fused context. Pattern: query-expansion fan-out then
merge — better recall than a single retrieval, and a clear multi-retrieve topology.

Run:
    pip install -r ../requirements.txt   # needs langchain-chroma, chromadb, langchain-openai
    export OPENAI_API_KEY=...            # embeddings + chat both use OpenAI by default
    python before.py
"""
from __future__ import annotations

import asyncio
import os
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import OpenAIEmbeddings
from langgraph.graph import END, START, StateGraph

DOCS = [
    "Solar panels convert sunlight into electricity using photovoltaic cells.",
    "Wind turbines generate power when moving air spins their blades.",
    "Lithium-ion batteries store energy for use when the sun is not shining.",
    "A home solar setup pairs panels with an inverter and a battery bank.",
    "Net metering lets homeowners sell surplus solar power back to the grid.",
    "Hydroelectric dams produce electricity from flowing water.",
]


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class State(TypedDict):
    question: str
    variants: list[str]
    context: list[str]
    answer: str


def build_graph() -> Runnable:
    llm = make_llm()
    store = Chroma.from_documents(
        [Document(page_content=d) for d in DOCS], OpenAIEmbeddings()
    )
    retriever = store.as_retriever(search_kwargs={"k": 2})

    expand = (
        ChatPromptTemplate.from_template(
            "Rewrite the question as 3 short, distinct search queries. "
            "One per line, no numbering.\n\nQuestion: {question}"
        )
        | llm
        | StrOutputParser()
    )
    answer = (
        ChatPromptTemplate.from_template(
            "Answer the question using ONLY the context.\n\n"
            "Context:\n{context}\n\nQuestion: {question}"
        )
        | llm
        | StrOutputParser()
    )

    async def expand_node(state: State) -> dict:
        text = await expand.ainvoke({"question": state["question"]})
        variants = [q.strip() for q in text.splitlines() if q.strip()][:3]
        return {"variants": variants or [state["question"]]}

    async def retrieve_node(state: State) -> dict:
        hits = await asyncio.gather(
            *[retriever.ainvoke(v) for v in state["variants"]]
        )
        seen: dict[str, None] = {}
        for docs in hits:  # fuse: flatten then de-duplicate by content
            for d in docs:
                seen.setdefault(d.page_content, None)
        return {"context": list(seen)}

    async def answer_node(state: State) -> dict:
        ctx = "\n".join(f"- {c}" for c in state["context"])
        return {"answer": await answer.ainvoke(
            {"context": ctx, "question": state["question"]}
        )}

    builder = StateGraph(State)
    builder.add_node("expand", expand_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("answer", answer_node)
    builder.add_edge(START, "expand")
    builder.add_edge("expand", "retrieve")
    builder.add_edge("retrieve", "answer")
    builder.add_edge("answer", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    question = "How can I store solar energy for night-time use at home?"
    print(f"Q: {question}\n")

    result = await graph.ainvoke({"question": question})
    print("Variants:", result["variants"])
    print("\nA:", result["answer"])


if __name__ == "__main__":
    asyncio.run(main())
