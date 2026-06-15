"""29 — Contract Clause Risk Analyzer (plain LangGraph).

Splits a short contract into clauses, fans out to classify each clause's risk level
concurrently (one LLM call per clause, returning level + reason), then a summary node
writes a risk memo that flags the high-risk clauses. Pattern: fan-out classify then
summarize — a map/reduce shape over independent LLM calls.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python before.py
"""
from __future__ import annotations

import asyncio
import os
from typing import Annotated, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

CLAUSES: list[str] = [
    "The Provider may terminate this agreement at any time without notice or cause.",
    "Either party may terminate with 30 days written notice.",
    "Customer's liability is unlimited for any breach of this agreement.",
    "Confidential information shall be protected for a period of three years.",
]


class ClauseRisk(BaseModel):
    level: str = Field(description="one of: low, medium, high")
    reason: str = Field(description="one short sentence explaining the risk")


class AnalysisState(TypedDict):
    clauses: list[str]
    findings: Annotated[list[dict], lambda a, b: a + b]
    memo: str


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def build_graph() -> Runnable:
    llm = make_llm()
    classifier = ChatPromptTemplate.from_template(
        "You are a contract risk analyst. Classify the legal clause's risk to the "
        "customer.\n\nClause: {clause}"
    ) | llm.with_structured_output(ClauseRisk)

    async def classify_clause(clause: str, index: int) -> dict:
        risk: ClauseRisk = await classifier.ainvoke({"clause": clause})
        return {"index": index, "clause": clause, "level": risk.level, "reason": risk.reason}

    async def fan_out(state: AnalysisState) -> dict:
        tasks = [classify_clause(c, i) for i, c in enumerate(state["clauses"])]
        findings = await asyncio.gather(*tasks)
        return {"findings": list(findings)}

    summarize_chain = (
        ChatPromptTemplate.from_template(
            "Write a 3-sentence risk memo for these classified clauses, explicitly "
            "naming any HIGH risk clauses first.\n\n{findings}"
        )
        | llm
        | StrOutputParser()
    )

    async def summarize(state: AnalysisState) -> dict:
        lines = [
            f"[{f['level'].upper()}] {f['clause']} — {f['reason']}"
            for f in sorted(state["findings"], key=lambda f: f["index"])
        ]
        memo = await summarize_chain.ainvoke({"findings": "\n".join(lines)})
        return {"memo": memo}

    builder = StateGraph(AnalysisState)
    builder.add_node("fan_out", fan_out)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "fan_out")
    builder.add_edge("fan_out", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    result = await graph.ainvoke({"clauses": CLAUSES, "findings": [], "memo": ""})
    for f in sorted(result["findings"], key=lambda f: f["index"]):
        print(f"[{f['level'].upper():6}] {f['reason']}")
    print("\nRisk memo:\n", result["memo"])


if __name__ == "__main__":
    asyncio.run(main())
