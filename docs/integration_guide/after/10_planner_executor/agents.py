"""Planner + executor agents for the iterative loop.

The planner runs once per task and emits a list of steps. The executor runs
once per remaining step (each step is one tool call). After every executor
invocation the graph checks whether the plan is exhausted; if not, it loops
back to the executor.
"""
from __future__ import annotations

from typing import TypedDict

from langchain_core.messages import HumanMessage

from llm import get_llm
from tools import read_doc, search, synthesize, take_notes


# Map step type -> tool. Used by the executor to dispatch each step.
_STEP_TOOLS = {
    "search": search,
    "read": read_doc,
    "notes": take_notes,
    "synthesize": synthesize,
}


class PlanState(TypedDict, total=False):
    task: str
    plan: list[str]              # remaining step types
    completed: list[str]         # in-order log of step types executed
    last_result: str             # result of most recent step
    final: str


# Planner emits a "type1,type2,type3" string. Demo flow (3 tasks):
#   Task 1 -> search,read,notes,synthesize  (4 steps)
#   Task 2 -> search,notes,synthesize       (3 steps)
#   Task 3 -> read,synthesize               (2 steps)
_PLANNER_RESPONSES = [
    "search,read,notes,synthesize",
    "search,notes,synthesize",
    "read,synthesize",
]
_planner_llm = get_llm(responses=_PLANNER_RESPONSES)

# Executor narrates each step (for demo readability). With a real LLM the
# narration would actually drive tool selection.
_executor_llm = get_llm(
    responses=[f"step {i + 1}" for i in range(20)]
)


async def planner_node(state: PlanState) -> dict:
    """Generate the plan for this task — runs ONCE per task."""
    msg = await _planner_llm.ainvoke(
        [HumanMessage(content=f"Plan the steps for: {state['task']}")]
    )
    plan = [s.strip() for s in msg.content.split(",") if s.strip()]
    return {"plan": plan, "completed": [], "last_result": ""}


async def executor_node(state: PlanState) -> dict:
    """Execute the next plan step — runs ONCE per step.

    Pops the head of `plan`, dispatches to the matching tool, appends the
    step type to `completed`, and stores the tool's output as `last_result`.
    """
    plan = list(state.get("plan") or [])
    if not plan:
        return {}

    step = plan[0]
    remaining = plan[1:]

    # Narration LLM (mostly for the trace; real systems wire the LLM to the tool).
    await _executor_llm.ainvoke(
        [HumanMessage(content=f"Executing step: {step} of plan {plan}")]
    )

    tool = _STEP_TOOLS.get(step)
    if tool is None:
        result = f"unknown step type: {step}"
    else:
        if step == "search":
            result = await tool.ainvoke({"query": state.get("task", "")})
        elif step == "read":
            result = await tool.ainvoke({"url": f"https://docs.example.com/{state.get('task', '')[:20]}"})
        elif step == "notes":
            result = await tool.ainvoke({"content": state.get("last_result", "")})
        elif step == "synthesize":
            result = await tool.ainvoke({"notes": state.get("last_result", "")})
        else:  # pragma: no cover - covered by unknown branch above
            result = ""

    return {
        "plan": remaining,
        "completed": [*state.get("completed", []), step],
        "last_result": result,
        # When this is the last step, surface the synthesized answer.
        **({"final": result} if not remaining else {}),
    }
