"""14 — Competitive Intelligence Crew (plain LangGraph).

Two scout nodes run in parallel (LangGraph fan-out: START → scout_a + scout_b), each
using DuckDuckGo web search to gather signal on a competitor angle. Both feed a single
analyst node (fan-in) that synthesizes a short strategic brief. Pattern: parallel agents
joining at a synthesis barrier.

Run:
    pip install -r ../requirements.txt          # needs duckduckgo-search + langchain-community
    export OPENAI_API_KEY=...
    python before.py
"""
from __future__ import annotations

import asyncio
import os
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph


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
    result = await graph.ainvoke({"company": company})
    print("Brief:\n", result["brief"])


if __name__ == "__main__":
    asyncio.run(main())
