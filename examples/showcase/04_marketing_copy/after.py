"""04 — Marketing Copy Generator (with tracesage).

Identical to before.py except for the tracesage lines. The trace lays out the three
chain stages in order with per-stage latency and tokens, and the full prompt/response
of each stage in the drawer — ideal for tuning a multi-step prompt pipeline.

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
from langchain_core.runnables import Runnable, RunnablePassthrough

from pathlib import Path  # ← tracesage
import tracesage  # ← tracesage

# tracesage: dedicated per-demo data dir so this app's runs, topology, and
# "Tools by source" stay isolated from other demos (each app = its own dir).
DATA_DIR = Path.home() / ".tracesage" / Path(__file__).resolve().parent.name



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

    with tracesage.trace(tracesage.TraceSageConfig(data_dir=DATA_DIR)):  # ← tracesage: starts the UI + captures every chain stage
        out = chain.invoke({"brief": brief})
        print("DRAFT:\n", out["draft"], "\n")
        print("VARIANTS:\n", out["variants"], "\n")
        print("FINAL:\n", out["final"])
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()
