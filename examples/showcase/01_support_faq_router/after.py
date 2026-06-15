"""01 — Support FAQ Router (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows the classifier LLM call, which branch fired, and the
specialist answer — so you can see *why* a question was routed where it was.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python after.py
"""
from __future__ import annotations

import os
import sys

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableBranch, RunnablePassthrough

import tracelens  # ← tracelens


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def _answer_chain(llm: Runnable, persona: str) -> Runnable:
    return (
        ChatPromptTemplate.from_template(
            f"You are a {persona} support specialist. Answer the customer concisely "
            "and helpfully.\n\nQuestion: {question}"
        )
        | llm
        | StrOutputParser()
    )


def build_chain() -> Runnable:
    llm = make_llm()

    classify = (
        ChatPromptTemplate.from_template(
            "Classify the support question into exactly one of: "
            "billing, technical, account, other. Reply with ONLY that word.\n\n"
            "Question: {question}"
        )
        | llm
        | StrOutputParser()
    )

    billing = _answer_chain(llm, "billing")
    technical = _answer_chain(llm, "technical")
    account = _answer_chain(llm, "account")
    escalation = (
        ChatPromptTemplate.from_template(
            "You are a support router. This question needs a human agent. Write a "
            "one-line, friendly hand-off message.\n\nQuestion: {question}"
        )
        | llm
        | StrOutputParser()
    )

    branch = RunnableBranch(
        (lambda x: x["category"].strip().lower().startswith("billing"), billing),
        (lambda x: x["category"].strip().lower().startswith("technical"), technical),
        (lambda x: x["category"].strip().lower().startswith("account"), account),
        escalation,  # default
    )

    return RunnablePassthrough.assign(
        category=lambda x: classify.invoke({"question": x["question"]})
    ) | branch


def main() -> None:
    chain = build_chain()
    question = "I was double-charged on my last invoice — can I get a refund?"
    print(f"Q: {question}\n")

    with tracelens.trace():  # ← tracelens: starts the UI + captures every call
        print("A:", chain.invoke({"question": question}))
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()
