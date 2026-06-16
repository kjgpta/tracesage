"""16 — Sales Lead Enrichment & Outreach (with tracesage).

Identical to before.py except for the tracesage lines marked below. Run it, then open
the printed link: the trace shows the `fake_crm_lookup` tool call, the structured qualify
score, and which conditional edge fired — so you can see *why* a lead was drafted or
dropped, end to end.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from tracesage import TraceSage  # ← tracesage


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


@tool
def fake_crm_lookup(company: str) -> str:
    """Look up firmographics for a company from the CRM (canned demo data)."""
    db = {
        "Acme Robotics": "industry=Industrial Automation; employees=1200; revenue=$240M; stack=AWS,Kubernetes",
        "Tiny Cafe": "industry=Food Service; employees=4; revenue=$300K; stack=Square POS",
    }
    return db.get(company, f"industry=Unknown; employees=?; revenue=?; company={company}")


class Verdict(BaseModel):
    qualified: bool = Field(description="True if the lead is a strong fit for an enterprise dev-tools sale.")
    reason: str = Field(description="One short sentence explaining the score.")


class LeadState(TypedDict):
    company: str
    firmographics: str
    qualified: bool
    reason: str
    outreach: str


def build_graph() -> Runnable:
    llm = make_llm()
    scorer = llm.with_structured_output(Verdict)

    async def enrich(state: LeadState) -> dict:
        firmographics = await fake_crm_lookup.ainvoke({"company": state["company"]})
        return {"firmographics": firmographics}

    async def qualify(state: LeadState) -> dict:
        verdict: Verdict = await scorer.ainvoke(
            "Score this lead as a fit for an enterprise developer-tools product. "
            f"Company: {state['company']}\nFirmographics: {state['firmographics']}"
        )
        return {"qualified": verdict.qualified, "reason": verdict.reason}

    async def draft_outreach(state: LeadState) -> dict:
        msg = await llm.ainvoke(
            "Write a 2-sentence cold outreach opener for "
            f"{state['company']} ({state['firmographics']}). Be specific and warm."
        )
        return {"outreach": msg.content}

    async def disqualify(state: LeadState) -> dict:
        return {"outreach": f"Skipped — not a fit. {state['reason']}"}

    def route(state: LeadState) -> str:
        return "draft_outreach" if state["qualified"] else "disqualify"

    builder = StateGraph(LeadState)
    builder.add_node("enrich", enrich)
    builder.add_node("qualify", qualify)
    builder.add_node("draft_outreach", draft_outreach)
    builder.add_node("disqualify", disqualify)
    builder.add_edge(START, "enrich")
    builder.add_edge("enrich", "qualify")
    builder.add_conditional_edges("qualify", route, ["draft_outreach", "disqualify"])
    builder.add_edge("draft_outreach", END)
    builder.add_edge("disqualify", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    company = "Acme Robotics"
    print(f"Lead: {company}\n")

    async with TraceSage.session(install=True) as tl:  # ← tracesage
        result = await graph.ainvoke({"company": company})
        await tl.flush()  # ← tracesage: ensure events persist
        print("Qualified:", result["qualified"], "—", result["reason"])
        print("\nOutreach:\n", result["outreach"])
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            await asyncio.to_thread(input, "\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    asyncio.run(main())
