"""30 — Insurance Claim Intake & Routing (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows the structured-extraction LLM call, the validation
node's issue list, and which of the three routes (auto_approve / manual_review /
fraud_review) the conditional edge fired — a clean audit trail for a regulated domain.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from tracelens import TraceLens  # ← tracelens


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class Claim(BaseModel):
    """Structured fields extracted from a free-text claim description."""

    claimant: str = Field(description="Full name of the person filing the claim.")
    claim_type: str = Field(description="auto, home, health, or other.")
    amount_usd: float = Field(description="Requested payout in USD, 0 if unknown.")
    incident_date: str = Field(description="Date of incident, or 'unknown'.")
    description: str = Field(description="One-line summary of what happened.")


class State(TypedDict):
    text: str
    claim: dict
    issues: list[str]
    decision: str


def build_graph() -> Runnable:
    llm = make_llm()
    extractor = llm.with_structured_output(Claim)

    async def extract(state: State) -> dict:
        claim: Claim = await extractor.ainvoke(
            "Extract the insurance claim fields from this description:\n\n"
            + state["text"]
        )
        return {"claim": claim.model_dump()}

    async def validate(state: State) -> dict:
        claim = state["claim"]
        issues: list[str] = []
        if not claim.get("claimant"):
            issues.append("missing claimant")
        if claim.get("incident_date", "unknown") == "unknown":
            issues.append("missing incident date")
        if claim.get("amount_usd", 0) <= 0:
            issues.append("missing amount")
        # Simple fraud-signal heuristic: large round-number payout + thin detail.
        amount = claim.get("amount_usd", 0)
        if amount >= 50_000 and amount % 10_000 == 0:
            issues.append("fraud-signal: large round amount")
        return {"issues": issues}

    def route(state: State) -> str:
        issues = state["issues"]
        if any(i.startswith("fraud-signal") for i in issues):
            return "fraud_review"
        if issues:
            return "manual_review"
        return "auto_approve"

    async def auto_approve(state: State) -> dict:
        return {"decision": "auto_approved"}

    async def manual_review(state: State) -> dict:
        return {"decision": "manual_review: " + ", ".join(state["issues"])}

    async def fraud_review(state: State) -> dict:
        return {"decision": "fraud_review: " + ", ".join(state["issues"])}

    builder = StateGraph(State)
    builder.add_node("extract", extract)
    builder.add_node("validate", validate)
    builder.add_node("auto_approve", auto_approve)
    builder.add_node("manual_review", manual_review)
    builder.add_node("fraud_review", fraud_review)
    builder.add_edge(START, "extract")
    builder.add_edge("extract", "validate")
    builder.add_conditional_edges("validate", route)
    builder.add_edge("auto_approve", END)
    builder.add_edge("manual_review", END)
    builder.add_edge("fraud_review", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    text = (
        "Hi, this is Dana Reyes. My car was rear-ended on 2026-05-30 and I'm "
        "requesting $50000 for repairs and a rental."
    )
    async with TraceLens.session(install=True) as tl:  # ← tracelens
        result = await graph.ainvoke({"text": text})
        await tl.flush()  # ← tracelens: ensure events persist
        print("Claim:", result["claim"])
        print("Issues:", result["issues"])
        print("Decision:", result["decision"])
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            await asyncio.to_thread(
                input, "\n🔍 Open the printed trace link, then press Enter to exit."
            )


if __name__ == "__main__":
    asyncio.run(main())
