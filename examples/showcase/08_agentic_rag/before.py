"""08 — Agentic RAG (plain LangGraph).

Retrieves docs from a small local Chroma store, then GRADES them for relevance. If they
are not relevant, a rewrite node reformulates the question and retrieves again (capped at
2 retries) before the answer node speaks. Pattern: a conditional retrieval loop —
retrieve → grade → (rewrite → retrieve)* → answer.

Run:
    pip install -r ../requirements.txt   # needs langchain-chroma + chromadb
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
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
    "TraceSage binds to 127.0.0.1:7842 by default; pass host/port to change it.",
    "TraceSage stores spans in SQLite and large payloads as blobs under base_dir.",
    "The TraceSage callback handler never raises; it logs to stderr and returns None.",
    "Refunds are issued to the original payment method within 5-7 business days.",
]


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def build_store() -> Chroma:
    return Chroma.from_documents(
        [Document(page_content=d) for d in DOCS], OpenAIEmbeddings()
    )


class State(TypedDict):
    question: str
    query: str
    docs: list[str]
    tries: int
    answer: str


def build_graph() -> Runnable:
    llm = make_llm()
    store = build_store()
    grade = (
        ChatPromptTemplate.from_template(
            "Are the docs relevant to the question? Reply ONLY yes or no.\n\n"
            "Question: {question}\n\nDocs:\n{docs}"
        )
        | llm
        | StrOutputParser()
    )
    rewrite = (
        ChatPromptTemplate.from_template(
            "Rewrite the question to retrieve better docs. Reply with ONLY the new "
            "query.\n\nQuestion: {question}"
        )
        | llm
        | StrOutputParser()
    )
    answer = (
        ChatPromptTemplate.from_template(
            "Answer the question using ONLY the docs.\n\n"
            "Question: {question}\n\nDocs:\n{docs}"
        )
        | llm
        | StrOutputParser()
    )

    def retrieve(state: State) -> dict:
        hits = store.similarity_search(state["query"], k=2)
        return {"docs": [d.page_content for d in hits], "tries": state["tries"] + 1}

    def grade_node(state: State) -> dict:
        verdict = grade.invoke(
            {"question": state["question"], "docs": "\n".join(state["docs"])}
        )
        return {"query": verdict.strip().lower()}  # stash verdict in query slot briefly

    def decide(state: State) -> str:
        if state["query"].startswith("yes") or state["tries"] >= 3:
            return "answer"
        return "rewrite"

    def rewrite_node(state: State) -> dict:
        return {"query": rewrite.invoke({"question": state["question"]})}

    def answer_node(state: State) -> dict:
        return {
            "answer": answer.invoke(
                {"question": state["question"], "docs": "\n".join(state["docs"])}
            )
        }

    builder = StateGraph(State)
    builder.add_node("retrieve", retrieve)
    builder.add_node("grade", grade_node)
    builder.add_node("rewrite", rewrite_node)
    builder.add_node("answer", answer_node)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "grade")
    builder.add_conditional_edges("grade", decide, {"rewrite": "rewrite", "answer": "answer"})
    builder.add_edge("rewrite", "retrieve")
    builder.add_edge("answer", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    question = "What port does TraceSage bind to by default?"
    print(f"Q: {question}\n")
    result = await graph.ainvoke({"question": question, "query": question, "tries": 0})
    print("A:", result["answer"])


if __name__ == "__main__":
    asyncio.run(main())
