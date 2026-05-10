# 10 — Planner-Executor (with tracelens)

Identical workflow to `../../before/10_planner_executor/`, plus tracelens.

## What changes from `before/`

- **`tracelens_setup.py`** (new) — tracer init helper.
- **`main.py`** — three lines added.

`llm.py`, `tools.py`, `agents.py`, `graph.py` are byte-identical.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

## What to look for in tracelens

- **Iterative loop visible** — `agent:executor` has `invocation_count = 9`
  across the demo (4 + 3 + 2 steps). `agent:planner` is 3 (one per task).
  The 3:1 ratio is the visual signature of a planner-executor pattern.
- **Per-task plan length** — open each run's timeline. Task 1 has 4
  executor steps stacked sequentially, task 2 has 3, task 3 has 2.
- **Tool dispatch by step type** — each step type maps to a different tool.
  `tool:search` invocations = 2 (tasks 1, 2). `tool:read_doc` = 2 (tasks 1, 3).
  `tool:take_notes` = 2 (tasks 1, 2). `tool:synthesize` = 3 (every task ends
  with one).
- **Loop edges** — `chain:LangGraph -> agent:executor` has a high count
  (9), reflecting the loop. The conditional edge function
  `chain:route_after_executor` runs the same number of times.

## What this system spotlights

- **Loop iterations are queryable** — many real-world agents iterate (ReAct,
  scratchpad agents, Plan-and-Solve). Without tracing, "did the agent finish
  early?" or "how many tool calls did this take?" require log archaeology.
  tracelens shows iteration depth directly.
- **Plan visible in run journey** — open any task's timeline. The first step
  is the planner's LLM call (with the comma-separated plan in its output),
  followed by the executor's tool calls in order. The whole cognitive loop
  is laid out chronologically.
