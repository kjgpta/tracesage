"""17 — Debate-to-Decision (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open the
printed link: the trace replays the optimist→skeptic loop round by round, shows the round
counter driving the conditional edge, and ends at the judge's verdict — the whole
back-and-forth as a single graph.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...
    python after.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from tracelens import TraceLens  # ← tracelens

MAX_ROUNDS = 2


def make_llm(temperature: float = 0.4) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class DebateState(TypedDict):
    topic: str
    transcript: Annotated[list, add_messages]
    rounds: int
    verdict: str


def _persona(llm: Runnable, persona: str, stance: str) -> Runnable:
    return (
        ChatPromptTemplate.from_messages(
            [
                ("system", f"You are the {persona}. {stance} Reply in ONE punchy sentence."),
                ("human", "Topic: {topic}\n\nDebate so far:\n{transcript}"),
            ]
        )
        | llm
        | StrOutputParser()
    )


def build_graph() -> Runnable:
    llm = make_llm()
    optimist = _persona(llm, "Optimist", "Argue FOR the proposal.")
    skeptic = _persona(llm, "Skeptic", "Argue AGAINST the proposal.")
    judge = (
        ChatPromptTemplate.from_template(
            "You are an impartial judge. Read the debate and decide: ADOPT or REJECT, "
            "with a one-line reason.\n\nTopic: {topic}\n\nDebate:\n{transcript}"
        )
        | llm
        | StrOutputParser()
    )

    def _join(state: DebateState) -> str:
        return "\n".join(m.content for m in state["transcript"]) or "(nothing yet)"

    async def optimist_node(state: DebateState) -> dict:
        line = await optimist.ainvoke({"topic": state["topic"], "transcript": _join(state)})
        return {"transcript": [("ai", f"Optimist: {line}")]}

    async def skeptic_node(state: DebateState) -> dict:
        line = await skeptic.ainvoke({"topic": state["topic"], "transcript": _join(state)})
        return {"transcript": [("ai", f"Skeptic: {line}")], "rounds": state["rounds"] + 1}

    async def judge_node(state: DebateState) -> dict:
        verdict = await judge.ainvoke({"topic": state["topic"], "transcript": _join(state)})
        return {"verdict": verdict}

    def route(state: DebateState) -> str:
        return "judge" if state["rounds"] >= MAX_ROUNDS else "optimist"

    builder = StateGraph(DebateState)
    builder.add_node("optimist", optimist_node)
    builder.add_node("skeptic", skeptic_node)
    builder.add_node("judge", judge_node)
    builder.add_edge(START, "optimist")
    builder.add_edge("optimist", "skeptic")
    builder.add_conditional_edges("skeptic", route, {"optimist": "optimist", "judge": "judge"})
    builder.add_edge("judge", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    topic = "Should our team adopt a four-day work week?"
    print(f"Topic: {topic}\n")

    async with TraceLens.session(install=True) as tl:  # ← tracelens: starts UI + captures every call
        result = await graph.ainvoke({"topic": topic, "transcript": [], "rounds": 0, "verdict": ""})
        await tl.flush()  # ← tracelens: ensure events persist
        for msg in result["transcript"]:
            print(" •", msg.content)
        print("\nVerdict:", result["verdict"])
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            await asyncio.to_thread(input, "\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    asyncio.run(main())
