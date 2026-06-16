"""27 — Map-Reduce Long-Doc Summarizer (with tracesage).

Identical to before.py except for the tracesage lines marked below. Run it, then open
the printed link: the trace shows the map fan-out (all chunk summaries running in
parallel) and the reduce call that folds them together — plus token usage per call.

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
from langchain_core.runnables import Runnable

import tracesage  # ← tracesage

DOCUMENT = """
The printing press, introduced by Johannes Gutenberg around 1440, transformed Europe.
Before it, books were copied by hand, making them rare and costly. Movable metal type let
a single shop produce hundreds of identical copies, collapsing the price of the written word.

Literacy spread as cheap books reached merchants, clerks, and eventually ordinary households.
Vernacular languages flourished because printers chose local tongues over Latin to widen sales.
Standardized spelling and grammar slowly emerged from the need to set consistent type.

The press also accelerated the spread of ideas. Reformers like Martin Luther used pamphlets
to reach mass audiences within weeks, something unthinkable in the manuscript era. Scientific
findings circulated faster, letting researchers in distant cities build on each other's work.

Yet the press disrupted established powers. Church and state struggled to control what was
printed, prompting the first censorship laws and licensing regimes. Printers became commercial
players whose choices about what to publish shaped public debate for centuries to come.
"""


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def split_chunks(text: str, n: int = 5) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return paras[:n]


def build_map_chain(llm: Runnable) -> Runnable:
    return (
        ChatPromptTemplate.from_template(
            "Summarize this passage in ONE concise sentence.\n\n{chunk}"
        )
        | llm
        | StrOutputParser()
    )


def build_reduce_chain(llm: Runnable) -> Runnable:
    return (
        ChatPromptTemplate.from_template(
            "Combine these passage summaries into one tight paragraph (3 sentences "
            "max).\n\nSummaries:\n{summaries}"
        )
        | llm
        | StrOutputParser()
    )


def summarize(text: str) -> str:
    llm = make_llm()
    chunks = split_chunks(text)

    map_chain = build_map_chain(llm)
    # MAP: fan out — summarize every chunk in parallel in one batch call.
    chunk_summaries = map_chain.batch([{"chunk": c} for c in chunks])

    reduce_chain = build_reduce_chain(llm)
    joined = "\n".join(f"- {s}" for s in chunk_summaries)
    # REDUCE: fold the chunk summaries into one final summary.
    return reduce_chain.invoke({"summaries": joined})


def main() -> None:
    print(f"Document: {len(DOCUMENT)} chars, summarizing via map-reduce...\n")

    with tracesage.trace():  # ← tracesage: starts the UI + captures every call
        print("Summary:", summarize(DOCUMENT))
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            input("\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    main()
