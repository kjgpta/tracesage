"""10 — Conversational RAG with memory (with tracesage).

Identical to before.py except for the tracesage lines marked below. Run it, then open the
printed link: the trace shows 3 linked runs on ONE thread_id, the history-aware `rewrite`
node turning each follow-up into a standalone query, the Chroma retrieval, and the answer.

Run:
    pip install -r ../requirements.txt   # needs langchain-chroma, chromadb, langchain-openai
    export OPENAI_API_KEY=...            # used for chat + embeddings
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated, TypedDict

from langchain.chat_models import init_chat_model
from langchain_chroma import Chroma
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import OpenAIEmbeddings
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from tracesage import TraceSage  # ← tracesage

DOCS = [
    "TraceSage is an observability tool for LangChain and LangGraph apps.",
    "TraceSage pricing: the open-source core is free; the hosted tier is $29/month.",
    "TraceSage stores traces locally under ~/.tracesage by default.",
    "To install TraceSage, run pip install tracesage[langchain].",
]


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class State(TypedDict):
    messages: Annotated[list, add_messages]
    query: str
    context: str


def build_graph() -> Runnable:
    llm = make_llm()
    store = Chroma.from_texts(DOCS, embedding=OpenAIEmbeddings())
    rewrite = (
        ChatPromptTemplate.from_messages(
            [
                ("system", "Given the chat history, rewrite the user's latest turn into "
                           "a standalone search query. Reply with ONLY the query."),
                ("placeholder", "{messages}"),
            ]
        )
        | llm
        | StrOutputParser()
    )
    answer = (
        ChatPromptTemplate.from_messages(
            [
                ("system", "Answer using ONLY this context:\n{context}"),
                ("placeholder", "{messages}"),
            ]
        )
        | llm
    )

    async def rewrite_node(state: State) -> dict:
        query = await rewrite.ainvoke({"messages": state["messages"]})
        return {"query": query.strip()}

    async def retrieve_node(state: State) -> dict:
        docs = await store.asimilarity_search(state["query"], k=2)
        return {"context": "\n".join(d.page_content for d in docs)}

    async def answer_node(state: State) -> dict:
        reply = await answer.ainvoke(
            {"messages": state["messages"], "context": state["context"]}
        )
        return {"messages": [reply]}

    builder = StateGraph(State)
    builder.add_node("rewrite", rewrite_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("answer", answer_node)
    builder.add_edge(START, "rewrite")
    builder.add_edge("rewrite", "retrieve")
    builder.add_edge("retrieve", "answer")
    builder.add_edge("answer", END)
    return builder.compile(checkpointer=MemorySaver())


async def main() -> None:
    graph = build_graph()
    config = {"configurable": {"thread_id": "demo-session-1"}}
    turns = [
        "What is TraceSage?",
        "How much does it cost?",
        "And how do I install it?",
    ]
    async with TraceSage.session(install=True) as tl:  # ← tracesage
        for turn in turns:
            print(f"\nQ: {turn}")
            result = await graph.ainvoke({"messages": [("user", turn)]}, config)
            print("A:", result["messages"][-1].content)
        await tl.flush()  # ← tracesage: ensure events persist
        if sys.stdin.isatty():
            await asyncio.to_thread(
                input, "\n🔍 Open the printed trace link, then press Enter to exit."
            )


if __name__ == "__main__":
    asyncio.run(main())
