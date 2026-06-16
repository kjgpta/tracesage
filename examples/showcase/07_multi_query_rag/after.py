"""07 — Multi-Query RAG (with tracesage).

Identical to before.py except for the tracesage lines marked below. Run it, then open
the printed link: the trace shows the expansion LLM call, the THREE parallel retriever
calls (one per query variant), the fuse/de-dup step, and the final grounded answer — so
query expansion and the merge become visible end to end.

Run:
    pip install -r ../requirements.txt   # needs langchain-chroma, chromadb, langchain-openai
    export OPENAI_API_KEY=...            # embeddings + chat both use OpenAI by default
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import OpenAIEmbeddings
from langgraph.graph import END, START, StateGraph

from tracesage import TraceSage  # ← tracesage

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

    async with TraceSage.session(install=True) as tl:  # ← tracesage
        result = await graph.ainvoke({"question": question})
        await tl.flush()  # ← tracesage: ensure events persist
        print("Variants:", result["variants"])
        print("\nA:", result["answer"])
        if sys.stdin.isatty():  # keep the UI up so you can explore (demo only)
            await asyncio.to_thread(
                input, "\n🔍 Open the printed trace link, then press Enter to exit."
            )


if __name__ == "__main__":
    asyncio.run(main())
