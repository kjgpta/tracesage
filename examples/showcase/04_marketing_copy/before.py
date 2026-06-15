"""04 — Marketing Copy Generator (plain LangChain).

Turns a product brief into finished copy through a 3-stage sequential chain:
draft → variants → polished final with a call-to-action. Pattern: pure LCEL pipeline
(no tools, no agent) — the cleanest possible chain topology.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python before.py
"""
from __future__ import annotations

import os

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnablePassthrough


def make_llm(temperature: float = 0.7) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def build_chain() -> Runnable:
    llm = make_llm()
    parser = StrOutputParser()

    draft = ChatPromptTemplate.from_template(
        "Write a punchy 2-sentence marketing draft for this product.\n\nBrief: {brief}"
    ) | llm | parser
    variants = ChatPromptTemplate.from_template(
        "Rewrite this into 3 distinct headline variants (one per line).\n\nDraft: {draft}"
    ) | llm | parser
    polish = ChatPromptTemplate.from_template(
        "Pick the strongest variant and add a one-line call-to-action.\n\nVariants:\n{variants}"
    ) | llm | parser

    return (
        RunnablePassthrough.assign(draft=lambda x: draft.invoke({"brief": x["brief"]}))
        | RunnablePassthrough.assign(variants=lambda x: variants.invoke({"draft": x["draft"]}))
        | RunnablePassthrough.assign(final=lambda x: polish.invoke({"variants": x["variants"]}))
    )


def main() -> None:
    chain = build_chain()
    brief = "A noise-cancelling water bottle that plays lo-fi music while you hydrate."
    out = chain.invoke({"brief": brief})
    print("DRAFT:\n", out["draft"], "\n")
    print("VARIANTS:\n", out["variants"], "\n")
    print("FINAL:\n", out["final"])


if __name__ == "__main__":
    main()
