"""13 — Support Triage + Specialists (plain LangGraph).

A support desk as a LangGraph: a `triage` node classifies an inbound ticket, then a
conditional edge routes it to one of three specialist nodes (`billing` / `technical` /
`account`). Each specialist decides — via its own conditional edge — whether it can
resolve the ticket or must hand off to a shared `escalate` node. Pattern:
triage → (billing | technical | account) → (resolved | escalate).

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python before.py
"""
from __future__ import annotations

import asyncio
import os
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class Triage(BaseModel):
    category: str = Field(description="One of: billing, technical, account.")


class Handling(BaseModel):
    resolved: bool = Field(description="True if you can fully resolve it; False to escalate to a human.")
    reply: str = Field(description="The one-paragraph reply or, if escalating, a short handoff note.")


class TicketState(TypedDict):
    ticket: str
    category: str
    specialist: str
    resolved: bool
    reply: str


def _specialist(llm: Runnable, persona: str):
    handler = llm.with_structured_output(Handling)

    async def node(state: TicketState) -> dict:
        result: Handling = await handler.ainvoke(
            f"You are a {persona} support specialist. Resolve the ticket if it is within "
            "your remit and you have enough information; otherwise escalate to a human.\n\n"
            f"Ticket: {state['ticket']}"
        )
        return {"specialist": persona, "resolved": result.resolved, "reply": result.reply}

    return node


def build_graph() -> Runnable:
    llm = make_llm()
    classifier = llm.with_structured_output(Triage)

    async def triage(state: TicketState) -> dict:
        verdict: Triage = await classifier.ainvoke(
            "Classify this support ticket into exactly one of: billing, technical, "
            f"account.\n\nTicket: {state['ticket']}"
        )
        return {"category": verdict.category.strip().lower()}

    async def escalate(state: TicketState) -> dict:
        return {"reply": f"[escalated to human · {state['specialist']}] {state['reply']}"}

    def route_specialist(state: TicketState) -> str:
        cat = state["category"]
        return cat if cat in {"billing", "technical", "account"} else "technical"

    def route_resolution(state: TicketState) -> str:
        return END if state["resolved"] else "escalate"

    builder = StateGraph(TicketState)
    builder.add_node("triage", triage)
    builder.add_node("billing", _specialist(llm, "billing"))
    builder.add_node("technical", _specialist(llm, "technical"))
    builder.add_node("account", _specialist(llm, "account"))
    builder.add_node("escalate", escalate)
    builder.add_edge(START, "triage")
    builder.add_conditional_edges("triage", route_specialist, ["billing", "technical", "account"])
    for specialist in ("billing", "technical", "account"):
        builder.add_conditional_edges(specialist, route_resolution, ["escalate", END])
    builder.add_edge("escalate", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    ticket = "After the latest update the desktop app crashes on launch with a 0xC0000005 error."
    print(f"Ticket: {ticket}\n")
    result = await graph.ainvoke({"ticket": ticket})
    print("Routed to:", result["specialist"], "| resolved:", result["resolved"])
    print("\nReply:\n", result["reply"])


if __name__ == "__main__":
    asyncio.run(main())
