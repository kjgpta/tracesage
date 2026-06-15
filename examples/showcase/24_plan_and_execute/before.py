"""24 — Plan-and-Execute Agent (plain LangGraph).

A planner LLM turns an arithmetic word problem into an ordered list of steps. An
executor node runs each step against a local calculator tool (the LLM emits the
expression, the tool evaluates it). If a step fails, a replan node revises the
remaining plan. Pattern: plan → execute-loop → conditional replan.

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
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression (digits and + - * / . () only)."""
    if not set(expression) <= set("0123456789+-*/(). "):
        raise ValueError(f"unsupported characters in: {expression!r}")
    return str(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307


class Plan(BaseModel):
    """Ordered arithmetic steps to solve the problem, each a single expression."""

    steps: list[str] = Field(description="Arithmetic expressions, max 4, in order.")


class PlanState(TypedDict):
    problem: str
    steps: list[str]
    results: list[str]
    cursor: int
    replans: int


def build_graph() -> Runnable:
    planner = make_llm().with_structured_output(Plan)
    translate = (
        ChatPromptTemplate.from_template(
            "Rewrite this step as ONE arithmetic expression using only digits and "
            "+ - * / ( ). Reply with the expression only.\n\nStep: {step}\n"
            "Known results so far: {results}"
        )
        | make_llm()
        | StrOutputParser()
    )

    async def plan(state: PlanState) -> PlanState:
        result: Plan = await planner.ainvoke(
            "Break this word problem into ordered arithmetic steps (max 4). "
            f"Problem: {state['problem']}"
        )
        return {**state, "steps": result.steps, "cursor": 0, "results": []}

    async def execute(state: PlanState) -> PlanState:
        step = state["steps"][state["cursor"]]
        expr = (await translate.ainvoke({"step": step, "results": state["results"]})).strip()
        value = calculator(expr)  # raises on a malformed expression → triggers replan
        return {
            **state,
            "results": [*state["results"], f"{step} = {value}"],
            "cursor": state["cursor"] + 1,
        }

    async def replan(state: PlanState) -> PlanState:
        result: Plan = await planner.ainvoke(
            "The previous plan hit a bad step. Re-plan the REMAINING arithmetic steps "
            f"(max 4) for: {state['problem']}. Done so far: {state['results']}"
        )
        return {**state, "steps": result.steps, "cursor": 0, "replans": state["replans"] + 1}

    def route(state: PlanState) -> str:
        return END if state["cursor"] >= len(state["steps"]) else "execute"

    builder = StateGraph(PlanState)
    builder.add_node("plan", plan)
    builder.add_node("execute", execute)
    builder.add_node("replan", replan)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_conditional_edges("execute", route, {"execute": "execute", END: END})
    builder.add_edge("replan", "execute")
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    problem = (
        "A cafe sells 12 muffins at $3 each and 8 coffees at $4 each. "
        "After a $10 discount, what is the total?"
    )
    state = {"problem": problem, "steps": [], "results": [], "cursor": 0, "replans": 0}
    print(f"Problem: {problem}\n")
    result = await graph.ainvoke(state)
    for line in result["results"]:
        print(" •", line)


if __name__ == "__main__":
    asyncio.run(main())
