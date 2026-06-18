"""26 — LLM-as-Judge Eval Harness (with tracesage).

Identical to before.py except for the tracesage lines marked below. Run it, then open
the printed link: each dataset item is a separate root run (task → judge), so you can
compare runs side by side with `tracesage diff`, inspect the judge's structured verdict,
and watch token spend across the whole batch — the eval loop made observable.

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
from pydantic import BaseModel, Field

from pathlib import Path  # ← tracesage
from tracesage import TraceSage, TraceSageConfig  # ← tracesage

# tracesage: dedicated per-demo data dir so this app's runs, topology, and
# "Tools by source" stay isolated from other demos (each app = its own dir).
DATA_DIR = Path.home() / ".tracesage" / Path(__file__).resolve().parent.name


DATASET: list[dict[str, str]] = [
    {"question": "What is the capital of France?", "expected": "Paris"},
    {"question": "What is 12 * 8?", "expected": "96"},
    {"question": "Who wrote the play 'Hamlet'?", "expected": "William Shakespeare"},
]


def make_llm(temperature: float = 0.0) -> Runnable:
    return init_chat_model(
        os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        model_provider=os.environ.get("LLM_PROVIDER", "openai"),
        temperature=temperature,
    )


class Verdict(BaseModel):
    """A judge's correctness verdict for one answer."""

    score: float = Field(description="Correctness from 0.0 (wrong) to 1.0 (perfect).")
    rationale: str = Field(description="One short sentence justifying the score.")


class EvalState(TypedDict):
    question: str
    expected: str
    answer: str
    score: float
    rationale: str


def build_graph() -> Runnable:
    task = (
        ChatPromptTemplate.from_template(
            "Answer the question in one short sentence.\n\nQuestion: {question}"
        )
        | make_llm()
        | StrOutputParser()
    )
    judge = make_llm().with_structured_output(Verdict)

    async def task_node(state: EvalState) -> dict:
        answer = await task.ainvoke({"question": state["question"]})
        return {"answer": answer}

    async def judge_node(state: EvalState) -> dict:
        verdict: Verdict = await judge.ainvoke(
            "You are a strict grader. Score how well the answer matches the expected "
            "answer, 0.0-1.0.\n\n"
            f"Question: {state['question']}\n"
            f"Expected: {state['expected']}\n"
            f"Answer: {state['answer']}"
        )
        return {"score": verdict.score, "rationale": verdict.rationale}

    builder = StateGraph(EvalState)
    builder.add_node("task", task_node)
    builder.add_node("judge", judge_node)
    builder.add_edge(START, "task")
    builder.add_edge("task", "judge")
    builder.add_edge("judge", END)
    return builder.compile()


async def main() -> None:
    graph = build_graph()
    rows: list[EvalState] = []
    async with TraceSage.session(TraceSageConfig(data_dir=DATA_DIR), install=True) as tl:  # ← tracesage: starts UI + captures every call
        for item in DATASET:
            result = await graph.ainvoke(
                {"question": item["question"], "expected": item["expected"],
                 "answer": "", "score": 0.0, "rationale": ""}
            )
            rows.append(result)
        await tl.flush()  # ← tracesage: ensure events persist

        print(f"{'score':>5}  {'question':<32}  rationale")
        print("-" * 78)
        for r in rows:
            print(f"{r['score']:>5.2f}  {r['question'][:32]:<32}  {r['rationale']}")
        avg = sum(r["score"] for r in rows) / len(rows)
        print("-" * 78)
        print(f"{avg:>5.2f}  average over {len(rows)} items")
        if sys.stdin.isatty():  # ← keep the UI up so you can explore (demo only)
            await asyncio.to_thread(input, "\n🔍 Open the printed trace link, then press Enter to exit.")


if __name__ == "__main__":
    asyncio.run(main())
