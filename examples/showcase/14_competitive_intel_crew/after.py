"""14 — Competitive Intelligence Crew (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows scout_a and scout_b executing concurrently (each with
its own DuckDuckGo search + scout LLM call) and joining at the analyst synthesis node —
a clean fan-out / fan-in topology.

Run:
    pip install -r ../requirements.txt          # needs duckduckgo-search + langchain-community
    export OPENAI_API_KEY=...
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph

from tracelens import TraceLens  # ← tracelens


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class IntelState(TypedDict):
    company: str
    pricing_notes: str
    product_notes: str
    brief: str


def _scout_chain(llm: Runnable, angle: str) -> Runnable:
    return (
        ChatPromptTemplate.from_template(
            f"You are a {angle} scout. From these search snippets about {{company}}, "
            f"write 3 terse bullet points on its {angle}.\n\nSnippets:\n{{snippets}}"
        )
        | llm
        | StrOutputParser()
    )


def build_graph() -> Runnable:
    llm = make_llm()
    search = DuckDuckGoSearchRun()
    pricing_scout = _scout_chain(llm, "pricing")
    product_scout = _scout_chain(llm, "product")
    analyst = (
        ChatPromptTemplate.from_template(
            "You are a strategy analyst. Synthesize a 4-line competitive brief on "
            "{company} from the two scout reports.\n\n"
            "Pricing:\n{pricing_notes}\n\nProduct:\n{product_notes}"
        )
        | llm
        | StrOutputParser()
    )

    async def scout_a(state: IntelState) -> dict[str, str]:
        snippets = await asyncio.to_thread(search.run, f"{state['company']} pricing plans")
        notes = await pricing_scout.ainvoke({"company": state["company"], "snippets": snippets})
        return {"pricing_notes": notes}

    async def scout_b(state: IntelState) -> dict[str, str]:
        snippets = await asyncio.to_thread(search.run, f"{state['company']} product launch")
        notes = await product_scout.ainvoke({"company": state["company"], "snippets": snippets})
        return {"product_notes": notes}

    async def synthesize(state: IntelState) -> dict[str, str]:
        brief = await analyst.ainvoke(
            {
                "company": state["company"],
                "pricing_notes": state["pricing_notes"],
                "product_notes": state["product_notes"],
            }
        )
        return {"brief": brief}

    builder = StateGraph(IntelState)
    builder.add_node("scout_a", scout_a)
    builder.add_node("scout_b", scout_b)
    builder.add_node("analyst", synthesize)
    builder.add_edge(START, "scout_a")
    builder.add_edge(START, "scout_b")
    builder.add_edge("scout_a", "analyst")
    builder.add_edge("scout_b", "analyst")
    builder.add_edge("analyst", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    company = "Notion"
    print(f"Target: {company}\n")
    async with TraceLens.session(install=True) as tl:  # ← tracelens
        result = await graph.ainvoke({"company": company})
        await tl.flush()  # ← tracelens: ensure events persist
        print("Brief:\n", result["brief"])
        if sys.stdin.isatty():
            await asyncio.to_thread(input, "\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    asyncio.run(main())
