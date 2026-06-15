"""11 — Supervisor Research Team (plain LangGraph).

A supervisor node routes between three worker nodes — researcher, writer, and
fact_checker — via conditional edges, looping until the work is done. The
researcher answers from the model directly (no external tools). Pattern: the
classic multi-agent supervisor that decides who acts next.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python before.py
"""
from __future__ import annotations

import asyncio
import os
from typing import Literal, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

WORKERS = ("researcher", "writer", "fact_checker")


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class Route(BaseModel):
    """Which worker should act next, or 'done' when the report is finished."""

    next: Literal["researcher", "writer", "fact_checker", "done"] = Field(
        description="The next worker to act, or 'done' to finish."
    )


class TeamState(TypedDict):
    topic: str
    notes: str
    draft: str
    verdict: str
    steps: int


def _worker(llm: Runnable, persona: str, instruction: str) -> Runnable:
    return (
        ChatPromptTemplate.from_template(
            f"You are the {persona}. {instruction}\n\nTopic: {{topic}}\n"
            "Research notes: {notes}\nCurrent draft: {draft}"
        )
        | llm
        | StrOutputParser()
    )


def build_graph() -> Runnable:
    llm = make_llm()
    router = make_llm().with_structured_output(Route)
    researcher = _worker(llm, "researcher", "List 3 concise factual bullet points.")
    writer = _worker(llm, "writer", "Write a tight 3-sentence summary of the topic.")
    checker = _worker(llm, "fact_checker", "Reply 'OK' or one correction, max 1 line.")

    async def supervise(state: TeamState) -> TeamState:
        if state["steps"] >= 4:
            return {**state, "verdict": state.get("verdict") or "stopped"}
        route: Route = await router.ainvoke(
            f"Topic: {state['topic']}. Notes done={bool(state['notes'])}, "
            f"draft done={bool(state['draft'])}, checked={bool(state['verdict'])}. "
            "Pick researcher, then writer, then fact_checker, then done."
        )
        return {**state, "steps": state["steps"] + 1, "next": route.next}

    async def run_researcher(state: TeamState) -> TeamState:
        return {**state, "notes": await researcher.ainvoke(state)}

    async def run_writer(state: TeamState) -> TeamState:
        return {**state, "draft": await writer.ainvoke(state)}

    async def run_checker(state: TeamState) -> TeamState:
        return {**state, "verdict": await checker.ainvoke(state)}

    def pick(state: TeamState) -> str:
        return END if state.get("next") == "done" else state["next"]

    builder = StateGraph(TeamState)
    builder.add_node("supervisor", supervise)
    builder.add_node("researcher", run_researcher)
    builder.add_node("writer", run_writer)
    builder.add_node("fact_checker", run_checker)
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", pick, {**{w: w for w in WORKERS}, END: END})
    for worker in WORKERS:
        builder.add_edge(worker, "supervisor")
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    topic = "Why are honeybees important to agriculture?"
    state = {"topic": topic, "notes": "", "draft": "", "verdict": "", "steps": 0}
    print(f"Topic: {topic}\n")
    result = await graph.ainvoke(state)
    print("Draft:", result["draft"])
    print("Check:", result["verdict"])


if __name__ == "__main__":
    asyncio.run(main())
