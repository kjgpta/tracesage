"""09 — RAG + Reranker (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows the retriever pulling top-8, a distinct LLM rerank
step scoring them down to top-3, and the cited answer — so you can see exactly which
chunks ended up grounding the response and how reranking changed that set.

Run:
    pip install -r ../requirements.txt   # needs langchain-chroma + chromadb + langchain-openai
    export OPENAI_API_KEY=...            # OpenAIEmbeddings needs an OpenAI key
    python after.py
"""
from __future__ import annotations

import os
import sys

from langchain.chat_models import init_chat_model
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_openai import OpenAIEmbeddings

import tracelens  # ← tracelens

DOCS = [
    "TraceLens binds to 127.0.0.1:7842 by default and refuses 0.0.0.0 without an auth token.",
    "Add observability by calling tracelens.trace(); it installs a global LangChain handler.",
    "The callback handler never raises: every method is wrapped in try/except and returns None.",
    "Events are batched by a worker; one bad event is skipped, the rest of the batch persists.",
    "Blob paths are stored relative to base_dir and validated on read to block path traversal.",
    "Sampling drops a run when random() exceeds sample_rate, checked after run_id extraction.",
    "The REST API exposes /api/health, which is the only route the bearer-auth middleware skips.",
    "WebSocket sends catch per-socket errors, mark dead sockets, and continue broadcasting.",
]


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def build_store() -> Chroma:
    docs = [Document(page_content=t, metadata={"id": i}) for i, t in enumerate(DOCS)]
    return Chroma.from_documents(docs, OpenAIEmbeddings(), collection_name="tracelens_faq")


def build_chain() -> Runnable:
    llm = make_llm()
    retriever = build_store().as_retriever(search_kwargs={"k": 8})

    rerank = (
        ChatPromptTemplate.from_template(
            "Score each passage 0-10 for how well it answers the question. Then reply with "
            "ONLY the 1-based indices of the top 3, most relevant first, comma-separated "
            "(e.g. '3,1,5').\n\nQuestion: {question}\n\nPassages:\n{passages}"
        )
        | llm
        | StrOutputParser()
    )

    def rerank_top3(payload: dict) -> dict:
        docs = payload["docs"]
        passages = "\n".join(f"[{i + 1}] {d.page_content}" for i, d in enumerate(docs))
        order = rerank.invoke({"question": payload["question"], "passages": passages})
        picks: list[int] = []
        for part in order.replace(" ", "").split(","):
            if part.isdigit() and 1 <= int(part) <= len(docs):
                picks.append(int(part) - 1)
        picks = picks[:3] or list(range(min(3, len(docs))))
        return {"question": payload["question"], "docs": [docs[i] for i in picks]}

    answer = (
        ChatPromptTemplate.from_template(
            "Answer the question using ONLY the passages. Cite sources inline with bracket "
            "numbers like [1]. Keep it to two sentences.\n\n"
            "Question: {question}\n\nPassages:\n{passages}"
        )
        | llm
        | StrOutputParser()
    )

    def format_answer(payload: dict) -> str:
        passages = "\n".join(
            f"[{i + 1}] {d.page_content}" for i, d in enumerate(payload["docs"])
        )
        return answer.invoke({"question": payload["question"], "passages": passages})

    return (
        RunnableLambda(lambda q: {"question": q, "docs": retriever.invoke(q)})
        | RunnableLambda(rerank_top3)
        | RunnableLambda(format_answer)
    )


def main() -> None:
    chain = build_chain()
    question = "How does tracelens keep a bad event from breaking my agent?"
    print(f"Q: {question}\n")

    with tracelens.trace():  # ← tracelens: starts the UI + captures every call
        print("A:", chain.invoke(question))
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()
