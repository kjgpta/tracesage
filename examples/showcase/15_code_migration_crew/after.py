"""15 — Code-Migration Crew (with tracesage).

Identical to before.py except for the tracesage lines marked below. Run it, then open
the printed link: the trace shows the planner → fan-out transform → reviewer shape, with
one similar LLM call per file inside the migrate node — so you can spot a slow or failing
work item among many.

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

from tracesage import TraceSage  # ← tracesage

REPO: dict[str, str] = {
    "area.py": "def area(r):\n    return 3.14159 * r * r",
    "greet.py": "def greet(name):\n    return 'hi ' + name",
    "total.py": "def total(items):\n    return sum(i['price'] for i in items)",
}


class State(TypedDict):
    files: list[str]
    migrated: dict[str, str]
    summary: str


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def build_graph() -> Runnable:
    llm = make_llm()

    transform = (
        ChatPromptTemplate.from_template(
            "Add Python type hints to this function. Reply with ONLY the rewritten "
            "code, no prose, no markdown fences.\n\nFile {name}:\n{code}"
        )
        | llm
        | StrOutputParser()
    )
    review = (
        ChatPromptTemplate.from_template(
            "You are a tech lead. In two short sentences, summarize this type-hint "
            "migration across {count} files.\n\n{diffs}"
        )
        | llm
        | StrOutputParser()
    )

    async def plan(state: State) -> dict:
        return {"files": sorted(REPO)}

    async def migrate(state: State) -> dict:
        migrated: dict[str, str] = {}
        for name in state["files"][:3]:  # cap fan-out for token budget
            migrated[name] = await transform.ainvoke({"name": name, "code": REPO[name]})
        return {"migrated": migrated}

    async def summarize(state: State) -> dict:
        diffs = "\n\n".join(f"# {n}\n{c}" for n, c in state["migrated"].items())
        summary = await review.ainvoke({"count": len(state["migrated"]), "diffs": diffs})
        return {"summary": summary}

    builder = StateGraph(State)
    builder.add_node("plan", plan)
    builder.add_node("migrate", migrate)
    builder.add_node("summarize", summarize)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "migrate")
    builder.add_edge("migrate", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    async with TraceSage.session(install=True) as tl:  # ← tracesage
        result = await graph.ainvoke({"files": [], "migrated": {}, "summary": ""})
        await tl.flush()  # ← tracesage: ensure events persist
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            await asyncio.to_thread(input, "\n🔍 Open the printed trace link, then press Enter to exit.")
    for name, code in result["migrated"].items():
        print(f"--- {name} ---\n{code}\n")
    print("Review:", result["summary"])


if __name__ == "__main__":
    asyncio.run(main())
