"""Data analyst agents — supervisor + 3 specialized workers.

The supervisor is invoked repeatedly: it picks the next worker (or `done`),
each worker runs its specialty tool + LLM, then control returns to the
supervisor. This continues until the supervisor decides we're done.
"""
from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import HumanMessage

from llm import get_llm
from tools import fetch_schema, plot_chart, run_sql, write_summary


class AnalystState(TypedDict, total=False):
    question: str
    next_worker: str
    sql_result: str
    chart_result: str
    narrative: str
    visited: list[str]
    final_answer: str


# Supervisor's routing decisions across the 3 demo queries.
# Q1 ("user signups")     -> sql, done
# Q2 ("revenue trend")    -> sql, chart, done
# Q3 ("quarterly review") -> sql, chart, narrative, done
_SUPERVISOR_PICKS = [
    "sql", "done",
    "sql", "chart", "done",
    "sql", "chart", "narrative", "done",
]
_supervisor_llm = get_llm(responses=_SUPERVISOR_PICKS)
_sql_llm = get_llm(responses=["query plan: filter on region", "query plan: trend over months", "query plan: quarter-over-quarter"])
_chart_llm = get_llm(responses=["x=month, y=revenue", "x=quarter, y=delta"])
_narrative_llm = get_llm(responses=["Quarterly summary: revenue up 12% YoY in EMEA, flat in APAC."])


async def supervisor(state: AnalystState) -> dict:
    """Pick the next worker (or `done`) based on the question + visited workers."""
    visited = state.get("visited", [])
    msg = await _supervisor_llm.ainvoke(
        [
            HumanMessage(
                content=(
                    f"Question: {state['question']}. Visited: {visited}. "
                    "Pick next: sql / chart / narrative / done."
                )
            )
        ]
    )
    return {"next_worker": msg.content.strip().lower()}


async def sql_agent(state: AnalystState) -> dict:
    """Specialist: schema lookup + SQL query."""
    schema = await fetch_schema.ainvoke({"table": "transactions"})
    plan = await _sql_llm.ainvoke([HumanMessage(content=f"Plan SQL for: {state['question']}")])
    rows = await run_sql.ainvoke({"query": f"SELECT ... -- {plan.content[:40]}"})
    return {
        "sql_result": f"{schema} | {rows}",
        "visited": [*state.get("visited", []), "sql"],
    }


async def chart_agent(state: AnalystState) -> dict:
    """Specialist: turn SQL output into a chart."""
    spec = await _chart_llm.ainvoke(
        [HumanMessage(content=f"Chart spec for: {state.get('sql_result', '')[:80]}")]
    )
    chart = await plot_chart.ainvoke(
        {"data": state.get("sql_result", "")[:80], "chart_type": "line"}
    )
    return {
        "chart_result": f"{spec.content} -> {chart}",
        "visited": [*state.get("visited", []), "chart"],
    }


async def narrative_agent(state: AnalystState) -> dict:
    """Specialist: write an executive narrative over SQL + chart outputs."""
    composed = f"sql={state.get('sql_result', '')[:80]} chart={state.get('chart_result', '')[:80]}"
    note = await _narrative_llm.ainvoke([HumanMessage(content=composed)])
    summary = await write_summary.ainvoke({"content": note.content})
    return {
        "narrative": summary,
        "visited": [*state.get("visited", []), "narrative"],
    }


async def finalize(state: AnalystState) -> dict:
    """Assemble the final answer from whatever workers contributed."""
    parts: list[str] = []
    if state.get("sql_result"):
        parts.append(f"SQL: {state['sql_result']}")
    if state.get("chart_result"):
        parts.append(f"Chart: {state['chart_result']}")
    if state.get("narrative"):
        parts.append(f"Narrative: {state['narrative']}")
    return {"final_answer": " || ".join(parts)}
