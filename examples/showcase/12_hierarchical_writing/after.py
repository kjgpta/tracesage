"""12 — Hierarchical Writing Org (with tracelens).

Identical to before.py except for the tracelens lines marked below. Run it, then open
the printed link: the trace shows the org graph and *drills into* each compiled subgraph
(outline_team → draft_team) and their inner nodes — deep nesting you can replay layer by
layer.

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
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph

from tracelens import TraceLens  # ← tracelens


def make_llm(temperature: float = 0.3) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class DocState(TypedDict):
    topic: str
    outline: str
    draft: str
    final: str


def _step(llm: Runnable, template: str) -> Runnable:
    return ChatPromptTemplate.from_template(template) | llm | StrOutputParser()


def build_outline_team(llm: Runnable) -> Runnable:
    brainstorm = _step(llm, "List 3 short bullet ideas for an article on: {topic}")
    structure = _step(
        llm, "Turn these ideas into a 3-section outline (one line each):\n{outline}"
    )

    team = StateGraph(DocState)
    team.add_node("brainstorm", lambda s: {"outline": brainstorm.invoke({"topic": s["topic"]})})
    team.add_node("structure", lambda s: {"outline": structure.invoke({"outline": s["outline"]})})
    team.add_edge(START, "brainstorm")
    team.add_edge("brainstorm", "structure")
    team.add_edge("structure", END)
    return team.compile()


def build_draft_team(llm: Runnable) -> Runnable:
    write = _step(llm, "Write a 4-sentence draft from this outline:\n{outline}")
    polish = _step(llm, "Tighten this draft to 3 crisp sentences:\n{draft}")

    team = StateGraph(DocState)
    team.add_node("write", lambda s: {"draft": write.invoke({"outline": s["outline"]})})
    team.add_node("polish", lambda s: {"draft": polish.invoke({"draft": s["draft"]})})
    team.add_edge(START, "write")
    team.add_edge("write", "polish")
    team.add_edge("polish", END)
    return team.compile()


def build_graph() -> Runnable:
    llm = make_llm()
    outline_team = build_outline_team(llm)
    draft_team = build_draft_team(llm)
    final_edit = _step(llm, "Add a one-line punchy title above this article:\n{draft}")

    org = StateGraph(DocState)
    org.add_node("outline_team", outline_team)  # ← a compiled subgraph
    org.add_node("draft_team", draft_team)  # ← another compiled subgraph
    org.add_node("edit", lambda s: {"final": final_edit.invoke({"draft": s["draft"]})})
    org.add_edge(START, "outline_team")
    org.add_edge("outline_team", "draft_team")
    org.add_edge("draft_team", "edit")
    org.add_edge("edit", END)
    return org.compile()


async def main() -> None:
    graph = build_graph()
    topic = "why small teams ship faster"
    print(f"Topic: {topic}\n")

    async with TraceLens.session(install=True) as tl:  # ← tracelens
        result = await graph.ainvoke({"topic": topic})
        print(result["final"])
        await tl.flush()  # ← tracelens: ensure events persist
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            await asyncio.to_thread(input, "\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    asyncio.run(main())
