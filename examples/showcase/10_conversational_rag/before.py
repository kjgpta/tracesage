"""10 — Conversational RAG with memory (plain LangGraph).

A multi-turn RAG assistant. A `rewrite` node turns a follow-up like "what about pricing?"
into a standalone query using the chat history, a `retrieve` node pulls matching docs from
a tiny local Chroma store, and an `answer` node responds. A MemorySaver checkpointer keeps
history per `thread_id`, so 3 turns on the same thread carry context forward.

Run:
    pip install -r ../requirements.txt   # needs langchain-chroma, chromadb, langchain-openai
    export OPENAI_API_KEY=...            # used for chat + embeddings
    python before.py
"""
from __future__ import annotations

import asyncio
import os
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
    for turn in turns:
        print(f"\nQ: {turn}")
        result = await graph.ainvoke({"messages": [("user", turn)]}, config)
        print("A:", result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
