"""23 — Reflexion Writer (plain LangGraph).

A writer node drafts a short paragraph; a critic node scores it 1-10 and gives one line
of feedback. A conditional edge loops back to the writer (carrying the feedback) until the
score reaches 8 or 3 iterations elapse. Pattern: bounded writer-critic reflection loop.

Run:
    pip install -r ../requirements.txt
    export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
    python before.py
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph

MAX_ITERS = 3
TARGET_SCORE = 8


def make_llm(temperature: float = 0.4) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class WriterState(TypedDict):
    task: str
    draft: str
    feedback: str
    score: int
    iters: int


def build_graph() -> Runnable:
    llm = make_llm()
    writer = (
        ChatPromptTemplate.from_messages(
            [
                ("system", "You are a concise writer. Write a single tight paragraph (<=80 words)."),
                ("human", "Task: {task}\n\nPrior feedback (revise to address it):\n{feedback}"),
            ]
        )
        | llm
        | StrOutputParser()
    )
    critic = (
        ChatPromptTemplate.from_template(
            "Critique the paragraph for the task. Reply EXACTLY as:\n"
            "SCORE: <1-10>\nFEEDBACK: <one actionable line>\n\n"
            "Task: {task}\n\nParagraph:\n{draft}"
        )
        | llm
        | StrOutputParser()
    )

    async def write_node(state: WriterState) -> dict:
        draft = await writer.ainvoke(
            {"task": state["task"], "feedback": state["feedback"] or "(none yet)"}
        )
        return {"draft": draft, "iters": state["iters"] + 1}

    async def critic_node(state: WriterState) -> dict:
        verdict = await critic.ainvoke({"task": state["task"], "draft": state["draft"]})
        m = re.search(r"SCORE:\s*(\d+)", verdict)
        score = int(m.group(1)) if m else 0
        fb = verdict.split("FEEDBACK:", 1)[-1].strip()
        return {"score": score, "feedback": fb}

    def route(state: WriterState) -> str:
        if state["score"] >= TARGET_SCORE or state["iters"] >= MAX_ITERS:
            return "done"
        return "revise"

    builder = StateGraph(WriterState)
    builder.add_node("write", write_node)
    builder.add_node("critic", critic_node)
    builder.add_edge(START, "write")
    builder.add_edge("write", "critic")
    builder.add_conditional_edges("critic", route, {"revise": "write", "done": END})
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    task = "Explain why code review matters, for a junior engineer."
    print(f"Task: {task}\n")

    result = await graph.ainvoke(
        {"task": task, "draft": "", "feedback": "", "score": 0, "iters": 0}
    )
    print(f"Final draft (score {result['score']}, {result['iters']} iters):\n")
    print(result["draft"])


if __name__ == "__main__":
    asyncio.run(main())
