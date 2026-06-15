"""06 — Internal Docs Q&A (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows the retriever node (and its latency), the exact chunks
it pulled in the drawer payloads, and the grounded answer that cites them.

Needs langchain-chroma + chromadb + OpenAIEmbeddings (these use OPENAI_API_KEY).

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # required: embeddings + answer LLM
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
from langchain_core.runnables import Runnable, RunnablePassthrough
from langchain_openai import OpenAIEmbeddings

import tracelens  # ← tracelens

FAQ: list[Document] = [
    Document(page_content="Acme Cloud has three plans: Free, Pro, and Enterprise. "
             "The Free plan includes 1 seat and 5 GB of storage.", metadata={"id": "plans"}),
    Document(page_content="The Pro plan costs $20 per seat per month, billed monthly, "
             "and includes 100 GB of storage and email support.", metadata={"id": "pro"}),
    Document(page_content="Enterprise plans add SSO, audit logs, and a 99.9% uptime "
             "SLA. Pricing is custom; contact sales@acme.example.", metadata={"id": "ent"}),
    Document(page_content="To reset your password, open Settings > Security and click "
             "'Reset password'. A reset link is emailed to you.", metadata={"id": "pw"}),
    Document(page_content="Data is encrypted at rest with AES-256 and in transit with "
             "TLS 1.3. Backups run nightly and are retained 30 days.", metadata={"id": "sec"}),
]


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def _format_docs(docs: list[Document]) -> str:
    return "\n\n".join(
        f"[{d.metadata.get('id', i)}] {d.page_content}" for i, d in enumerate(docs)
    )


def build_chain() -> Runnable:
    store = Chroma.from_documents(FAQ, embedding=OpenAIEmbeddings())
    retriever = store.as_retriever(search_kwargs={"k": 3})

    prompt = ChatPromptTemplate.from_template(
        "Answer the question using ONLY the context below. Cite the bracketed source "
        "id(s) you used, e.g. [pro]. If the answer is not in the context, say so.\n\n"
        "Context:\n{context}\n\nQuestion: {question}"
    )

    return (
        RunnablePassthrough.assign(
            context=lambda x: _format_docs(retriever.invoke(x["question"]))
        )
        | prompt
        | make_llm()
        | StrOutputParser()
    )


def main() -> None:
    chain = build_chain()
    question = "How much does the Pro plan cost and how much storage does it include?"
    print(f"Q: {question}\n")

    with tracelens.trace():  # ← tracelens: starts the UI + captures every call
        print("A:", chain.invoke({"question": question}))
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()
